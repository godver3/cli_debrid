import logging
import random
import time
import os
import sqlite3
import plexapi # Added import
import uuid
import ctypes
import platform
import gc
from utilities.log_redaction import RedactingFormatter
# Import tracemalloc
try:
    import tracemalloc
    tracemalloc_available = True
except ImportError:
    tracemalloc = None # Define as None if import fails
    tracemalloc_available = False
# Add psutil import
try:
    import psutil
except ImportError:
    psutil = None # Handle missing import gracefully
from apscheduler.schedulers.background import BackgroundScheduler
# Add ThreadPoolExecutor for explicit configuration
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.interval import IntervalTrigger
# Add threading.Lock
import threading # For scheduler lock, concurrent queue processing, AND heavy task lock
import functools # Added for partial
import apscheduler.events # Added for listener events
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED, EVENT_JOB_MAX_INSTANCES
from queues.initialization import initialize
from utilities.settings import get_setting, get_all_settings
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss, get_wanted_from_friends_plex_rss
from content_checkers.trakt import (
    get_wanted_from_trakt_lists,
    get_wanted_from_trakt_watchlist,
    get_wanted_from_trakt_collection,
    get_wanted_from_friend_trakt_watchlist,
    get_wanted_from_special_trakt_lists # New import
)
from content_checkers.scrob import (
    get_wanted_from_scrob_lists,
    get_wanted_from_scrob_collection,
    get_wanted_from_scrob_special
)
from content_checkers.mdb_list import get_wanted_from_mdblist_source
from content_checkers.content_source_detail import append_content_source_detail
from database.not_wanted_magnets import purge_not_wanted_magnets_file
import traceback
from datetime import datetime, timedelta, time as dt_time, timezone # Modified import
import asyncio
from utilities.plex_functions import run_get_collected_from_plex, run_get_recent_from_plex
from routes.notifications import send_notifications, _send_notifications, get_enabled_notifications
import requests
from pathlib import Path
import pickle
from utilities.zurg_utilities import run_get_collected_from_zurg, run_get_recent_from_zurg
import ntplib
from content_checkers.trakt import check_trakt_early_releases
from debrid.base import TooManyDownloadsError, RateLimitError
import tempfile
from routes.api_tracker import api  # Add this import for the api module
from plexapi.server import PlexServer
import json
from utilities.post_processing import handle_state_change
from content_checkers.content_cache_management import (
    load_source_cache, save_source_cache, 
    should_process_item, update_cache_for_item,
    metadata_output_ids, item_has_metadata_output, UNRESOLVED_RETRY_HOURS,
    load_live_content_source_config, normalize_enabled_versions
)
from content_checkers.source_run_report import SourceRunReport
from content_checkers.blocked_items import drop_blocked_items
from collections import deque # Import deque for efficient queue operations
from database.symlink_verification import (
    create_overlay_removal_queue_table,
    get_pending_removal_paths,
    update_removal_status, # Renamed from update_removal_verification_status
    cleanup_old_verified_removals, # Renamed from remove_verified_paths
    increment_removal_attempt, # Renamed from increment_removal_attempt
    migrate_plex_removal_database
)
from utilities.plex_functions import (
    get_section_type, # Need this to determine search type
    find_plex_library_and_section, # Added import
    remove_file_from_plex, # Added import
)
from plexapi.exceptions import NotFound
import pytz # Added import
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError # Keep this if needed elsewhere, or remove if only _get_local_timezone uses it
from database.core import get_db_connection # Add DB connection import
from database.database_reading import get_media_item_by_id
from utilities.local_library_scan import check_local_file_for_item # Add local scan import
from cli_battery.app.direct_api import DirectAPI # Import DirectAPI
import json # Added for loading intervals
# Add Debrid imports for library size task
from debrid import get_debrid_provider, ProviderUnavailableError
from utilities.plex_removal_cache import process_removal_cache # Added import for standalone removal processing
import sys # Add for checking apscheduler.events
from collections import defaultdict  # Added alongside deque above for runtime tracking
# Try to import resource module (Unix only), fallback to time.process_time()
try:
    import resource
except ImportError:
    resource = None

queue_logger = logging.getLogger('queue_logger')
program_runner = None

# Database migration check at startup
migrate_plex_removal_database()


def clear_nzb_job_health_cache(job_id: str) -> None:
    """Drop cached NZB health-check state for a cancelled or superseded cli_mount job."""
    bare = job_id[4:] if str(job_id).startswith('nzb:') else str(job_id or '')
    if not bare:
        return
    ProgramRunner._nzb_confirmed_complete.pop(bare, None)
    ProgramRunner._nzb_folder_wait_counts.pop(bare, None)


# Set while the startup sync (queues/initialization.py) runs in its background
# thread. Module-level rather than on ProgramRunner: reinitialize() re-runs
# __init__, which must not forget a sync that is still going.
_initial_sync_active = threading.Event()
_initial_sync_guard = threading.Lock()


def is_initial_sync_running():
    return _initial_sync_active.is_set()


def _build_scheduler_config():
    """Executors and job defaults shared by every BackgroundScheduler we build."""
    queue_workers = max(1, min(3, int(get_setting('Queue', 'queue_pool_workers', 2))))
    executors = {
        'default': ThreadPoolExecutor(max_workers=1),  # scheduled/maintenance tasks — sequential
        'queue': ThreadPoolExecutor(max_workers=queue_workers),
    }
    job_defaults = {
        'coalesce': True,
        'max_instances': 1,
        'misfire_grace_time': None,
    }
    return queue_workers, executors, job_defaults


def _parse_last_updated(value):
    """A media_items last_updated value as a naive datetime, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00').split('+')[0])
    except ValueError:
        return None


# Tasks whose on/off switch is a setting. ProgramRunner schedules them from the setting
# alone (saved Task Manager toggles are ignored), and flipping one in the Task Manager
# writes the setting (see _sync_setting_backed_tasks in program_operation_routes).
SETTING_BACKED_TASKS = {
    'task_process_plex_reported_issues': ('Plex Reported Issues', 'enabled'),
}


# ProgramRunner's task methods live in queues/runner/, one mixin per area.
from queues.runner.content_sources import ContentSourceTasks
from queues.runner.connectivity import ConnectivityTasks
from queues.runner.plex_tasks import PlexTasks
from queues.runner.library_tasks import LibraryTasks
from queues.runner.usenet_tasks import UsenetTasks
from queues.runner.metadata_tasks import MetadataTasks
from queues.runner.maintenance_tasks import MaintenanceTasks


class ProgramRunner(ContentSourceTasks, ConnectivityTasks, PlexTasks, LibraryTasks, UsenetTasks, MetadataTasks, MaintenanceTasks):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ProgramRunner, cls).__new__(cls)
            # Initialize _initialized here before __init__ is called
            cls._instance._initialized_runner_attributes = False # Ensure this is named uniquely
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized_runner_attributes') and self._initialized_runner_attributes:
            return
        
        self._running = False
        self._initializing = False
        self._stopping = False # ADDED: New state for stopping phase
        # Tracks whether self.scheduler.start() has ever actually been called on
        # this scheduler instance - distinct from scheduler.running, which is
        # False both before the very first start AND after a real crash/zombie.
        # _execute_start_program's "scheduler exists but isn't running" check
        # needs this to tell those two states apart; without it, the freshly
        # __init__-created scheduler on every process boot looked identical to a
        # zombie and got discarded/rebuilt (with the wrong timezone, since the
        # rebuild path reads a different setting) before it ever got to run once.
        self._scheduler_started_once = False
        
        # Use pause_info instead of pause_reason
        self.pause_info = {
            "reason_string": None,
            "error_type": None,  # e.g., "CONNECTION_ERROR", "UNAUTHORIZED", "SYSTEM_SCHEDULED", "RATE_LIMIT", "DB_HEALTH"
            "service_name": None, # e.g., "Real-Debrid API", "Plex", "System"
            "status_code": None,  # HTTP status code if applicable
            "retry_count": 0
        }
        self.connectivity_failure_time = None
        self.connectivity_retry_count = 0 # This will now primarily be for logging/timing, actual count in pause_info
        self.queue_paused = False

        # PAUSE/RESUME THROTTLING: Prevent rapid pause/resume cycles
        self.last_pause_time = None
        self.last_resume_time = None
        self.pause_resume_cooldown = 30  # Minimum seconds between pause/resume operations
        
        # Task schedule persistence — load saved next-run times so timers survive restarts.
        import os as _os
        _db_dir = _os.environ.get('USER_DB_CONTENT', '/user/db_content')
        _os.makedirs(_db_dir, exist_ok=True)
        self._task_schedule_file = _os.path.join(_db_dir, 'task_schedule.json')
        self._task_schedules = self._load_task_schedules()

        # In-memory job store. Task next-run times are NOT restored from this store —
        # that's handled separately by _load_task_schedules()/task_schedule.json above,
        # which is the mechanism _schedule_task's initial_run branch actually reads from.
        # A persistent (pickling) store like SQLAlchemyJobStore cannot be used here: every
        # task's target is a bound method on this ProgramRunner instance, which itself
        # holds self.scheduler, and APScheduler refuses to pickle a job whose bound-method
        # owner carries a scheduler reference ("Schedulers cannot be serialized").
        from apscheduler.jobstores.memory import MemoryJobStore
        _jobstores = {'default': MemoryJobStore()}

        # Configure scheduler timezone using the local timezone helper
        try:
            from metadata.metadata import _get_local_timezone # Added import
            tz = _get_local_timezone()
            logging.info(f"Initializing APScheduler with timezone: {tz.key}")
            _queue_workers, executors, job_defaults = _build_scheduler_config()
            self.scheduler = BackgroundScheduler(jobstores=_jobstores, executors=executors, job_defaults=job_defaults, timezone=tz)
            logging.info(f"APScheduler configured with separate thread pools: queue({_queue_workers}) and default(1).")
        except Exception as e:
            logging.error(f"Failed to get local timezone for scheduler, using system default: {e}")
            _queue_workers, executors, job_defaults = _build_scheduler_config()
            self.scheduler = BackgroundScheduler(jobstores=_jobstores, executors=executors, job_defaults=job_defaults)
            logging.info(f"APScheduler configured with separate thread pools: queue({_queue_workers}) and default(1) (system default timezone).")

        # self.scheduler_lock = threading.Lock() # Previous version
        self.scheduler_lock = threading.RLock() # MODIFIED: Ensure RLock for reentrancy
        self.heavy_task_lock = threading.Lock()
        self.paused_jobs_by_queue = set() # Keep track of jobs paused by pause_queue
        
        self.executing_task_start_times = {}
        self._executing_task_start_times_lock = threading.Lock()
        
        from queues.queue_manager import QueueManager
        
        # Initialize queue manager with logging
        logging.info("Initializing QueueManager")
        self.queue_manager = QueueManager()
        
        # Verify queue initialization
        expected_queues = ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading', 'Final_Check', 'Pre_release']
        missing_queues = [q for q in expected_queues if q not in self.queue_manager.queues]
        if missing_queues:
            logging.error(f"Missing queues during initialization: {missing_queues}")
            raise RuntimeError(f"Queue initialization failed. Missing queues: {missing_queues}")
        
        # Always resume queue on startup to ensure we're not stuck in paused state
        self.queue_manager.resume_queue()
        
        logging.info("Successfully initialized QueueManager with queues: " + ", ".join(self.queue_manager.queues.keys()))
        
        # Define queue_processing_map FIRST
        # Define queue processing map needed early by _normalize_task_name
        # This map connects queue names (often used in toggles/settings)
        # to the corresponding processing methods in QueueManager.
        self.queue_processing_map = {
            'Wanted': 'process_wanted',
            'Scraping': 'process_scraping',
            'Adding': 'process_adding',
            'Checking': 'process_checking',
            'Sleeping': 'process_sleeping',
            'Unreleased': 'process_unreleased',
            'Blacklisted': 'process_blacklisted',
            'Pending Uncached': 'process_pending_uncached',
            'Upgrading': 'process_upgrading',
            'final_check_queue': 'process_final_check', # Use lowercase key matching the task ID
            'Pre_release': 'process_pre_release'
        }

        # Define BASE set of heavy DB tasks
        # Add tasks that are always considered heavy, except content sources
        # Content source tasks ('task_..._wanted') will NOT use this lock.
        self.HEAVY_DB_TASKS = {
            'task_reconcile_queues',
            'task_check_database_health',
            'task_run_library_maintenance',
            'task_update_show_ids',
            'task_update_show_titles',
            'task_update_movie_ids',
            'task_update_movie_titles',
            'task_update_tv_show_status',
            'task_plex_full_scan',
            'task_get_plex_watch_history',
            'task_refresh_release_dates',
        }
        # --- Updated log message ---
        logging.info(f"Defined {len(self.HEAVY_DB_TASKS)} base tasks requiring exclusive execution lock. Global sequential execution is handled by scheduler config.")

        # Base Task Intervals
        self.task_intervals = {
            # Queue Processing Tasks (intervals for individual queues are less critical now)
            'Wanted': 60,             # Increased from 5
            'Scraping': 1,           # Increased from 5
            'Adding': 1,             # Increased from 5
            'Checking': 30,
            'Sleeping': 300,
            'Unreleased': 300,
            'Blacklisted': 7200,
            'Pending Uncached': 3600,
            'Upgrading': 3600,
            'final_check_queue': 900, # Use lowercase key matching the task ID
            'Pre_release': 24 * 60 * 60, # Run every 24 hours (daily)
            # Combined/High Frequency Tasks
            'task_update_queue_views': 30,     # Update queue views every 30 seconds
            'task_send_notifications': 15,       # Run every 15 seconds
            'task_check_plex_files': 60,         # Run every 60 seconds (if enabled)
            # Periodic Maintenance/Update Tasks
            'task_check_service_connectivity': 60, # Run every 60 seconds
            'task_heartbeat': 120,               # Run every 2 minutes
            # 'task_update_statistics_summary': 300, # Run every 5 minutes
            'task_refresh_download_stats': 300,    # Run every 5 minutes
            'task_precompute_airing_shows': 600,   # Precompute airing shows every 10 minutes
            'task_verify_symlinked_files': 7200,    # Run every 120 minutes (if enabled)
            'task_verify_plex_removals': 900,      # Run every 15 minutes (if enabled) - supports both Plex and Jellyfin/Emby
            'task_reconcile_queues': 3600,         # Run every 1 hour
            'task_check_database_health': 3600,    # Run every hour
            'task_sync_time': 3600,                # Run every hour
            'task_check_trakt_early_releases': 7200,# Run every 2 hours (reduced to minimize API calls)
            'task_update_show_ids': 40600,         # Run every ~11 hours
            'task_update_show_titles': 45600,      # Run every ~12 hours
            'task_update_movie_ids': 50600,        # Run every ~14 hours
            'task_update_movie_titles': 55600,     # Run every ~15 hours
            'task_refresh_release_dates': 36600,   # Run every 10h 10m (10 hours + 600s stagger to avoid overlap with early_releases)
            'task_sync_episode_metadata': 24 * 60 * 60,  # Run every 24 hours to sync episode titles from TMDB/Trakt
            'task_cleanup_title_year_suffixes': 24 * 60 * 60,  # Run every 24 hours
            # 'task_generate_airtime_report': 3600,  # Run every hour
            'task_run_library_maintenance': 12 * 60 * 60, # Run every twelve hours (if enabled)
            'task_sync_library_metadata': 24 * 60 * 60,  # Run every 24 hours (if enabled)
            'task_get_plex_watch_history': 24 * 60 * 60,  # Run every 24 hours (if enabled)
            'task_sync_viewer_history': 6 * 60 * 60,  # Run every 6 hours (if enabled) - per-user history for Discover recommendations
            'task_refresh_plex_tokens': 24 * 60 * 60,   # Run every 24 hours
            'task_sync_plex_labels': 30 * 60,           # Run every 30 minutes
            'task_process_plex_reported_issues': 30, # Run every 30 seconds (if enabled) - Plex has no push for reports
            'task_update_tv_show_status': 172800,       # Run every 48 hours
            # 'task_purge_not_wanted_magnets_file': 604800, # Default: 1 week (Can be added if needed)
            # 'task_local_library_scan': 900, # Default: 15 mins (Can be added if needed)
            'task_plex_full_scan': 1800, # Run every hour (Can be adjusted)
            # NEW Load Adjustment Task
            # 'task_adjust_intervals_for_load': 120, # Run every 2 minutes
            # Add new task for library size refresh
            'task_refresh_library_size_cache': 12 * 60 * 60, # Run every 12 hours
            'task_backup_database': 24 * 60 * 60, # Run every 24 hours (daily backup)
            'task_backup_debrid': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_cleanup_debrid': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_backfill_nzb_torrent_ids': 24 * 60 * 60, # Run once (disabled by default)
            'task_repair_broken_nzbs': 6 * 60 * 60, # Run every 6 hours
            'task_repair_broken_debrids': 6 * 60 * 60, # Run every 6 hours
            'task_sync_cli_mount_changes': 5 * 60, # Run every 5 minutes
            'task_push_pending_climount_tags': 5 * 60, # Run every 5 minutes — catches tags changed on cli_debrid side only
            'task_scan_mount_for_external_adds': 15 * 60, # Run every 15 minutes (disabled by default)
            'task_nzb_health_check': 10,             # Run every 10 seconds — polls NZB items in Adding
            'task_nzb_playback_repair_completion': 15, # Confirm only already-started playback repairs
            'task_nzb_playback_cleanup_retry': 20 * 60, # Background retry for old-file cleanup deferred after finalization
            'task_backfill_plex_guids': 24 * 60 * 60,    # Run once (disabled by default)
            'task_backfill_plex_ms_item_id': 24 * 60 * 60, # Run once (disabled by default)
            'task_process_standalone_plex_removals': 60 * 60, # Run every hour
            # Add media analysis task interval
            'task_analyze_media_files': 1 * 60 * 60, # Once an hour
            # Add manual Plex full scan task
            'task_manual_plex_full_scan': 3600, # Run every 60 minutes, disabled by default
            # Add bulk subtitle processing task
            'task_process_bulk_subtitles': 3600, # Run every hour, disabled by default
            # Add Plex overlay tasks
            'task_overlay_sync': 3600, # Run every 1 hour (if overlays enabled)
            'task_overlay_cleanup': 86400, # Run every 24 hours (if overlays enabled)
            'task_plex_smart_collection_posters': 86400, # Run every 24 hours (if Plex mode)
            'task_plex_movie_boxsets': 86400, # Run every 24 hours (if Plex mode)
            # 'task_artificial_long_run': 1*60*60, # Run every 2 minutes
            'task_regulate_system_load': 30, # Check system load every 30 seconds
            'task_upgrade_hub_scan': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_upgrade_hub_auto_queue': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_trim_memory': 60 * 60, # Run every hour
        }
        # Store original intervals for reference (will be updated after content sources)
        self.original_task_intervals = self.task_intervals.copy()
        logging.info(f"Base default task intervals defined: {len(self.original_task_intervals)}")

        # Load custom intervals
        custom_intervals_applied = 0
        intervals_file_path = None # Define outside try
        try:
            # Add import os
            import os
            import json
            from routes.program_operation_routes import _get_task_intervals_file_path # Import helper
            intervals_file_path = _get_task_intervals_file_path()
            if os.path.exists(intervals_file_path): # Error occurred here
                # Added log
                logging.info(f"Loading custom task intervals from {intervals_file_path}")
                with open(intervals_file_path, 'r') as f:
                    # Added try-except for JSON decode
                    try:
                        saved_intervals = json.load(f)
                        logging.debug(f"Successfully loaded JSON data: {saved_intervals}") # Log loaded data
                    except json.JSONDecodeError as json_e:
                        logging.error(f"Failed to decode JSON from {intervals_file_path}: {json_e}")
                        saved_intervals = {} # Use empty dict on decode error

                # Process values as SECONDS
                for task_name, interval_seconds_val in saved_intervals.items(): # Rename loop variable
                    # Log raw values
                    logging.debug(f"Processing raw custom interval: Task='{task_name}', Value='{interval_seconds_val}'")
                    normalized_name = self._normalize_task_name(task_name) # Normalize saved task name
                    # Log normalized name
                    logging.debug(f"Normalized task name: '{normalized_name}'")

                    # Check if this task exists in our task_intervals (which start as defaults)
                    if normalized_name in self.task_intervals: # Check against task_intervals
                        # Log default interval
                        # default_interval here refers to the current value in self.task_intervals,
                        # which might have already been set by a previous custom rule if keys overlap, or is the hardcoded default.
                        current_effective_interval = self.task_intervals[normalized_name]
                        logging.debug(f"Task '{normalized_name}' exists. Current effective interval: {current_effective_interval}s")

                        if interval_seconds_val is not None: # Ignore None values (means reset)
                            try:
                                # Value is already in seconds
                                interval_sec_int = int(interval_seconds_val)
                                # Log parsed custom interval
                                logging.debug(f"Parsed custom interval for '{normalized_name}' as {interval_sec_int} seconds.")

                                MIN_INTERVAL_SECONDS = 1 # Define or import this constant
                                if interval_sec_int >= MIN_INTERVAL_SECONDS:
                                    # Log comparison
                                    # Compare with current effective, not original_task_intervals necessarily
                                    logging.debug(f"Comparing current effective ({current_effective_interval}s) with custom ({interval_sec_int}s) for '{normalized_name}'")
                                    if current_effective_interval != interval_sec_int: # Apply if different
                                        # Log application
                                        logging.info(f"Applying custom interval to '{normalized_name}': {interval_sec_int} seconds (Previous effective: {current_effective_interval}s)")
                                        self.task_intervals[normalized_name] = interval_sec_int # MODIFIES task_intervals ONLY
                                        custom_intervals_applied += 1
                                    else:
                                        # Log skipping
                                        logging.debug(f"Custom interval ({interval_sec_int}s) for '{normalized_name}' matches current effective ({current_effective_interval}s). Skipping update.")
                                else:
                                    logging.warning(f"Skipping invalid custom interval for '{normalized_name}': {interval_sec_int}s (must be >= {MIN_INTERVAL_SECONDS} seconds).")
                            except (ValueError, TypeError) as parse_e:
                                logging.warning(f"Skipping invalid custom interval format for '{normalized_name}': {interval_seconds_val}. Error: {parse_e}")
                        else:
                             # Custom interval is None, reset to original default for this task
                             if normalized_name in self.original_task_intervals:
                                 original_default = self.original_task_intervals[normalized_name]
                                 if self.task_intervals.get(normalized_name) != original_default:
                                     logging.info(f"Resetting custom interval for '{normalized_name}' to its original default: {original_default}s.")
                                     self.task_intervals[normalized_name] = original_default
                                     custom_intervals_applied += 1 # Count as an applied change
                                 else:
                                     logging.debug(f"Custom interval for '{normalized_name}' is None, and it already matches original default. No change.")
                             else:
                                 logging.warning(f"Custom interval for '{normalized_name}' is None, but no original default found. Cannot reset.")
                    else:
                        # Task from custom file is not in our initial defaults. Add it to task_intervals
                        # but NOT to original_task_intervals.
                        if interval_seconds_val is not None:
                            try:
                                interval_sec_int = int(interval_seconds_val)
                                MIN_INTERVAL_SECONDS = 10
                                if interval_sec_int >= MIN_INTERVAL_SECONDS:
                                    logging.info(f"Custom interval for new task '{normalized_name}' found: {interval_sec_int}s. Adding to effective intervals.")
                                    self.task_intervals[normalized_name] = interval_sec_int
                                    # DO NOT ADD TO self.original_task_intervals
                                    custom_intervals_applied += 1
                                else:
                                    logging.warning(f"Skipping invalid custom interval for new task '{normalized_name}': {interval_sec_int}s.")
                            except (ValueError, TypeError) as parse_e:
                                logging.warning(f"Skipping invalid custom interval format for new task '{normalized_name}': {interval_seconds_val}. Error: {parse_e}")
                        else:
                            logging.debug(f"Custom interval for new task '{normalized_name}' is None. Ignoring.")


            else:
                logging.info("No custom task_intervals.json found, using default intervals.")
        except Exception as e:
            log_path_str = f" at {intervals_file_path}" if intervals_file_path else ""
            logging.error(f"Error loading custom task intervals{log_path_str}: {e}", exc_info=True)

        if custom_intervals_applied > 0:
             logging.info(f"Applied {custom_intervals_applied} custom task intervals to effective set.")

        # Define constants for dynamic interval adjustment
        # Based on slowdown_candidates logic from task_adjust_intervals_for_load
        self.DYNAMIC_INTERVAL_TASKS = {
            'Checking', 'Sleeping', 'Blacklisted', 'Pending Uncached', 'Upgrading',
            'task_refresh_release_dates', 'task_purge_not_wanted_magnets_file',
            'task_generate_airtime_report', 'task_sync_time', 'task_check_trakt_early_releases',
            'task_reconcile_queues', 'task_refresh_download_stats',
            'task_update_show_ids', 'task_update_show_titles', 'task_update_movie_ids',
            'task_update_movie_titles', 'task_sync_episode_metadata', 'task_cleanup_title_year_suffixes', 'task_get_plex_watch_history', 'task_refresh_plex_tokens',
            'task_check_database_health', 'task_run_library_maintenance',
            'task_verify_symlinked_files', 'task_update_statistics_summary',
            'task_precompute_airing_shows',
            'task_update_tv_show_status',
            # Add new task to dynamic intervals
            'task_refresh_library_size_cache',
            'task_process_standalone_plex_removals', # Add to dynamic intervals as well
            'task_scan_mount_for_external_adds',
            # Add media analysis task to dynamic intervals
            'task_analyze_media_files',
            # Add manual plex scan to dynamic intervals
            'task_manual_plex_full_scan',
        }
        # Add content source tasks with interval > 900s (15 min) to dynamic set
        # This needs to happen *after* content sources are processed, let's refine this later if needed
        # For now, initialize with the base set. We can add sources dynamically later.

        self.MAX_INTERVAL_MULTIPLIER = 4 # Example: Max increase is 4x original
        self.ABSOLUTE_MAX_INTERVAL = 24 * 60 * 60 # Example: Max interval is 24 hours

        # Initialize content_sources attribute FIRST
        self.content_sources = None

        self.start_time = time.time()

        # --- START: Task Enabling Logic Reorder ---

        # 1. Initialize enabled_tasks with base/essential tasks
        self.enabled_tasks = {
            # Core Queue Processing (Individual queues are less important to enable here)
            'Wanted',
            'Scraping',
            'Adding',
            'Checking',
            'Sleeping',
            'Unreleased',
            'Blacklisted',
            'Pending Uncached',
            'Upgrading',
            'final_check_queue', # Use lowercase key matching the task ID
            'Pre_release',
            # Combined/High Frequency Tasks
            'task_update_queue_views',
            'task_send_notifications',
            'task_nzb_health_check',
            'task_nzb_playback_repair_completion',
            'task_nzb_playback_cleanup_retry',
            # Essential Periodic Tasks
            'task_check_service_connectivity',
            'task_heartbeat',
            # 'task_update_statistics_summary',
            'task_refresh_download_stats',
            'task_precompute_airing_shows',
            'task_reconcile_queues',
            'task_check_database_health',
            'task_sync_time',
            'task_check_trakt_early_releases',
            # 'task_update_show_ids',
            # 'task_update_show_titles',
            # 'task_update_movie_ids',
            # 'task_update_movie_titles',
            'task_refresh_release_dates',
            # 'task_generate_airtime_report',
            'task_refresh_plex_tokens',
            # 'task_update_tv_show_status',
            # NEW Load Adjustment Task
            # 'task_adjust_intervals_for_load',
            # Add 'task_verify_plex_removals' back to default set
            'task_verify_plex_removals',
            # Enable new library size task by default
            'task_refresh_library_size_cache',
            # Enable database backup task by default
            'task_backup_database',
            'task_process_standalone_plex_removals', # Enable by default
            # Enable media analysis task by default
            # 'task_analyze_media_files', # disabled by default
            # 'task_artificial_long_run',
            'task_trim_memory',
            'task_sync_library_metadata',
            'task_repair_broken_nzbs',
            'task_repair_broken_debrids',
            'task_sync_cli_mount_changes',
            'task_push_pending_climount_tags',
        }
        logging.info("Initialized base enabled tasks.")
        # (The accurate default snapshot will be captured later, after content-source and conditional tasks.)

        # 2. Load task_toggles.json ONCE and update enabled_tasks
        # Initialize saved_states before try block
        saved_states = {} # Ensure saved_states exists even if file loading fails

        try:
            import os
            import json

            db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
            toggles_file_path = os.path.join(db_content_dir, 'task_toggles.json')

            if os.path.exists(toggles_file_path):
                logging.info(f"Loading task toggle states from {toggles_file_path}")
                with open(toggles_file_path, 'r') as f:
                    saved_states = json.load(f)

                # Defer actual enabling/disabling based on toggles until after all default/conditional logic is done.
                self._saved_toggle_states = saved_states
            else:
                logging.info("No task_toggles.json found, using default enabled tasks.")
        except Exception as e:
            logging.error(f"Error loading saved task toggle states: {str(e)}")

        # 3. Get Content Sources (populates intervals AND updates enabled_tasks based on source settings)
        logging.info("Populating content source intervals and updating enabled tasks based on source settings...")
        self.get_content_sources(force_refresh=True) # This populates task_intervals and toggles sources
        logging.info("Content source processing complete.")

        # 4. Apply remaining get_setting() checks for specific tasks
        # Add file_collection_management check before other settings
        file_management_mode = get_setting('File Management', 'file_collection_management', 'Symlinked/Local') # Default for safety
        logging.info(f"File management mode: {file_management_mode}")

        # Enable 'task_plex_full_scan' only if mode is NOT Symlinked/Local
        # Check against toggle state first
        plex_scan_task = 'task_plex_full_scan'
        is_plex_scan_toggled_off = saved_states.get(self._normalize_task_name(plex_scan_task), True) is False
        if file_management_mode != 'Symlinked/Local':
            if not is_plex_scan_toggled_off and plex_scan_task not in self.enabled_tasks:
                self.enabled_tasks.add(plex_scan_task)
                logging.info(f"Enabled '{plex_scan_task}' as mode is not Symlinked/Local and not toggled off.")
        else:
            # Ensure it's disabled if mode IS Symlinked/Local, unless manually toggled ON
            is_plex_scan_toggled_on = saved_states.get(self._normalize_task_name(plex_scan_task), False) is True
            if plex_scan_task in self.enabled_tasks and not is_plex_scan_toggled_on:
                self.enabled_tasks.remove(plex_scan_task)
                logging.info(f"Disabled '{plex_scan_task}' as mode is Symlinked/Local and not toggled on.")

        if get_setting('File Management', 'file_collection_management') == 'Plex':
            # Check if Plex library checks are disabled and mounted path is blank
            plex_mounted_location = get_setting('Plex', 'mounted_file_location', default='')
            disable_plex_library_checks = get_setting('Plex', 'disable_plex_library_checks', default=False)
            mounted_path_is_blank = not plex_mounted_location or plex_mounted_location.strip() == ''
            
            # If library checks are disabled AND mounted path is blank, disable all Plex collection tasks
            if disable_plex_library_checks and mounted_path_is_blank:
                # Disable Plex file checking task
                if 'Checking' in self.enabled_tasks:
                    self.enabled_tasks.remove('Checking')
                    logging.info("Disabled 'Checking' as Plex library checks are disabled and mounted path is blank.")
                
                # Disable Plex full scan task
                if 'task_plex_full_scan' in self.enabled_tasks:
                    self.enabled_tasks.remove('task_plex_full_scan')
                    logging.info("Disabled 'task_plex_full_scan' as Plex library checks are disabled and mounted path is blank.")

                if 'task_check_plex_files' not in self.enabled_tasks:
                    self.enabled_tasks.add('task_check_plex_files')
                    logging.info("Enabled 'task_check_plex_files' as Plex library checks are disabled and mounted path is blank.")
                    
            else:
                # Enable Plex file checking if either setting is true and not explicitly disabled by toggle
                if get_setting('Plex', 'update_plex_on_file_discovery') or get_setting('Plex', 'disable_plex_library_checks'):
                     if 'task_check_plex_files' not in self.enabled_tasks:
                          # Check if it was disabled by toggle before enabling
                          # This logic might be complex depending on desired precedence (setting vs toggle)
                          # Assuming setting enables it unless explicitly toggled off:
                          # Check toggle state again (or rely on previous toggle load)
                          is_toggled_off = saved_states.get(self._normalize_task_name('task_check_plex_files'), True) is False
                          if not is_toggled_off:
                              self.enabled_tasks.add('task_check_plex_files')
                              logging.info("Enabled 'task_check_plex_files' based on Plex settings.")
                else:
                     # Ensure it's disabled if conditions aren't met AND wasn't manually enabled by toggle
                     is_toggled_on = saved_states.get(self._normalize_task_name('task_check_plex_files'), False) is True
                     if 'task_check_plex_files' in self.enabled_tasks and not is_toggled_on:
                          self.enabled_tasks.remove('task_check_plex_files')
                          logging.info("Disabled 'task_check_plex_files' as relevant Plex settings are off.")

        if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
             # Enable symlink task if configured (Plex OR Jellyfin/Emby) and not toggled off
            plex_configured = (get_setting('File Management', 'plex_url_for_symlink') and 
                              get_setting('File Management', 'plex_token_for_symlink'))
            jellyfin_configured = (get_setting('Debug', 'emby_jellyfin_url', default='').strip() and 
                                  get_setting('Debug', 'emby_jellyfin_token', default='').strip())
            
            if plex_configured or jellyfin_configured:
                symlink_task = 'task_verify_symlinked_files'
                is_symlink_toggled_off = saved_states.get(self._normalize_task_name(symlink_task), True) is False

                if not is_symlink_toggled_off and symlink_task not in self.enabled_tasks:
                    self.enabled_tasks.add(symlink_task)
                    media_server = "Jellyfin/Emby" if jellyfin_configured else "Plex"
                    logging.info(f"Enabled symlink verification task based on {media_server} settings.")
            else:
                 # Disable if no media server settings are configured and not toggled on
                 is_symlink_toggled_on = saved_states.get(self._normalize_task_name('task_verify_symlinked_files'), False) is True
                 if 'task_verify_symlinked_files' in self.enabled_tasks and not is_symlink_toggled_on:
                     self.enabled_tasks.remove('task_verify_symlinked_files')
                     logging.info("Disabled symlink verification task as no media server settings are configured.")

        # Enable the external-add mount scan only in symlink mode and only when opted in.
        # Picks up content added straight to the debrid account (DMM, provider UI) on
        # setups whose mount provider has no library-update webhook.
        _ext_scan_task = 'task_scan_mount_for_external_adds'
        if (file_management_mode == 'Symlinked/Local'
                and get_setting('File Management', 'scan_mount_for_external_adds', False)):
            _is_ext_toggled_off = saved_states.get(self._normalize_task_name(_ext_scan_task), True) is False
            if not _is_ext_toggled_off and _ext_scan_task not in self.enabled_tasks:
                self.enabled_tasks.add(_ext_scan_task)
                logging.info(f"Enabled '{_ext_scan_task}' based on File Management setting.")
        else:
            _is_ext_toggled_on = saved_states.get(self._normalize_task_name(_ext_scan_task), False) is True
            if _ext_scan_task in self.enabled_tasks and not _is_ext_toggled_on:
                self.enabled_tasks.remove(_ext_scan_task)
                logging.info(f"Disabled '{_ext_scan_task}' as the setting is off or mode is not Symlinked/Local.")


        if get_setting('Debug', 'do_not_add_plex_watch_history_items_to_queue', False):
             task_name = 'task_get_plex_watch_history'
             is_toggled_off = saved_states.get(self._normalize_task_name(task_name), True) is False
             if not is_toggled_off and task_name not in self.enabled_tasks:
                self.enabled_tasks.add(task_name)
                logging.info(f"Enabled '{task_name}' based on Debug setting.")
        else:
            task_name = 'task_get_plex_watch_history'
            is_toggled_on = saved_states.get(self._normalize_task_name(task_name), False) is True
            if task_name in self.enabled_tasks and not is_toggled_on:
                 self.enabled_tasks.remove(task_name)
                 logging.info(f"Disabled '{task_name}' as Debug setting is off.")

        # Check for limited environment and enable library maintenance task by default
        from utilities.set_supervisor_env import is_limited_environment
        if is_limited_environment():
            task_name = 'task_run_library_maintenance'
            is_toggled_off = saved_states.get(self._normalize_task_name(task_name), True) is False
            if not is_toggled_off and task_name not in self.enabled_tasks:
                self.enabled_tasks.add(task_name)
                logging.info(f"Enabled '{task_name}' by default in limited environment.")
            system_load_take_name = 'task_regulate_system_load'
            self.enabled_tasks.add(system_load_take_name)
            logging.info(f"Enabled '{system_load_take_name}' by default in limited environment.")
        
        if get_setting('Debug', 'enable_library_maintenance_task', False):
            task_name = 'task_run_library_maintenance'
            is_toggled_off = saved_states.get(self._normalize_task_name(task_name), True) is False
            if not is_toggled_off and task_name not in self.enabled_tasks:
                self.enabled_tasks.add(task_name)
                logging.info(f"Enabled '{task_name}' based on Debug setting.")
        else:
            # Only disable if not in limited environment (limited environment takes precedence)
            if not is_limited_environment():
                task_name = 'task_run_library_maintenance'
                is_toggled_on = saved_states.get(self._normalize_task_name(task_name), False) is True
                if task_name in self.enabled_tasks and not is_toggled_on:
                     self.enabled_tasks.remove(task_name)
                     logging.info(f"Disabled '{task_name}' as Debug setting is off.")

        # Enable Plex smart collection poster task if Plex is configured and not Jellyfin symlink mode
        _fm = get_setting('File Management', 'file_collection_management', '')
        _ms = get_setting('File Management', 'media_server_type', '')
        _plex_mode = not (_fm == 'Symlinked/Local' and _ms == 'jellyfin')
        if _plex_mode:
            _task_name = 'task_plex_smart_collection_posters'
            _is_toggled_off = saved_states.get(self._normalize_task_name(_task_name), True) is False
            if not _is_toggled_off and _task_name not in self.enabled_tasks:
                self.enabled_tasks.add(_task_name)
        else:
            _task_name = 'task_plex_smart_collection_posters'
            _is_toggled_on = saved_states.get(self._normalize_task_name(_task_name), False) is True
            if _task_name in self.enabled_tasks and not _is_toggled_on:
                self.enabled_tasks.remove(_task_name)

        # Enable Plex movie box sets task (same Plex-mode guard)
        if _plex_mode:
            _bs_task = 'task_plex_movie_boxsets'
            _is_toggled_off = saved_states.get(self._normalize_task_name(_bs_task), True) is False
            if not _is_toggled_off and _bs_task not in self.enabled_tasks:
                self.enabled_tasks.add(_bs_task)
        else:
            _bs_task = 'task_plex_movie_boxsets'
            _is_toggled_on = saved_states.get(self._normalize_task_name(_bs_task), False) is True
            if _bs_task in self.enabled_tasks and not _is_toggled_on:
                self.enabled_tasks.remove(_bs_task)

        # Setting-backed tasks follow their setting alone (see SETTING_BACKED_TASKS).
        for _sb_task, (_sb_section, _sb_key) in SETTING_BACKED_TASKS.items():
            if get_setting(_sb_section, _sb_key, False):
                self.enabled_tasks.add(_sb_task)
            else:
                self.enabled_tasks.discard(_sb_task)

        # Enable overlay tasks if overlays are enabled
        if get_setting('Overlay Settings', 'overlays_enabled', False):
            overlay_tasks = ['task_overlay_sync', 'task_overlay_cleanup']
            for task_name in overlay_tasks:
                is_toggled_off = saved_states.get(self._normalize_task_name(task_name), True) is False
                if not is_toggled_off and task_name not in self.enabled_tasks:
                    self.enabled_tasks.add(task_name)
                    logging.info(f"Enabled '{task_name}' as overlays are enabled.")
        else:
            # Disable overlay tasks if overlays are disabled and not manually toggled on
            overlay_tasks = ['task_overlay_sync', 'task_overlay_cleanup']
            for task_name in overlay_tasks:
                is_toggled_on = saved_states.get(self._normalize_task_name(task_name), False) is True
                if task_name in self.enabled_tasks and not is_toggled_on:
                    self.enabled_tasks.remove(task_name)
                    logging.info(f"Disabled '{task_name}' as overlays are disabled.")

        # 5. Ensure legacy individual Scraping/Adding tasks are removed *after* all logic
        # --- START REVERT: Comment out or remove this block ---
        # if 'Scraping' in self.enabled_tasks:
        #     logging.info("Removing legacy 'Scraping' task from enabled tasks (handled by combined task).")
        #     self.enabled_tasks.remove('Scraping')
        # if 'Adding' in self.enabled_tasks:
        #     logging.info("Removing legacy 'Adding' task from enabled tasks (handled by combined task).")
        #     self.enabled_tasks.remove('Adding')
        # --- END REVERT ---

        # 6. Finalize original task intervals *after* content sources potentially added intervals
        # self.original_task_intervals = self.task_intervals.copy() # REMOVE THIS LINE
        logging.info("Finalized original task intervals after all task definitions and settings.") # Comment becomes slightly less accurate but fine

        # --- NEW STEP: Capture default-enabled snapshot AFTER content-source and conditional tasks,
        # but BEFORE applying user toggle overrides. This snapshot now truly represents defaults.
        if not hasattr(self, 'default_enabled_tasks_snapshot'):
            self.default_enabled_tasks_snapshot = set(self.enabled_tasks)
            logging.info(f"Captured default_enabled_tasks_snapshot with {len(self.default_enabled_tasks_snapshot)} tasks.")

        # --- NOW apply saved toggle overrides (if any) ---
        saved_states_to_apply = getattr(self, '_saved_toggle_states', {})
        if saved_states_to_apply:
            logging.info("Applying saved task toggles after default snapshot capture...")
            for task_name, enabled in saved_states_to_apply.items():
                # Skip non-task entries (like version tracking fields)
                if not task_name.startswith('task_'):
                    logging.debug(f"Skipping non-task entry in task_toggles.json: {task_name}")
                    continue

                normalized_name = self._normalize_task_name(task_name)
                if normalized_name not in self.original_task_intervals and normalized_name not in self.task_intervals:
                    logging.warning(f"Task '{normalized_name}' in task_toggles.json not defined in intervals. Skipping toggle application.")
                    continue
                if normalized_name in SETTING_BACKED_TASKS:
                    continue
                if enabled:
                    if normalized_name not in self.enabled_tasks:
                        self.enabled_tasks.add(normalized_name)
                        logging.info(f"Toggle applied: ENABLED '{normalized_name}' from saved settings.")
                else:
                    if normalized_name in self.enabled_tasks:
                        self.enabled_tasks.remove(normalized_name)
                        logging.info(f"Toggle applied: DISABLED '{normalized_name}' from saved settings.")
        else:
            logging.info("No saved toggle states to apply.")

        # --- END: Task Enabling Logic Reorder ---


        # Define queue processing map EARLIER
        self.queue_processing_map = {
            'Wanted': 'process_wanted',
            'Scraping': 'process_scraping',
            'Adding': 'process_adding',
            'Checking': 'process_checking',
            'Sleeping': 'process_sleeping',
            'Unreleased': 'process_unreleased',
            'Blacklisted': 'process_blacklisted',
            'Pending Uncached': 'process_pending_uncached',
            'Upgrading': 'process_upgrading',
            'final_check_queue': 'process_final_check', # Use lowercase key matching the task ID
            'Pre_release': 'process_pre_release'
        }

        # Log the final set of enabled tasks right before starting the scheduling process
        logging.info(f"Final enabled tasks before initial scheduling: {sorted(list(self.enabled_tasks))}")

        # Serialize all scheduled tasks
        # To address potential DB contention by making all scheduled tasks run sequentially.
        # logging.info("Configuring all scheduled tasks to run sequentially to prevent DB contention.") # This logic is now handled by scheduler config
        # if not hasattr(self, 'task_intervals') or not self.task_intervals:
            # logging.error("Cannot configure HEAVY_DB_TASKS for serialization: self.task_intervals is not populated. Tasks will use original HEAVY_DB_TASKS definition if any.")
            # If self.HEAVY_DB_TASKS was already defined with a base set, it would be used.
            # Since we initialized it to set(), if this unlikely error occurs, no tasks will be considered "heavy" by default.
        # else:
            # self.HEAVY_DB_TASKS = set(self.task_intervals.keys()) # REVERTED: Scheduler now handles global sequential execution. HEAVY_DB_TASKS reverts to original intent.
            # logging.info(f"All {len(self.HEAVY_DB_TASKS)} tasks defined in task_intervals will now use the exclusive execution lock, effectively running sequentially.")

        # Capture baseline enabled tasks snapshot
        # Store the set of tasks that were enabled immediately after initialization (including
        # any changes applied from task_toggles.json, content-source processing, and settings).
        # When the user later saves toggle states we can compare against this snapshot and only
        # persist differences, keeping the JSON file minimal.
        self.initial_enabled_tasks_snapshot = set(self.enabled_tasks)

        # Schedule initial tasks
        self._schedule_initial_tasks()

        # Modify tracemalloc conditional start in __init__
        # Track if tracemalloc is enabled via setting AND if it was imported
        self._tracemalloc_enabled = tracemalloc_available and get_setting('Debug', 'enable_tracemalloc', False)
        if self._tracemalloc_enabled:
            # This check is redundant now due to the above, but safe
            if tracemalloc_available and tracemalloc:
                 logging.warning("Tracemalloc memory tracking is enabled. This adds overhead.")
                 try:
                     tracemalloc.start(10) # Start tracking with a stack depth of 10
                 except Exception as e_start:
                     logging.error(f"Failed to start tracemalloc: {e_start}")
                     self._tracemalloc_enabled = False # Disable if start fails
            else:
                 # Should not happen if _tracemalloc_enabled is True, but log just in case
                 logging.warning("Tracemalloc setting enabled but module not available. Disabling.")
                 self._tracemalloc_enabled = False

        # Add task execution counter and sample rate for tracemalloc
        self.task_execution_count = 0
        # Read sample rate from settings, default to 100 (sample 1 in every 100 tasks)
        self.tracemalloc_sample_rate = int(get_setting('Debug', 'tracemalloc_sample_rate', 100))
        # Ensure sample rate is at least 1 to avoid division by zero or weird behavior
        if self.tracemalloc_sample_rate < 1:
            logging.warning(f"Invalid tracemalloc_sample_rate ({self.tracemalloc_sample_rate}), defaulting to 1.")
            self.tracemalloc_sample_rate = 1

        # Add variable to store previous snapshot
        self.previous_tracemalloc_snapshot = None
        # Log tracemalloc status with sample rate
        if self._tracemalloc_enabled:
            logging.warning(f"Tracemalloc memory tracking is ENABLED (Sample Rate: 1/{self.tracemalloc_sample_rate}). This adds overhead.")
            # Check again if it's actually tracing (might have failed to start)
            if not (tracemalloc_available and tracemalloc and tracemalloc.is_tracing()):
                 logging.error("Tracemalloc was enabled but is not tracing. Check for startup errors.")
                 self._tracemalloc_enabled = False # Ensure flag reflects reality
        # ... rest of __init__ ...

        self.current_running_task = None
        self._running_task_lock = threading.Lock() # Lock for thread-safe access

        # Add inter-task sleep variables
        self.base_inter_task_sleep = float(get_setting('Queue', 'main_loop_sleep_seconds', 0.0))
        self.current_inter_task_sleep = self.base_inter_task_sleep
        logging.info(f"Initialized inter-task sleep to {self.current_inter_task_sleep}s based on settings.")

        # Add long-running content source tasks to DYNAMIC_INTERVAL_TASKS
        # This should run *after* self.content_sources is populated and intervals set
        # Ideally placed after self.get_content_sources(force_refresh=True) call inside __init__
        if self.content_sources: # Check if sources were loaded
            for task_id, interval in self.task_intervals.items():
                 # Check if it's a content source task and interval is long
                 if task_id.startswith('task_') and task_id.endswith('_wanted') and interval > 900:
                      self.DYNAMIC_INTERVAL_TASKS.add(task_id)
            logging.info(f"Updated DYNAMIC_INTERVAL_TASKS with long-running content sources. Total: {len(self.DYNAMIC_INTERVAL_TASKS)}")

        # Add currently_executing_tasks set
        self.currently_executing_tasks = set()

        # Remove single current_running_task
        # self.current_running_task = None # Removed
        self._running_task_lock = threading.Lock() # Lock for thread-safe access to the set


        # Add task execution counter and sample rate for tracemalloc
        self.task_execution_count = 0
        # ... (rest of __init__)

        # In __init__, add:
        self.manual_tasks = set()  # Track manually triggered tasks
        self._initialized_runner_attributes = True # Mark as initialized at the end of actual init logic
        # Runtime tracking attributes
        self.task_runtime_totals = defaultdict(float)  # Accumulated runtime per task in current window
        self.task_runtime_lock = threading.Lock()  # Protect access to task_runtime_totals
        self._runtime_log_interval_sec = 300  # How often to emit runtime percentage report (seconds)
        self._last_runtime_log_time = time.monotonic()
        # CPU usage tracking attributes
        # Accumulate per-task CPU seconds in current window
        self.task_cpu_totals = defaultdict(float)
        self.task_cpu_lock = threading.Lock()
        # Share the same interval as runtime logging to avoid log noise
        self._cpu_log_interval_sec = self._runtime_log_interval_sec
        self._last_cpu_log_time = time.monotonic()
        # Control per-run CPU logging via environment variable
        self._log_per_run_cpu = os.environ.get('LOG_PER_RUN_CPU', 'false').lower() == 'true'

    # New method to get task target
    def _get_task_target(self, task_name: str, manual: bool = False):
        """Resolves the target function and arguments for a given task name."""
        target_func = None
        args = []
        kwargs = {}
        task_type_determined = "Unknown"

        # 1. Queue Processing Tasks (using the map)
        if task_name in self.queue_processing_map:
            task_type_determined = "Queue Task (Map)"
            method_name = self.queue_processing_map[task_name]
            if hasattr(self.queue_manager, method_name):
                target_func = getattr(self.queue_manager, method_name)
                if task_name == 'Checking':
                    args = [self] # Pass ProgramRunner instance
            else:
                logging.error(f"Method '{method_name}' not found in QueueManager for task '{task_name}'")

        # 2. Content Source Tasks (task_SOURCE_wanted)
        elif task_name.startswith('task_') and task_name.endswith('_wanted'):
            task_type_determined = "Content Source Task"
            source_id = task_name[5:-7]
            if self.content_sources is None:
                self.get_content_sources(force_refresh=True)
            source_data = self.content_sources.get(source_id)
            if source_data:
                target_func = self.process_content_source
                args = [source_id, source_data]
                if manual:
                    # Explicit manual/debug-triggered runs bypass the source cache so a
                    # user flipping unblacklist_on_source_run and re-running immediately
                    # actually reaches add_wanted_items instead of being cache-skipped.
                    kwargs = {'bypass_cache': True}
            else:
                logging.warning(f"Content source data not found for source ID '{source_id}' derived from task '{task_name}'. This task will be skipped.")

        # 3. Regular task_* methods (including combined tasks and new load adjustment task)
        elif task_name.startswith('task_'):
            task_type_determined = "Regular Task (task_*)"
            if hasattr(self, task_name):
                target_func = getattr(self, task_name)
            else:
                logging.error(f"Method '{task_name}' not found in ProgramRunner")

        # Default/Error case
        else:
            task_type_determined = "ERROR - Unknown Format"
            logging.error(f"Unknown task type or name format for task resolution: '{task_name}'")

        logging.debug(f"Resolved task '{task_name}' as Type: {task_type_determined}")
        return target_func, args, kwargs


    # Use _get_task_target in _schedule_task
    def _schedule_task(self, task_name: str, interval_seconds: int, initial_run: bool = False, initial_delay_seconds: int = 0):
        """Schedules a single task in APScheduler, wrapped for duration measurement.

        Args:
            task_name: Name of the task to schedule
            interval_seconds: Interval in seconds between task runs
            initial_run: Whether this is the initial scheduling run
            initial_delay_seconds: Delay in seconds before first run (for staggering startup tasks)
        """
        # Safety check: scheduler must be initialized before scheduling tasks
        if self.scheduler is None:
            logging.warning(f"Cannot schedule task '{task_name}': scheduler is not initialized. Start the program first.")
            return False

        # When disable_initialization is enabled, delay heavy tasks by their full interval
        # This prevents release_dates and content sources from running immediately on startup
        if initial_run and initial_delay_seconds == 0:
            disable_init = get_setting('Debug', 'disable_initialization', '')
            if disable_init:
                # Heavy tasks: release_dates and content sources (_wanted tasks)
                is_heavy_task = (task_name == 'task_refresh_release_dates' or task_name.endswith('_wanted'))
                if is_heavy_task:
                    initial_delay_seconds = interval_seconds
                    logging.info(f"[disable_initialization] Delaying heavy task '{task_name}' by {interval_seconds}s (full interval)")

        current_thread_id_outer = threading.get_ident()
        logging.debug(f"Attempting to schedule task: '{task_name}' with interval {interval_seconds}s (initial_run: {initial_run}, initial_delay: {initial_delay_seconds}s) (Thread: {current_thread_id_outer})")
        lock_acquired = False # Flag to track lock acquisition
        try:
            current_thread_id = threading.get_ident()
            logging.info(f"SCHED_TASK_PRE_LOCK: Preparing to acquire scheduler_lock for task '{task_name}' (Thread: {current_thread_id})")
            with self.scheduler_lock:
                lock_acquired = True
                current_thread_id_inner = threading.get_ident() # Get ID again after lock
                logging.info(f"SCHED_TASK_POST_LOCK: Acquired scheduler_lock for task '{task_name}' (Thread: {current_thread_id_inner})")
                job_id = task_name # Use task name as job ID for regular tasks

                # Check if job already exists
                existing_job = self.scheduler.get_job(job_id)
                if existing_job:
                    if initial_run:
                        logging.debug(f"Task '{job_id}' already scheduled. Skipping initial schedule.")
                        return True
                    logging.info(f"Task '{job_id}' already exists. Removing old job before rescheduling.")
                    try:
                        self.scheduler.remove_job(job_id)
                    except Exception as e:
                        logging.error(f"Error removing existing job '{job_id}': {e}")
                        return False

                # --- Resolve target function using helper ---
                target_func, args, kwargs = self._get_task_target(task_name)
                # ------------------------------------------

                if target_func:
                    try:
                        # For regular tasks, actual_job_id and task_name_for_logging are the same (job_id).
                        # Pass the bound method + its args separately (not via functools.partial) —
                        # APScheduler's SQLAlchemyJobStore pickles jobs on start()/add_job(), and
                        # functools.partial objects can never be resolved to a serializable reference
                        # (apscheduler.util.obj_to_ref unconditionally rejects partials), which crashes
                        # scheduling with "This Job cannot be serialized...".

                        # Explicitly pass scheduler's timezone to add_job
                        # This should prevent the IntervalTrigger from calling tzlocal.get_localzone()
                        resolved_timezone = self.scheduler.timezone
                        logging.debug(f"Passing timezone '{resolved_timezone}' explicitly to add_job for task '{job_id}'")

                        # *** START STAGGER EDIT: Support initial delay for startup staggering ***
                        # Create trigger with start_date if delay is specified
                        # Add jitter to interval to prevent task alignment (±10% randomization)
                        import random
                        jitter_factor = random.uniform(0.9, 1.1)  # ±10% jitter
                        jittered_interval = int(interval_seconds * jitter_factor)

                        if initial_delay_seconds > 0:
                            # Explicit delay requested (stagger, disable_initialization, etc.) — honour it.
                            from datetime import datetime, timedelta
                            first_run_time = datetime.now(resolved_timezone) + timedelta(seconds=initial_delay_seconds)
                            trigger = IntervalTrigger(seconds=jittered_interval, start_date=first_run_time, timezone=resolved_timezone)
                            logging.info(f"Task '{job_id}' will start in {initial_delay_seconds}s (at {first_run_time.strftime('%H:%M:%S')}) for startup staggering, interval: {jittered_interval}s (jittered from {interval_seconds}s)")
                        elif initial_run:
                            # On startup, resume from the persisted next-run time so the timer continues
                            # where it left off before the restart instead of resetting to a full interval.
                            from datetime import datetime, timedelta
                            _persisted_next = getattr(self, '_task_schedules', {}).get(job_id)
                            if _persisted_next:
                                first_run_time = datetime.fromtimestamp(_persisted_next, tz=resolved_timezone)
                                trigger = IntervalTrigger(seconds=jittered_interval, start_date=first_run_time, timezone=resolved_timezone)
                                _remaining = (_persisted_next - time.time())
                                if _remaining > 0:
                                    logging.info(f"Task '{job_id}' resuming persisted schedule: next run in {_remaining/3600:.1f}h")
                                else:
                                    logging.info(f"Task '{job_id}' was overdue by {-_remaining/3600:.1f}h, will run immediately")
                            else:
                                # No persisted schedule — use exact interval for first fire so jitter
                                # doesn't cause the first run to land outside the configured interval.
                                from datetime import datetime, timedelta
                                first_run_time = datetime.now(resolved_timezone) + timedelta(seconds=interval_seconds)
                                trigger = IntervalTrigger(seconds=jittered_interval, start_date=first_run_time, timezone=resolved_timezone)
                                logging.debug(f"Task '{job_id}' no persisted schedule, first run in {interval_seconds}s, interval jittered to {jittered_interval}s")
                        else:
                            trigger = IntervalTrigger(seconds=jittered_interval, timezone=resolved_timezone)
                            if jittered_interval != interval_seconds:
                                logging.debug(f"Task '{job_id}' interval jittered: {jittered_interval}s (from {interval_seconds}s)")
                        # *** END STAGGER EDIT ***

                        # Queue-processing tasks get their own executor so they don't
                        # block scheduled maintenance tasks and vice versa.
                        _QUEUE_TASKS = {
                            'Adding', 'Checking', 'Scraping', 'Wanted',
                            'Sleeping', 'Unreleased',
                            'Blacklisted', 'Pending Uncached', 'Upgrading',
                            'final_check_queue', 'Pre_release',
                            'task_check_plex_files', 'task_send_notifications',
                            'task_update_queue_views',
                            'task_regulate_system_load',
                            'task_nzb_health_check',
                        }
                        _executor = 'queue' if job_id in _QUEUE_TASKS else 'default'

                        add_job_kwargs = {
                            'func': self._run_and_measure_task,
                            'args': (job_id, task_name, target_func, args, kwargs),
                            'trigger': trigger,
                            'id': job_id,
                            'name': job_id,
                            'replace_existing': True,
                            'misfire_grace_time': None,
                            'max_instances': 1,
                            'timezone': resolved_timezone,
                            'executor': _executor,
                        }

                        self.scheduler.add_job(**add_job_kwargs)

                        # Updated log message with stagger info
                        if initial_delay_seconds > 0:
                            logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds}s (staggered start: +{initial_delay_seconds}s, max_instances=1)")
                        else:
                            logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds} seconds (max_instances=1, wrapped for duration measurement).")
                        return True
                    except Exception as e:
                        logging.error(f"Error scheduling task '{job_id}': {e}", exc_info=True)
                        return False
                else:
                     logging.warning(f"Failed to determine target function for task '{task_name}'. Cannot schedule. This might be an obsolete task toggle.")
                     return False
        finally:
            current_thread_id_finally = threading.get_ident()
            if lock_acquired:
                logging.info(f"SCHED_TASK_FINALLY: Releasing scheduler_lock implicitly for task '{task_name}' (Thread: {current_thread_id_finally}, Lock Acquired: True).")
            else:
                logging.info(f"SCHED_TASK_FINALLY: Lock was not acquired or error for task '{task_name}' (Thread: {current_thread_id_finally}, Lock Acquired: False).")


    def _schedule_initial_tasks(self):
        """Schedules all enabled tasks based on initial configuration and prunes obsolete content source tasks."""
        logging.info("Scheduling initial tasks and checking for obsolete toggles...")
        scheduled_count = 0
        failed_to_schedule_count = 0 # Tasks that couldn't be scheduled for various reasons (e.g. no interval)
        pruned_obsolete_task_count = 0

        # Iterate over a copy of the set to allow modification of the original self.enabled_tasks
        tasks_to_process = list(self.enabled_tasks)

        # *** START STAGGER EDIT: Calculate stagger delays for Trakt tasks ***
        # Identify all enabled Trakt tasks that need staggering on startup
        trakt_tasks_to_stagger = sorted([
            task for task in tasks_to_process
            if task.startswith('task_Trakt Lists_') or task == 'task_Trakt Collection_1'
        ])

        # Build a map of task_name -> initial_delay_seconds (always stagger, even after reinitialize)
        trakt_task_stagger_map = {}
        for idx, task_name in enumerate(trakt_tasks_to_stagger):
            delay_seconds = (idx + 1) * 240  # 240s (4 min), 480s (8 min), 720s (12 min), etc.
            trakt_task_stagger_map[task_name] = delay_seconds

        # Log stagger plan
        if trakt_task_stagger_map:
            logging.info(f"Staggering {len(trakt_task_stagger_map)} enabled Trakt tasks to prevent concurrent API calls:")
            for task_name, delay in sorted(trakt_task_stagger_map.items(), key=lambda x: x[1]):
                logging.info(f"  - {task_name}: +{delay}s delay")
        # *** END STAGGER EDIT ***

        for task_name in tasks_to_process:
            # Ensure task is still in self.enabled_tasks; it might have been removed if tasks_to_process had duplicates
            # and one was already processed and removed. However, list(set) makes duplicates unlikely.
            # This check is more of a safeguard if self.enabled_tasks was manipulated externally during this loop,
            # or if tasks_to_process could somehow have a task not currently in self.enabled_tasks.
            if task_name not in self.enabled_tasks:
                continue

            interval = self.task_intervals.get(task_name)
            if interval is not None:
                # *** START STAGGER EDIT: Apply stagger delay for Trakt tasks ***
                # Get the stagger delay for this task (0 if not a Trakt task)
                delay = trakt_task_stagger_map.get(task_name, 0)

                # Attempt to schedule with the appropriate delay
                if self._schedule_task(task_name, interval, initial_run=True, initial_delay_seconds=delay):
                    scheduled_count += 1
                # *** END STAGGER EDIT ***
                else:
                    # Scheduling failed. _schedule_task already logged a warning if it was due to target_func being None.
                    # Now, specifically check if it was an obsolete content source task.
                    is_content_source_task_pattern = task_name.startswith('task_') and task_name.endswith('_wanted')
                    
                    if is_content_source_task_pattern:
                        # Confirm the failure was due to a missing content source by re-checking _get_task_target's outcome.
                        # _get_task_target logs its own warning if the source_id is not found.
                        target_func_check, _, _ = self._get_task_target(task_name)
                        if target_func_check is None:
                            logging.warning(
                                f"Obsolete task toggle found for missing content source: '{task_name}'. "
                                f"Removing it from active enabled tasks. This change will be saved if/when task toggles are persisted."
                            )
                            self.enabled_tasks.discard(task_name) # Remove from the live set
                            pruned_obsolete_task_count += 1
                        else:
                            # Task matched content source pattern, _schedule_task failed, but _get_task_target now finds a function.
                            # This is an unexpected state, possibly due to timing or a different scheduling issue.
                            logging.error(f"Task '{task_name}' (content source type) failed to schedule, but a target function was found on re-check. Investigate.")
                            failed_to_schedule_count += 1
                    else:
                        # Failed to schedule, and it's not a content source task pattern.
                        # The warning for this was already logged by _schedule_task if target_func was None.
                        failed_to_schedule_count += 1
            else:
                logging.warning(f"Task '{task_name}' is enabled but has no interval defined in task_intervals. Skipping scheduling.")
                failed_to_schedule_count += 1
        
        if pruned_obsolete_task_count > 0:
            logging.info(f"Pruned {pruned_obsolete_task_count} obsolete content source task toggle(s) from the active configuration during this startup.")
        
        logging.info(f"Initial task scheduling Tally: "
                     f"Successfully Scheduled: {scheduled_count}, "
                     f"Failed/Skipped (e.g. no interval, other errors): {failed_to_schedule_count}, "
                     f"Pruned Obsolete Content Source Tasks: {pruned_obsolete_task_count}.")

        # Seed task_schedule.json with the initial next-run times for any task that doesn't
        # already have a persisted entry. This ensures that even daily tasks that have never
        # completed survive their first restart correctly.
        try:
            _seeded = 0
            for job in self.scheduler.get_jobs():
                _jinterval = self.task_intervals.get(job.id, 0)
                if job.next_run_time and job.id not in self._task_schedules and _jinterval >= 1200:
                    self._task_schedules[job.id] = job.next_run_time.timestamp()
                    _seeded += 1
            if _seeded:
                import json as _json_seed
                with open(self._task_schedule_file, 'w') as _f_seed:
                    _json_seed.dump(self._task_schedules, _f_seed)
                logging.info(f"[TaskScheduler] Seeded {_seeded} new task schedules into task_schedule.json")
        except Exception as _e_seed:
            logging.debug(f"[TaskScheduler] Could not seed task schedules: {_e_seed}")

        # Schedule a periodic snapshot so restarts always restore the actual remaining time.
        try:
            from apscheduler.triggers.interval import IntervalTrigger as _SnapshotTrigger
            self.scheduler.add_job(
                self._snapshot_task_schedules,
                trigger=_SnapshotTrigger(minutes=5),
                id='task_snapshot_schedules',
                name='Snapshot Task Schedules',
                replace_existing=True,
                misfire_grace_time=60,
            )
            logging.info("[TaskScheduler] Snapshot job registered (every 5 min)")
        except Exception as _e_snap:
            logging.warning(f"[TaskScheduler] Could not register snapshot job: {_e_snap}")

    def _is_within_pause_schedule(self):
        """Checks if the current time is within the configured pause schedule."""
        if not get_setting('Queue', 'enable_pause_schedule', False):
            return False # Schedule not enabled

        start_time_str = get_setting('Queue', 'pause_start_time', '00:00')
        end_time_str = get_setting('Queue', 'pause_end_time', '00:00')

        try:
            start_time = dt_time.fromisoformat(start_time_str)
            end_time = dt_time.fromisoformat(end_time_str)
        except ValueError:
            logging.error(f"Invalid pause time format: start='{start_time_str}', end='{end_time_str}'. Must be HH:MM.")
            return False # Treat invalid format as schedule not active

        # Get current time in the configured timezone using the imported function
        from metadata.metadata import _get_local_timezone
        tz = _get_local_timezone() # Use the imported function directly
        now = datetime.now(tz).time()

        # Handle overnight schedules (e.g., start 22:00, end 06:00)
        if start_time <= end_time:
            # Normal schedule within the same day
            return start_time <= now <= end_time
        else:
            # Overnight schedule
            return now >= start_time or now <= end_time

    def task_heartbeat(self):
        random_number = random.randint(1, 100)
        if self._running:
            if random_number < 100:
                logging.info("Program running...")
            else:
                logging.info("Program running...is your fridge?")

        # Add psutil memory logging
        if psutil:
            try:
                # 1. Explicit full garbage collection, at most every 15 minutes.
                # A full collection pauses every thread (page requests included)
                # and Python's own collector already runs continuously; the
                # malloc_trim below still runs on every heartbeat.
                now_mono = time.monotonic()
                if now_mono - getattr(self, '_last_full_gc', 0.0) >= 900:
                    self._last_full_gc = now_mono
                    gc_start = time.perf_counter()
                    collected = gc.collect()
                    logging.debug(f"Explicitly ran gc.collect(): {collected} objects in {(time.perf_counter() - gc_start) * 1000:.0f} ms")

                # 2. Log memory before trim
                process = psutil.Process(os.getpid())
                mem_info_before = process.memory_info()
                rss_mb_before = mem_info_before.rss / (1024 * 1024)
                vms_mb_before = mem_info_before.vms / (1024 * 1024)
                
                log_message = f"[Memory Usage] Before: RSS={rss_mb_before:.2f}MB, VMS={vms_mb_before:.2f}MB."

                # 3. Attempt to release memory to OS
                system = platform.system()
                if system == "Linux":
                    try:
                        ctypes.CDLL('libc.so.6').malloc_trim(0)
                        logging.debug("malloc_trim(0) called on Linux.")
                    except Exception as e:
                        logging.warning(f"Failed to call malloc_trim(0): {e}")
                elif system == "Windows":
                    try:
                        ctypes.CDLL('msvcrt')._heapmin()
                        logging.debug("_heapmin() called on Windows.")
                    except Exception as e:
                        logging.warning(f"Failed to call _heapmin(): {e}")
                
                # 4. Log memory after trim
                mem_info_after = process.memory_info()
                rss_mb_after = mem_info_after.rss / (1024 * 1024)
                vms_mb_after = mem_info_after.vms / (1024 * 1024)
                
                log_message += f" After: RSS={rss_mb_after:.2f}MB, VMS={vms_mb_after:.2f}MB."
                logging.info(log_message)

            except Exception as e:
                logging.error(f"Error in heartbeat memory management: {e}")
        else:
            # Log less frequently if psutil is missing
            if random_number < 10: # Log warning occasionally
                 logging.warning("psutil not installed, cannot report detailed memory usage in heartbeat.")

        # Add tracemalloc snapshot comparison in heartbeat
        # Check if enabled AND available before using
        if self._tracemalloc_enabled and tracemalloc_available and tracemalloc and tracemalloc.is_tracing():
            try:
                current_snapshot = tracemalloc.take_snapshot()
                if self.previous_tracemalloc_snapshot:
                    # Compare the current snapshot to the previous one
                    stats = current_snapshot.compare_to(self.previous_tracemalloc_snapshot, 'lineno')

                    # Log the top 10 differences (lines allocating the most *new* memory)
                    logging.info("[Tracemalloc Heartbeat] Top 10 memory differences since last heartbeat:")
                    total_diff = 0
                    for i, stat in enumerate(stats[:10], 1):
                        total_diff += stat.size_diff
                        # Limit traceback line length for cleaner logs
                        trace_line = stat.traceback.format()[-1]
                        trace_line = trace_line[:150] + '...' if len(trace_line) > 150 else trace_line
                        logging.info(f"  {i}: {trace_line} | Diff: {stat.size_diff / 1024:+.1f} KiB | Count Diff: {stat.count_diff:+} | New Size: {stat.size / 1024:.1f} KiB")
                    logging.info(f"[Tracemalloc Heartbeat] Total Diff in Top 10: {total_diff / 1024:+.1f} KiB")

                # Store the current snapshot for the next comparison
                self.previous_tracemalloc_snapshot = current_snapshot

            except Exception as e_trace_hb:
                logging.error(f"[Tracemalloc Heartbeat] Error processing snapshot comparison: {e_trace_hb}")

        # Watchdog: ensure critical queue tasks are still scheduled.
        # APScheduler can silently drop jobs when misfire_grace_time=None and the thread
        # pool is saturated (e.g. task_nzb_health_check consuming 99% of runtime).
        #
        # Skipped while a reinitialize() is in progress: scheduler.shutdown(wait=True)
        # blocks the executor's shutdown on this very task finishing, while get_jobs()
        # below needs the same _jobstores_lock shutdown() already holds — calling it here
        # during a reinit deadlocks task_heartbeat against reinitialize() forever, which is
        # exactly what made adding/deleting a content source "take forever".
        if not getattr(self, '_reinitializing', False):
            try:
                _CRITICAL_QUEUES = {
                    'Scraping': 1,
                    'Adding': 1,
                    'Checking': 30,
                    'Wanted': 30,
                }
                _scheduled_job_ids = {job.id for job in self.scheduler.get_jobs()}
                for _task_name, _interval in _CRITICAL_QUEUES.items():
                    if _task_name not in _scheduled_job_ids and _task_name in self.enabled_tasks:
                        logging.warning(f'[Heartbeat] Watchdog: {_task_name!r} missing from scheduler — rescheduling')
                        self._schedule_task(_task_name, _interval)
            except Exception as _wd_err:
                logging.debug(f'[Heartbeat] Watchdog error: {_wd_err}')

        # Scraping-queue stuck detector: if the in-memory Scraping queue has been at the
        # same size >= WANTED_THROTTLE_SCRAPING_SIZE (100) for more than 10 minutes without
        # any items being moved out, force-sync by moving all stale items back to Wanted
        # so the throttle releases and the system recovers without a restart.
        try:
            _scraping_q = self.queue_manager.queues.get('Scraping')
            if _scraping_q is not None:
                _scraping_size = len(_scraping_q.get_contents())
                _now = time.time()
                if not hasattr(self, '_scraping_stuck_since'):
                    self._scraping_stuck_since = None
                    self._scraping_stuck_last_size = 0
                if _scraping_size >= 100:
                    if self._scraping_stuck_last_size != _scraping_size:
                        # Size changed — reset the stuck timer
                        self._scraping_stuck_since = _now
                        self._scraping_stuck_last_size = _scraping_size
                    elif self._scraping_stuck_since is None:
                        self._scraping_stuck_since = _now
                    elif _now - self._scraping_stuck_since > 600:  # 10 minutes stuck
                        logging.warning(
                            f'[Heartbeat] Scraping queue stuck at {_scraping_size} items for '
                            f'{int(_now - self._scraping_stuck_since)}s — '
                            f'force-syncing in-memory queue from DB to release throttle'
                        )
                        _new_size = _scraping_size
                        try:
                            _scraping_q.update()
                            _new_size = len(_scraping_q.get_contents())
                            logging.info(f'[Heartbeat] Scraping queue after force-sync: {_new_size} items')
                        except Exception as _sync_err:
                            logging.warning(f'[Heartbeat] Scraping queue force-sync error: {_sync_err}')
                        self._scraping_stuck_since = _now  # Reset timer after intervention
                        self._scraping_stuck_last_size = _new_size
                else:
                    self._scraping_stuck_since = None
                    self._scraping_stuck_last_size = _scraping_size
        except Exception as _stuck_err:
            logging.debug(f'[Heartbeat] Scraping stuck detector error: {_stuck_err}')

        

    


            # The old logic to stop the program after 5 retries is already commented out, which is good.

    def pause_queue(self):
        # THROTTLING: Prevent rapid pause operations
        from datetime import datetime
        now = datetime.now()
        if self.last_pause_time:
            elapsed = (now - self.last_pause_time).total_seconds()
            if elapsed < self.pause_resume_cooldown:
                logging.debug(f"Pause throttled ({elapsed:.1f}s < {self.pause_resume_cooldown}s). Skipping pause_queue().")
                return

        self.last_pause_time = now

        # Pause ALL running jobs
        with self.scheduler_lock:
            if self.scheduler.state != 1: # 1 = STATE_RUNNING
                logging.warning("Scheduler is not running, cannot pause jobs.")
                return

            all_jobs = self.scheduler.get_jobs()
            paused_count = 0
            logging.debug(f"Pausing all running jobs. Total jobs found: {len(all_jobs)}")
            logging.debug(f"Jobs already tracked as paused by this mechanism: {sorted(list(self.paused_jobs_by_queue))}")

            # Define tasks that should never be paused (essential monitoring tasks)
            never_pause_tasks = {'task_check_service_connectivity', 'task_heartbeat'}

            for job in all_jobs:
                job_id = job.id
                
                # Skip pausing essential monitoring tasks
                if job_id in never_pause_tasks:
                    logging.debug(f"Skipping pause for essential task: {job_id}")
                    continue
                
                # Only pause if the job is scheduled to run (not already paused indefinitely)
                # and not already tracked by this mechanism
                if job.next_run_time is not None and job_id not in self.paused_jobs_by_queue:
                     try:
                         self.scheduler.pause_job(job_id)
                         self.paused_jobs_by_queue.add(job_id)
                         paused_count += 1
                         logging.debug(f"Paused scheduler job via pause_queue: {job_id}")
                     except Exception as e:
                         logging.error(f"Error pausing job '{job_id}': {e}")
                elif job.next_run_time is None:
                    logging.debug(f"Job '{job_id}' was already paused. Ensuring it's tracked.")
                    # Ensure it's tracked if it was already paused by other means
                    if job_id not in self.paused_jobs_by_queue:
                        self.paused_jobs_by_queue.add(job_id)
                elif job_id in self.paused_jobs_by_queue:
                     logging.debug(f"Job '{job_id}' was already in paused_jobs_by_queue set. Skipping pause action.")


            # Use the existing QueueManager pause state if needed for UI/status
        from queues.queue_manager import QueueManager
        # Pass the reason_string to QueueManager's pause
        reason_for_qm = self.pause_info.get("reason_string") if self.pause_info else "Unknown reason"
        QueueManager().pause_queue(reason=reason_for_qm)

        self.queue_paused = True
        # Log using pause_info
        log_reason = self.pause_info.get('reason_string', 'Unknown') if self.pause_info else 'Unknown'
        logging.info(f"Queue paused. Attempted to pause all running jobs (except essential monitoring tasks)... Reason: {log_reason}")

    def resume_queue(self):
        # THROTTLING: Prevent rapid resume operations
        from datetime import datetime
        now = datetime.now()
        if self.last_resume_time:
            elapsed = (now - self.last_resume_time).total_seconds()
            if elapsed < self.pause_resume_cooldown:
                logging.debug(f"Resume throttled ({elapsed:.1f}s < {self.pause_resume_cooldown}s). Skipping resume_queue().")
                return

        self.last_resume_time = now

        # Resume logic remains the same, but update log context
        logging.info(f"[Resume Queue] Starting resume process. Queue paused: {self.queue_paused}, Pause type: {self.pause_info.get('error_type') if self.pause_info else 'None'}")

        with self.scheduler_lock:
            if self.scheduler.state != 1: # 1 = STATE_RUNNING
                logging.warning("Scheduler is not running, cannot resume jobs.")
                return

            resumed_count = 0
            # Only resume jobs that were paused by pause_queue (or added to the set)
            jobs_to_resume = list(self.paused_jobs_by_queue) # Copy to avoid modification issues
            logging.debug(f"Attempting to resume jobs tracked during pause: {sorted(jobs_to_resume)}")

            for job_id in jobs_to_resume:
                 try:
                     job = self.scheduler.get_job(job_id)
                     if job:
                        # Check if the job is actually paused (next_run_time is None)
                        if job.next_run_time is None:
                            self.scheduler.resume_job(job_id)
                            self.paused_jobs_by_queue.remove(job_id) # Remove from tracked set
                            resumed_count += 1
                            logging.debug(f"Resumed scheduler job via resume_queue: {job_id}")
                        else:
                            logging.debug(f"Job '{job_id}' was found but already running (not paused). Removing from paused_jobs_by_queue set.")
                            # Remove from set even if not paused, as it shouldn't be tracked anymore
                            if job_id in self.paused_jobs_by_queue:
                                self.paused_jobs_by_queue.remove(job_id)
                     else:
                         # Job doesn't exist anymore, remove it from the tracking set
                         logging.warning(f"Job '{job_id}' not found while resuming, removing from paused_jobs_by_queue set.")
                         if job_id in self.paused_jobs_by_queue:
                              self.paused_jobs_by_queue.remove(job_id)

                 except Exception as e:
                     # Log specific errors during resume attempt
                     logging.error(f"Error resuming job '{job_id}': {e}")
                     # Decide if we should keep it in the set or remove on error?
                     # Removing might be safer to prevent infinite loops if resume fails consistently.
                     if job_id in self.paused_jobs_by_queue:
                           logging.warning(f"Removing job '{job_id}' from paused_jobs_by_queue set due to resume error.")
                           self.paused_jobs_by_queue.remove(job_id)


            # Use the existing QueueManager resume state if needed for UI/status
        from queues.queue_manager import QueueManager
        QueueManager().resume_queue()

        # Clear all pause-related state
        self.queue_paused = False
        self.connectivity_failure_time = None
        self.connectivity_retry_count = 0
        # Clear pause_info
        self.pause_info = {
            "reason_string": None, "error_type": None, "service_name": None,
            "status_code": None, "retry_count": 0
        }
        logging.info(f"[Resume Queue] Queue resumed successfully. Resumed {resumed_count} jobs.")  # Better log message

        

    

            # Don't re-raise - allow other content sources to continue processing

    


    # def task_generate_airtime_report(self):
    #     generate_airtime_report()

    # ---------------------------------------------------------------------------
    # Task schedule persistence helpers
    # ---------------------------------------------------------------------------

    def _load_task_schedules(self) -> dict:
        """Load persisted task next-run timestamps from disk."""
        import json
        path = getattr(self, '_task_schedule_file', None)
        if path:
            try:
                if os.path.exists(path):
                    with open(path, 'r') as f:
                        data = json.load(f)
                    logging.info(f"[TaskScheduler] Loaded {len(data)} persisted task schedules from task_schedule.json")
                    return data
            except Exception as e:
                logging.warning(f"[TaskScheduler] Could not load task_schedule.json: {e}")
        return {}

    def _save_task_schedule(self, task_name: str, next_run_time: float):
        """Persist a task's next scheduled run time to disk."""
        import json
        path = getattr(self, '_task_schedule_file', None)
        if not path:
            return
        try:
            self._task_schedules[task_name] = next_run_time
            with open(path, 'w') as f:
                json.dump(self._task_schedules, f)
        except Exception as e:
            logging.debug(f"[TaskScheduler] Could not save task_schedule.json: {e}")

    def _snapshot_task_schedules(self):
        """Snapshot every qualifying task's current next_run_time to disk.

        Runs every 5 minutes so that on restart the actual remaining time is
        restored rather than resetting to the full default interval.
        """
        import json
        path = getattr(self, '_task_schedule_file', None)
        if not path:
            return
        try:
            updated = 0
            for job in self.scheduler.get_jobs():
                if job.id == 'task_snapshot_schedules':
                    continue
                _jinterval = self.task_intervals.get(job.id, 0)
                if job.next_run_time and _jinterval >= 1200:
                    self._task_schedules[job.id] = job.next_run_time.timestamp()
                    updated += 1
            if updated:
                with open(path, 'w') as f:
                    json.dump(self._task_schedules, f)
                logging.debug(f"[TaskScheduler] Snapshot saved {updated} task schedules")
        except Exception as e:
            logging.debug(f"[TaskScheduler] Snapshot error: {e}")

    # ---------------------------------------------------------------------------


    def run_initialization(self, is_restart=False):
        """Run initialization sequence.
        
        Args:
            is_restart (bool): If True, skip initialization as this is a mid-run restart.
                              This prevents unnecessary processing and potential data loss
                              when the program is restarted after settings changes.

        Runs in the background (see _start_initial_sync): the scheduler is already
        processing the queues, so the program reports Running throughout.
        """
        logging.info("Running initialization...")

        # Skip initialization if this is a restart (mid-run) or if explicitly disabled
        if is_restart:
            logging.info("Skipping initialization as this is a restart (mid-run)")
            return
            
        skip_initial_plex_update = get_setting('Debug', 'skip_initial_plex_update', False)
        
        disable_initialization = get_setting('Debug', 'disable_initialization', '')
        if not disable_initialization:
            initialize(skip_initial_plex_update)
            logging.info("Initialization complete")
        else:
            logging.info("Initialization disabled, skipping...")
        
        # Sync config file with runtime state after initialization is complete
        try:
            from utilities.settings import get_all_settings, save_config
            config = get_all_settings()
            content_sources = config.get('Content Sources', {})
            config_updated = False
            
            for source, data in content_sources.items():
                task_name = f'task_{source}_wanted'
                current_enabled = task_name in self.enabled_tasks
                
                if data.get('enabled') != current_enabled:
                    data['enabled'] = current_enabled
                    config_updated = True
                    logging.info(f"Updated config for {source}: enabled = {current_enabled} (to match runtime state)")
            
            if config_updated:
                save_config(config)
                logging.info("Updated content source enabled states in config file to match runtime state after initialization")
                
        except Exception as e:
            logging.error(f"Failed to update config file with runtime enabled states after initialization: {e}")

    def _start_initial_sync(self, is_restart=False):
        """Run run_initialization in a background thread.

        It used to run inline in run(), holding the program at "Starting" (the
        Queues page hidden, Stop refused) for the minutes it takes to pull every
        content source and refresh release dates, although the scheduler was
        already processing the queues. A sync still going from an earlier start
        isn't started twice.
        """
        with _initial_sync_guard:
            if _initial_sync_active.is_set():
                logging.info("Initial sync from an earlier start is still running; not starting another.")
                return
            _initial_sync_active.set()

        def _sync():
            try:
                self.run_initialization(is_restart=is_restart)
            except Exception as e:
                logging.error(f"Initial sync failed: {e}", exc_info=True)
            finally:
                _initial_sync_active.clear()

        threading.Thread(target=_sync, name='initial-sync', daemon=True).start()

    def is_initial_sync_running(self):
        return is_initial_sync_running()

    def start(self, is_restart=False):
        if self._running and self.scheduler and self.scheduler.running:
            logging.info("ProgramRunner.start called, but program is already running.")
            return
        if self._initializing:
            logging.info("ProgramRunner.start called, but program is already initializing.")
            return
        if self._stopping: 
            logging.warning("ProgramRunner.start called, but program is currently stopping.")
            return

        # On each start, reset the inter-task sleep to the configured default
        # to override any changes made by the dynamic load regulator in a previous run.
        self.base_inter_task_sleep = float(get_setting('Queue', 'main_loop_sleep_seconds', 0.0))
        self.current_inter_task_sleep = self.base_inter_task_sleep
        logging.info(f"Inter-task sleep reset to {self.current_inter_task_sleep}s on program start.")

        self._initializing = True
        self._stopping = False 
        # Store the is_restart parameter for use in run_initialization
        self._is_restart = is_restart
        logging.info("ProgramRunner: Initializing...")
        
        try:
            logging.info("Starting APScheduler...")
            if self.scheduler and not self.scheduler.running:
                start_paused = self._is_within_pause_schedule()
                self.scheduler.start(paused=start_paused)
                self._scheduler_started_once = True
                logging.info(f"APScheduler started. Paused: {start_paused}")
            elif not self.scheduler:
                logging.error("ProgramRunner.start: CRITICAL - Scheduler not initialized. Cannot start.")
                self._initializing = False # Ensure initializing is reset
                return

            # _running will now be set inside run(), after it passes its own guard.

            # Clear the initializing flag *before* entering the main run loop so run() doesn't
            # mistakenly believe initialization is still underway.
            self._initializing = False
            self.update_heartbeat() 
            logging.info("ProgramRunner: Started successfully.")
            # self.run() # The run loop should be started by the external caller if this is a thread target.
                       # If ProgramRunner.start() is the entry point for its own thread, then self.run() is appropriate here.
                       # Based on program_operation_routes, a new thread is created for runner_instance.start,
                       # so this start method itself becomes the thread's target.
                       # However, the `run_program` function at the end of the file calls program_runner.start() and then returns,
                       # implying start() might be expected to block or manage its own loop if it's the main program thread.
                       # The `run` method contains the main while loop.
                       # If `start` is called in a new thread, and `start` calls `self.run()`, that is correct.
            self.run() # Assuming start() is the entry point for the ProgramRunner's main execution flow.

        except Exception as e:
            logging.error(f"ProgramRunner: Error during start: {e}", exc_info=True)
            self._running = False
            # self._initializing = False # Moved to finally
            if self.scheduler and self.scheduler.running:
                try:
                    self.scheduler.shutdown(wait=False)
                except Exception as e_shutdown:
                    logging.error(f"Error shutting down scheduler after failed start: {e_shutdown}")
        finally:
            self._initializing = False # Ensure initializing is false after attempt.


    def stop(self):
        # Check if already fully stopped and not in the process of stopping
        if not self._running and not self._initializing and not self._stopping:
            logging.info("ProgramRunner.stop called, but program is not running, initializing, or actively stopping.")
            self._running = False
            self._initializing = False
            self._stopping = False 
            return

        logging.info(f"ProgramRunner.stop called. Current state: running={self._running}, initializing={self._initializing}, stopping={self._stopping}")
        
        self._stopping = True 
        self._initializing = False 
        
        try:
            if self._running: # If it thought it was running, mark it as not running anymore.
                self._running = False 
            
            if self.scheduler:
                try:
                    logging.info("Attempting to shut down APScheduler...")
                    if self.scheduler.running:
                        self.scheduler.shutdown(wait=True)
                        logging.info("APScheduler shut down successfully.")
                    else:
                        logging.info("APScheduler was not running when stop was called.")
                except Exception as e:
                    logging.error(f"Error shutting down APScheduler: {e}", exc_info=True)
                self.scheduler = None 
            else:
                logging.info("No APScheduler instance to shut down (was None).")
            
            self._running = False # Final confirmation
            logging.info("ProgramRunner: Stop sequence completed.")

        except Exception as e_stop_main:
            logging.error(f"ProgramRunner: Error during main stop logic: {e_stop_main}", exc_info=True)
            self._running = False # Ensure running is false on error
        finally:
            self._stopping = False # Reset stopping flag
            logging.info(f"ProgramRunner: _stopping flag set to False. Final state: running={self._running}")


    def is_running(self):
        return self._running

    def is_initializing(self): 
        return self._initializing

    def is_stopping(self): 
        return self._stopping

    def get_status(self):
        """Returns the current status of the program as a string."""
        if self.is_initializing():
            return "Starting"
        if self.is_stopping():
            return "Stopping"
        if self.is_running():
            return "Running"
        return "Stopped"

    def run(self):
        # Guard against duplicate starts. We only consider the running flag now.
        if self._running:
            logging.warning("Attempted to start program, but it's already running.")
            return
        
        # Mark as running. The startup sync below runs in its own thread, so the
        # program reports Running while it works.
        self._running = True

        try:
            logging.info("Starting program run loop (monitoring scheduler state)")
            self._running = True  # Make sure running flag is set

            # Pass the is_restart parameter to run_initialization
            self._start_initial_sync(is_restart=getattr(self, '_is_restart', False))

            # Simplified run loop with CPU monitoring
            # The main loop now just keeps the script alive while the scheduler runs.
            # We can add checks here if needed (e.g., monitoring scheduler health).
            
            # CPU monitoring variables
            cpu_monitor_start = time.time()
            cpu_monitor_interval = 300.0  # Log CPU usage every 5 minutes
            loop_count = 0
            
            while self._running or getattr(self, '_reinitializing', False):
                try:
                    # Check scheduler status periodically
                    if not self.scheduler or not self.scheduler.running:
                         if getattr(self, '_reinitializing', False):
                             # reinitialize() intentionally shuts the scheduler down
                             # (and its own __init__ call transiently resets _running)
                             # before building a new one — this is expected, not a crash.
                             time.sleep(1.0)
                             continue
                         logging.error("APScheduler is not running. Stopping program.")
                         self.stop() # Trigger stop if scheduler died
                         break
            
                    # Perform checks that still need to run outside scheduled tasks
                    # e.g., connectivity checks that might pause/resume scheduler jobs
                    if self.connectivity_failure_time or self.queue_paused:
                        # If we're in a failure state, check more aggressively
                        self.check_connectivity_status()

                    # Fail-safe: if connectivity recovery logic never resumes the queue,
                    # kick a watchdog that will forcibly resume after a timeout.
                    self._fail_safe_resume_if_stuck()

                    is_scheduled_pause = self._is_within_pause_schedule()
                    current_pause_type = self.pause_info.get("error_type") if self.pause_info else None

                    if is_scheduled_pause and not self.queue_paused: # Or if paused for a different, non-schedule reason
                        pause_start = get_setting('Queue', 'pause_start_time', '00:00')
                        pause_end = get_setting('Queue', 'pause_end_time', '00:00')
                        new_reason_string = f"Scheduled pause active ({pause_start} - {pause_end})"
                        
                        # Update pause_info for scheduled pause
                        self.pause_info = {
                            "reason_string": new_reason_string,
                            "error_type": "SYSTEM_SCHEDULED",
                            "service_name": "System",
                            "status_code": None,
                            "retry_count": 0
                        }
                        self.pause_queue()
                        logging.info(f"Queue automatically paused due to schedule: {new_reason_string}")
                    elif not is_scheduled_pause and self.queue_paused and current_pause_type == "SYSTEM_SCHEDULED":
                        logging.info("Scheduled pause period ended. Resuming queue.")
                        # Clear pause_info on resume
                        self.pause_info = {
                            "reason_string": None, "error_type": None, "service_name": None,
                            "status_code": None, "retry_count": 0
                        }
                        self.resume_queue()
                    # ... (sleep) ...

                    # CPU monitoring and main loop sleep
                    loop_count += 1
                    current_time = time.time()
                    
                    # Log CPU usage every 5 seconds
                    if current_time - cpu_monitor_start >= cpu_monitor_interval:
                        try:
                            import psutil
                            cpu_percent = psutil.cpu_percent(interval=None)
                            memory_percent = psutil.virtual_memory().percent
                            process = psutil.Process()
                            process_cpu_percent = process.cpu_percent()
                            process_memory_mb = process.memory_info().rss / 1024 / 1024
                            
                            logging.info(f"CPU MONITORING - Loop #{loop_count}: "
                                        f"System CPU: {cpu_percent:.1f}%, "
                                        f"Process CPU: {process_cpu_percent:.1f}%, "
                                        f"Memory: {memory_percent:.1f}% ({process_memory_mb:.1f}MB), "
                                        f"Loop rate: {loop_count / (current_time - cpu_monitor_start + cpu_monitor_interval):.1f} loops/sec")
                            
                            # If system CPU is high but our process isn't using much, log top processes
                            if cpu_percent > 50 and process_cpu_percent < 5:
                                try:
                                    top_processes = []
                                    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                                        try:
                                            if proc.info['cpu_percent'] > 5:  # Only show processes using >5% CPU
                                                top_processes.append(proc.info)
                                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                                            pass
                                    
                                    # Sort by CPU usage and show top 5
                                    top_processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
                                    if top_processes:
                                        logging.info("TOP CPU PROCESSES (system high, app low):")
                                        for proc in top_processes[:5]:
                                            logging.info(f"  PID {proc['pid']}: {proc['name']} - CPU: {proc['cpu_percent']:.1f}%, Memory: {proc['memory_percent']:.1f}%")
                                except Exception as e:
                                    logging.error(f"Error getting top processes: {e}")
                        except ImportError:
                            logging.info(f"CPU MONITORING - Loop #{loop_count}: psutil not available")
                        except Exception as e:
                            logging.error(f"CPU MONITORING - Error getting CPU stats: {e}")
                        
                        # Reset monitoring
                        cpu_monitor_start = current_time
                        loop_count = 0
                    
                    # Main loop sleep
                    try:
                        main_loop_sleep_seconds = float(get_setting('Queue', 'main_loop_sleep_seconds', 5.0))
                    except (ValueError, TypeError):
                        main_loop_sleep_seconds = 5.0
                    # Ensure sleep is not too low. The setting defaults to 0 (it is
                    # meant for the gap between tasks), which made this loop wake
                    # 10 times a second just to check connectivity/pause state.
                    if main_loop_sleep_seconds < 1.0:
                        main_loop_sleep_seconds = 1.0
                    time.sleep(main_loop_sleep_seconds) # Check status based on setting

                except Exception as loop_error:
                     logging.error(f"Error in main monitoring loop: {loop_error}", exc_info=True)
                     time.sleep(10) # Longer sleep on error

            logging.warning("Program run loop exited.")
            # Ensure scheduler is stopped if loop exits unexpectedly
            if self.scheduler and self.scheduler.running:
                 self.stop()

        except Exception as e:
            logging.error(f"Fatal error in run method: {str(e)}")
            logging.error(traceback.format_exc())
            # Ensure stop is called on fatal error
            self.stop()

    def invalidate_content_sources_cache(self):
        self.content_sources = None

    def task_adjust_intervals_for_load(self): # Renamed
        """
        Task to dynamically adjust non-critical task intervals based on queue load.
        Intervals are increased when Scraping/Adding queues are empty,
        and reset when they have items.
        """
        # Remove [Refactor Needed] log
        # logging.debug("[Refactor Needed] adjust_task_intervals_based_on_load needs to use scheduler.modify_job/reschedule_job")

        # --- START REFACTOR ---
        if not hasattr(self, '_interval_adjustment_time'):
            self._interval_adjustment_time = 0

        current_time = time.time()
        if current_time - self._interval_adjustment_time < 60:
            return

        self._interval_adjustment_time = current_time

        # Define non-critical tasks (same logic as before)
        slowdown_candidates = {
            'Checking', 'Sleeping', 'Blacklisted', 'Pending Uncached', 'Upgrading',
            'task_refresh_release_dates', 'task_purge_not_wanted_magnets_file',
            'task_generate_airtime_report', 'task_sync_time', 'task_check_trakt_early_releases',
            'task_reconcile_queues', 'task_refresh_download_stats',
            'task_update_show_ids', 'task_update_show_titles', 'task_update_movie_ids',
            'task_update_movie_titles', 'task_sync_episode_metadata', 'task_cleanup_title_year_suffixes', 'task_get_plex_watch_history', 'task_refresh_plex_tokens',
            'task_check_database_health', 'task_run_library_maintenance',
            'task_verify_symlinked_files', 'task_update_statistics_summary',
            'task_precompute_airing_shows',
            'task_update_tv_show_status'
        }
        # Add content source tasks with interval > 900s (15 min) to dynamic set
        # This needs to happen *after* content sources are processed, let's refine this later if needed
        # For now, initialize with the base set. We can add sources dynamically later.

        idle_increase_seconds = 300
        # DELAY_THRESHOLD = 3 # Remove delay threshold

        # --- Determine idle state based on Scraping/Adding queues ---
        system_is_idle = False
        # Initialize queue status variables to prevent UnboundLocalError if queue check fails
        scraping_empty = True 
        adding_empty = True
        checking_empty = True
        if hasattr(self, 'queue_manager') and self.queue_manager:
            scraping_queue = self.queue_manager.queues.get('Scraping')
            adding_queue = self.queue_manager.queues.get('Adding')
            checking_queue = self.queue_manager.queues.get('Checking') # Get the Checking queue
            if scraping_queue and adding_queue and checking_queue: # Ensure all queues are found
                try:
                    # Use get_contents with limit 1 for efficiency
                    scraping_empty = len(scraping_queue.get_contents()) == 0
                    adding_empty = len(adding_queue.get_contents()) == 0
                    checking_empty = len(checking_queue.get_contents()) == 0 # Check if Checking queue is empty
                    system_is_idle = scraping_empty and adding_empty and checking_empty # Update idle condition
                except Exception as e:
                    logging.error(f"Error checking Scraping/Adding/Checking queue state for idle check: {e}")
                    # Default to not idle on error
                    system_is_idle = False
            else:
                logging.warning("Scraping, Adding, or Checking queue not found for idle check.")
                system_is_idle = False # Assume not idle if queues are missing
        else:
             logging.warning("Queue manager not available for idle check.")
             system_is_idle = False # Assume not idle if manager is missing

        # --- End Determine idle state ---

        with self.scheduler_lock:
            if system_is_idle:
                if not hasattr(self, '_last_idle_adjustment_log') or current_time - self._last_idle_adjustment_log >= 600:
                     # Updated log message
                     logging.info(f"System idle (Scraping, Adding, and Checking queues empty) - increasing non-critical task intervals by {idle_increase_seconds}s.")
                     self._last_idle_adjustment_log = current_time

                for task_id in slowdown_candidates:
                     job = self.scheduler.get_job(task_id)
                     original_interval = self.original_task_intervals.get(task_id)
                     if job and original_interval:
                         current_job_interval = job.trigger.interval.total_seconds()
                         new_interval = original_interval + idle_increase_seconds
                         if new_interval > current_job_interval: # Only modify if increasing
                             try:
                                 self.scheduler.modify_job(task_id, trigger=IntervalTrigger(seconds=new_interval))
                                 logging.debug(f"Increased interval for '{task_id}' to {new_interval}s")
                             except Exception as e:
                                 logging.error(f"Error modifying job '{task_id}' interval to {new_interval}s: {e}")

            else: # System is active
                 active_reason_parts = [] # Renamed for clarity
                 # Update active reason based on new check
                 # This logic seems to check specific queues if not idle, let's ensure it's correct
                 if not scraping_empty: active_reason_parts.append("Scraping queue has items")
                 if not adding_empty: active_reason_parts.append("Adding queue has items")
                 if not checking_empty: active_reason_parts.append("Checking queue has items")
                 
                 final_active_reason_str = "; ".join(active_reason_parts) if active_reason_parts else "One or more core queues have items"


                 log_now = False 
                 if not hasattr(self, '_last_active_state_log'):
                      self._last_active_state_log = 0
                      # Ensure _was_idle_last_check is initialized if it's the first run or after a restart
                      if not hasattr(self, '_was_idle_last_check'):
                          self._was_idle_last_check = True # Assume was idle to trigger initial active log if system starts active

                 if not self._was_idle_last_check: 
                      if current_time - self._last_active_state_log >= 600:
                           log_now = True
                 else: 
                      log_now = True


                 if log_now:
                      logging.info(f"System active ({final_active_reason_str}) - ensuring default task intervals.")
                      self._last_active_state_log = current_time

                 needs_reset = False
                 tasks_to_reset = []
                 for task_id in slowdown_candidates:
                      job = self.scheduler.get_job(task_id)
                      base_interval = self.task_intervals.get(task_id)
                      if job and base_interval:
                           current_job_interval = job.trigger.interval.total_seconds()
                           if current_job_interval != base_interval:
                                needs_reset = True
                                tasks_to_reset.append(task_id)

                 if needs_reset:
                      logging.info(f"Resetting intervals for {len(tasks_to_reset)} tasks to default values.")
                      for task_id in tasks_to_reset:
                           base_interval = self.task_intervals.get(task_id)
                           if base_interval:
                                try:
                                    self.scheduler.modify_job(task_id, trigger=IntervalTrigger(seconds=base_interval))
                                    logging.debug(f"Reset interval for '{task_id}' to {base_interval}s")
                                    if task_id == "Checking":
                                        job_check = self.scheduler.get_job(task_id) # Re-fetch job
                                        if job_check:
                                            live_interval = job_check.trigger.interval.total_seconds()
                                            logging.info(f"[DEBUG] After reset: self.task_intervals['Checking']={self.task_intervals.get('Checking')}, scheduler job interval={live_interval}")
                                except Exception as e:
                                    logging.error(f"Error resetting job '{task_id}' interval to {base_interval}s: {e}")
            
            # Conditional block for forcing next_run_time on state change:
            if not hasattr(self, '_was_idle_last_check'): # Initialize if it doesn't exist (e.g. first run)
                self._was_idle_last_check = not system_is_idle # Set to opposite of current to ensure first run acts as a change if needed by logging

            if system_is_idle != self._was_idle_last_check:
                logging.info(f"System idle state changed (was_idle: {self._was_idle_last_check}, is_idle: {system_is_idle}). Forcing next run time for slowdown_candidates.")
                for task_id in slowdown_candidates:
                    job = self.scheduler.get_job(task_id)
                    base_interval = self.task_intervals.get(task_id) # Uses current configured interval
                    if job and base_interval:
                        try:
                            next_run_utc = datetime.now(self.scheduler.timezone) + timedelta(seconds=base_interval)
                            job.modify(next_run_time=next_run_utc)
                            # Convert to local time for logging
                            from metadata.metadata import _get_local_timezone # Added import
                            local_tz = _get_local_timezone()
                            next_run_local = next_run_utc.astimezone(local_tz)
                            logging.info(f"[DEBUG] Forced next run time for '{task_id}' to {next_run_local} (interval {base_interval}s) due to state change.")
                        except Exception as e:
                            logging.error(f"Error forcing next run time for '{task_id}' due to state change: {e}")
            # --- END NEW BLOCK / MODIFIED BLOCK ---

        self._was_idle_last_check = system_is_idle
         # --- END REFACTOR ---


    def check_task_health(self):
        """
        Check task health. This check is currently disabled as APScheduler's
        misfire handling is preferred over manual delay detection based on
        next_run_time.
        """
        # --- START REFACTOR (Disabled) ---
        logging.info("Task health check is disabled. Returning 0 delayed tasks.")
        return 0 # Return 0, indicating no delayed tasks detected by this method
        # --- END REFACTOR (Disabled) ---


    def update_heartbeat(self):
        """Update the heartbeat file directly."""
        import os

        # Save the current time as the last heartbeat
        current_time = int(time.time())
        
        # Store the heartbeat in memory to reduce I/O operations
        if not hasattr(self, '_last_heartbeat_time'):
            self._last_heartbeat_time = 0
            self._last_heartbeat_file_write = 0
            self._heartbeat_io_slow = False
            self._heartbeat_io_check_time = 0
        
        self._last_heartbeat_time = current_time
        
        # If I/O was previously detected as slow, use a longer interval (5 minutes)
        file_write_interval = 300 if self._heartbeat_io_slow else 30
        
        # Only write to disk periodically to reduce I/O
        if current_time - self._last_heartbeat_file_write >= file_write_interval:
            db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
            heartbeat_file = os.path.join(db_content_dir, 'program_heartbeat')
            
            try:
                # Ensure the directory exists
                os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
                
                # Measure how long the I/O operation takes
                io_start_time = time.time()
                
                # Write directly to the heartbeat file
                with open(heartbeat_file, 'w') as f:
                    f.write(str(current_time))
                    f.flush()
                    os.fsync(f.fileno())
                
                io_duration = time.time() - io_start_time
                
                # If I/O takes more than 100ms, it's slow
                if io_duration > 0.1 and not self._heartbeat_io_slow:
                    logging.warning(f"Heartbeat file I/O is slow ({io_duration:.2f}s) - reducing write frequency")
                    self._heartbeat_io_slow = True
                
                # Periodically re-check if I/O speed has improved (every 30 minutes)
                elif self._heartbeat_io_slow and current_time - self._heartbeat_io_check_time > 1800:
                    if io_duration < 0.05:  # If improved to under 50ms
                        logging.info("Heartbeat file I/O speed has improved - resuming normal write frequency")
                        self._heartbeat_io_slow = False
                    self._heartbeat_io_check_time = current_time
                
                self._last_heartbeat_file_write = current_time
            except (IOError, OSError) as e:
                logging.error(f"Failed to update heartbeat file: {e}")
                # Mark I/O as slow if we get errors
                self._heartbeat_io_slow = True

    def check_heartbeat(self):
        """Check heartbeat using memory cache with fallback to file."""
        import os
        
        current_time = int(time.time())
        
        # Initialize in-memory heartbeat tracking
        if not hasattr(self, '_last_heartbeat_time'):
            self._last_heartbeat_time = 0
            self._last_heartbeat_file_write = 0
        
        # If in-memory heartbeat is recent enough, use it
        if self._last_heartbeat_time > 0:
            time_diff = current_time - self._last_heartbeat_time
            
            # If memory indicates a stale heartbeat (over 5 minutes)
            if time_diff > 300:
                logging.warning(f"Stale heartbeat detected in memory - {time_diff} seconds since last update")
                return False
                
            # If the memory heartbeat is recent, no need to check file
            return True
                
        # If no memory heartbeat or it's stale, check file as fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        heartbeat_file = os.path.join(db_content_dir, 'program_heartbeat')
        
        # Ensure directory exists
        try:
            os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
        except (IOError, OSError) as e:
            logging.error(f"Failed to create heartbeat directory: {e}")
            return False
        
        if not os.path.exists(heartbeat_file):
            logging.warning("Heartbeat file does not exist - creating new one")
            self.update_heartbeat()
            return True

        try:
            with open(heartbeat_file, 'r') as f:
                content = f.read().strip()
                if not content:
                    logging.warning("Heartbeat file exists but is empty - updating it")
                    self.update_heartbeat()
                    return True
                    
                try:
                    last_heartbeat = int(content)
                except ValueError:
                    logging.error(f"Invalid heartbeat value in file: '{content}' - updating it")
                    self.update_heartbeat()
                    return True
                    
                time_diff = current_time - last_heartbeat
                
                # Update in-memory cache from file
                self._last_heartbeat_time = last_heartbeat
                
                # If more than 5 minutes have passed since last heartbeat
                if time_diff > 300:
                    logging.warning(f"Stale heartbeat detected in file - {time_diff} seconds since last update")
                    return False
                
                return True
        except (IOError, OSError) as e:
            logging.error(f"Error reading heartbeat file: {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error checking heartbeat: {e}")
            return False


    # Track last health check duration to implement skip-if-slow guard
    _nzb_health_last_duration: float = 0.0
    _NZB_HEALTH_MAX_JOBS_PER_TICK = 30   # cap jobs polled per tick
    _nzb_health_job_cursor: int = 0      # where the next capped tick starts polling
    _nzb_job_started_at: dict = {}       # job_id → datetime its download clock started (timeout anchor)
    _NZB_HEALTH_JOB_TIMEOUT = 20         # seconds per job poll before giving up
    _NZB_HEALTH_SKIP_THRESHOLD = 20.0   # if last run took this long, skip next tick
    _nzb_folder_wait_counts: dict = {}   # job_id → consecutive ticks where folder not found
    _NZB_FOLDER_WAIT_MAX = 10            # after this many ticks (~100s) treat as broken
    _nzb_health_triggered: dict = {}     # entry_name → tick count since trigger (0 = not yet triggered)
    _NZB_HEALTH_MAX_POLL_TICKS = 6       # give up polling after ~60s (6 ticks × 10s)
    _nzb_confirmed_complete: dict = {}   # job_id → entry_name for jobs confirmed complete+folder found
    _nzb_ghost_repeat_counts: dict = {}  # item_id → consecutive ghost-job cycles for this item
    _NZB_GHOST_BLACKLIST_THRESHOLD = 3   # blacklist instead of retrying after this many ghost repeats


    def reinitialize(self):
        """Force reinitialization of the program runner to pick up new settings"""
        logging.info("Reinitializing ProgramRunner...")
        # Tell the run() monitoring loop to tolerate a momentarily-stopped
        # scheduler during this window, instead of treating it as a crash
        # and tearing down the whole run loop out from under us.
        self._reinitializing = True
        was_running_before_reinit = self._running
        try:
            # Need to shutdown and restart scheduler carefully
            with self.scheduler_lock:
                if self.scheduler and self.scheduler.running:
                    logging.info("Shutting down scheduler for reinitialization...")
                    # wait=False: don't block here until every in-flight job finishes.
                    # shutdown(wait=True) holds APScheduler's internal _jobstores_lock
                    # while it waits — and any running task that itself calls back into
                    # self.scheduler (e.g. task_heartbeat's watchdog, which calls
                    # scheduler.get_jobs()) needs that same lock, deadlocking forever.
                    # In-flight jobs aren't killed by wait=False, they just keep running
                    # to completion on their own threads, detached from this call.
                    self.scheduler.shutdown(wait=False)
                    logging.info("Scheduler stopped.")

            self._initialized_runner_attributes = False
            self.__init__() # Re-runs init, including scheduling initial tasks

            # __init__ unconditionally resets _running to False — restore it so
            # the still-alive run() loop (in the 'was already running' case)
            # doesn't see _running=False and exit on its next iteration.
            if was_running_before_reinit:
                self._running = True

                # __init__ only builds a fresh scheduler object, it doesn't start
                # it — without this, jobs would be scheduled but never fire.
                with self.scheduler_lock:
                    if self.scheduler and not self.scheduler.running:
                        start_paused = self._is_within_pause_schedule()
                        self.scheduler.start(paused=start_paused)
                        self._scheduler_started_once = True
                        logging.info(f"Scheduler restarted after reinitialization. Paused: {start_paused}")

            logging.info("ProgramRunner reinitialized successfully.")
        finally:
            self._reinitializing = False

    def handle_rate_limit(self):
        """Handle rate limit by pausing relevant jobs for a period."""
        pause_duration = 1800 # 30 minutes
        logging.warning(f"Rate limit exceeded. Pausing relevant Debrid-interacting jobs for {pause_duration // 60} minutes.")

        # --- Send Notification as Queue Pause ---
        try:
            enabled_notifications = get_enabled_notifications()
            if enabled_notifications: # Only send if notifications are configured
                # Construct the specific message for the pause reason
                message = f"Queue paused for {pause_duration // 60} minutes due to Debrid rate limit."
                # Call with 'queue_pause' category
                _send_notifications(message, enabled_notifications, notification_category='queue_pause')
        except Exception as e:
            logging.error(f"Failed to send rate limit pause notification: {e}")
        # --- End Send Notification ---

        jobs_to_pause = set()
        # Identify jobs that might hit Debrid APIs
        # --- START REVERT: Replace combined task with individual ones ---
        debrid_related_ids = {
            'Wanted', 'Scraping', 'Adding', 'Checking', 'Upgrading',
        # --- END REVERT ---
            # Content sources that *might* trigger checks? Less likely direct Debrid hits.
            # task_reconcile_queues? Unlikely.
            # task_process_pending_rclone_paths? Depends on handle_rclone_file logic.
        }
        # Add content source tasks that might trigger searches
        for task_id in self.task_intervals:
             if task_id.startswith('task_') and task_id.endswith('_wanted'):
                 debrid_related_ids.add(task_id)


        with self.scheduler_lock:
            if self.scheduler.state != 1: return # Not running

            # Define tasks that should never be paused (essential monitoring tasks)
            never_pause_tasks = {'task_check_service_connectivity', 'task_heartbeat'}

            paused_count = 0
            for job_id in debrid_related_ids:
                 # Skip pausing essential monitoring tasks
                 if job_id in never_pause_tasks:
                     logging.debug(f"Rate Limit: Skipping pause for essential task: {job_id}")
                     continue
                     
                 try:
                     job = self.scheduler.get_job(job_id)
                     if job and job.next_run_time is not None: # Only pause if running
                          # Pause the job temporarily
                          self.scheduler.pause_job(job_id)
                          jobs_to_pause.add(job_id) # Track jobs we actually paused
                          paused_count += 1
                          logging.debug(f"Rate Limit: Paused job {job_id}")
                     elif job and job.next_run_time is None:
                          logging.debug(f"Rate Limit: Job {job_id} was already paused.")
                          jobs_to_pause.add(job_id) # Also track already paused jobs to ensure they get resumed
                 except Exception as e:
                      logging.error(f"Rate Limit: Error pausing job {job_id}: {e}")

            if paused_count > 0:
                 logging.info(f"Rate Limit: Paused {paused_count} active jobs. Scheduling resume in {pause_duration} seconds for {len(jobs_to_pause)} total affected jobs.")
            elif jobs_to_pause:
                 logging.info(f"Rate Limit: No active jobs needed pausing, but scheduling resume check for {len(jobs_to_pause)} already paused jobs in {pause_duration} seconds.")
            else:
                 logging.info("Rate Limit: No relevant jobs found to pause or schedule for resume.")
                 return # No need to schedule resume if nothing was affected


            # Schedule a one-off job to resume these tasks
            resume_time = datetime.now(self.scheduler.timezone) + timedelta(seconds=pause_duration)
            self.scheduler.add_job(
                self._resume_rate_limited_jobs,
                trigger='date',
                run_date=resume_time,
                args=[list(jobs_to_pause)], # Pass the list of jobs to resume
                id='rate_limit_resume_job',
                name='RateLimitResume',
                replace_existing=True
            )

        # Set pause reason for status (though queue might not be fully paused)
        self.pause_info = {
            "reason_string": f"Debrid Rate Limit - Resuming tasks around {resume_time.strftime('%H:%M:%S')}",
            "error_type": "RATE_LIMIT",
            "service_name": "Debrid Service", # Or be more specific if possible
            "status_code": None, # Typically rate limits are 429, but we might not get it directly here
            "retry_count": 0 # Not a retry scenario in the same way as connection
        }
        # Optionally pause the entire queue manager status reporting
        # from queues.queue_manager import QueueManager
        # QueueManager().pause_queue(reason=self.pause_reason)
        self.queue_paused = True # Indicate partial pause state

    def _resume_rate_limited_jobs(self, job_ids_to_resume):
        """Internal function called by scheduler to resume jobs after rate limit pause."""
        logging.info(f"Rate limit pause period complete. Resuming {len(job_ids_to_resume)} jobs.")
        with self.scheduler_lock:
             if self.scheduler.state != 1: return # Not running

             resumed_count = 0
             for job_id in job_ids_to_resume:
                  try:
                       job = self.scheduler.get_job(job_id)
                       # Only resume if the job exists and is actually paused
                       if job and job.next_run_time is None:
                           self.scheduler.resume_job(job_id)
                           resumed_count += 1
                           logging.debug(f"Rate Limit: Resumed job {job_id}")
                       elif job:
                            logging.debug(f"Rate Limit: Job {job_id} was already running, no resume needed.")
                       # If job doesn't exist, ignore
                  except Exception as e:
                       logging.error(f"Rate Limit: Error resuming job {job_id}: {e}")

        logging.info(f"Rate Limit: Resumed {resumed_count} jobs.")
        # Clear the rate limit pause reason and state
        if self.pause_info and self.pause_info.get("error_type") == "RATE_LIMIT":
            self.pause_info = {
                "reason_string": None, "error_type": None, "service_name": None,
                "status_code": None, "retry_count": 0
            }
            self.queue_paused = False


            # Updated sections log moved to after the scan loop


    def trigger_task(self, task_name):
        """Manually trigger a task to run immediately by adding it to APScheduler's queue."""
        normalized_name = self._normalize_task_name(task_name)
        job_id_base = normalized_name # This is the base task name, e.g., "task_artificial_long_run"

        logging.info(f"Attempting to manually trigger task: {job_id_base} by adding it to APScheduler queue.")

        target_func, args, kwargs = self._get_task_target(job_id_base, manual=True)

        if target_func:
            try:
                # Prevent duplicate concurrent runs — check if this task is already executing.
                with self._running_task_lock:
                    _already_running = any(
                        job_id_base in jid for jid in self.currently_executing_tasks
                    )
                if _already_running:
                    logging.warning(f"Task '{job_id_base}' is already running — skipping manual trigger.")
                    return {"success": False, "message": f"Task '{job_id_base}' is already running."}

                # Generate a unique ID for this manual job instance
                manual_job_instance_id = f"manual_{job_id_base}_{uuid.uuid4()}"
                
                # Pass the unique manual_job_instance_id as the first arg (actual_job_id_from_scheduler)
                # and job_id_base as the second arg (task_name_for_logging) to _run_and_measure_task.
                # Pass the bound method + its args separately (not via functools.partial) — see the
                # matching comment in _schedule_task for why partials break APScheduler's job store.
                manual_job_args = (manual_job_instance_id, job_id_base, target_func, args, kwargs)

                # Get timezone safely, with fallback if scheduler is None
                if self.scheduler and hasattr(self.scheduler, 'timezone'):
                    scheduler_timezone = self.scheduler.timezone
                else:
                    # Fallback timezone detection
                    try:
                        from metadata.metadata import _get_local_timezone
                        scheduler_timezone = _get_local_timezone()
                        logging.warning(f"Scheduler timezone not available, using fallback: {scheduler_timezone}")
                    except Exception as e:
                        logging.error(f"Failed to get fallback timezone: {e}, using UTC")
                        scheduler_timezone = timezone.utc
                
                run_now_date = datetime.now(scheduler_timezone)

                with self.scheduler_lock:
                    # Ensure scheduler exists before adding job
                    if not self.scheduler:
                        logging.error(f"Cannot trigger task '{job_id_base}': Scheduler is not initialized")
                        raise RuntimeError(f"Scheduler not initialized for manual task '{job_id_base}'")
                    
                    _QUEUE_TASKS_MANUAL = {
                        'Adding', 'Checking', 'Sleeping', 'Unreleased',
                        'Blacklisted', 'Pending Uncached', 'Upgrading',
                        'final_check_queue', 'Pre_release',
                        'task_check_plex_files', 'task_send_notifications',
                        'task_update_queue_views',
                        'task_regulate_system_load',
                    }
                    _manual_executor = 'queue' if job_id_base in _QUEUE_TASKS_MANUAL else 'default'
                    self.scheduler.add_job(
                        func=self._run_and_measure_task,
                        args=manual_job_args,
                        trigger='date',
                        run_date=run_now_date,
                        id=manual_job_instance_id,
                        name=f"Manual run of {job_id_base}",
                        replace_existing=False,
                        max_instances=1,
                        misfire_grace_time=600,
                        executor=_manual_executor,
                    )
                        
                    logging.info(f"Task '{job_id_base}' (Manual Job ID: {manual_job_instance_id}) successfully queued for immediate execution via APScheduler.")
                    return {"success": True, "message": f"Task '{job_id_base}' queued for execution.", "job_id": manual_job_instance_id}

            except Exception as e:
                logging.error(f"Error submitting manual task '{job_id_base}' to APScheduler: {e}", exc_info=True)
                raise RuntimeError(f"Failed to queue manual task '{job_id_base}': {e}")
        else:
            logging.error(f"Could not determine target function for manual trigger of '{job_id_base}'")
            if job_id_base not in self.task_intervals:
                raise ValueError(f"Task '{job_id_base}' is not defined. Cannot queue.")
            else:
                raise ValueError(f"Task function for '{job_id_base}' not found despite task being defined.")

    def enable_task(self, task_name):
        """Enable a task by adding/resuming its job in the scheduler."""
        current_thread_id_outer = threading.get_ident()
        normalized_name = self._normalize_task_name(task_name)
        job_id = normalized_name
        logging.info(f"ENABLE_TASK: Attempting to enable task '{normalized_name}' (Job ID: {job_id}) (Thread: {current_thread_id_outer}).")

        if normalized_name not in self.task_intervals:
             logging.error(f"ENABLE_TASK: Cannot enable task '{normalized_name}': No interval defined. (Thread: {current_thread_id_outer})")
             return False

        current_thread_id_before_lock = threading.get_ident()
        logging.info(f"ENABLE_TASK: Preparing to acquire scheduler_lock for '{normalized_name}'. (Thread: {current_thread_id_before_lock})")
        with self.scheduler_lock:
            current_thread_id_after_lock = threading.get_ident()
            logging.info(f"ENABLE_TASK: Acquired scheduler_lock for '{normalized_name}'. (Thread: {current_thread_id_after_lock})")
            job = self.scheduler.get_job(job_id)
            if job:
                 logging.debug(f"ENABLE_TASK: Job '{job_id}' exists. (Thread: {current_thread_id_after_lock})")
                 if job.next_run_time is not None: # Job exists and is scheduled (not paused indefinitely)
                     logging.info(f"ENABLE_TASK: Task '{normalized_name}' is already scheduled and enabled. (Thread: {current_thread_id_after_lock})")
                     # Ensure it's in our enabled_tasks set
                     if normalized_name not in self.enabled_tasks: self.enabled_tasks.add(normalized_name)
                     return True
                 else: # Job exists but is paused
                     logging.info(f"ENABLE_TASK: Job '{job_id}' exists but is paused. Resuming. (Thread: {current_thread_id_after_lock})")
                     try:
                         self.scheduler.resume_job(job_id)
                         self.enabled_tasks.add(normalized_name) # Add to set
                         # Remove from manual pause set if it was there
                         if job_id in self.paused_jobs_by_queue: self.paused_jobs_by_queue.remove(job_id)
                         logging.info(f"ENABLE_TASK: Resumed existing paused job for task: {normalized_name} (Thread: {current_thread_id_after_lock})")
                         return True
                     except Exception as e_resume:
                         logging.error(f"ENABLE_TASK: Error resuming job '{job_id}': {e_resume} (Thread: {current_thread_id_after_lock})", exc_info=True)
                         return False
            else: # Job doesn't exist, need to add it
                 logging.info(f"ENABLE_TASK: Job '{job_id}' does not exist. Scheduling new job. (Thread: {current_thread_id_after_lock})")
                 interval = self.task_intervals.get(normalized_name)
                 if interval:
                     logging.debug(f"ENABLE_TASK: Interval for new job '{normalized_name}' is {interval}s. Calling _schedule_task. (Thread: {current_thread_id_after_lock})")
                     if self._schedule_task(normalized_name, interval): # Use the schedule method
                         self.enabled_tasks.add(normalized_name) # Add to set
                         logging.info(f"ENABLE_TASK: Scheduled and enabled new task: {normalized_name} (Thread: {current_thread_id_after_lock})")
                         return True
                     else:
                         logging.error(f"ENABLE_TASK: Failed to schedule new job for task: {normalized_name} (Thread: {current_thread_id_after_lock})")
                         return False
                 else:
                     logging.error(f"ENABLE_TASK: Interval not found for task '{normalized_name}' during enable. (Thread: {current_thread_id_after_lock})")
                     return False
        current_thread_id_finally = threading.get_ident()
        logging.info(f"ENABLE_TASK: Finished attempt to enable task '{normalized_name}'. Lock released implicitly. (Thread: {current_thread_id_finally})")
        return False # Should have returned earlier in most cases

    def disable_task(self, task_name):
        """Disable a task by pausing its job in the scheduler."""
        normalized_name = self._normalize_task_name(task_name)
        job_id = normalized_name

        # Don't allow disabling essential tasks? Or handle via UI?
        # essential = {'task_heartbeat', 'task_check_service_connectivity', ...}
        # if job_id in essential:
        #    logging.warning(f"Cannot disable essential task: {job_id}")
        #    return False

        if normalized_name not in self.task_intervals:
             # Should not happen if task was previously enabled, but good practice
             logging.warning(f"Task '{normalized_name}' not found in intervals. Cannot disable.")
             # Still ensure it's removed from enabled_tasks set if present
             if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name)
             return True # Consider successful if not defined/already disabled

        with self.scheduler_lock:
            job = self.scheduler.get_job(job_id)
            if job:
                 if job.next_run_time is None: # Already paused
                      logging.info(f"Task '{normalized_name}' job is already paused.")
                      # Ensure it's removed from enabled_tasks set
                      if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name)
                      return True
                 else: # Job exists and is running, pause it
                      try:
                          self.scheduler.pause_job(job_id)
                          if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name) # Remove from set
                          logging.info(f"Paused job for task: {normalized_name}")
                          return True
                      except Exception as e:
                          logging.error(f"Error pausing job '{job_id}': {e}")
                          return False
            else: # Job doesn't exist
                 logging.info(f"Task '{normalized_name}' job not found (already removed or never scheduled). Considered disabled.")
                 # Ensure it's removed from enabled_tasks set
                 if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name)
                 return True

    def _normalize_task_name(self, task_name):
        """Normalize task name to match how it's stored internally."""
        # Ensure queue_processing_map exists before trying to access it
        if not hasattr(self, 'queue_processing_map'):
             # This might happen if called extremely early, though unlikely now
             logging.error("_normalize_task_name called before queue_processing_map was defined!")
             # Fallback: try other normalization rules without map check
        else:
            # Handle queue tasks (which might be passed without task_ prefix)
            # Use the map keys now
            for queue_name_key in self.queue_processing_map.keys():
                if task_name.lower() == queue_name_key.lower():
                    return queue_name_key # Return the canonical name used as the key/job_id

        # Handle task_ prefix for other tasks
        if task_name.startswith('task_'):
            # Check if it's a known task (including the combined one)
            if task_name in self.task_intervals:
                 return task_name
            # Maybe it's a content source task passed with underscores?
            elif '_wanted' in task_name:
                 # Try replacing underscores with spaces (except first one) for content sources
                 parts = task_name.split('_')
                 if len(parts) > 2 and parts[0] == 'task' and parts[-1] == 'wanted':
                     # Reconstruct potential name with spaces (handle multi-word sources)
                     # Example: task_My_Overseerr_Instance_wanted -> task_My Overseerr Instance_wanted
                     content_part = "_".join(parts[1:-1]) # Get 'My_Overseerr_Instance'
                     # This simple reconstruction might fail if source names have underscores themselves
                     # A better approach would be to iterate self.task_intervals keys if needed.
                     # For now, assume simple cases or that keys match `task_..._wanted` format.
                     # Let's just check if the original task_name is in intervals again, as that's the primary key format.
                     if task_name in self.task_intervals:
                           return task_name
                 # Fallback: return original if complex content source name check failed or not found
                 # Check if original name exists before warning
                 if task_name in self.task_intervals: return task_name

            # If it starts with task_ but isn't found, return as is but maybe log?
            # Let's assume if it starts with task_ it should be in intervals if valid.

        # Try adding task_ prefix if not present
        potential_task_name = f'task_{task_name}'
        if potential_task_name in self.task_intervals:
            return potential_task_name

        # Handle potential content source task passed without prefix/suffix
        potential_content_source_task = f'task_{task_name}_wanted'
        if potential_content_source_task in self.task_intervals:
             return potential_content_source_task
        # Handle content source task passed with spaces needing underscores
        potential_content_source_task_underscores = f'task_{task_name.replace(" ", "_")}_wanted'
        if potential_content_source_task_underscores in self.task_intervals:
             return potential_content_source_task_underscores


        # If no match found after all checks, return the original input
        # logging.warning(f"Could not normalize task name '{task_name}' to a known task key.")
        return task_name # Return original if no normalization rule matched


    # def task_update_statistics_summary(self):
    #     """Update the statistics summary table for faster statistics page loading"""
    #     try:
    #         # Use the directly imported function with force=True
    #         from database.statistics import update_statistics_summary
    #         update_statistics_summary(force=True)
    #         logging.debug("Scheduled statistics summary update complete")
    #     except Exception as e:
    #         logging.error(f"Error updating statistics summary: {str(e)}")


    # --- END: New Task Implementation ---


    # Add Listener Method
    def _job_listener(self, event: apscheduler.events.JobEvent):
        """Listener called for various job events to track executing tasks."""
        task_id_for_log = getattr(event, 'job_id', 'unknown_job')
        event_code = event.code 
        is_manual_task = task_id_for_log.startswith('manual_')
        log_prefix = f"APScheduler event for task '{task_id_for_log}':"

        if event_code == EVENT_JOB_SUBMITTED:
            if task_id_for_log != 'Wanted' and task_id_for_log != 'Scraping' and task_id_for_log != 'Adding':
                logging.info(f"{log_prefix} Job Submitted.")
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.add(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' added to manual_tasks set.")
            return # Submitted jobs don't proceed to interval adjustment logic

        elif event_code == EVENT_JOB_EXECUTED: # Successful completion
            if task_id_for_log != 'Wanted' and task_id_for_log != 'Scraping' and task_id_for_log != 'Adding':
                logging.info(f"{log_prefix} Job Executed Successfully.")
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set after successful execution.")
                    # DO NOT return here; successful executions proceed to interval adjustment logic below.

        elif event_code == EVENT_JOB_ERROR:
            exception_info = getattr(event, 'exception', 'N/A')
            logging.error(f"{log_prefix} Job Errored. Exception: {exception_info}", exc_info=True if event.exception else False)
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set due to error.")
            return # Errors skip interval adjustment logic

        elif event_code == EVENT_JOB_MISSED:
            logging.warning(f"{log_prefix} Job Missed.")
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set due to being missed.")
            return # Missed jobs skip interval adjustment logic

        elif event_code == EVENT_JOB_MAX_INSTANCES:
            logging.warning(f"{log_prefix} Job Max Instances Reached (skipped).")
            if is_manual_task: # If a manual task was somehow submitted then skipped for max_instances
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set due to max instances.")
            return # Max_instances skips interval adjustment logic
        
        else: # Other unknown event codes
            logging.debug(f"{log_prefix} Unhandled event code {event_code}.")
            return # Unhandled events skip interval adjustment logic

        # Original comment: Handle Interval Adjustment (Only for successful execution)
        # The logic here ensures that only EVENT_JOB_EXECUTED reaches this point.
        # ... (rest of interval adjustment logic remains the same) ...

    # Modify _run_and_measure_task for tracemalloc sampling AND heavy task locking
    def _run_and_measure_task(self, actual_job_id_from_scheduler, task_name_for_logging, func, args, kwargs): # Added task_name_for_log
        """Wraps a task function to measure execution duration, track memory usage with tracemalloc, and handle locking for heavy DB tasks."""
        start_time = time.monotonic()
        
        log_display_name = task_name_for_logging
        if actual_job_id_from_scheduler != task_name_for_logging: # True for manual tasks
            log_display_name = f"{task_name_for_logging} (Job ID: {actual_job_id_from_scheduler})"

        mem_before = 0
        mem_after = 0
        run_tracemalloc_sample = False # Flag to indicate if we run tracemalloc this time
        # lock_acquired = False # Flag to track if heavy task lock was acquired # REVERTED

        # --- Heavy Task Lock Handling ---
        # is_heavy_task = task_name_for_log in self.HEAVY_DB_TASKS # REVERTED - Scheduler handles global queue
        # if is_heavy_task: # REVERTED
            # logging.debug(f"Task '{task_name_for_logging}' requires heavy task lock. Attempting acquisition...") # REVERTED
            # lock_acquired = self.heavy_task_lock.acquire(blocking=False) # REVERTED
            # if not lock_acquired: # REVERTED
                 # logging.info(f"Skipping heavy task '{task_name_for_logging}' execution: Another heavy task is currently running.") # REVERTED
                 # return # Skip execution if lock not acquired # REVERTED
            # else: # REVERTED
                 # logging.info(f"Heavy task lock acquired for '{task_name_for_logging}'. Proceeding with execution.") # REVERTED
        # --- End Heavy Task Lock Handling ---

        # Manage currently_executing_tasks
        with self._running_task_lock:
            self.currently_executing_tasks.add(actual_job_id_from_scheduler)
            if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
                logging.info(f"Task '{log_display_name}' started execution, added to currently_executing_tasks.")

        # Record start time for UI stifling
        # 'start_time' is already time.monotonic() from the beginning of this function
        with self._executing_task_start_times_lock:
            self.executing_task_start_times[actual_job_id_from_scheduler] = start_time 
        if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
            logging.debug(f"Task '{log_display_name}' start time {start_time:.3f} recorded for UI stifling.")

        # Determine if we should sample this execution
        # Check if enabled AND available AND actually tracing
        if self._tracemalloc_enabled and tracemalloc_available and tracemalloc and tracemalloc.is_tracing():
            self.task_execution_count += 1
            # Check if the current count is a multiple of the sample rate
            if self.task_execution_count % self.tracemalloc_sample_rate == 0:
                run_tracemalloc_sample = True
                # Log when a sample is being taken for visibility
                logging.info(f"[Tracemalloc] Sampling task '{log_display_name}' (Execution #{self.task_execution_count})")

        # Get memory usage before if sampling this execution
        # Check available again just before use
        if run_tracemalloc_sample and tracemalloc_available and tracemalloc:
            try:
                mem_before, _ = tracemalloc.get_traced_memory()
            except Exception as e_mem:
                logging.error(f"[Tracemalloc] Error getting memory before task '{log_display_name}': {e_mem}")
                run_tracemalloc_sample = False # Don't try 'after' if 'before' failed
        elif run_tracemalloc_sample:
             # Should not happen if checks above are correct, but log defensively
             logging.warning(f"[Tracemalloc] Attempted sample for '{log_display_name}', but tracemalloc not available/tracing at point of memory check.")
             run_tracemalloc_sample = False

        # Capture CPU time before task execution
        cpu_start = self._get_current_thread_cpu_seconds()

        try:
            # Execute the original task function
            func(*args, **kwargs)
            duration = time.monotonic() - start_time # Measure duration regardless
            # Record runtime on successful completion
            self._record_task_runtime(task_name_for_logging, duration)

            # Persist next-run time for tasks with interval >= 20 min so the schedule
            # survives restarts. Only for regular scheduled tasks (not manual runs).
            _task_interval = self.task_intervals.get(task_name_for_logging, 0)
            if _task_interval >= 1200 and actual_job_id_from_scheduler == task_name_for_logging:
                self._save_task_schedule(task_name_for_logging, time.time() + _task_interval)

            # Get memory usage after and log delta if sampling this execution
            # Check available again just before use
            if run_tracemalloc_sample and tracemalloc_available and tracemalloc:
                try:
                    mem_after, _ = tracemalloc.get_traced_memory()
                    mem_delta = mem_after - mem_before
                    mem_delta_mb = mem_delta / (1024 * 1024)
                    mem_before_mb = mem_before / (1024 * 1024)
                    mem_after_mb = mem_after / (1024 * 1024)

                    log_level = logging.INFO if abs(mem_delta_mb) < 1 else logging.WARNING # Log higher if delta > 1MB
                    # Added [Tracemalloc Sample] prefix for clarity
                    logging.log(log_level, f"Task '{log_display_name}' completed in {duration:.3f}s. [Tracemalloc Sample] Mem Before: {mem_before_mb:.2f}MB, Mem After: {mem_after_mb:.2f}MB, Delta: {mem_delta_mb:+.2f}MB")

                    # If memory increased significantly during the sample, log top allocations
                    if mem_delta > 1024 * 1024: # Log top allocations if increase > 1MB (adjust threshold if needed)
                        snapshot = tracemalloc.take_snapshot()
                        # Log top allocations from the end snapshot. Comparing snapshots adds complexity.
                        top_stats = snapshot.statistics('lineno')
                        logging.warning(f"[Tracemalloc] Task '{log_display_name}' sample showed positive memory delta > 1MB. Top 5 allocations at end:")
                        for i, stat in enumerate(top_stats[:5], 1):
                            # Limit traceback line length for cleaner logs
                            trace_line = stat.traceback.format()[-1]
                            trace_line = trace_line[:200] + '...' if len(trace_line) > 200 else trace_line
                            logging.warning(f"  {i}: {trace_line} - Size: {stat.size / 1024:.1f} KiB, Count: {stat.count}")

                except Exception as e_mem:
                    logging.error(f"[Tracemalloc] Error getting memory after task '{log_display_name}': {e_mem}")
            elif run_tracemalloc_sample:
                # Log if we intended to sample but tracemalloc became unavailable
                 logging.warning(f"[Tracemalloc] Attempted sample for '{log_display_name}', but tracemalloc not available at point of 'after' memory check.")

            # Capture CPU time after task execution
            cpu_end = self._get_current_thread_cpu_seconds()
            cpu_seconds = cpu_end - cpu_start
            cpu_util = (cpu_seconds / duration) * 100 if duration > 0 else 0
            self._record_task_cpu(task_name_for_logging, cpu_seconds)
            if self._log_per_run_cpu:
                logging.debug(f"[CPU] Task '{log_display_name}' cpu={cpu_seconds:.3f}s, wall={duration:.3f}s, util={cpu_util:.1f}%")

            # Optional: Log normal duration if not sampling (can be noisy)
            # else:
            #    logging.debug(f"Task '{log_display_name}' completed successfully in {duration:.3f}s (No tracemalloc sample this time)")

            return duration # Return duration for the listener

        except Exception as e:
            duration = time.monotonic() - start_time
            logging.error(f"Error during execution of job '{log_display_name}': {e}", exc_info=True)
            # Record runtime even on error
            self._record_task_runtime(task_name_for_logging, duration)

            # Log memory even on error if sampling this execution
            # Check available again just before use
            if run_tracemalloc_sample and tracemalloc_available and tracemalloc:
                 try:
                     mem_after, _ = tracemalloc.get_traced_memory()
                     # Note: mem_before might be 0 if the 'before' call failed
                     mem_delta = mem_after - mem_before
                     mem_delta_mb = mem_delta / (1024 * 1024)
                     mem_before_mb = mem_before / (1024 * 1024)
                     mem_after_mb = mem_after / (1024 * 1024)
                     logging.error(f"[Tracemalloc] Memory state after error in '{log_display_name}'. Mem Before: {mem_before_mb:.2f}MB, Mem After: {mem_after_mb:.2f}MB, Delta: {mem_delta_mb:+.2f}MB")
                 except Exception as e_mem_err:
                     logging.error(f"[Tracemalloc] Error getting memory after task error in '{log_display_name}': {e_mem_err}")
            elif run_tracemalloc_sample:
                 # Log if we intended to sample but tracemalloc became unavailable
                 logging.warning(f"[Tracemalloc] Attempted sample for '{log_display_name}', but tracemalloc not available at point of error memory check.")

            # Capture CPU time after task execution on error
            cpu_end = self._get_current_thread_cpu_seconds()
            cpu_seconds = cpu_end - cpu_start
            cpu_util = (cpu_seconds / duration) * 100 if duration > 0 else 0
            self._record_task_cpu(task_name_for_logging, cpu_seconds)
            if self._log_per_run_cpu:
                logging.debug(f"[CPU] Task '{log_display_name}' cpu={cpu_seconds:.3f}s, wall={duration:.3f}s, util={cpu_util:.1f}%")

            raise # Re-raise the exception
        finally:
            # Manage currently_executing_tasks
            with self._running_task_lock:
                self.currently_executing_tasks.discard(actual_job_id_from_scheduler)
                if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
                    logging.info(f"Task '{log_display_name}' finished execution, removed from currently_executing_tasks.")
                    
            # Add inter-task sleep for low power mode
            try:
                inter_task_sleep_seconds = self.current_inter_task_sleep
                if inter_task_sleep_seconds > 0:
                    logging.debug(f"Sleeping for {inter_task_sleep_seconds:.2f}s after task '{log_display_name}' due to current inter-task sleep setting.")
                    time.sleep(inter_task_sleep_seconds)
            except (ValueError, TypeError):
                # If setting is invalid, don't sleep
                pass

            # Clear start time for UI stifling
            with self._executing_task_start_times_lock:
                removed_start_time = self.executing_task_start_times.pop(actual_job_id_from_scheduler, None)
            if removed_start_time is None:
                logging.warning(f"Task '{log_display_name}' was not found in executing_task_start_times upon completion/error for UI stifling.")
            elif log_display_name not in ['Wanted', 'Scraping', 'Adding']:
                logging.debug(f"Task '{log_display_name}' start time removed for UI stifling.")
            # --- Release heavy task lock if acquired ---
            # if lock_acquired: # REVERTED
                # try: # REVERTED
                    # self.heavy_task_lock.release() # REVERTED
                    # logging.info(f"Heavy task lock released for '{task_name_for_logging}'.") # REVERTED
                # except Exception as e_release: # REVERTED
                    # Should not happen if lock_acquired is True, but log defensively # REVERTED
                    # logging.error(f"Error releasing heavy task lock for '{task_name_for_logging}': {e_release}") # REVERTED
            # --- End Release heavy task lock --- # REVERTED

    def task_regulate_system_load(self):
        """Monitors CPU and RAM usage and dynamically adjusts inter-task sleep time to regulate system load."""
        if not psutil:
            logging.warning("Cannot regulate system load: psutil library is not installed.")
            self.disable_task('task_regulate_system_load')
            return

        base_sleep = float(get_setting('Queue', 'main_loop_sleep_seconds', 0.0))

        # Get regulation parameters
        cpu_threshold = int(get_setting('System Load Regulation', 'cpu_threshold_percent', 75))
        ram_threshold = int(get_setting('System Load Regulation', 'ram_threshold_percent', 75))
        increase_step = float(get_setting('System Load Regulation', 'regulation_increase_step_seconds', 1.0))
        decrease_step = float(get_setting('System Load Regulation', 'regulation_decrease_step_seconds', 1.0))
        max_sleep = float(get_setting('System Load Regulation', 'regulation_max_sleep_seconds', 60.0))

        # Get system usage - non-blocking
        try:
            cpu_usage = psutil.cpu_percent(interval=None)
            ram_usage = psutil.virtual_memory().percent
        except Exception as e:
            logging.error(f"Error getting system usage: {e}")
            return

        high_load = False
        if cpu_usage > cpu_threshold:
            logging.warning(f"CPU usage ({cpu_usage:.1f}%) exceeds threshold ({cpu_threshold}%). Increasing inter-task sleep.")
            high_load = True
        
        if ram_usage > ram_threshold:
            logging.warning(f"RAM usage ({ram_usage:.1f}%) exceeds threshold ({ram_threshold}%). Increasing inter-task sleep.")
            high_load = True

        if high_load:
            new_sleep = self.current_inter_task_sleep + increase_step
            self.current_inter_task_sleep = min(new_sleep, max_sleep)
            logging.info(f"System load high. Inter-task sleep increased to {self.current_inter_task_sleep:.2f}s")
        else:
            if self.current_inter_task_sleep > base_sleep:
                new_sleep = self.current_inter_task_sleep - decrease_step
                self.current_inter_task_sleep = max(new_sleep, base_sleep)
                logging.debug(f"System load normal. Inter-task sleep adjusted to {self.current_inter_task_sleep:.2f}s")

    # Add method for live interval updates
    def update_task_interval(self, task_name: str, interval_seconds: int | None):
        """
        Updates the interval (in seconds) for a specific task live.
        If interval_seconds is None, resets to the default interval.
        """
        normalized_name = self._normalize_task_name(task_name)

        if normalized_name not in self.original_task_intervals:
            logging.error(f"Cannot update interval for '{normalized_name}': Task not defined.")
            return False

        # Define minimum seconds
        MIN_INTERVAL_SECONDS = 1 # Must match validation

        target_interval_seconds = 0
        is_resetting = interval_seconds is None

        if is_resetting:
            target_interval_seconds = self.original_task_intervals.get(normalized_name)
            logging.info(f"Resetting task '{normalized_name}' interval to default: {target_interval_seconds}s")
            if normalized_name in self.task_intervals:
                 self.task_intervals[normalized_name] = target_interval_seconds
        else:
            # Validate the provided seconds
            try:
                interval_sec_int = int(interval_seconds)
                 # Validate seconds
                if interval_sec_int >= MIN_INTERVAL_SECONDS:
                    target_interval_seconds = interval_sec_int
                else:
                    logging.error(f"Invalid interval for '{normalized_name}': {interval_seconds} (must be >= {MIN_INTERVAL_SECONDS} seconds). Cannot apply live update.")
                    return False
            except (ValueError, TypeError):
                logging.error(f"Invalid interval format for '{normalized_name}': {interval_seconds}. Cannot apply live update.")
                return False

            logging.info(f"Updating task '{normalized_name}' interval live to: {target_interval_seconds}s")
            self.task_intervals[normalized_name] = target_interval_seconds # Update internal map

        # Apply the change to the scheduler
        with self.scheduler_lock:
            job = self.scheduler.get_job(normalized_name)
            if not job:
                logging.info(f"Task '{normalized_name}' is not currently scheduled. Interval preference updated internally.")
                return True

            try:
                self.scheduler.reschedule_job(
                    normalized_name,
                    trigger=IntervalTrigger(seconds=target_interval_seconds) # Use target seconds
                )
                logging.info(f"Successfully rescheduled task '{normalized_name}' with new interval {target_interval_seconds}s.")
                return True
            except Exception as e:
                logging.error(f"Error rescheduling job '{normalized_name}' with new interval: {e}", exc_info=True)
                return False


    def task_trim_memory(self):
        """Run gc.collect() + malloc_trim(0) to return glibc arena pages to the OS.

        Heavy operations (upgrade hub scan, debrid manager audit, rclone file count, etc.)
        allocate large temporary objects that Python frees via GC but glibc holds in its
        malloc arenas indefinitely. This task forces both layers to release those pages,
        keeping the baseline RSS stable over long uptimes.
        """
        import gc

        def _read_current_rss_kb():
            """Read current RSS from /proc/self/status (VmRSS), not the peak high-water mark."""
            try:
                with open('/proc/self/status') as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            return int(line.split()[1])
            except Exception:
                pass
            return None

        import time as _time
        before_rss = _read_current_rss_kb()

        # Evict RD library stable cache if idle for 2+ hours
        lib_torrents_freed = 0
        try:
            from routes.debrid_manager_routes import _lib, _lib_last_accessed
            idle_secs = _time.time() - _lib_last_accessed
            if _lib['stable'] is not None and idle_secs > 7200:
                with _lib['lock']:
                    if _lib['stable'] is not None:
                        lib_torrents_freed = len(_lib['stable'].get('torrents', []))
                        _lib['stable'] = None
                logging.info(f"[TRIM_MEMORY] Evicted idle RD library cache ({lib_torrents_freed} torrents, idle {idle_secs/3600:.1f}h)")
        except Exception as e:
            logging.debug(f"[TRIM_MEMORY] Could not evict RD library cache: {e}")

        collected = gc.collect()

        trim_ok = False
        try:
            import ctypes
            ctypes.CDLL('libc.so.6').malloc_trim(0)
            trim_ok = True
        except Exception as e:
            logging.debug(f"[TRIM_MEMORY] malloc_trim unavailable: {e}")

        after_rss = _read_current_rss_kb()

        if before_rss and after_rss:
            freed_kb = before_rss - after_rss
            logging.info(f"[TRIM_MEMORY] gc collected {collected} objects; malloc_trim={'ok' if trim_ok else 'unavailable'}; RSS delta: {freed_kb/1024:.1f} MB (before={before_rss//1024} MB, after={after_rss//1024} MB)")
        else:
            logging.info(f"[TRIM_MEMORY] gc collected {collected} objects; malloc_trim={'ok' if trim_ok else 'unavailable'}")

    def _fail_safe_resume_if_stuck(self):
        """Force-resume the queue if it has been paused due to connectivity issues for too long.

        The normal resume flow relies on periodic connectivity checks.  If, for any
        reason, those checks fail to un-pause the queue even after connectivity is
        restored (for example because the check itself is failing), this method
        will act as a watchdog.  Once the pause has lasted longer than
        `Queue -> connectivity_fail_safe_minutes` (defaults to 3 minutes) it
        will clear the pause state and invoke `resume_queue()` unconditionally.
        """
        try:
            # Only act when the queue is actually paused
            if not self.queue_paused:
                return
                
            # Check all pause types, not just CONNECTION_ERROR
            current_pause_type = self.pause_info.get("error_type") if self.pause_info else None
            
            # For scheduled pauses, don't use fail-safe
            if current_pause_type == "SYSTEM_SCHEDULED":
                return

            # How long has it been since the connectivity failure was first detected?
            if not self.connectivity_failure_time:
                # If we're paused but don't have a failure time, something's wrong
                if self.queue_paused and current_pause_type in ["CONNECTION_ERROR", "UNAUTHORIZED", "FORBIDDEN", "DB_HEALTH"]:
                    logging.warning(f"Queue is paused ({current_pause_type}) but no failure time tracked. Setting failure time now.")
                    self.connectivity_failure_time = time.time()
                return

            elapsed = time.time() - self.connectivity_failure_time
            from utilities.settings import get_setting  # Local import to avoid cycles
            try:
                threshold_minutes = float(get_setting('Queue', 'connectivity_fail_safe_minutes', 3))
            except (ValueError, TypeError):
                threshold_minutes = 3.0
            threshold_seconds = threshold_minutes * 60

            # Log periodically that we're still stuck
            if not hasattr(self, '_last_failsafe_log_time'):
                self._last_failsafe_log_time = 0
                
            if elapsed > 60 and time.time() - self._last_failsafe_log_time > 60:
                logging.warning(
                    f"[Fail-safe] Queue has been paused for {elapsed/60:.1f} minutes "
                    f"(threshold: {threshold_minutes} minutes). Type: {current_pause_type}"
                )
                self._last_failsafe_log_time = time.time()

            if elapsed < threshold_seconds:
                return  # Still within grace period – keep waiting

            logging.warning(
                f"[Fail-safe] WATCHDOG TRIGGERED: Queue has been paused for "
                f"{elapsed/60:.1f} minutes (>{threshold_minutes} minute threshold). "
                f"Pause type: {current_pause_type}. Forcibly resuming!"
            )

            # Clear connectivity tracking and pause info before resuming
            self.connectivity_failure_time = None
            self.connectivity_retry_count = 0
            self.pause_info = {
                "reason_string": None,
                "error_type": None,
                "service_name": None,
                "status_code": None,
                "retry_count": 0,
            }
            self._last_failsafe_log_time = 0
            
            # Attempt to resume irrespective of the current connectivity check result
            self.resume_queue()
            
            logging.info("[Fail-safe] Queue forcibly resumed by watchdog. Services may still be unavailable.")
        except Exception as e:
            logging.error(f"Error in fail-safe resume logic: {e}", exc_info=True)

    def _record_task_runtime(self, task_name: str, duration_seconds: float):
        """Accumulate runtime and periodically log per-task percentage."""
        now = time.monotonic()
        with self.task_runtime_lock:
            self.task_runtime_totals[task_name] += duration_seconds
            if now - self._last_runtime_log_time >= self._runtime_log_interval_sec:
                self._emit_task_runtime_report_locked(now)

    def _emit_task_runtime_report_locked(self, now: float):
        """Assumes task_runtime_lock held. Emits report and resets counters."""
        if not self.task_runtime_totals:
            self._last_runtime_log_time = now
            return
        total = sum(self.task_runtime_totals.values())
        if total <= 0:
            self.task_runtime_totals.clear()
            self._last_runtime_log_time = now
            return
        parts = [f"{t}={v / total * 100:.1f} %" for t, v in sorted(self.task_runtime_totals.items(), key=lambda x: x[1], reverse=True)]
        logging.info(f"[RUNTIME] Last {self._runtime_log_interval_sec} s: "+", ".join(parts)+f"  (total={total:.1f} s)")
        self.task_runtime_totals.clear()
        self._last_runtime_log_time = now

    # Add method to get current thread CPU seconds
    def _get_current_thread_cpu_seconds(self):
        """Get the current thread's CPU time in seconds."""
        try:
            if resource is not None and hasattr(resource, 'getrusage'):
                if hasattr(resource, 'RUSAGE_THREAD'):
                    return resource.getrusage(resource.RUSAGE_THREAD).ru_utime
                else:
                    return resource.getrusage(resource.RUSAGE_SELF).ru_utime
            else:
                return time.process_time()
        except Exception as e:
            logging.error(f"Error getting current thread CPU time: {e}", exc_info=True)
            return 0

    # Add method to record task CPU seconds
    def _record_task_cpu(self, task_name: str, cpu_seconds: float):
        """Accumulate task CPU seconds and periodically log per-task CPU share."""
        now = time.monotonic()
        with self.task_cpu_lock:
            self.task_cpu_totals[task_name] += cpu_seconds
            if now - self._last_cpu_log_time >= self._cpu_log_interval_sec:
                self._emit_task_cpu_report_locked(now)

    def _emit_task_cpu_report_locked(self, now: float):
        """Assumes task_cpu_lock held. Emits report and resets counters."""
        if not self.task_cpu_totals:
            self._last_cpu_log_time = now
            return
        total = sum(self.task_cpu_totals.values())
        if total <= 0:
            self.task_cpu_totals.clear()
            self._last_cpu_log_time = now
            return
        parts = [f"{t}={v / total * 100:.1f} % ({v:.2f}s)" for t, v in sorted(self.task_cpu_totals.items(), key=lambda x: x[1], reverse=True)]
        logging.info(f"[CPU] Last {self._cpu_log_interval_sec} s: "+", ".join(parts)+f"  (total_cpu={total:.2f} s)")
        self.task_cpu_totals.clear()
        self._last_cpu_log_time = now

def process_overseerr_webhook(data):
    notification_type = data.get('notification_type')

    if notification_type == 'TEST_NOTIFICATION':
        logging.info("Received test notification from Overseerr")
        return

    media = data.get('media')
    if not media:
        logging.warning("Received webhook without media information")
        return

    media_type = media.get('media_type')
    tmdb_id = media.get('tmdbId')
    # Extract additional info if available
    imdb_id = media.get('imdbId')
    tvdb_id = media.get('tvdbId')
    title = data.get('subject') # Title might be in subject

    # Extract requester information from webhook payload
    # Webhook payload has flat structure: requestedBy_username, requestedBy_email, etc.
    request_data = data.get('request', {})
    requester_display_name = request_data.get('requestedBy_username', 'Unknown')
    requester_email = request_data.get('requestedBy_email')
    request_id = request_data.get('request_id')  # Overseerr's request ID for tracking


    if not media_type or not tmdb_id:
        logging.error(f"Invalid Overseerr/Agregarr webhook data: missing media_type or tmdbId. Data: {data}")
        return

    logging.info(f"Processing Overseerr/Agregarr webhook: Type={media_type}, TMDB={tmdb_id}, Title='{title}', Requester='{requester_display_name}' (email: {requester_email}), Request ID={request_id}")

    wanted_item = {
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        # Include other IDs if available
        'imdb_id': imdb_id,
        'tvdb_id': tvdb_id,
    }

    # Handle TV Show specific data
    if media_type == 'tv':
        # Requested seasons (array of numbers)
        requested_seasons = media.get('requested_seasons')
        if requested_seasons:
            wanted_item['requested_seasons'] = requested_seasons
            logging.info(f"Added requested seasons to wanted item: {requested_seasons}")

        # Specific episode request? (Usually full season requests)
        # episode_number = media.get('episodeNumber')
        # season_number = media.get('seasonNumber')
        # if season_number is not None and episode_number is not None:
        #     wanted_item['season_number'] = season_number
        #     wanted_item['episode_number'] = episode_number
        #     logging.info(f"Webhook specified specific episode: S{season_number}E{episode_number}")


    wanted_content = [wanted_item]
    logging.debug(f"Processing wanted content from webhook: {wanted_item}")
    from metadata.metadata import process_metadata
    wanted_content_processed = process_metadata(wanted_content)

    if wanted_content_processed:
        # Get the versions for the relevant Overseerr or Agregarr source from settings
        content_sources = ProgramRunner().get_content_sources(force_refresh=False)

        # Determine if this is from Agregarr based on requester name
        is_agregarr = requester_display_name == "Agregarr"

        from content_checkers.overseerr import _source_allows_requester
        from database import add_wanted_items
        from content_checkers.content_source_detail import append_content_source_detail

        if is_agregarr:
            # Agregarr: single source, no requester filtering
            source_key = next((s for s, d in content_sources.items() if s.startswith('Agregarr')), None)
            matched_sources = [(source_key, content_sources[source_key])] if source_key else []
            source_type = 'Agregarr'
        else:
            # Overseerr: match ALL enabled sources whose allowed_requesters includes this requester
            source_type = 'Overseerr'
            matched_sources = []
            for s, d in content_sources.items():
                if not s.startswith('Overseerr'):
                    continue
                allowed = d.get('allowed_requesters', ['__all__'])
                if not allowed:
                    allowed = ['__all__']
                if _source_allows_requester(allowed, requester_display_name):
                    matched_sources.append((s, d))

        if not matched_sources:
            logging.warning(f"No enabled {source_type} content source matched requester '{requester_display_name}' for webhook (TMDB ID: {tmdb_id}). Item not added.")
            return

        all_items_base = wanted_content_processed.get('movies', []) + wanted_content_processed.get('episodes', []) + wanted_content_processed.get('anime', [])

        if not all_items_base:
            logging.warning(f"Metadata processing for {source_type} webhook (TMDB ID: {tmdb_id}) resulted in no items to add.")
            return

        for source_key, source_data in matched_sources:
            versions = source_data.get('versions', {})
            import copy
            items_for_source = copy.deepcopy(all_items_base)
            for item in items_for_source:
                item['content_source'] = source_key
                item['content_source_detail'] = requester_display_name
                item['overseerr_request_id'] = request_id
                item = append_content_source_detail(item, source_type=source_type)
            add_wanted_items(items_for_source, versions, force_granular_versions=True)
            logging.info(f"Processed {len(items_for_source)} wanted item(s) from {source_type} webhook via source '{source_key}' (TMDB ID: {tmdb_id}). Requester: {requester_display_name}, Versions: {versions}")

def generate_airtime_report():
    from metadata.metadata import _get_local_timezone # Added import here
    logging.info("Generating airtime report for wanted and unreleased items...")

    from database.core import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()

    # Fetch all wanted and unreleased items
    cursor.execute("""
        SELECT id, title, type, release_date, airtime, state
        FROM media_items
        WHERE state IN ('Wanted', 'Unreleased')
        AND (ghostlisted IS NULL OR ghostlisted = 0)
        ORDER BY release_date, airtime
    """)
    items = cursor.fetchall()
    conn.close() # Close connection after fetching

    current_datetime_local = datetime.now(_get_local_timezone()) # Use local timezone
    report = []

    # Add try-except for float conversions
    try:
        movie_airtime_offset_min = float(get_setting("Queue", "movie_airtime_offset", "19")) * 60
    except (ValueError, TypeError):
        movie_airtime_offset_min = 19.0 * 60

    try:
        episode_airtime_offset_min = float(get_setting("Queue", "episode_airtime_offset", "0")) * 60
    except (ValueError, TypeError):
        episode_airtime_offset_min = 0.0 * 60

    logging.info(f"Movie airtime offset: {movie_airtime_offset_min / 60} hours")
    logging.info(f"Episode airtime offset: {episode_airtime_offset_min / 60} hours")


    for item_dict in items: # Use dicts
        item_id = item_dict['id']
        title = item_dict['title']
        item_type = item_dict['type']
        release_date_str = item_dict['release_date']
        airtime_str = item_dict['airtime']
        state = item_dict['state']

        if not release_date_str or release_date_str.lower() == "unknown":
            report.append(f"{title} ({item_type}, ID: {item_id}): Unknown release date (State: {state})")
            continue

        try:
            release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
        except ValueError:
            report.append(f"{title} ({item_type}, ID: {item_id}): Invalid release date format '{release_date_str}' (State: {state})")
            continue

        # Determine airtime and offset based on type
        airtime_offset_minutes = 0
        airtime = dt_time(0, 0) # Default to midnight
        try:
            if item_type == 'movie':
                airtime_offset_minutes = movie_airtime_offset_min
                # Movies often don't have specific airtime, use default (midnight)
            elif item_type == 'episode':
                airtime_offset_minutes = episode_airtime_offset_min
                if airtime_str:
                    airtime = datetime.strptime(airtime_str, '%H:%M').time()
                else:
                     # Use default if airtime is missing for episode
                     airtime = dt_time(0, 0)
            # else: handle other types if necessary
        except ValueError:
             report.append(f"{title} ({item_type}, ID: {item_id}): Invalid airtime format '{airtime_str}' (State: {state})")
             continue


        # Combine date and time using local timezone awareness
        tz = _get_local_timezone()
        try:
            # Assume release_date and airtime are naive, localize them to the system's configured timezone
            naive_release_datetime = datetime.combine(release_date, airtime)
            # Use replace() for zoneinfo, which handles DST transitions (raises errors for invalid times, assumes standard time for ambiguous times by default)
            local_release_datetime = naive_release_datetime.replace(tzinfo=tz) 

            # Calculate scrape datetime by adding offset
            scrape_datetime_local = local_release_datetime + timedelta(minutes=airtime_offset_minutes)

            time_until_scrape = scrape_datetime_local - current_datetime_local

            # Format datetimes for readability
            scrape_dt_str = scrape_datetime_local.strftime('%Y-%m-%d %H:%M:%S %Z%z')

            if time_until_scrape > timedelta(0):
                # Format timedelta nicely (e.g., remove microseconds)
                days, remainder = divmod(time_until_scrape.total_seconds(), 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes, seconds = divmod(remainder, 60)
                time_until_str = f"{int(days)}d {int(hours)}h {int(minutes)}m" if days > 0 else f"{int(hours)}h {int(minutes)}m {int(seconds)}s"

                report.append(f"{title} ({item_type}, ID: {item_id}): State={state}. Scrape at ~{scrape_dt_str} (In: {time_until_str})")
            else:
                report.append(f"{title} ({item_type}, ID: {item_id}): State={state}. Ready to scrape (Scrape time was ~{scrape_dt_str})")

        except Exception as dt_err:
            logging.error(f"Error calculating airtime for item {item_id}: {dt_err}", exc_info=True)
            report.append(f"{title} ({item_type}, ID: {item_id}): Error calculating airtime (State: {state})")


    # Log the report
    logging.info("--- Airtime Report Start ---")
    if report:
        for line in report:
            logging.info(line)
    else:
        logging.info("No Wanted/Unreleased items found for report.")
    logging.info("--- Airtime Report End ---")

def append_runtime_airtime(items):
    from metadata.metadata import get_runtime, get_episode_airtime # Added import here
    logging.info(f"Starting to append runtime and airtime for {len(items)} items")
    processed_count = 0
    for index, item in enumerate(items, start=1):
        # Use dict access with .get() for safety
        imdb_id = item.get('imdb_id')
        media_type = item.get('type') # Changed 'type' to 'media_type' based on other usage? Check consistency. Let's assume 'type'.

        if not imdb_id or not media_type:
            logging.warning(f"Item {index} is missing imdb_id ('{imdb_id}') or type ('{media_type}'). Skipping runtime/airtime.")
            continue

        try:
            runtime = None
            airtime = None
            if media_type == 'movie':
                runtime = get_runtime(imdb_id, 'movie')
            elif media_type == 'episode':
                runtime = get_runtime(imdb_id, 'episode') # Assuming get_runtime handles episode lookup
                airtime = get_episode_airtime(imdb_id) # Assuming uses imdb_id of episode
            else:
                logging.warning(f"Unknown media type '{media_type}' for item {index} (IMDb: {imdb_id}). Cannot get runtime/airtime.")
                continue # Skip unknown types

            # Append to item if values were found
            if runtime is not None:
                item['runtime'] = runtime
            if airtime is not None:
                item['airtime'] = airtime
            processed_count += 1

        except Exception as e:
            logging.error(f"Error processing runtime/airtime for item {index} (IMDb: {imdb_id}, Type: {media_type}): {str(e)}")
            # Avoid logging full traceback for potentially common API errors? Optional.
            # logging.error(traceback.format_exc())

    logging.info(f"Finished appending runtime/airtime. Processed {processed_count}/{len(items)} items.")


def get_and_add_all_collected_from_plex(bypass=False, backfill=False):
    """
    Get all collected content from Plex/Symlink/Zurg modes.

    BACKFILL OPTIMIZATION (backfill=True):
    - Symlink mode: Uses filesystem-first approach (always)
    - Plex mode: Uses filesystem-first if 'mounted_file_location' is configured
      - Remaps Plex paths (/debrid/*) to CLI mount paths (/media/mount/*)
      - Extracts size_gb and resolution from filesystem (MediaInfo hybrid approach)
      - Skips non-debrid paths (e.g., NAS mounts)
      - Falls back to Plex API if success rate < 50% or mount not available
      - Performance: ~270x faster (90 seconds vs 7 hours for 85K items)

    REGULAR SCAN (backfill=False):
    - Always uses Plex API (preserves existing behavior)
    """
    collected_content = None  # Initialize here
    data_source = 'plex'  # Track data source for accurate logging
    mode = get_setting('File Management', 'file_collection_management')

    # OPTIMIZATION: For Symlink/Plex mode backfill, use filesystem FIRST (instant, no API calls)
    # This prevents 85K+ Plex API calls and rate limiting/timeouts
    use_filesystem_first = False
    mount_path = None

    if backfill and not bypass:
        if mode in ['Symlink', 'Symlinked/Local']:
            # Symlink mode: use location_on_disk as-is
            mount_path = get_setting('File Management', 'symlinked_directory', '')
            if mount_path and os.path.isdir(mount_path):
                use_filesystem_first = True
                logging.info(f"[BACKFILL_FS] Using filesystem-first approach for Symlink mode")
            else:
                logging.info(f"[BACKFILL_SYMLINK] No valid mount configured, will use Plex API")

        elif mode == 'Plex':
            # Plex mode: remap Plex paths to mount paths
            mount_path = get_setting('Plex', 'mounted_file_location', '')
            if mount_path and os.path.isdir(mount_path):
                # Strip __all__ from mount path since Plex uses individual folders
                if mount_path.endswith('/__all__'):
                    mount_path = mount_path[:-8]  # Remove "/__all__"
                    logging.info(f"[BACKFILL_FS] Stripped __all__ from mount path, using: {mount_path}")

                use_filesystem_first = True
                logging.info(f"[BACKFILL_FS] Using filesystem-first approach for Plex mode with mount: {mount_path}")
            else:
                logging.info(f"[BACKFILL_PLEX] No valid mount configured (path: '{mount_path}'), will use Plex API")

    if use_filesystem_first:
        try:
            from database.database_reading import get_all_media_items
            from utilities.local_library_scan import local_library_scan, remap_plex_paths_to_mount

            # Get all Collected items from database
            db_items = get_all_media_items(state='Collected')
            logging.info(f"[BACKFILL_FS] Got {len(db_items)} items from database for filesystem scan")

            if db_items:
                # For Plex mode, remap paths to mount structure
                if mode == 'Plex' and mount_path:
                    logging.info(f"[BACKFILL_FS] Remapping {len(db_items)} Plex paths to mount structure...")
                    db_items = remap_plex_paths_to_mount(db_items, mount_path)

                    # Separate successfully remapped and failed items
                    remapped_items = [item for item in db_items if not item.get('_remap_failed')]
                    failed_items = [item for item in db_items if item.get('_remap_failed')]

                    if failed_items:
                        logging.warning(f"[BACKFILL_FS] {len(failed_items)} items failed path remapping, will use Plex API fallback per-item")

                    logging.info(f"[BACKFILL_FS] Successfully remapped {len(remapped_items)} items, scanning filesystem...")
                else:
                    remapped_items = db_items
                    failed_items = []

                # Scan filesystem to get size/resolution data (PRIMARY method)
                if remapped_items:
                    scan_results = local_library_scan(remapped_items, extract_resolution=True)
                    logging.info(f"[BACKFILL_FS] Filesystem scan completed for {len(scan_results)} items")

                    # Convert scan results to expected format
                    filesystem_items = list(scan_results.values())
                else:
                    filesystem_items = []

                if filesystem_items:
                    movies = [item for item in filesystem_items if item.get('type') == 'movie']
                    episodes = [item for item in filesystem_items if item.get('type') == 'episode']
                    logging.info(f"[BACKFILL_FS] Filesystem found {len(movies)} movies and {len(episodes)} episodes")

                    collected_content = {'movies': movies, 'episodes': episodes}
                    data_source = 'filesystem'  # Mark that we used filesystem scan

                    # Per-item fallback: Check for items with missing size/resolution and get from Plex
                    if mode == 'Plex' and backfill:
                        items_needing_plex = []
                        for item in filesystem_items:
                            # Check if size or resolution is missing
                            if item.get('size_gb') is None or item.get('resolution') is None:
                                items_needing_plex.append(item)

                        if items_needing_plex:
                            logging.info(f"[BACKFILL_PLEX_FALLBACK] {len(items_needing_plex)} items missing size/resolution, fetching from Plex API...")

                            try:
                                # Get Plex library content to match against
                                from utilities.plex_functions import get_collected_from_plex
                                plex_content = asyncio.run(get_collected_from_plex())

                                if plex_content:
                                    plex_movies = plex_content.get('movies', [])
                                    plex_episodes = plex_content.get('episodes', [])

                                    # Create lookup dict by unique identifier
                                    plex_lookup = {}
                                    for plex_item in plex_movies + plex_episodes:
                                        # Use imdb_id or tmdb_id + type as key
                                        if plex_item.get('imdb_id'):
                                            key = ('imdb', plex_item['imdb_id'], plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('tmdb_id'):
                                            key = ('tmdb', str(plex_item['tmdb_id']), plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        # For episodes, also use season/episode as key
                                        if plex_item.get('type') == 'episode':
                                            if plex_item.get('imdb_id') and plex_item.get('season_number') and plex_item.get('episode_number'):
                                                key = ('episode', plex_item['imdb_id'], plex_item['season_number'], plex_item['episode_number'])
                                                plex_lookup[key] = plex_item

                                    # Match and update items
                                    updated_count = 0
                                    for item in items_needing_plex:
                                        plex_match = None

                                        # Try to find match in Plex data
                                        if item.get('imdb_id'):
                                            plex_match = plex_lookup.get(('imdb', item['imdb_id'], item.get('type')))
                                        if not plex_match and item.get('tmdb_id'):
                                            plex_match = plex_lookup.get(('tmdb', str(item['tmdb_id']), item.get('type')))
                                        if not plex_match and item.get('type') == 'episode':
                                            if item.get('imdb_id') and item.get('season_number') and item.get('episode_number'):
                                                plex_match = plex_lookup.get(('episode', item['imdb_id'], item['season_number'], item['episode_number']))

                                        if plex_match:
                                            # Update missing fields from Plex
                                            if item.get('size_gb') is None and plex_match.get('size_gb'):
                                                item['size_gb'] = plex_match['size_gb']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated size for {item.get('title', 'Unknown')}: {plex_match['size_gb']}GB")
                                            if item.get('resolution') is None and plex_match.get('resolution'):
                                                item['resolution'] = plex_match['resolution']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated resolution for {item.get('title', 'Unknown')}: {plex_match['resolution']}")
                                            updated_count += 1

                                    logging.info(f"[BACKFILL_PLEX_FALLBACK] Updated {updated_count}/{len(items_needing_plex)} items from Plex API")

                            except Exception as e:
                                logging.error(f"[BACKFILL_PLEX_FALLBACK] Error during per-item Plex fallback: {e}", exc_info=True)

                        # Also fetch data for items that failed path remapping
                        if failed_items:
                            logging.info(f"[BACKFILL_PLEX_FALLBACK] Fetching {len(failed_items)} failed remap items from Plex API...")
                            try:
                                from utilities.plex_functions import get_collected_from_plex
                                plex_content = asyncio.run(get_collected_from_plex())

                                if plex_content:
                                    plex_movies = plex_content.get('movies', [])
                                    plex_episodes = plex_content.get('episodes', [])

                                    # Create lookup dict
                                    plex_lookup = {}
                                    for plex_item in plex_movies + plex_episodes:
                                        if plex_item.get('imdb_id'):
                                            key = ('imdb', plex_item['imdb_id'], plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('tmdb_id'):
                                            key = ('tmdb', str(plex_item['tmdb_id']), plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('type') == 'episode':
                                            if plex_item.get('imdb_id') and plex_item.get('season_number') and plex_item.get('episode_number'):
                                                key = ('episode', plex_item['imdb_id'], plex_item['season_number'], plex_item['episode_number'])
                                                plex_lookup[key] = plex_item

                                    # Match failed items and add to results
                                    matched_count = 0
                                    for failed_item in failed_items:
                                        plex_match = None

                                        # Try to find match
                                        if failed_item.get('imdb_id'):
                                            plex_match = plex_lookup.get(('imdb', failed_item['imdb_id'], failed_item.get('type')))
                                        if not plex_match and failed_item.get('tmdb_id'):
                                            plex_match = plex_lookup.get(('tmdb', str(failed_item['tmdb_id']), failed_item.get('type')))
                                        if not plex_match and failed_item.get('type') == 'episode':
                                            if failed_item.get('imdb_id') and failed_item.get('season_number') and failed_item.get('episode_number'):
                                                plex_match = plex_lookup.get(('episode', failed_item['imdb_id'], failed_item['season_number'], failed_item['episode_number']))

                                        if plex_match:
                                            # Add to results with Plex data
                                            if plex_match.get('type') == 'movie':
                                                movies.append(plex_match)
                                            else:
                                                episodes.append(plex_match)
                                            matched_count += 1

                                    logging.info(f"[BACKFILL_PLEX_FALLBACK] Matched {matched_count}/{len(failed_items)} failed items from Plex API")

                                    # Update collected_content with new items
                                    collected_content = {'movies': movies, 'episodes': episodes}

                            except Exception as e:
                                logging.error(f"[BACKFILL_PLEX_FALLBACK] Error fetching failed items from Plex: {e}", exc_info=True)

                    # Per-item fallback for Symlink mode (same logic as Plex mode)
                    elif mode in ['Symlink', 'Symlinked/Local'] and backfill:
                        items_needing_plex = []
                        for item in filesystem_items:
                            # Check if size or resolution is missing
                            if item.get('size_gb') is None or item.get('resolution') is None:
                                items_needing_plex.append(item)

                        if items_needing_plex:
                            logging.info(f"[BACKFILL_PLEX_FALLBACK] {len(items_needing_plex)} Symlink items missing size/resolution, fetching from Plex API...")

                            try:
                                from utilities.plex_functions import get_collected_from_plex
                                plex_content = asyncio.run(get_collected_from_plex())

                                if plex_content:
                                    plex_movies = plex_content.get('movies', [])
                                    plex_episodes = plex_content.get('episodes', [])

                                    # Create lookup dict by unique identifier
                                    plex_lookup = {}
                                    for plex_item in plex_movies + plex_episodes:
                                        if plex_item.get('imdb_id'):
                                            key = ('imdb', plex_item['imdb_id'], plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('tmdb_id'):
                                            key = ('tmdb', str(plex_item['tmdb_id']), plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('type') == 'episode':
                                            if plex_item.get('imdb_id') and plex_item.get('season_number') and plex_item.get('episode_number'):
                                                key = ('episode', plex_item['imdb_id'], plex_item['season_number'], plex_item['episode_number'])
                                                plex_lookup[key] = plex_item

                                    # Match and update items
                                    updated_count = 0
                                    for item in items_needing_plex:
                                        plex_match = None

                                        # Try to find match in Plex data
                                        if item.get('imdb_id'):
                                            plex_match = plex_lookup.get(('imdb', item['imdb_id'], item.get('type')))
                                        if not plex_match and item.get('tmdb_id'):
                                            plex_match = plex_lookup.get(('tmdb', str(item['tmdb_id']), item.get('type')))
                                        if not plex_match and item.get('type') == 'episode':
                                            if item.get('imdb_id') and item.get('season_number') and item.get('episode_number'):
                                                plex_match = plex_lookup.get(('episode', item['imdb_id'], item['season_number'], item['episode_number']))

                                        if plex_match:
                                            # Update missing fields from Plex
                                            if item.get('size_gb') is None and plex_match.get('size_gb'):
                                                item['size_gb'] = plex_match['size_gb']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated size for {item.get('title', 'Unknown')}: {plex_match['size_gb']}GB")
                                            if item.get('resolution') is None and plex_match.get('resolution'):
                                                item['resolution'] = plex_match['resolution']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated resolution for {item.get('title', 'Unknown')}: {plex_match['resolution']}")
                                            updated_count += 1

                                    logging.info(f"[BACKFILL_PLEX_FALLBACK] Updated {updated_count}/{len(items_needing_plex)} Symlink items from Plex API")

                                    # Update collected_content with updated items
                                    movies = [item for item in filesystem_items if item.get('type') == 'movie']
                                    episodes = [item for item in filesystem_items if item.get('type') == 'episode']
                                    collected_content = {'movies': movies, 'episodes': episodes}

                            except Exception as e:
                                logging.error(f"[BACKFILL_PLEX_FALLBACK] Error during Symlink per-item Plex fallback: {e}", exc_info=True)

                    # Check success rate for Plex mode (AFTER fallback completes)
                    if mode == 'Plex':
                        total_items = len(db_items)
                        filesystem_count = len([item for item in filesystem_items if not item.get('_from_plex_fallback')])
                        # Total found = filesystem items + movies/episodes from fallback
                        total_found = len(movies) + len(episodes)
                        success_rate = (total_found / total_items * 100) if total_items > 0 else 0

                        if failed_items:
                            logging.warning(f"[BACKFILL_FS] {len(failed_items)} items failed path remapping - check logs for details")

                        # Log breakdown
                        logging.info(f"[BACKFILL_FS] Filesystem: {filesystem_count} items, Plex fallback: {total_found - filesystem_count} items")
                        logging.info(f"[BACKFILL_FS] Total success rate: {success_rate:.1f}% ({total_found}/{total_items} items)")

                        # If success rate is very low, warn and consider falling back to Plex API
                        if success_rate < 50 and total_items > 100:
                            logging.warning(f"[BACKFILL_FS] Low success rate ({success_rate:.1f}%), check mount configuration. Falling back to Plex API...")
                            collected_content = None  # Trigger Plex API fallback
                else:
                    logging.warning(f"[BACKFILL_FS] Filesystem scan returned 0 items, will fall back to Plex")
            else:
                logging.warning(f"[BACKFILL_FS] No Collected items in database, will fall back to Plex")
        except Exception as e:
            logging.error(f"[BACKFILL_FS] Error during filesystem scan: {e}", exc_info=True)
            logging.warning(f"[BACKFILL_FS] Filesystem scan failed, will fall back to Plex")

    # PLEX API PATH: Used for Plex mode users OR as fallback if filesystem failed/returned 0 items
    # Backward compatible: preserves existing behavior for non-symlink modes
    if collected_content is None and (mode == 'Plex' or bypass or backfill):
        if backfill and mode in ['Symlink', 'Symlinked/Local']:
            logging.info("[BACKFILL_FALLBACK] Filesystem returned no data, falling back to Plex API...")

        if backfill:
            logging.info("[BACKFILL_PLEX] Getting all collected content from Plex API (with size/resolution fetch)...")
        else:
            logging.info("Getting all collected content from Plex...")
        try:
            collected_content = asyncio.run(run_get_collected_from_plex(bypass=bypass, fetch_sizes=backfill))
        except Exception as e:
             logging.error(f"Error running run_get_collected_from_plex: {e}", exc_info=True)
             return None # Return None on error during fetch

    elif collected_content is None and mode == 'Zurg':
        logging.info("Getting all collected content from Zurg...")
        try:
             # Assuming a similar function exists or needs to be created for Zurg full scan
            collected_content = asyncio.run(run_get_collected_from_zurg(bypass=bypass)) # Added bypass
        except Exception as e:
            logging.error(f"Error running run_get_collected_from_zurg: {e}", exc_info=True)
            return None # Return None on error during fetch
    elif collected_content is None:
        logging.info(f"File collection management mode ('{mode}') does not support full library scan for collected items.")
        return None


    if collected_content:
        movies = collected_content.get('movies', []) # Use .get for safety
        episodes = collected_content.get('episodes', [])

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from {mode}.")

        # LEGACY FALLBACK: Keep old logic for edge cases (should rarely trigger now)
        # Only triggers if Plex API was used but returned 0 items
        if backfill and len(movies) == 0 and len(episodes) == 0 and mode == 'Symlinked/Local':
            logging.warning("[BACKFILL_FALLBACK] Plex returned 0 items, falling back to filesystem scan for backfill...")
            try:
                from database.database_reading import get_all_media_items
                from utilities.local_library_scan import local_library_scan

                # Get all Collected items from database
                db_items = get_all_media_items(state='Collected')
                logging.info(f"[BACKFILL_FALLBACK] Got {len(db_items)} items from database for filesystem backfill")

                if db_items:
                    # Scan filesystem to get size/resolution data
                    scan_results = local_library_scan(db_items)
                    logging.info(f"[BACKFILL_FALLBACK] Filesystem scan found data for {len(scan_results)} items")

                    # Convert scan results to the format expected by add_collected_items
                    # scan_results is a dict: {item_id: {updated_item_dict}}
                    filesystem_items = list(scan_results.values())

                    if filesystem_items:
                        movies = [item for item in filesystem_items if item.get('type') == 'movie']
                        episodes = [item for item in filesystem_items if item.get('type') == 'episode']
                        logging.info(f"[BACKFILL_FALLBACK] Filesystem fallback found {len(movies)} movies and {len(episodes)} episodes")

                        # Update collected_content to include filesystem data
                        collected_content = {'movies': movies, 'episodes': episodes}
                        data_source = 'filesystem'  # Mark that we used filesystem scan
            except Exception as e:
                logging.error(f"[BACKFILL_FALLBACK] Error during filesystem fallback: {e}", exc_info=True)

        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            from database import add_collected_items # Keep import local
            add_collected_items(movies + episodes, backfill=backfill, data_source=data_source)
            logging.info(f"Finished adding {len(movies) + len(episodes)} collected items to database.")

            # -------- Memory cleanup to avoid JSON blob retention --------
            try:
                import gc
                collected_content.clear()  # remove references to large lists
                # Conditionally delete large local lists if they exist
                for _var in ['movies', 'episodes', 'all_raw_movies', 'all_raw_episodes']:
                    if _var in locals():
                        locals()[_var].clear()
                # Drop references so GC can reclaim
                movies = episodes = None
                if 'all_raw_movies' in locals():
                    all_raw_movies = None  # type: ignore
                if 'all_raw_episodes' in locals():
                    all_raw_episodes = None  # type: ignore
                gc.collect()
                try:
                    import ctypes as _ct
                    _ct.CDLL('libc.so.6').malloc_trim(0)
                    logging.info("[MemCleanup] Cleared collected content, GC + malloc_trim after Plex full scan.")
                except Exception:
                    logging.info("[MemCleanup] Cleared collected content and forced GC after Plex full scan.")
            except Exception as e_cleanup:
                logging.debug(f"[MemCleanup] Exception during cleanup: {e_cleanup}")
            # ----------------------------------------------------------------
            return collected_content  # Return the original content even if some items were skipped

    logging.warning(f"Failed to retrieve or process collected content from {mode}.")
    return None


def backfill_resolution_from_stored_paths():
    """
    Backfill resolution for Collected items with NULL resolution by extracting from stored paths.
    Uses location_on_disk or filled_by_file fields already in the database.

    Returns:
        dict: Results with counts of updated items
    """
    try:
        from utilities.local_library_scan import extract_resolution_from_filename
        from database.database_reading import get_db_connection

        logging.info("[BACKFILL_RESOLUTION] Starting resolution backfill from stored paths...")

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all Collected items with NULL resolution
        cursor.execute("""
            SELECT id, title, type, location_on_disk, filled_by_file
            FROM media_items
            WHERE state = 'Collected'
              AND resolution IS NULL
              AND (ghostlisted = 0 OR ghostlisted IS NULL)
        """)

        items = cursor.fetchall()
        total_items = len(items)
        logging.info(f"[BACKFILL_RESOLUTION] Found {total_items} Collected items with NULL resolution")

        if total_items == 0:
            conn.close()
            return {'success': True, 'total_items': 0, 'updated': 0, 'failed': 0}

        updated_count = 0
        failed_count = 0
        by_resolution = {}

        for item in items:
            item_id = item['id']
            title = item['title']
            item_type = item['type']
            location = item['location_on_disk']
            filename = item['filled_by_file']

            # Try to extract resolution from location_on_disk first
            resolution = None
            if location:
                resolution = extract_resolution_from_filename(location)
                if resolution:
                    logging.debug(f"[BACKFILL_RESOLUTION] Extracted '{resolution}' from location: {location}")

            # Fallback to filled_by_file if location didn't work
            if not resolution and filename:
                resolution = extract_resolution_from_filename(filename)
                if resolution:
                    logging.debug(f"[BACKFILL_RESOLUTION] Extracted '{resolution}' from filename: {filename}")

            if resolution:
                # Update database
                cursor.execute("""
                    UPDATE media_items
                    SET resolution = ?
                    WHERE id = ?
                """, (resolution, item_id))

                updated_count += 1
                by_resolution[resolution] = by_resolution.get(resolution, 0) + 1

                if updated_count % 100 == 0:
                    logging.info(f"[BACKFILL_RESOLUTION] Progress: {updated_count}/{total_items} items updated")
            else:
                failed_count += 1
                logging.debug(f"[BACKFILL_RESOLUTION] Could not extract resolution for {title} (ID: {item_id})")

        conn.commit()
        conn.close()

        logging.info(f"[BACKFILL_RESOLUTION] Completed: {updated_count} updated, {failed_count} failed")
        logging.info(f"[BACKFILL_RESOLUTION] By resolution: {by_resolution}")

        return {
            'success': True,
            'total_items': total_items,
            'updated': updated_count,
            'failed': failed_count,
            'by_resolution': by_resolution
        }

    except Exception as e:
        logging.error(f"[BACKFILL_RESOLUTION] Error during resolution backfill: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


# FIX: Debounce for Plex recent scan to prevent API spam
# Track the last time get_and_add_recent_collected_from_plex was called
_last_plex_recent_scan_time = None
_PLEX_RECENT_SCAN_COOLDOWN_SECONDS = 300  # 5 minutes minimum between scans

def get_and_add_recent_collected_from_plex():
    global _last_plex_recent_scan_time
    from datetime import datetime

    # FIX: Debounce - skip if scan was run recently
    if _last_plex_recent_scan_time is not None:
        elapsed = (datetime.now() - _last_plex_recent_scan_time).total_seconds()
        if elapsed < _PLEX_RECENT_SCAN_COOLDOWN_SECONDS:
            logging.debug(f"[PlexRecentScan] Debounce: Skipping scan, last ran {elapsed:.0f}s ago (cooldown: {_PLEX_RECENT_SCAN_COOLDOWN_SECONDS}s)")
            return None

    _last_plex_recent_scan_time = datetime.now()

    collected_content = None
    mode = get_setting('File Management', 'file_collection_management')
    logging.info(f"Getting recently added content from {mode}...")

    try:
        if mode == 'Plex':
            collected_content = asyncio.run(run_get_recent_from_plex())
        elif mode == 'Zurg':
            collected_content = asyncio.run(run_get_recent_from_zurg())
        else:
            logging.info(f"File collection management mode ('{mode}') does not support recent library scan.")
            return None
    except Exception as e:
         logging.error(f"Error running recent scan function for {mode}: {e}", exc_info=True)
         return None


    if collected_content:
        movies = collected_content.get('movies', [])
        episodes = collected_content.get('episodes', [])

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} recent episodes from {mode}.")

        # Check and fix any unmatched items before adding to database if enabled
        if get_setting('Debug', 'enable_unmatched_items_check', True):
            logging.info("Checking and fixing unmatched items before adding to database")
            try:
                from utilities.plex_matching_functions import check_and_fix_unmatched_items
                collected_content = check_and_fix_unmatched_items(collected_content)
                # Get updated counts after matching check
                movies = collected_content.get('movies', [])
                episodes = collected_content.get('episodes', [])
                logging.info(f"Counts after unmatched check: {len(movies)} movies, {len(episodes)} episodes.")
            except Exception as e_match:
                logging.error(f"Error during check_and_fix_unmatched_items: {e_match}", exc_info=True)
                # Proceed with potentially unmatched items? Or return? Let's proceed.


        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            from database import add_collected_items
            try:
                add_collected_items(movies + episodes, recent=True)
                logging.info(f"Finished adding {len(movies) + len(episodes)} recent items to database.")
                return collected_content  # Return the original content even if some items were skipped
            except Exception as e_add:
                 logging.error(f"Error during add_collected_items for recent items: {e_add}", exc_info=True)
                 return None # Return None if adding fails
        else:
            logging.info("No recent movies or episodes to add after processing.")
            return collected_content # Return empty dict if nothing to add

    logging.warning(f"Failed to retrieve or process recent content from {mode}.")
    return None

def run_local_library_scan():
    # ... (This function seems unused/disabled, no changes needed) ...
    from utilities.local_library_scan import local_library_scan
    logging.info("Full library scan disabled for now")
    #local_library_scan()

def run_recent_local_library_scan():
    # ... (This function seems unused/disabled, no changes needed) ...
    from utilities.local_library_scan import recent_local_library_scan
    logging.info("Recent library scan disabled for now")
    #recent_local_library_scan()

# Add Listener Setup Method
def _setup_scheduler_listeners(runner_instance):
    """
    Ensures the scheduler is initialized and adds necessary event listeners.
    If runner_instance.scheduler is None, a new scheduler will be created.
    """
    logging.info(f"[_setup_scheduler_listeners] Entered for runner (ID: {id(runner_instance)}).")
    current_scheduler_id = id(runner_instance.scheduler) if runner_instance.scheduler else "None"
    logging.info(f"[_setup_scheduler_listeners] Initial runner_instance.scheduler is {current_scheduler_id}. initial_listeners_setup_complete: {getattr(runner_instance, 'initial_listeners_setup_complete', 'N/A')}")

    scheduler_recreated = False
    if runner_instance.scheduler is None:
        logging.info(f"[_setup_scheduler_listeners] runner_instance.scheduler is None. Creating new BackgroundScheduler.")
        
        try:
            from metadata.metadata import _get_local_timezone # Local import for safety
            tz_setting = get_setting('UI Settings', 'timezone', 'UTC')
            try:
                tz = pytz.timezone(tz_setting)
            except pytz.exceptions.UnknownTimeZoneError:
                logging.warning(f"[_setup_scheduler_listeners] Unknown timezone '{tz_setting}' in settings. Defaulting to UTC.")
                tz = pytz.utc
            logging.info(f"[_setup_scheduler_listeners] Initializing new APScheduler with timezone: {tz.key if hasattr(tz, 'key') else tz}")
        except Exception as e_tz:
            logging.error(f"[_setup_scheduler_listeners] Failed to get local timezone for scheduler, using UTC fallback: {e_tz}")
            tz = pytz.utc

        _queue_workers, executors, job_defaults = _build_scheduler_config()

        try:
            new_scheduler = BackgroundScheduler(
                executors=executors,
                job_defaults=job_defaults,
                timezone=tz
            )
            runner_instance.scheduler = new_scheduler
            scheduler_recreated = True
            # Mark listeners as NOT setup for this new scheduler instance
            runner_instance.initial_listeners_setup_complete = False 
            logging.info(f"[_setup_scheduler_listeners] New BackgroundScheduler CREATED and ASSIGNED. New scheduler ID: {id(runner_instance.scheduler)}.")
        except Exception as e_create_scheduler:
            logging.error(f"[_setup_scheduler_listeners] FAILED to create new BackgroundScheduler: {e_create_scheduler}", exc_info=True)
            runner_instance.scheduler = None # Ensure it's None if creation failed
            runner_instance.initial_listeners_setup_complete = False
            raise # Re-raise the exception to signal failure to the caller
    
    # Add listeners if scheduler exists and listeners are not yet marked complete for this instance
    # The initial_listeners_setup_complete flag is now specific to a scheduler instance.
    if runner_instance.scheduler and not getattr(runner_instance, 'initial_listeners_setup_complete', False):
        try:
            logging.info(f"[_setup_scheduler_listeners] Setting up APScheduler job listeners for scheduler (ID: {id(runner_instance.scheduler)}).")
            runner_instance.scheduler.add_listener(
                runner_instance._job_listener,
                apscheduler.events.EVENT_JOB_SUBMITTED |
                apscheduler.events.EVENT_JOB_EXECUTED |
                apscheduler.events.EVENT_JOB_ERROR |
                apscheduler.events.EVENT_JOB_MISSED |
                apscheduler.events.EVENT_JOB_MAX_INSTANCES # Added EVENT_JOB_MAX_INSTANCES
            )
            runner_instance.initial_listeners_setup_complete = True # Mark listeners as setup
            logging.info(f"[_setup_scheduler_listeners] APScheduler job listeners added successfully for scheduler (ID: {id(runner_instance.scheduler)}).")
        except Exception as e_add_listener:
            logging.error(f"[_setup_scheduler_listeners] Failed to add APScheduler listener: {e_add_listener}", exc_info=True)
            runner_instance.initial_listeners_setup_complete = False # Failed to setup
            # If listener setup fails, the scheduler might still run but without our custom listener logic.
            # Depending on how critical _job_listener is, might need to raise here.
    elif runner_instance.scheduler:
         logging.info(f"[_setup_scheduler_listeners] Listeners already marked as setup for scheduler (ID: {id(runner_instance.scheduler)}).")
    else: # Should not happen if creation logic is correct
        logging.error(f"[_setup_scheduler_listeners] Cannot setup listeners as runner_instance.scheduler is still None after creation attempt.")


    # If scheduler was recreated, or if initial tasks need to be (re)scheduled for any other reason
    # For instance, if task definitions changed and we need a full reschedule.
    # For now, only do this if scheduler was just recreated.
    if scheduler_recreated and runner_instance.scheduler:
        try:
            logging.info(f"[_setup_scheduler_listeners] Scheduler was recreated. Re-scheduling initial tasks for scheduler (ID: {id(runner_instance.scheduler)}).")
            runner_instance._schedule_initial_tasks() # Populate the new scheduler with tasks
            logging.info(f"[_setup_scheduler_listeners] Initial tasks (re)scheduled successfully for new scheduler (ID: {id(runner_instance.scheduler)}).")
        except Exception as e_schedule_tasks:
            logging.error(f"[_setup_scheduler_listeners] Error (re)scheduling initial tasks for new scheduler: {e_schedule_tasks}", exc_info=True)
            # This is critical. If tasks can't be added, the new scheduler is useless.
            # Consider shutting down the new scheduler and setting runner_instance.scheduler back to None.
            try:
                runner_instance.scheduler.shutdown(wait=False)
            except: pass
            runner_instance.scheduler = None
            runner_instance.initial_listeners_setup_complete = False
            raise RuntimeError(f"Failed to schedule tasks on newly created scheduler: {e_schedule_tasks}")

    logging.info(f"[_setup_scheduler_listeners] Completed. Final runner_instance.scheduler ID: {id(runner_instance.scheduler) if runner_instance.scheduler else 'None'}. initial_listeners_setup_complete: {getattr(runner_instance, 'initial_listeners_setup_complete', 'N/A')}")

def run_program():
    global program_runner
    logging.info("Program start requested")

    if program_runner is None or not program_runner.is_running():
        logging.info("Initializing ProgramRunner...")
        program_runner = ProgramRunner()
        # Setup listeners after init
        try:
            _setup_scheduler_listeners(program_runner) # Use the correct function name
        except Exception as e:
             logging.error(f"Failed to set up scheduler listeners during startup: {e}", exc_info=True)
        # Update the program runner in program_operation_routes
        from routes.program_operation_routes import program_operation_bp
        program_operation_bp.program_runner = program_runner # Ensure routes use the instance
        logging.info("Starting ProgramRunner instance...")
        program_runner.start()  # Starts the scheduler and run loop
    else:
        logging.info("Program is already running")
    return program_runner

if __name__ == "__main__":
    run_program()
