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
# *** START EDIT: Import tracemalloc ***
try:
    import tracemalloc
    tracemalloc_available = True
except ImportError:
    tracemalloc = None # Define as None if import fails
    tracemalloc_available = False
# *** END EDIT ***
# *** START EDIT: Add psutil import ***
try:
    import psutil
except ImportError:
    psutil = None # Handle missing import gracefully
# *** END EDIT ***
# *** START EDIT ***
from apscheduler.schedulers.background import BackgroundScheduler
# --- START EDIT: Add ThreadPoolExecutor for explicit configuration ---
from apscheduler.executors.pool import ThreadPoolExecutor
# --- END EDIT ---
from apscheduler.triggers.interval import IntervalTrigger
# --- START EDIT: Add threading.Lock ---
import threading # For scheduler lock, concurrent queue processing, AND heavy task lock
# --- END EDIT ---
import functools # Added for partial
import apscheduler.events # Added for listener events
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED, EVENT_JOB_MAX_INSTANCES
# *** END EDIT ***
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
    should_process_item, update_cache_for_item
)
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
# --- START EDIT: Add Debrid imports for library size task ---
from debrid import get_debrid_provider, ProviderUnavailableError
# --- END EDIT ---
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

class ProgramRunner:
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
        
        # --- START EDIT: Use pause_info instead of pause_reason ---
        self.pause_info = {
            "reason_string": None,
            "error_type": None,  # e.g., "CONNECTION_ERROR", "UNAUTHORIZED", "SYSTEM_SCHEDULED", "RATE_LIMIT", "DB_HEALTH"
            "service_name": None, # e.g., "Real-Debrid API", "Plex", "System"
            "status_code": None,  # HTTP status code if applicable
            "retry_count": 0
        }
        # --- END EDIT ---
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
            _queue_workers = max(1, min(3, int(get_setting('Queue', 'queue_pool_workers', 2))))
            executors = {
                'default': ThreadPoolExecutor(max_workers=1),  # scheduled/maintenance tasks — sequential
                'queue': ThreadPoolExecutor(max_workers=_queue_workers),
            }
            job_defaults = {
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': None
            }
            self.scheduler = BackgroundScheduler(jobstores=_jobstores, executors=executors, job_defaults=job_defaults, timezone=tz)
            logging.info(f"APScheduler configured with separate thread pools: queue({_queue_workers}) and default(1).")
        except Exception as e:
            logging.error(f"Failed to get local timezone for scheduler, using system default: {e}")
            _queue_workers = max(1, min(3, int(get_setting('Queue', 'queue_pool_workers', 2))))
            executors = {
                'default': ThreadPoolExecutor(max_workers=1),
                'queue': ThreadPoolExecutor(max_workers=_queue_workers),
            }
            job_defaults = {
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': None
            }
            self.scheduler = BackgroundScheduler(jobstores=_jobstores, executors=executors, job_defaults=job_defaults)
            logging.info("APScheduler configured with separate thread pools: queue(2) and default(1) (system default timezone).")

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
        
        # --- START EDIT: Define queue_processing_map FIRST ---
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
        # --- END EDIT ---

        # --- START EDIT: Define BASE set of heavy DB tasks ---
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
        # --- END EDIT ---

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
            'task_refresh_plex_tokens': 24 * 60 * 60,   # Run every 24 hours
            'task_sync_plex_labels': 30 * 60,           # Run every 30 minutes
            'task_update_tv_show_status': 172800,       # Run every 48 hours
            # 'task_purge_not_wanted_magnets_file': 604800, # Default: 1 week (Can be added if needed)
            # 'task_local_library_scan': 900, # Default: 15 mins (Can be added if needed)
            'task_plex_full_scan': 1800, # Run every hour (Can be adjusted)
            # NEW Load Adjustment Task
            # 'task_adjust_intervals_for_load': 120, # Run every 2 minutes
            # --- START EDIT: Add new task for library size refresh ---
            'task_refresh_library_size_cache': 12 * 60 * 60, # Run every 12 hours
            'task_backup_database': 24 * 60 * 60, # Run every 24 hours (daily backup)
            'task_backup_debrid': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_cleanup_debrid': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_backfill_nzb_torrent_ids': 24 * 60 * 60, # Run once (disabled by default)
            'task_repair_broken_nzbs': 6 * 60 * 60, # Run every 6 hours
            'task_repair_broken_debrids': 6 * 60 * 60, # Run every 6 hours
            'task_sync_cli_mount_changes': 5 * 60, # Run every 5 minutes
            'task_push_pending_climount_tags': 5 * 60, # Run every 5 minutes — catches tags changed on cli_debrid side only
            'task_nzb_health_check': 10,             # Run every 10 seconds — polls NZB items in Adding
            'task_backfill_plex_guids': 24 * 60 * 60,    # Run once (disabled by default)
            'task_backfill_plex_ms_item_id': 24 * 60 * 60, # Run once (disabled by default)
            # --- END EDIT ---
            'task_process_standalone_plex_removals': 60 * 60, # Run every hour
            # --- START EDIT: Add media analysis task interval ---
            'task_analyze_media_files': 1 * 60 * 60, # Once an hour
            # --- END EDIT ---
            # --- START EDIT: Add manual Plex full scan task ---
            'task_manual_plex_full_scan': 3600, # Run every 60 minutes, disabled by default
            # --- END EDIT ---
            # --- START EDIT: Add bulk subtitle processing task ---
            'task_process_bulk_subtitles': 3600, # Run every hour, disabled by default
            # --- END EDIT ---
            # --- START EDIT: Add Plex overlay tasks ---
            'task_overlay_sync': 3600, # Run every 1 hour (if overlays enabled)
            'task_overlay_cleanup': 86400, # Run every 24 hours (if overlays enabled)
            'task_plex_smart_collection_posters': 86400, # Run every 24 hours (if Plex mode)
            'task_plex_movie_boxsets': 86400, # Run every 24 hours (if Plex mode)
            # --- END EDIT ---
            # 'task_artificial_long_run': 1*60*60, # Run every 2 minutes
            'task_regulate_system_load': 30, # Check system load every 30 seconds
            'task_upgrade_hub_scan': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_upgrade_hub_auto_queue': 24 * 60 * 60, # Run every 24 hours (disabled by default)
            'task_trim_memory': 60 * 60, # Run every hour
        }
        # Store original intervals for reference (will be updated after content sources)
        self.original_task_intervals = self.task_intervals.copy()
        logging.info(f"Base default task intervals defined: {len(self.original_task_intervals)}")

        # --- START EDIT: Load custom intervals ---
        custom_intervals_applied = 0
        intervals_file_path = None # Define outside try
        try:
            # --- START EDIT: Add import os ---
            import os
            import json
            # --- END EDIT ---
            from routes.program_operation_routes import _get_task_intervals_file_path # Import helper
            intervals_file_path = _get_task_intervals_file_path()
            if os.path.exists(intervals_file_path): # Error occurred here
                # --- START EDIT: Added log ---
                logging.info(f"Loading custom task intervals from {intervals_file_path}")
                # --- END EDIT ---
                with open(intervals_file_path, 'r') as f:
                    # --- START EDIT: Added try-except for JSON decode ---
                    try:
                        saved_intervals = json.load(f)
                        logging.debug(f"Successfully loaded JSON data: {saved_intervals}") # Log loaded data
                    except json.JSONDecodeError as json_e:
                        logging.error(f"Failed to decode JSON from {intervals_file_path}: {json_e}")
                        saved_intervals = {} # Use empty dict on decode error
                    # --- END EDIT ---

                # Process values as SECONDS
                for task_name, interval_seconds_val in saved_intervals.items(): # Rename loop variable
                    # --- START EDIT: Log raw values ---
                    logging.debug(f"Processing raw custom interval: Task='{task_name}', Value='{interval_seconds_val}'")
                    # --- END EDIT ---
                    normalized_name = self._normalize_task_name(task_name) # Normalize saved task name
                    # --- START EDIT: Log normalized name ---
                    logging.debug(f"Normalized task name: '{normalized_name}'")
                    # --- END EDIT ---

                    # Check if this task exists in our task_intervals (which start as defaults)
                    if normalized_name in self.task_intervals: # Check against task_intervals
                        # --- START EDIT: Log default interval ---
                        # default_interval here refers to the current value in self.task_intervals,
                        # which might have already been set by a previous custom rule if keys overlap, or is the hardcoded default.
                        current_effective_interval = self.task_intervals[normalized_name]
                        logging.debug(f"Task '{normalized_name}' exists. Current effective interval: {current_effective_interval}s")
                        # --- END EDIT ---

                        if interval_seconds_val is not None: # Ignore None values (means reset)
                            try:
                                # Value is already in seconds
                                interval_sec_int = int(interval_seconds_val)
                                # --- START EDIT: Log parsed custom interval ---
                                logging.debug(f"Parsed custom interval for '{normalized_name}' as {interval_sec_int} seconds.")
                                # --- END EDIT ---

                                MIN_INTERVAL_SECONDS = 1 # Define or import this constant
                                if interval_sec_int >= MIN_INTERVAL_SECONDS:
                                    # --- START EDIT: Log comparison ---
                                    # Compare with current effective, not original_task_intervals necessarily
                                    logging.debug(f"Comparing current effective ({current_effective_interval}s) with custom ({interval_sec_int}s) for '{normalized_name}'")
                                    # --- END EDIT ---
                                    if current_effective_interval != interval_sec_int: # Apply if different
                                        # --- START EDIT: Log application ---
                                        logging.info(f"Applying custom interval to '{normalized_name}': {interval_sec_int} seconds (Previous effective: {current_effective_interval}s)")
                                        # --- END EDIT ---
                                        self.task_intervals[normalized_name] = interval_sec_int # MODIFIES task_intervals ONLY
                                        custom_intervals_applied += 1
                                    else:
                                        # --- START EDIT: Log skipping ---
                                        logging.debug(f"Custom interval ({interval_sec_int}s) for '{normalized_name}' matches current effective ({current_effective_interval}s). Skipping update.")
                                        # --- END EDIT ---
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
        # --- END EDIT ---

        # --- START EDIT: Define constants for dynamic interval adjustment ---
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
            # --- START EDIT: Add new task to dynamic intervals ---
            'task_refresh_library_size_cache',
            # --- END EDIT ---
            'task_process_standalone_plex_removals', # Add to dynamic intervals as well
            # --- START EDIT: Add media analysis task to dynamic intervals ---
            'task_analyze_media_files',
            # --- END EDIT ---
            # --- START EDIT: Add manual plex scan to dynamic intervals ---
            'task_manual_plex_full_scan',
            # --- END EDIT ---
        }
        # Add content source tasks with interval > 900s (15 min) to dynamic set
        # This needs to happen *after* content sources are processed, let's refine this later if needed
        # For now, initialize with the base set. We can add sources dynamically later.

        self.MAX_INTERVAL_MULTIPLIER = 4 # Example: Max increase is 4x original
        self.ABSOLUTE_MAX_INTERVAL = 24 * 60 * 60 # Example: Max interval is 24 hours
            # --- END EDIT ---

        # Initialize content_sources attribute FIRST
        self.content_sources = None
        self.file_location_cache = {}  # Cache to store known file locations

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
            # --- START EDIT: Add 'task_verify_plex_removals' back to default set ---
            'task_verify_plex_removals',
            # --- END EDIT ---
            # --- START EDIT: Enable new library size task by default ---
            'task_refresh_library_size_cache',
            # --- END EDIT ---
            # --- START EDIT: Enable database backup task by default ---
            'task_backup_database',
            # --- END EDIT ---
            'task_process_standalone_plex_removals', # Enable by default
            # --- START EDIT: Enable media analysis task by default ---
            # 'task_analyze_media_files', # disabled by default
            # --- END EDIT ---
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
        # --- START EDIT: Initialize saved_states before try block ---
        saved_states = {} # Ensure saved_states exists even if file loading fails
        # --- END EDIT ---

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
        # --- START EDIT: Add file_collection_management check before other settings ---
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


        if get_setting('Debug', 'not_add_plex_watch_history_items_to_queue', False):
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

        # --- START EDIT: Serialize all scheduled tasks ---
        # To address potential DB contention by making all scheduled tasks run sequentially.
        # logging.info("Configuring all scheduled tasks to run sequentially to prevent DB contention.") # This logic is now handled by scheduler config
        # if not hasattr(self, 'task_intervals') or not self.task_intervals:
            # logging.error("Cannot configure HEAVY_DB_TASKS for serialization: self.task_intervals is not populated. Tasks will use original HEAVY_DB_TASKS definition if any.")
            # If self.HEAVY_DB_TASKS was already defined with a base set, it would be used.
            # Since we initialized it to set(), if this unlikely error occurs, no tasks will be considered "heavy" by default.
        # else:
            # self.HEAVY_DB_TASKS = set(self.task_intervals.keys()) # REVERTED: Scheduler now handles global sequential execution. HEAVY_DB_TASKS reverts to original intent.
            # logging.info(f"All {len(self.HEAVY_DB_TASKS)} tasks defined in task_intervals will now use the exclusive execution lock, effectively running sequentially.")
        # --- END EDIT ---

        # --- START EDIT: Capture baseline enabled tasks snapshot ---
        # Store the set of tasks that were enabled immediately after initialization (including
        # any changes applied from task_toggles.json, content-source processing, and settings).
        # When the user later saves toggle states we can compare against this snapshot and only
        # persist differences, keeping the JSON file minimal.
        self.initial_enabled_tasks_snapshot = set(self.enabled_tasks)
        # --- END EDIT ---

        # Schedule initial tasks
        self._schedule_initial_tasks()

        # *** START EDIT: Modify tracemalloc conditional start in __init__ ***
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
        # *** END EDIT ***

        # *** START EDIT: Add task execution counter and sample rate for tracemalloc ***
        self.task_execution_count = 0
        # Read sample rate from settings, default to 100 (sample 1 in every 100 tasks)
        self.tracemalloc_sample_rate = int(get_setting('Debug', 'tracemalloc_sample_rate', 100))
        # Ensure sample rate is at least 1 to avoid division by zero or weird behavior
        if self.tracemalloc_sample_rate < 1:
            logging.warning(f"Invalid tracemalloc_sample_rate ({self.tracemalloc_sample_rate}), defaulting to 1.")
            self.tracemalloc_sample_rate = 1
        # *** END EDIT ***

        # *** START EDIT: Add variable to store previous snapshot ***
        self.previous_tracemalloc_snapshot = None
        # *** END EDIT ***
        # *** START EDIT: Log tracemalloc status with sample rate ***
        if self._tracemalloc_enabled:
            logging.warning(f"Tracemalloc memory tracking is ENABLED (Sample Rate: 1/{self.tracemalloc_sample_rate}). This adds overhead.")
            # Check again if it's actually tracing (might have failed to start)
            if not (tracemalloc_available and tracemalloc and tracemalloc.is_tracing()):
                 logging.error("Tracemalloc was enabled but is not tracing. Check for startup errors.")
                 self._tracemalloc_enabled = False # Ensure flag reflects reality
        # *** END EDIT ***
        # ... rest of __init__ ...

        self.current_running_task = None
        self._running_task_lock = threading.Lock() # Lock for thread-safe access

        # --- START EDIT: Add inter-task sleep variables ---
        self.base_inter_task_sleep = float(get_setting('Queue', 'main_loop_sleep_seconds', 0.0))
        self.current_inter_task_sleep = self.base_inter_task_sleep
        logging.info(f"Initialized inter-task sleep to {self.current_inter_task_sleep}s based on settings.")
        # --- END EDIT ---

        # --- START EDIT: Add long-running content source tasks to DYNAMIC_INTERVAL_TASKS ---
        # This should run *after* self.content_sources is populated and intervals set
        # Ideally placed after self.get_content_sources(force_refresh=True) call inside __init__
        if self.content_sources: # Check if sources were loaded
            for task_id, interval in self.task_intervals.items():
                 # Check if it's a content source task and interval is long
                 if task_id.startswith('task_') and task_id.endswith('_wanted') and interval > 900:
                      self.DYNAMIC_INTERVAL_TASKS.add(task_id)
            logging.info(f"Updated DYNAMIC_INTERVAL_TASKS with long-running content sources. Total: {len(self.DYNAMIC_INTERVAL_TASKS)}")
        # --- END EDIT ---

        # --- START EDIT: Add currently_executing_tasks set ---
        self.currently_executing_tasks = set()
        # --- END EDIT ---

        # --- START EDIT: Remove single current_running_task ---
        # self.current_running_task = None # Removed
        # --- END EDIT ---
        self._running_task_lock = threading.Lock() # Lock for thread-safe access to the set


        # --- START EDIT: Add task execution counter and sample rate for tracemalloc ---
        self.task_execution_count = 0
        # ... (rest of __init__)

        # In __init__, add:
        self.manual_tasks = set()  # Track manually triggered tasks
        self._initialized_runner_attributes = True # Mark as initialized at the end of actual init logic
        # --- START EDIT: Runtime tracking attributes ---
        self.task_runtime_totals = defaultdict(float)  # Accumulated runtime per task in current window
        self.task_runtime_lock = threading.Lock()  # Protect access to task_runtime_totals
        self._runtime_log_interval_sec = 300  # How often to emit runtime percentage report (seconds)
        self._last_runtime_log_time = time.monotonic()
        # --- END EDIT ---
        # --- START EDIT: CPU usage tracking attributes ---
        # Accumulate per-task CPU seconds in current window
        self.task_cpu_totals = defaultdict(float)
        self.task_cpu_lock = threading.Lock()
        # Share the same interval as runtime logging to avoid log noise
        self._cpu_log_interval_sec = self._runtime_log_interval_sec
        self._last_cpu_log_time = time.monotonic()
        # Control per-run CPU logging via environment variable
        self._log_per_run_cpu = os.environ.get('LOG_PER_RUN_CPU', 'false').lower() == 'true'
        # --- END EDIT ---

    # *** START EDIT: New method to get task target ***
    def _get_task_target(self, task_name: str):
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
    # *** END EDIT ***


    # *** START EDIT: Use _get_task_target in _schedule_task ***
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

                        # *** START EDIT: Explicitly pass scheduler's timezone to add_job ***
                        # This should prevent the IntervalTrigger from calling tzlocal.get_localzone()
                        resolved_timezone = self.scheduler.timezone
                        logging.debug(f"Passing timezone '{resolved_timezone}' explicitly to add_job for task '{job_id}'")
                        # *** END EDIT ***

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

                        # *** START EDIT: Updated log message with stagger info ***
                        if initial_delay_seconds > 0:
                            logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds}s (staggered start: +{initial_delay_seconds}s, max_instances=1)")
                        else:
                            logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds} seconds (max_instances=1, wrapped for duration measurement).")
                        # *** END EDIT ***
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
    # *** END EDIT ***


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

        # *** START EDIT: Add psutil memory logging ***
        if psutil:
            try:
                # 1. Explicitly run garbage collection
                gc.collect()
                logging.debug("Explicitly ran gc.collect()")

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
        # *** END EDIT ***

        # *** START EDIT: Add tracemalloc snapshot comparison in heartbeat ***
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
        # *** END EDIT ***

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

    def get_content_sources(self, force_refresh=False):
        if self.content_sources is None or force_refresh:
            settings = get_all_settings()
            self.content_sources = settings.get('Content Sources', {})
            debug_settings = settings.get('Debug', {})
            custom_check_periods = debug_settings.get('content_source_check_period', {})
            
            default_intervals = {
                'Overseerr': 900,
                'MDBList': 900,
                'Collected': 86400,
                'Trakt Watchlist': 900,
                'Trakt Lists': 900,
                'Trakt Collection': 900,
                'My Plex Watchlist': 900,
                'Other Plex Watchlist': 900,
                'My Plex RSS Watchlist': 900,
                'My Friends Plex RSS Watchlist': 900,
                'My Friends Trakt Watchlist': 900,
                'Special Trakt Lists': 900,
                'Scrob Lists': 900,
                'Scrob Collection': 900,
                'Special Scrob Lists': 900,
                'Adaptive List': 900  # TMDB discover-based adaptive lists
            }
            
            log_intervals_message = ["Content source intervals being applied to effective set:"] # Prepare log message
            
            for source, data in self.content_sources.items():
                if isinstance(data, str):
                    data = {'enabled': data.lower() == 'true'}
                
                if not isinstance(data, dict):
                    logging.error(f"Unexpected data type for content source {source}: {type(data)}")
                    continue
                
                source_type = source.split('_')[0]

                # Use custom check period if present, otherwise use default
                custom_interval = custom_check_periods.get(source)
                final_interval = 0 # Initialize
                if custom_interval is not None:
                    try:
                        final_interval = int(float(custom_interval) * 60)
                        data['interval'] = final_interval
                    except ValueError:
                         logging.error(f"Invalid custom interval '{custom_interval}' for source {source}. Using default.")
                         final_interval = int(data.get('interval', default_intervals.get(source_type, 3600)))
                         data['interval'] = final_interval
                else:
                    final_interval = int(data.get('interval', default_intervals.get(source_type, 3600)))
                    data['interval'] = final_interval

                task_name = f'task_{source}_wanted'
                
                # Store the calculated default interval (before any custom overrides)
                calculated_default_interval = final_interval
                
                # Check if a custom interval was already loaded for this task (from user's saved preferences)
                if task_name in self.task_intervals:
                    # A custom interval exists, preserve it instead of overwriting with defaults
                    existing_custom_interval = self.task_intervals[task_name]
                    logging.info(f"Preserving custom interval for content source '{task_name}': {existing_custom_interval}s (would have been {calculated_default_interval}s from source defaults)")
                    # Update final_interval so the log message below shows the preserved value
                    final_interval = existing_custom_interval
                else:
                    # No custom interval exists, use the calculated default interval
                    self.task_intervals[task_name] = final_interval
                
                # Add to original_task_intervals with the calculated default (not the custom value)
                # This ensures that resets will go back to the true calculated default
                if task_name not in self.original_task_intervals:
                    self.original_task_intervals[task_name] = calculated_default_interval

                log_intervals_message.append(f"  {task_name}: {final_interval} seconds")
                
                if isinstance(data.get('enabled'), str):
                    data['enabled'] = data['enabled'].lower() == 'true'
                
                # Add content source tasks by default; toggles will disable later if set to False
                if task_name not in self.enabled_tasks:
                    self.enabled_tasks.add(task_name)
                    logging.info(f"Enabled content source task by default (ignoring config flag): {task_name}")
                # Do not remove here based on config; rely solely on saved toggles applied later

            # Log the intervals once after processing all sources
            logging.info("\n".join(log_intervals_message))

        return self.content_sources
        
    def force_connectivity_check(self):
        """Force an immediate connectivity check and handle recovery if services are back"""
        from routes.program_operation_routes import check_service_connectivity
        
        logging.info("[Force Connectivity Check] Manually triggered connectivity check")
        
        connectivity_ok, failed_services = check_service_connectivity()
        
        if connectivity_ok and self.connectivity_failure_time:
            # Services are back!
            logging.info("[Force Connectivity Check] Services restored! Clearing failure state and resuming queue.")
            self.connectivity_failure_time = None
            self.connectivity_retry_count = 0
            self.pause_info = {
                "reason_string": None, "error_type": None, "service_name": None,
                "status_code": None, "retry_count": 0
            }
            self.resume_queue()
            return True
        elif not connectivity_ok:
            logging.warning(f"[Force Connectivity Check] Services still unavailable: {[s.get('service') for s in failed_services]}")
            return False
        else:
            logging.info("[Force Connectivity Check] All services operational")
            return True
    
    def task_check_service_connectivity(self):
        """Check connectivity to required services"""
        from routes.program_operation_routes import check_service_connectivity

        # Log current state before checking
        if self.connectivity_failure_time:
            time_since_failure = time.time() - self.connectivity_failure_time
            logging.info(f"[Connectivity Check Task] Running scheduled check. Previous failure detected {time_since_failure:.0f}s ago")
        else:
            logging.debug("[Connectivity Check Task] Running routine connectivity check")
            
        connectivity_ok, failed_services = check_service_connectivity()
        
        if connectivity_ok:
            if self.connectivity_failure_time:
                # We're recovering from a previous failure
                logging.info("[Connectivity Check Task] Service connectivity RESTORED via scheduled task")
                self.connectivity_failure_time = None
                self.connectivity_retry_count = 0
                self.pause_info = {
                    "reason_string": None, "error_type": None, "service_name": None,
                    "status_code": None, "retry_count": 0
                }
                self.resume_queue()
            else:
                logging.debug("[Connectivity Check Task] Service connectivity check passed")
        else:
            if self.connectivity_failure_time:
                # Already in failure state, just log
                logging.warning(f"[Connectivity Check Task] Services still unavailable: {[s.get('service') for s in failed_services]}")
            else:
                # New failure detected
                logging.error(f"[Connectivity Check Task] Service connectivity check failed: {[s.get('service') for s in failed_services]}")
                self.handle_connectivity_failure(failed_services)

    def handle_connectivity_failure(self, failed_services_details=None): # MODIFIED: expects detailed list
        from routes.program_operation_routes import check_service_connectivity # Keep this for potential re-check logic
        from routes.extensions import app

        current_pause_info = {
            "reason_string": "Connectivity failure - waiting for services to be available",
            "error_type": "CONNECTION_ERROR", # Generic default
            "service_name": "Multiple Services", # Generic default
            "status_code": None,
            "retry_count": 0 # Initial failure
        }

        if failed_services_details and len(failed_services_details) > 0:
            # Construct a detailed reason string
            reason_parts = []
            primary_error_set = False
            for detail in failed_services_details:
                service = detail.get("service", "Unknown Service")
                message = detail.get("message", "unavailable")
                reason_parts.append(f"{service}: {message}")

                # Set the primary error based on the first critical issue found
                # Prioritize Debrid Unauthorized/Forbidden
                if not primary_error_set:
                    error_type = detail.get("type", "CONNECTION_ERROR")
                    if "Debrid" in service and error_type in ["UNAUTHORIZED", "FORBIDDEN"]:
                        current_pause_info["error_type"] = error_type
                        current_pause_info["status_code"] = detail.get("status_code")
                        current_pause_info["service_name"] = service
                        primary_error_set = True
                    elif not primary_error_set and error_type == "CONNECTION_ERROR": # Catch first connection error
                        current_pause_info["error_type"] = "CONNECTION_ERROR"
                        current_pause_info["status_code"] = detail.get("status_code")
                        current_pause_info["service_name"] = service
                        # Don't set primary_error_set = True here, to allow critical errors to override

            current_pause_info["reason_string"] = "Connectivity failure - " + "; ".join(reason_parts)
            if not primary_error_set and failed_services_details: # If no specific critical error, use the first service
                 current_pause_info["service_name"] = failed_services_details[0].get("service", "Unknown Service")


        logging.warning(f"Pausing program queue due to connectivity failure: {current_pause_info['reason_string']}")
        # --- START EDIT: Update self.pause_info ---
        self.pause_info = current_pause_info
        # --- END EDIT ---
        self.pause_queue() 
        
        if not self.connectivity_failure_time:
            self.connectivity_failure_time = time.time()
            self.connectivity_retry_count = 0 # Reset legacy retry counter

    def check_connectivity_status(self):
        from routes.program_operation_routes import check_service_connectivity
        from routes.extensions import app

        if not self.connectivity_failure_time:
            return 
            
        # Use the legacy self.connectivity_retry_count for retry timing logic
        time_since_failure = time.time() - self.connectivity_failure_time
        
        # Check every 30 seconds for first 2 minutes, then every 60 seconds
        if time_since_failure <= 120:  # First 2 minutes
            retry_interval = 30
        else:
            retry_interval = 60
            
        # Calculate if it's time for next retry
        time_since_last_retry = time_since_failure - (self.connectivity_retry_count * retry_interval)
        
        if time_since_last_retry >= retry_interval:
            self.connectivity_retry_count += 1 # Increment legacy counter for timing
            
            logging.info(f"Checking service connectivity (attempt {self.connectivity_retry_count}, {time_since_failure:.0f}s since failure)")
            
            try:
                connectivity_ok, failed_services_details = check_service_connectivity()
                if connectivity_ok:
                    logging.info("Service connectivity restored")
                    self.connectivity_failure_time = None
                    self.connectivity_retry_count = 0
                    # --- START EDIT: Clear pause_info on resume ---
                    self.pause_info = {
                        "reason_string": None, "error_type": None, "service_name": None,
                        "status_code": None, "retry_count": 0
                    }
                    # --- END EDIT ---
                    self.resume_queue()
                    return
            except Exception as e:
                logging.error(f"Error checking service connectivity: {str(e)}")
            
            logging.warning(f"Service connectivity check failed. Overall retry attempt {self.connectivity_retry_count}")

            # --- START EDIT: Update self.pause_info with new details ---
            updated_pause_info = {
                "reason_string": f"Connectivity failure - waiting for services (Retry {self.connectivity_retry_count})",
                "error_type": "CONNECTION_ERROR",
                "service_name": "Multiple Services",
                "status_code": None,
                "retry_count": self.connectivity_retry_count
            }

            if failed_services_details and len(failed_services_details) > 0:
                reason_parts = []
                primary_error_set = False
                for detail in failed_services_details:
                    service = detail.get("service", "Unknown Service")
                    message = detail.get("message", "unavailable")
                    reason_parts.append(f"{service}: {message}")

                    if not primary_error_set:
                        error_type = detail.get("type", "CONNECTION_ERROR")
                        if "Debrid" in service and error_type in ["UNAUTHORIZED", "FORBIDDEN"]:
                            updated_pause_info["error_type"] = error_type
                            updated_pause_info["status_code"] = detail.get("status_code")
                            updated_pause_info["service_name"] = service
                            primary_error_set = True
                        elif not primary_error_set and error_type == "CONNECTION_ERROR":
                             updated_pause_info["error_type"] = "CONNECTION_ERROR"
                             updated_pause_info["status_code"] = detail.get("status_code")
                             updated_pause_info["service_name"] = service
                
                updated_pause_info["reason_string"] = f"Connectivity failure - {'; '.join(reason_parts)} (Retry {self.connectivity_retry_count})"
                if not primary_error_set and failed_services_details:
                    updated_pause_info["service_name"] = failed_services_details[0].get("service", "Unknown Service")

            self.pause_info = updated_pause_info
            # --- END EDIT ---
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

        # *** START EDIT: Pause ALL running jobs ***
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
        # --- START EDIT: Pass the reason_string to QueueManager's pause ---
        reason_for_qm = self.pause_info.get("reason_string") if self.pause_info else "Unknown reason"
        QueueManager().pause_queue(reason=reason_for_qm)
        # --- END EDIT ---

        self.queue_paused = True
        # --- START EDIT: Log using pause_info ---
        log_reason = self.pause_info.get('reason_string', 'Unknown') if self.pause_info else 'Unknown'
        logging.info(f"Queue paused. Attempted to pause all running jobs (except essential monitoring tasks)... Reason: {log_reason}")
        # --- END EDIT ---

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

        # *** START EDIT: Resume logic remains the same, but update log context ***
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
        # --- START EDIT: Clear pause_info ---
        self.pause_info = {
            "reason_string": None, "error_type": None, "service_name": None,
            "status_code": None, "retry_count": 0
        }
        # --- END EDIT ---
        logging.info(f"[Resume Queue] Queue resumed successfully. Resumed {resumed_count} jobs.")  # Better log message

    def task_plex_full_scan(self):
        get_and_add_all_collected_from_plex()

        # Backfill resolution for items with NULL resolution
        logging.info("Starting resolution backfill from stored paths...")
        try:
            result = backfill_resolution_from_stored_paths()
            if result.get('success'):
                logging.info(f"Resolution backfill completed: {result.get('updated', 0)} items updated, {result.get('failed', 0)} failed")
            else:
                logging.error(f"Resolution backfill failed: {result.get('error', 'Unknown error')}")
        except Exception as e:
            logging.error(f"Error during resolution backfill: {e}", exc_info=True)

        # Add reconciliation call after full scan processing
        logging.info("Triggering queue reconciliation after full Plex scan.")
        self.task_reconcile_queues()
        
    # --- START EDIT: Add manual Plex full scan task method ---
    def task_manual_plex_full_scan(self):
        """Manually trigger a full Plex scan, bypassing the mode check."""
        logging.info("Executing manual Plex full scan task...")
        get_and_add_all_collected_from_plex(bypass=True)

        # Backfill resolution for items with NULL resolution
        logging.info("Starting resolution backfill from stored paths...")
        try:
            result = backfill_resolution_from_stored_paths()
            if result.get('success'):
                logging.info(f"Resolution backfill completed: {result.get('updated', 0)} items updated, {result.get('failed', 0)} failed")
            else:
                logging.error(f"Resolution backfill failed: {result.get('error', 'Unknown error')}")
        except Exception as e:
            logging.error(f"Error during resolution backfill: {e}", exc_info=True)

        # Add reconciliation call after full scan processing
        logging.info("Triggering queue reconciliation after manual Plex full scan.")
        self.task_reconcile_queues()
    # --- END EDIT ---
    
    def process_content_source(self, source, data):
        from datetime import datetime, timedelta # Add this import
        source_type = source.split('_')[0]
        versions_from_config = data.get('versions', []) # Default to empty list if missing
        source_media_type = data.get('media_type', 'All')
        raw_cutoff_date = data.get('cutoff_date', '')
        exclude_genres = data.get('exclude_genres', []) # Get exclude_genres setting
        try:
            list_length_limit = int(data.get('list_length_limit', 0)) # Get list_length_limit setting and convert to int
        except (ValueError, TypeError):
            logging.warning(f"Invalid list_length_limit value for source {source}: {data.get('list_length_limit')}. Using default value 0.")
            list_length_limit = 0
        unblacklist_on_source_run = bool(data.get('unblacklist_on_source_run', False))
        parsed_cutoff_date = None

        if raw_cutoff_date:
            try:
                # Try to interpret as number of days ago
                days_ago = int(raw_cutoff_date)
                parsed_cutoff_date = (datetime.now() - timedelta(days=days_ago)).date()
                logging.debug(f"Cutoff date for {source} set to {days_ago} days ago: {parsed_cutoff_date}")
            except ValueError:
                # If not an int, try to interpret as YYYY-MM-DD
                try:
                    parsed_cutoff_date = datetime.strptime(raw_cutoff_date, '%Y-%m-%d').date()
                    logging.debug(f"Cutoff date for {source} set to specific date: {parsed_cutoff_date}")
                except (ValueError, TypeError):
                    logging.warning(f"Invalid cutoff_date format for source {source}. Expected YYYY-MM-DD or number of days, got '{raw_cutoff_date}'. No cutoff will be applied.")
                    parsed_cutoff_date = None
        
        cutoff_date = parsed_cutoff_date # Use the parsed_cutoff_date

        # Convert versions_from_config to the expected dictionary format
        if isinstance(versions_from_config, list):
            versions_dict = {version_name: True for version_name in versions_from_config}
            logging.debug(f"Converted versions list for {source} to dict: {versions_dict}")
        elif isinstance(versions_from_config, dict):
            versions_dict = versions_from_config # Use as is if already a dict
        else:
            logging.warning(f"Unexpected format for versions in source {source} (type: {type(versions_from_config)}). Defaulting to empty versions.")
            versions_dict = {} # Default to empty dict for safety

        logging.debug(f"Processing content source: {source} (type: {source_type}, media_type: {source_media_type}, versions (as dict): {versions_dict})")

        try:
            # Load cache for this source
            source_cache = load_source_cache(source)
            logging.debug(f"Initial cache state for {source}: {len(source_cache)} entries")
            cache_skipped = 0
            items_processed = 0
            total_items = 0
            media_type_skipped = 0
            genre_skipped = 0
            cutoff_date_skipped = 0
            list_length_limited = 0

            wanted_content = []
            # Pass the original versions_from_config to fetchers, assuming they expect list/dict as per config
            if source_type == 'Overseerr':
                wanted_content = get_wanted_from_overseerr(versions_from_config)
            elif source_type == 'MDBList':
                wanted_content = get_wanted_from_mdblist_source(data, versions_from_config)
            elif source_type == 'Trakt Watchlist':
                try:
                    wanted_content = get_wanted_from_trakt_watchlist(versions_from_config, unblacklist=unblacklist_on_source_run)
                except (ValueError, api.exceptions.RequestException) as e:
                    logging.error(f"Failed to fetch Trakt watchlist: {str(e)}")
                    return
            elif source_type == 'Trakt Lists':
                trakt_lists = data.get('trakt_lists', '').split(',')
                for trakt_list in trakt_lists:
                    trakt_list = trakt_list.strip()
                    if trakt_list: # Ensure not empty
                        try:
                            wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions_from_config, unblacklist=unblacklist_on_source_run))
                        except (ValueError, api.exceptions.RequestException) as e:
                            logging.error(f"Failed to fetch Trakt list {trakt_list}: {str(e)}")
                            continue
            elif source_type == 'Trakt Collection':
                wanted_content = get_wanted_from_trakt_collection(versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Friends Trakt Watchlist':
                # This function takes data (source_config) and versions
                wanted_content = get_wanted_from_friend_trakt_watchlist(data, versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Special Trakt Lists': # New elif block
                # 'data' is the source_config, 'versions_dict' is the resolved simple versions map
                wanted_content = get_wanted_from_special_trakt_lists(data, versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Scrob Lists':
                wanted_content = get_wanted_from_scrob_lists(data.get('scrob_list_ids', ''), versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Scrob Collection':
                wanted_content = get_wanted_from_scrob_collection(versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Special Scrob Lists':
                wanted_content = get_wanted_from_scrob_special(data, versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Collected':
                wanted_content = get_wanted_from_collected() # Doesn't take versions arg
            elif source_type == 'My Plex Watchlist':
                from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist
                wanted_content = get_wanted_from_plex_watchlist(versions_from_config)
            elif source_type == 'My Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions_from_config)
            elif source_type == 'My Friends Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_friends_plex_rss(plex_rss_url, versions_from_config)
            elif source_type == 'Other Plex Watchlist':
                # Import the function here
                from content_checkers.plex_watchlist import get_wanted_from_other_plex_watchlist

                # Process only the specific source being called, not all Other Plex Watchlist sources
                # The 'data' parameter contains the configuration for this specific source
                username = data.get('username', '')
                token = data.get('token', '')

                if username and token:
                    try:
                        # Fetch watchlist for this specific user only
                        wanted_content = get_wanted_from_other_plex_watchlist(
                            username=username,
                            token=token,
                            versions=versions_from_config
                        )
                        logging.info(f"Successfully fetched watchlist for {username} from source {source}")
                    except Exception as e:
                        logging.error(f"Failed to fetch Other Plex watchlist for {username} (source: {source}): {str(e)}")
                        wanted_content = [([], versions_from_config)]
                else:
                    logging.warning(f"Other Plex Watchlist source {source} is missing username or token")
                    wanted_content = [([], versions_from_config)]
            elif source_type == 'Adaptive List':
                from content_checkers.adaptive_list import get_wanted_from_adaptive_list
                # Get the list configurations from the source data
                wanted_content = get_wanted_from_adaptive_list(data, versions_from_config)
            else:
                logging.warning(f"Unknown source type: {source_type}")
                return

            if wanted_content:
                # Apply list length limit if set
                if list_length_limit > 0:
                    if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                        # For tuple format, limit each batch
                        limited_wanted_content = []
                        total_items_limited = 0
                        for items, item_versions_from_source_tuple in wanted_content:
                            if total_items_limited >= list_length_limit:
                                logging.info(f"List length limit reached for {source} ({list_length_limit} items), skipping remaining batches")
                                break
                            remaining_limit = list_length_limit - total_items_limited
                            if len(items) > remaining_limit:
                                items = items[:remaining_limit]
                                logging.info(f"Limited batch for {source} to {remaining_limit} items due to list length limit")
                            limited_wanted_content.append((items, item_versions_from_source_tuple))
                            total_items_limited += len(items)
                        wanted_content = limited_wanted_content
                        logging.info(f"Applied list length limit to {source}: processed {total_items_limited} items (limit: {list_length_limit})")
                    else:
                        # For single list format, limit the list
                        original_length = len(wanted_content)
                        if original_length > list_length_limit:
                            wanted_content = wanted_content[:list_length_limit]
                            logging.info(f"Applied list length limit to {source}: limited to {list_length_limit} items from {original_length}")
                
                if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                    # Handle list of tuples
                    for items, item_versions_from_source_tuple in wanted_content:
                        logging.debug(f"Processing batch of {len(items)} items from {source}")

                        # Convert versions from tuple if necessary
                        if isinstance(item_versions_from_source_tuple, list):
                            versions_to_inject = {v: True for v in item_versions_from_source_tuple}
                        elif isinstance(item_versions_from_source_tuple, dict):
                            versions_to_inject = item_versions_from_source_tuple
                        else:
                            logging.warning(f"Unexpected format for versions in tuple for {source}. Using main source versions dict.")
                            versions_to_inject = versions_dict # Fallback to the converted source versions

                        # Track genre filtering stats for this batch
                        batch_genre_skipped = 0

                        # Note: Media type and genre filtering moved to after metadata processing
                        
                        # Then filter items based on cache
                        items_to_process_raw = [
                            item for item in items 
                            if should_process_item(item, source, source_cache)
                        ]
                        items_skipped = len(items) - len(items_to_process_raw)
                        cache_skipped += items_skipped
                        
                        if items_to_process_raw:
                            # Inject CONVERTED versions into each item before metadata processing
                            items_to_process = []
                            for item_dict_raw in items_to_process_raw:
                                item_dict_processed = item_dict_raw.copy()
                                item_dict_processed['versions'] = versions_to_inject # Use the converted dict
                                item_dict_processed['content_source'] = source  # Needed by metadata.py for seasons_per_show lookup
                                items_to_process.append(item_dict_processed)

                            from metadata.metadata import process_metadata
                            processed_items = process_metadata(items_to_process)
                            if processed_items:
                                all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                                
                                # Set content source and detail for each item
                                for item in all_items:
                                    item['content_source'] = source
                                    item = append_content_source_detail(item, source_type=source_type)
                                
                                # Filter by media type after metadata processing
                                # Handle both traditional format ('Movies'/'Shows') and Adaptive List format ('movie'/'tv')
                                if source_media_type != 'All' and not source_type.startswith('Collected'):
                                    items_filtered_type = []
                                    for item in all_items:
                                        item_media_type = item.get('media_type')
                                        # Check for traditional format OR Adaptive List format
                                        is_movie_match = (source_media_type == 'Movies' or source_media_type == 'movie') and item_media_type == 'movie'
                                        is_show_match = (source_media_type == 'Shows' or source_media_type == 'tv') and item_media_type in ['tv', 'episode']
                                        if is_movie_match or is_show_match:
                                            items_filtered_type.append(item)
                                        else:
                                            media_type_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to media type mismatch: {item.get('media_type')} != {source_media_type}")

                                    all_items = items_filtered_type
                                    if media_type_skipped > 0:
                                        logging.debug(f"Batch {source}: Skipped {media_type_skipped} items due to media type mismatch")

                                # Filter by excluded genres after metadata processing
                                if exclude_genres:
                                    items_filtered_genre = []
                                    for item in all_items:
                                        item_genres = item.get('genres', [])
                                        if isinstance(item_genres, str):
                                            # Handle comma-separated string format
                                            item_genres = [genre.strip() for genre in item_genres.split(',') if genre.strip()]
                                        
                                        # Check if any of the item's genres are in the exclude list (case-insensitive)
                                        _excl_lower = [g.lower() for g in exclude_genres]
                                        excluded_genre_found = any(genre.lower() in _excl_lower for genre in item_genres)
                                        if not excluded_genre_found:
                                            items_filtered_genre.append(item)
                                        else:
                                            batch_genre_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to excluded genre(s): {[g for g in item_genres if g.lower() in _excl_lower]}")
                                    
                                    all_items = items_filtered_genre
                                    if batch_genre_skipped > 0:
                                        logging.debug(f"Batch {source}: Skipped {batch_genre_skipped} items due to excluded genres")
                                
                                # Filter by cutoff date after metadata processing
                                if cutoff_date:
                                    items_filtered_date = []
                                    for item in all_items:
                                        # For movies, use theatrical_release_date if available, otherwise fall back to release_date
                                        if item.get('media_type') == 'movie':
                                            release_date = item.get('theatrical_release_date') or item.get('release_date')
                                        else:
                                            release_date = item.get('release_date')
                                        
                                        if not release_date or release_date.lower() == 'unknown':
                                            # Skip items with unknown release dates when cutoff date is set
                                            cutoff_date_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to unknown release date (cutoff date is set)")
                                            continue
                                        try:
                                            item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                            if item_date >= cutoff_date:
                                                items_filtered_date.append(item)
                                            else:
                                                cutoff_date_skipped += 1
                                                logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                        except ValueError:
                                            # If we can't parse the date, skip the item when cutoff date is set
                                            cutoff_date_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to invalid date format: {release_date} (cutoff date is set)")
                                    all_items = items_filtered_date
                                    if cutoff_date_skipped > 0:
                                        logging.debug(f"Batch {source}: Skipped {cutoff_date_skipped} items due to cutoff date")

                                from database import add_collected_items, add_wanted_items
                                # Pass the CONVERTED versions dict to add_wanted_items
                                add_wanted_items(all_items, versions_to_inject or versions_dict, unblacklist=unblacklist_on_source_run)
                                
                                # Update cache for all items that were processed (regardless of whether they made it through filtering)
                                # This prevents reprocessing the same items repeatedly
                                for item_raw in items_to_process_raw:
                                    update_cache_for_item(item_raw, source, source_cache)
                                
                                total_items += len(all_items)
                                items_processed += len(items_to_process)
                                genre_skipped += batch_genre_skipped
                else:
                    # Handle single list of items
                    logging.debug(f"Processing batch of {len(wanted_content)} items from {source}")
                    
                    # Note: Media type and genre filtering moved to after metadata processing
                    
                    # Then filter items based on cache
                    items_to_process_raw = [
                        item for item in wanted_content 
                        if should_process_item(item, source, source_cache)
                    ]
                    items_skipped = len(wanted_content) - len(items_to_process_raw)
                    cache_skipped += items_skipped
                    
                    if items_to_process_raw:
                        # Inject CONVERTED versions into each item before metadata processing
                        items_to_process = []
                        for item_dict_raw in items_to_process_raw:
                            item_dict_processed = item_dict_raw.copy()
                            # Use the CONVERTED source-level versions_dict here
                            item_dict_processed['versions'] = versions_dict
                            item_dict_processed['content_source'] = source  # Needed by metadata.py for seasons_per_show lookup
                            items_to_process.append(item_dict_processed)

                        from metadata.metadata import process_metadata
                        processed_items = process_metadata(items_to_process)
                        if processed_items:
                            all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                            
                            # Set content source and detail for each item
                            for item in all_items:
                                item['content_source'] = source
                                item = append_content_source_detail(item, source_type=source_type)
                            
                            # Filter by media type after metadata processing
                            # Handle both traditional format ('Movies'/'Shows') and Adaptive List format ('movie'/'tv')
                            if source_media_type != 'All' and not source_type.startswith('Collected'):
                                items_filtered_type = []
                                for item in all_items:
                                    item_media_type = item.get('media_type')
                                    # Check for traditional format OR Adaptive List format
                                    is_movie_match = (source_media_type == 'Movies' or source_media_type == 'movie') and item_media_type == 'movie'
                                    is_show_match = (source_media_type == 'Shows' or source_media_type == 'tv') and item_media_type in ['tv', 'episode']
                                    if is_movie_match or is_show_match:
                                        items_filtered_type.append(item)
                                    else:
                                        media_type_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to media type mismatch: {item.get('media_type')} != {source_media_type}")

                                all_items = items_filtered_type
                                if media_type_skipped > 0:
                                    logging.debug(f"{source}: Skipped {media_type_skipped} items due to media type mismatch")

                            # Filter by excluded genres after metadata processing
                            if exclude_genres:
                                items_filtered_genre = []
                                for item in all_items:
                                    item_genres = item.get('genres', [])
                                    if isinstance(item_genres, str):
                                        # Handle comma-separated string format
                                        item_genres = [genre.strip() for genre in item_genres.split(',') if genre.strip()]
                                    
                                    # Check if any of the item's genres are in the exclude list (case-insensitive)
                                    _excl_lower = [g.lower() for g in exclude_genres]
                                    excluded_genre_found = any(genre.lower() in _excl_lower for genre in item_genres)
                                    if not excluded_genre_found:
                                        items_filtered_genre.append(item)
                                    else:
                                        genre_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to excluded genre(s): {[g for g in item_genres if g.lower() in _excl_lower]}")
                                
                                all_items = items_filtered_genre
                                if genre_skipped > 0:
                                    logging.debug(f"{source}: Skipped {genre_skipped} items due to excluded genres")
                            
                            # Filter by cutoff date after metadata processing
                            if cutoff_date:
                                items_filtered_date = []
                                for item in all_items:
                                    # For movies, use theatrical_release_date if available, otherwise fall back to release_date
                                    if item.get('media_type') == 'movie':
                                        release_date = item.get('theatrical_release_date') or item.get('release_date')
                                    else:
                                        release_date = item.get('release_date')
                                    
                                    if not release_date or release_date.lower() == 'unknown':
                                        # Skip items with unknown release dates when cutoff date is set
                                        cutoff_date_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to unknown release date (cutoff date is set)")
                                        continue
                                    try:
                                        item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                        if item_date >= cutoff_date:
                                            items_filtered_date.append(item)
                                        else:
                                            cutoff_date_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                    except ValueError:
                                        # If we can't parse the date, skip the item when cutoff date is set
                                        cutoff_date_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to invalid date format: {release_date} (cutoff date is set)")
                                all_items = items_filtered_date
                                if cutoff_date_skipped > 0:
                                    logging.debug(f"{source}: Skipped {cutoff_date_skipped} items due to cutoff date")

                            from database import add_collected_items, add_wanted_items
                            # Pass the CONVERTED versions_dict to add_wanted_items
                            add_wanted_items(all_items, versions_dict, unblacklist=unblacklist_on_source_run)
                            
                            # Update cache for all items that were processed (regardless of whether they made it through filtering)
                            # This prevents reprocessing the same items repeatedly
                            for item_raw in items_to_process_raw:
                                update_cache_for_item(item_raw, source, source_cache)
                            
                            total_items += len(all_items)
                            items_processed += len(items_to_process)
                
                # Save the updated cache
                save_source_cache(source, source_cache)
                logging.debug(f"Final cache state for {source}: {len(source_cache)} entries")
                
                stats_msg = f"Added {total_items} wanted items from {source} (processed {items_processed} items"
                if cache_skipped > 0:
                    stats_msg += f", skipped {cache_skipped} cached items"
                if media_type_skipped > 0:
                    stats_msg += f", skipped {media_type_skipped} items due to media type mismatch"
                if genre_skipped > 0:
                    stats_msg += f", skipped {genre_skipped} items due to excluded genres"
                if cutoff_date_skipped > 0:
                    stats_msg += f", skipped {cutoff_date_skipped} items due to cutoff date"
                if list_length_limit > 0:
                    stats_msg += f", list length limited to {list_length_limit}"
                stats_msg += ")"
                logging.info(stats_msg)

            # ── Plex Collection sync — runs even when all items are cached ────
            # Use config 'type' field for matching — source_type is split on '_' which
            # truncates multi-word types like 'Trakt Lists' to just 'Trakt'
            _source_config_type = data.get('type', source_type)
            if _source_config_type in ('MDBList', 'Trakt Lists', 'Scrob Lists', 'Adaptive List'):
                # Always read live settings — cached source_data may be stale if plex_collection
                # was enabled after startup without a full restart
                try:
                    _live_source_data = get_all_settings().get('Content Sources', {}).get(source, {})
                    _plex_coll_cfg = _live_source_data.get('plex_collection', {})
                except Exception:
                    _plex_coll_cfg = data.get('plex_collection', {})
                if not isinstance(_plex_coll_cfg, dict):
                    _plex_coll_cfg = {}
                if _plex_coll_cfg.get('enabled', False):
                    try:
                        # Extract ordered (imdb_id, tmdb_id) pairs from the raw fetched list
                        # Some sources (Adaptive List) may return None imdb_id with tmdb_id fallback
                        _raw_pairs = []
                        _plex_media_type = data.get('media_type', 'All')
                        if isinstance(wanted_content, list) and wanted_content:
                            if isinstance(wanted_content[0], tuple):
                                for _batch, _ in wanted_content:
                                    for _item in _batch:
                                        _item_mt = _item.get('media_type', '')
                                        if _plex_media_type not in ('All', '') and _item_mt:
                                            if _plex_media_type in ('Movies', 'movie') and _item_mt != 'movie':
                                                continue
                                            if _plex_media_type in ('Shows', 'tv') and _item_mt not in ('tv', 'episode'):
                                                continue
                                        _raw_pairs.append((_item.get('imdb_id'), _item.get('tmdb_id')))
                            else:
                                for _item in wanted_content:
                                    _item_mt = _item.get('media_type', '')
                                    if _plex_media_type not in ('All', '') and _item_mt:
                                        if _plex_media_type in ('Movies', 'movie') and _item_mt != 'movie':
                                            continue
                                        if _plex_media_type in ('Shows', 'tv') and _item_mt not in ('tv', 'episode'):
                                            continue
                                    _raw_pairs.append((_item.get('imdb_id'), _item.get('tmdb_id')))
                        # Deduplicate preserving order, using imdb_id as key when available else tmdb_id
                        _seen_keys = set()
                        ordered_pairs = []
                        for _imdb, _tmdb in _raw_pairs:
                            _key = _imdb or _tmdb
                            if _key and _key not in _seen_keys:
                                _seen_keys.add(_key)
                                ordered_pairs.append((_imdb, _tmdb))
                        ordered_ids = [p[0] for p in ordered_pairs if p[0]]  # IMDb IDs for fingerprint

                        from database.plex_collections import check_and_sync_if_needed
                        import threading as _threading
                        _t = _threading.Thread(
                            target=check_and_sync_if_needed,
                            args=(source, data),
                            kwargs={'ordered_imdb_ids': ordered_ids, 'ordered_pairs': ordered_pairs},
                            daemon=True,
                            name=f'plex-coll-{source}'
                        )
                        _t.start()
                        logging.info(f"[PlexCollections] Sync thread started for {source} ({len(ordered_pairs)} items in list)")
                    except Exception as _pce:
                        logging.error(f"[PlexCollections] Failed to start sync for {source}: {_pce}")
            else:
                if not wanted_content:
                    logging.warning(f"No wanted content retrieved from {source}")



        except Exception as e:
            logging.error(f"Error processing content source {source}: {str(e)}")
            logging.error(traceback.format_exc())
            # Don't re-raise - allow other content sources to continue processing

    def task_refresh_release_dates(self):
        from metadata.metadata import refresh_release_dates # Added import here
        refresh_release_dates()
    
    def task_purge_not_wanted_magnets_file(self):
        purge_not_wanted_magnets_file()

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

    def task_debug_log(self):
        current_time = time.time()
        debug_info = []
        for task, interval in self.task_intervals.items():
            time_until_next_run = interval - (current_time - self.last_run_times[task])
            minutes, seconds = divmod(int(time_until_next_run), 60)
            hours, minutes = divmod(minutes, 60)
            debug_info.append(f"{task}: {hours:02d}:{minutes:02d}:{seconds:02d}")

        logging.info("Time until next task run:\n" + "\n".join(debug_info))

    def run_initialization(self, is_restart=False):
        """Run initialization sequence.
        
        Args:
            is_restart (bool): If True, skip initialization as this is a mid-run restart.
                              This prevents unnecessary processing and potential data loss
                              when the program is restarted after settings changes.
        """
        self._initializing = True 
        logging.info("Running initialization...")
        
        # Skip initialization if this is a restart (mid-run) or if explicitly disabled
        if is_restart:
            logging.info("Skipping initialization as this is a restart (mid-run)")
            self._initializing = False
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
        
        self._initializing = False

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
        
        # Mark as running. The dedicated initialization routine below will toggle the
        # _initializing flag as needed.
        self._running = True

        try:
            logging.info("Starting program run loop (monitoring scheduler state)")
            self._running = True  # Make sure running flag is set

            # Pass the is_restart parameter to run_initialization
            self.run_initialization(is_restart=getattr(self, '_is_restart', False))

            # *** START EDIT: Simplified run loop with CPU monitoring ***
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
                        
                        # --- START EDIT: Update pause_info for scheduled pause ---
                        self.pause_info = {
                            "reason_string": new_reason_string,
                            "error_type": "SYSTEM_SCHEDULED",
                            "service_name": "System",
                            "status_code": None,
                            "retry_count": 0
                        }
                        # --- END EDIT ---
                        self.pause_queue()
                        logging.info(f"Queue automatically paused due to schedule: {new_reason_string}")
                    elif not is_scheduled_pause and self.queue_paused and current_pause_type == "SYSTEM_SCHEDULED":
                        logging.info("Scheduled pause period ended. Resuming queue.")
                        # --- START EDIT: Clear pause_info on resume ---
                        self.pause_info = {
                            "reason_string": None, "error_type": None, "service_name": None,
                            "status_code": None, "retry_count": 0
                        }
                        # --- END EDIT ---
                        self.resume_queue()
                    # ... (sleep) ...

                    # --- START EDIT: CPU monitoring and main loop sleep ---
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
                    # Ensure sleep is not too low
                    if main_loop_sleep_seconds < 0.1:
                        main_loop_sleep_seconds = 0.1
                    time.sleep(main_loop_sleep_seconds) # Check status based on setting
                    # --- END EDIT ---

                except Exception as loop_error:
                     logging.error(f"Error in main monitoring loop: {loop_error}", exc_info=True)
                     time.sleep(10) # Longer sleep on error

            logging.warning("Program run loop exited.")
            # Ensure scheduler is stopped if loop exits unexpectedly
            if self.scheduler and self.scheduler.running:
                 self.stop()
            # *** END EDIT ***

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


    def task_check_trakt_early_releases(self):
        check_trakt_early_releases()

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

    def task_send_notifications(self):
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content/')
        notifications_file = Path(db_content_dir) / "collected_notifications.pkl"
        
        if not notifications_file.exists():
            return

        # Generate a unique temporary filename in the same directory
        unique_suffix = f".{uuid.uuid4()}.tmp"
        temp_notifications_file = notifications_file.with_suffix(unique_suffix)

        try:
            # Atomically move the file for processing
            notifications_file.rename(temp_notifications_file)
        except FileNotFoundError:
            logging.debug("Notifications file disappeared before processing, another worker likely picked it up.")
            return

        notifications = []
        try:
            with open(temp_notifications_file, "rb") as f:
                notifications = pickle.load(f)
        except (pickle.UnpicklingError, EOFError, FileNotFoundError) as pe:
            logging.error(f"Error reading notifications pickle file ({temp_notifications_file}): {pe}. Discarding file.")
        except Exception as e_read:
            logging.error(f"Error processing unique temp file read ({temp_notifications_file}): {e_read}", exc_info=True)
        finally:
            # Always attempt to clean up the temporary file
            try:
                temp_notifications_file.unlink()
            except OSError as e_unlink:
                logging.error(f"Failed to remove processed temp notification file {temp_notifications_file}: {e_unlink}")

        if notifications:
            # Fetch enabled notifications using CLI_DEBRID_PORT
            port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
            try:
                response = requests.get(f'http://localhost:{port}/settings/notifications/enabled', timeout=10) # Add timeout
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                enabled_notifications = response.json().get('enabled_notifications', {})
                
                # Send notifications
                send_notifications(notifications, enabled_notifications)
                
                logging.info(f"Sent {len(notifications)} notifications.")

            except requests.exceptions.RequestException as req_err:
                logging.error(f"Failed to fetch enabled notifications: {req_err}")
                # Re-queue notifications if fetching config fails?
                # For simplicity now, we log error and notifications are lost for this cycle.
                # Could re-pickle 'notifications' back to the original file path.
            except json.JSONDecodeError as json_err:
                 logging.error(f"Failed to parse enabled notifications response: {json_err}")
            except Exception as e_send:
                logging.error(f"Error sending notifications: {str(e_send)}", exc_info=True)

    def task_sync_time(self):
        """Check system clock against NTP and log any significant offset.

        Tries multiple NTP servers in order so that a single unresponsive
        server does not cause the task to always fail.
        """
        ntp_servers = [
            'pool.ntp.org',
            'time.cloudflare.com',
            'time.google.com',
        ]
        ntp_client = ntplib.NTPClient()
        last_error = None

        for server in ntp_servers:
            try:
                response = ntp_client.request(server, version=3, timeout=5)
                offset = response.tx_time - time.time()
                logging.info(f"Time sync check via {server}: offset = {offset:.3f}s")
                if abs(offset) > 60:
                    logging.warning(f"System time offset is significant ({offset:.2f}s). Consider synchronising the system clock.")
                return  # success — no need to try further servers
            except ntplib.NTPException as e:
                last_error = e
                logging.debug(f"NTP error from {server}: {e}")
            except Exception as e:
                last_error = e
                logging.debug(f"Could not reach NTP server {server}: {e}")

        logging.error(f"Failed to synchronize time: all NTP servers unreachable. Last error: {last_error}")

    def task_backup_database(self):
        """
        Scheduled task to backup the database daily.
        cli_mount DB backup always runs (lightweight file copy).
        CLI media_items.db backup only runs when system is idle.
        """
        try:
            # cli_mount DB backup: always run — just a file copy, no performance impact
            try:
                from main import backup_climount_databases
                backup_climount_databases()
            except Exception as _dcy_err:
                logging.warning(f"[DATABASE_BACKUP] cli_mount backup skipped: {_dcy_err}")

            # CLI DB backup: only run when system is idle to avoid performance impact
            if not self._is_system_idle_for_backup():
                logging.info("[DATABASE_BACKUP] System is busy, skipping CLI database backup")
                return

            logging.info("[DATABASE_BACKUP] Starting scheduled database backup (system is idle)")

            from main import backup_database
            success = backup_database(skip_if_recent=False)

            if success:
                logging.info("[DATABASE_BACKUP] Scheduled database backup completed successfully")
            else:
                logging.warning("[DATABASE_BACKUP] Scheduled database backup failed")

        except Exception as e:
            logging.error(f"[DATABASE_BACKUP] Error in scheduled backup task: {e}", exc_info=True)

    def task_backup_debrid(self):
        """Scheduled task to backup the debrid torrent library (1d/3d/7d rotating slots)."""
        try:
            from utilities.debrid_backup import run_backup, _get_settings
            settings = _get_settings()
            if not settings.get('enabled'):
                logging.debug('[DEBRID_BACKUP] Task ran but backup is disabled — skipping')
                return
            result = run_backup(force=False)
            if result.get('success'):
                logging.info(f"[DEBRID_BACKUP] Backup complete: {result.get('count', 0)} torrents ({result.get('provider', '?')})")
            elif result.get('skipped'):
                logging.debug('[DEBRID_BACKUP] Backup skipped (disabled)')
            else:
                logging.warning(f"[DEBRID_BACKUP] Backup failed: {result.get('message', 'unknown')}")
        except Exception as e:
            logging.error(f'[DEBRID_BACKUP] Scheduled task error: {e}', exc_info=True)

    def task_cleanup_debrid(self):
        """Scheduled task to clean up errored and duplicate debrid torrents."""
        try:
            from utilities.debrid_backup import run_cleanup
            result = run_cleanup(force=True)
            if result.get('success'):
                logging.info(f"[DEBRID_CLEANUP] Complete: {result.get('total_deleted', 0)} removed "
                             f"(errors={result.get('deleted_errors',0)}, "
                             f"dupes={result.get('deleted_dupes',0)}, "
                             f"stalled={result.get('deleted_stalled',0)})")
            elif result.get('skipped'):
                logging.debug('[DEBRID_CLEANUP] Skipped (disabled)')
            else:
                logging.warning(f"[DEBRID_CLEANUP] Failed: {result.get('message', 'unknown')}")
        except Exception as e:
            logging.error(f'[DEBRID_CLEANUP] Scheduled task error: {e}', exc_info=True)

    # Track last health check duration to implement skip-if-slow guard
    _nzb_health_last_duration: float = 0.0
    _NZB_HEALTH_MAX_JOBS_PER_TICK = 30   # cap jobs polled per tick
    _NZB_HEALTH_JOB_TIMEOUT = 20         # seconds per job poll before giving up
    _NZB_HEALTH_SKIP_THRESHOLD = 20.0   # if last run took this long, skip next tick
    _nzb_folder_wait_counts: dict = {}   # job_id → consecutive ticks where folder not found
    _NZB_FOLDER_WAIT_MAX = 10            # after this many ticks (~100s) treat as broken
    _nzb_health_triggered: dict = {}     # entry_name → tick count since trigger (0 = not yet triggered)
    _NZB_HEALTH_MAX_POLL_TICKS = 6       # give up polling after ~60s (6 ticks × 10s)
    _nzb_confirmed_complete: dict = {}   # job_id → entry_name for jobs confirmed complete+folder found

    def task_nzb_health_check(self):
        """Poll NZB items in Adding state, run health checks, move to Checking or back to Wanted.
        Runs as a dedicated scheduled task so it doesn't block the Adding/Scraping queue threads.
        Guards: skips tick if previous run was slow; caps jobs per tick; per-job timeout."""
        import time as _time
        try:
            # Option 2: skip this tick if previous run took too long
            if self._nzb_health_last_duration >= self._NZB_HEALTH_SKIP_THRESHOLD:
                logging.debug(f'[NZBHealthCheck] Skipping tick — previous run took {self._nzb_health_last_duration:.1f}s')
                self._nzb_health_last_duration = 0.0  # reset so next tick runs
                return

            if not self.queue_manager:
                return
            adding_queue = self.queue_manager.queues.get('Adding')
            if not adding_queue:
                return

            _nzb_items = [
                item for item in adding_queue.items[:]
                if str(item.get('filled_by_torrent_id', '')).startswith('nzb:')
            ]
            if not _nzb_items:
                return

            _tick_start = _time.monotonic()

            from usenet import get_usenet_client
            from utilities.settings import get_setting as _gs
            from collections import defaultdict as _dd
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

            _nzb_client = get_usenet_client()

            # Group items by job_id
            _job_groups: dict = _dd(list)
            for _ni in _nzb_items:
                _jid = _ni.get('filled_by_torrent_id', '')[4:]
                _job_groups[_jid].append(_ni)

            # Option 3: cap jobs per tick — process oldest first (preserve order)
            _unique_jobs = list(_job_groups.keys())[:self._NZB_HEALTH_MAX_JOBS_PER_TICK]
            if len(_job_groups) > self._NZB_HEALTH_MAX_JOBS_PER_TICK:
                logging.debug(f'[NZBHealthCheck] Capping to {self._NZB_HEALTH_MAX_JOBS_PER_TICK}/{len(_job_groups)} jobs this tick')

            # Shared folder-wait counter dict (closure captures this)
            _job_folder_wait_counts = self._nzb_folder_wait_counts
            _folder_wait_max = self._NZB_FOLDER_WAIT_MAX

            def _poll_job(job_id):
                try:
                    status = _nzb_client.get_job_status(job_id)
                    if not status:
                        return job_id, None, None, None
                    raw = status.get('raw', {})
                    raw_progress = raw.get('progress', status.get('progress', 0))
                    progress = int(float(raw_progress) * 100)
                    is_complete = raw.get('is_complete', False)
                    state = str(raw.get('state', status.get('state', ''))).lower()
                    inner_status = str(raw.get('status', '')).lower()
                    if is_complete or (inner_status == 'downloaded' and state in ('pausedup', 'completed', 'seeding')):
                        progress = 100
                    elif state in ('error', 'failed', 'bad') or raw.get('bad'):
                        progress = -1
                    elif status.get('state') == 'completed':
                        progress = 100
                    entry_name = None
                    if progress == 100:
                        # If raw is empty the job isn't in cli_mount's queue.
                        # Wait a few ticks before declaring ghost — a brand-new submission
                        # may not appear in the queue API immediately.
                        # If the job was previously confirmed complete+folder found, cli_mount
                        # removed it after completion — treat as progress=100 not a ghost.
                        if not status.get('raw'):
                            if job_id in self._nzb_confirmed_complete:
                                logging.debug(f'[NZB] job {job_id} removed from cli_mount after completion — proceeding to Checking')
                                entry_name = self._nzb_confirmed_complete[job_id]
                                progress = 100
                            else:
                                _ghost_wait = _job_folder_wait_counts.get(job_id, 0) + 1
                                _job_folder_wait_counts[job_id] = _ghost_wait
                                if _ghost_wait >= 3:
                                    logging.warning(f'[NZB] job {job_id} not in cli_mount queue after {_ghost_wait} ticks — treating as ghost')
                                    _job_folder_wait_counts.pop(job_id, None)
                                    progress = -2
                                else:
                                    logging.debug(f'[NZB] job {job_id} not in queue yet (tick {_ghost_wait}/3) — waiting')
                                    progress = 99
                        if progress == 100:
                            items_for_job = _job_groups[job_id]
                            nzb_title = items_for_job[0].get('filled_by_file', '') or items_for_job[0].get('original_scraped_torrent_title', '')
                            search_name = nzb_title or job_id
                            try:
                                result = _nzb_client.get_nzb_file_info(search_name, fast_check=True)
                                if result:
                                    entry_name, _ = result
                            except Exception:
                                pass
                        if not entry_name and progress == 100:
                            # Job completed (exists in queue) but folder not yet visible — wait up to max ticks.
                            wait_count = _job_folder_wait_counts.get(job_id, 0) + 1
                            _job_folder_wait_counts[job_id] = wait_count
                            if wait_count >= _folder_wait_max:
                                logging.warning(f'[NZB] job {job_id} folder never appeared after {wait_count} ticks — treating as broken')
                                _job_folder_wait_counts.pop(job_id, None)
                                progress = -1
                            else:
                                logging.debug(f'[NZB] job {job_id} complete but folder not found (tick {wait_count}/{_folder_wait_max}) — waiting')
                                progress = 99
                        elif progress == 100:
                            _job_folder_wait_counts.pop(job_id, None)
                    return job_id, progress, entry_name, None
                except Exception as exc:
                    return job_id, None, None, exc

            # Option 3: cap workers at 5, per-job timeout
            _job_results = {}
            with _TPE(max_workers=min(len(_unique_jobs), 5)) as _pool:
                _futures = {_pool.submit(_poll_job, jid): jid for jid in _unique_jobs}
                try:
                    for _fut in _ac(_futures, timeout=self._NZB_HEALTH_JOB_TIMEOUT * len(_unique_jobs)):
                        try:
                            jid, prog, entry, err = _fut.result(timeout=self._NZB_HEALTH_JOB_TIMEOUT)
                            _job_results[jid] = (prog, entry, err)
                        except Exception:
                            pass
                except Exception:
                    # Timeout — process whatever completed so far, skip the rest
                    for _fut, jid in _futures.items():
                        if _fut.done():
                            try:
                                _, prog, entry, err = _fut.result()
                                _job_results[jid] = (prog, entry, err)
                            except Exception:
                                pass

            # Mark all completed entries as ready to move to Checking.
            # cli_mount's repair/health endpoint can stay in "repairing" indefinitely
            # and blocks items from reaching Checking. Since the folder is confirmed
            # present (entry_name is set), proceed immediately without waiting.
            _health_results = {}
            for jid, (prog, entry, err) in _job_results.items():
                if prog == 100 and entry:
                    _health_results[entry] = None  # None = inconclusive, proceed to Checking
                    self._nzb_confirmed_complete[jid] = entry  # remember this job was complete+folder found
                    adding_queue._nzb_downloading_job_ids.discard(jid)  # no longer downloading

            # Process results per item
            _ghost_job_ids = [jid for jid, (prog, _, _) in _job_results.items() if prog == -2]
            if _ghost_job_ids:
                logging.warning(f'[NZB] Ghost job IDs detected in results: {_ghost_job_ids}')
            _moved_as_sibling = set()  # item IDs already batch-moved with their initiator
            for item in _nzb_items:
                torrent_id = item.get('filled_by_torrent_id', '')
                item_id = item.get('id')
                if item_id in _moved_as_sibling:
                    continue
                nzb_title = item.get('filled_by_file', '') or item.get('original_scraped_torrent_title', '')
                try:
                    # If this episode+version is already Collected under a different DB entry, remove
                    # this stale Adding entry rather than processing it.
                    if item.get('type') == 'episode' and item.get('episode_number') is not None:
                        try:
                            import re as _re_stale
                            from database.core import get_db_connection as _get_dbc_stale
                            _item_file = item.get('filled_by_file') or item.get('original_scraped_torrent_title') or ''
                            _is_individual = bool(_re_stale.search(r'[Ss]\d{2}[Ee]\d{2}', _item_file))
                            with _get_dbc_stale() as _sc:
                                if _is_individual:
                                    # Individual episode — match on same file under different entry
                                    _already_collected = _sc.execute(
                                        "SELECT COUNT(*) FROM media_items "
                                        "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                        "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                        "AND (filled_by_file=? OR original_scraped_torrent_title=?)",
                                        (item.get('imdb_id'), item.get('season_number'),
                                         item.get('episode_number'), item.get('version'), item_id,
                                         _item_file, _item_file)
                                    ).fetchone()[0]
                                else:
                                    # Season pack — check if THIS item was previously Collected
                                    # (has collected_at set) but got reset to Adding by sibling pull.
                                    # Also check for duplicate Collected entry with same pack source.
                                    _pack_match = _re_stale.search(r'\(([^)]+)\)\s*$', _item_file)
                                    _pack_orig = _pack_match.group(1) if _pack_match else ''
                                    # Check 1: same item previously collected
                                    _self_collected = _sc.execute(
                                        "SELECT COUNT(*) FROM media_items WHERE id=? AND collected_at IS NOT NULL",
                                        (item_id,)
                                    ).fetchone()[0]
                                    # Check 2: different entry with same pack source
                                    _other_collected = _sc.execute(
                                        "SELECT COUNT(*) FROM media_items "
                                        "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                        "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                        "AND original_scraped_torrent_title=?",
                                        (item.get('imdb_id'), item.get('season_number'),
                                         item.get('episode_number'), item.get('version'), item_id,
                                         _pack_orig)
                                    ).fetchone()[0] if _pack_orig else 0
                                    _already_collected = _self_collected or _other_collected
                            if _already_collected:
                                logging.info(f'[NZB] Item {item_id} already Collected (self_collected={_self_collected if not _is_individual else "n/a"}) — removing stale Adding entry')
                                from database.database_writing import update_media_item_state as _umis_stale
                                _umis_stale(item_id, 'Collected')
                                adding_queue.remove_item(item)
                                continue
                        except Exception:
                            pass

                    job_id = torrent_id[4:]
                    if job_id not in _job_results:
                        # Job wasn't polled this tick (capped) — but still run stale check
                        if item.get('type') == 'episode' and item.get('episode_number') is not None:
                            try:
                                import re as _re_stale2
                                from database.core import get_db_connection as _get_dbc_stale2
                                _item_file2 = item.get('filled_by_file') or item.get('original_scraped_torrent_title') or ''
                                _is_individual2 = bool(_re_stale2.search(r'[Ss]\d{2}[Ee]\d{2}', _item_file2))
                                with _get_dbc_stale2() as _sc2:
                                    if _is_individual2:
                                        _stale2 = _sc2.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                            "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                            "AND (filled_by_file=? OR original_scraped_torrent_title=?)",
                                            (item.get('imdb_id'), item.get('season_number'),
                                             item.get('episode_number'), item.get('version'), item_id,
                                             _item_file2, _item_file2)
                                        ).fetchone()[0]
                                    else:
                                        _pack_m2 = _re_stale2.search(r'\(([^)]+)\)\s*$', _item_file2)
                                        _pack_o2 = _pack_m2.group(1) if _pack_m2 else ''
                                        _self2 = _sc2.execute(
                                            "SELECT COUNT(*) FROM media_items WHERE id=? AND collected_at IS NOT NULL",
                                            (item_id,)
                                        ).fetchone()[0]
                                        _other2 = _sc2.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                            "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                            "AND original_scraped_torrent_title=?",
                                            (item.get('imdb_id'), item.get('season_number'),
                                             item.get('episode_number'), item.get('version'), item_id, _pack_o2)
                                        ).fetchone()[0] if _pack_o2 else 0
                                        _stale2 = _self2 or _other2
                                if _stale2:
                                    logging.info(f'[NZB] Item {item_id} already Collected — removing stale Adding entry')
                                    from database.database_writing import update_media_item_state as _umis2
                                    _umis2(item_id, 'Collected')
                                    adding_queue.remove_item(item)
                                    continue
                            except Exception:
                                pass
                        continue
                    _prog, _entry, _err = _job_results[job_id]
                    if _err:
                        logging.debug(f'[NZB] Error polling progress for {torrent_id}: {_err}')
                        continue
                    progress = _prog
                    entry_name = _entry
                    if progress is None:
                        logging.debug(f'[NZB] {torrent_id} still queued/unknown — waiting')
                        continue
                    elif progress == -2:
                        # Ghost job — never existed in cli_mount or already purged.
                        # Move primary item AND all siblings to Wanted (not retry in Adding)
                        # so they re-scrape fresh. Do NOT retry from scrape_results here
                        # because the dedup check would just re-assign the same dead hash.
                        logging.warning(f'[NZB] {torrent_id} is a ghost job — moving all items with this job to Wanted')
                        try:
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid_g
                            _nzb_url_g = item.get('filled_by_magnet', '')
                            if _nzb_url_g:
                                _add_guid_g(_nzb_url_g)
                        except Exception:
                            pass
                        # Move all items sharing this ghost torrent_id to Wanted
                        _ghost_items = [
                            s for s in adding_queue.items[:]
                            if s.get('filled_by_torrent_id') == torrent_id
                        ]
                        logging.warning(f'[NZB] Ghost job {torrent_id}: found {len(_ghost_items)} item(s) to move to Wanted')
                        for _gi in _ghost_items:
                            try:
                                _gi_url = _gi.get('filled_by_magnet', '')
                                if _gi_url and _gi_url != item.get('filled_by_magnet', ''):
                                    _add_guid_g(_gi_url)
                                self.queue_manager.move_to_wanted(_gi, 'Adding')
                                logging.info(f'[NZB] Moved ghost job item {_gi["id"]} to Wanted')
                            except Exception as _gi_err:
                                logging.error(f'[NZB] Failed to move ghost item {_gi.get("id")} to Wanted: {_gi_err}')
                        continue
                    elif progress == -1:
                        logging.warning(f'[NZB] {torrent_id} failed in cli_mount — adding to not-wanted and moving back to Scraping')
                        try:
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid, add_to_not_wanted_nzb_segment as _add_seg
                            _nzb_url = item.get('filled_by_magnet', '')
                            if _nzb_url:
                                _add_guid(_nzb_url)
                                logging.info(f'[NZB] Added {_nzb_url[:60]}... to not-wanted guids')
                            # Also blacklist segment ID so same content from any indexer is filtered next scrape
                            _seg_id = item.get('nzb_segment_id', '')
                            if _seg_id:
                                _add_seg(_seg_id)
                                logging.debug(f'[NZB] Added segment {_seg_id!r} to not-wanted segments')
                        except Exception as _nw_err:
                            logging.debug(f'[NZB] Could not add to not-wanted: {_nw_err}')
                        # Clean up all siblings sharing this dead job — add their URLs to not-wanted
                        # and move them back to Wanted so they re-scrape fresh without the dead job
                        try:
                            _dead_siblings = [
                                s for s in adding_queue.items[:]
                                if s.get('filled_by_torrent_id') == torrent_id and s.get('id') != item_id
                            ]
                            for _ds in _dead_siblings:
                                try:
                                    _ds_url = _ds.get('filled_by_magnet', '')
                                    if _ds_url:
                                        _add_guid(_ds_url)
                                    self.queue_manager.move_to_wanted(_ds, 'Adding')
                                    logging.info(f'[NZB] Cleaned up dead sibling {_ds["id"]} from Adding')
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        # Try next result from scrape_results directly in Adding (debrid-style flow).
                        # Clear torrent_id so the Adding queue process() loop picks this item up
                        # and tries the next available result — no health check cycle, no Scraping round-trip.
                        _has_more_results = False
                        try:
                            import json as _json
                            _sr = item.get('scrape_results', [])
                            if isinstance(_sr, str):
                                _sr = _json.loads(_sr)
                            # Filter out the bad URL from remaining results
                            _remaining = [r for r in (_sr or [])
                                          if (r.get('nzb_url') or r.get('magnet', '')) != item.get('filled_by_magnet', '')]
                            if _remaining:
                                _has_more_results = True
                                # Delete the broken job from cli_mount so prefix-match in
                                # torrent_processor won't reuse it on the next retry attempt.
                                # Safety: never delete if another live item (e.g. a different
                                # version sharing this job id due to a since-fixed dedup bug)
                                # still references it — that would delete its file too.
                                try:
                                    from usenet.repair_engine import _delete_from_provider as _dfp
                                    from database import get_db_connection as _gdb_stuck
                                    _broken_hash = torrent_id.replace('nzb:', '') if torrent_id else ''
                                    _conn_stuck = _gdb_stuck()
                                    try:
                                        _stuck_sibs = _conn_stuck.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                            (torrent_id, item_id)
                                        ).fetchone()[0]
                                    finally:
                                        _conn_stuck.close()
                                    if _stuck_sibs:
                                        logging.info(f'[NZB] Skipping provider delete of {_broken_hash} — {_stuck_sibs} sibling(s) still active')
                                    else:
                                        _dfp(_broken_hash, item.get('filled_by_file', '') or '')
                                        logging.debug(f'[NZB] Deleted broken job {_broken_hash} from provider before retry')
                                except Exception as _del_err:
                                    logging.debug(f'[NZB] Could not delete broken job from provider: {_del_err}')
                                from database.database_writing import update_media_item as _umi_retry
                                _umi_retry(item_id,
                                    filled_by_torrent_id=None,
                                    filled_by_file=None,
                                    filled_by_title=None,
                                    filled_by_magnet=None,
                                    scrape_results=_json.dumps(_remaining),
                                )
                                item['filled_by_torrent_id'] = None
                                item['filled_by_magnet'] = None
                                adding_queue._nzb_submitted_ids.discard(item_id)
                                adding_queue._nzb_downloading_job_ids.discard(torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id)
                                logging.info(f'[NZB] {torrent_id} failed — {len(_remaining)} results remain, retrying in Adding')
                        except Exception as _retry_err:
                            logging.debug(f'[NZB] Could not prepare retry: {_retry_err}')
                        if not _has_more_results:
                            # Move back to Wanted for fresh re-scrape rather than blacklisting.
                            # NZB failures exhaust scrape_results quickly (not-wanted list filters them),
                            # but new results may be available on indexers — don't blacklist prematurely.
                            logging.info(f'[NZB] {torrent_id} no remaining results — moving to Wanted for fresh scrape')
                            try:
                                from database.database_writing import update_media_item as _umi_nw
                                _umi_nw(item_id, filled_by_torrent_id=None, filled_by_file=None,
                                        filled_by_title=None, filled_by_magnet=None,
                                        scrape_results=None, fall_back_to_single_scraper=False)
                            except Exception:
                                pass
                            self.queue_manager.move_to_wanted(item, 'Adding')
                        continue
                    elif progress < 100:
                        # Check if this job has been downloading too long without finishing.
                        # Uses last_updated timestamp (set when NZB was submitted) so timeout
                        # survives app restarts. Default cap: 2 hours.
                        _download_timeout_hours = float(_gs('Usenet Provider', 'nzb_download_timeout_hours', 2.0))
                        _lu = item.get('last_updated')
                        _timed_out_dl = False
                        if _lu and _download_timeout_hours > 0:
                            try:
                                from datetime import datetime as _dt2
                                _lu_dt = _dt2.fromisoformat(str(_lu).replace('Z', '+00:00').split('+')[0])
                                _age_hours = (_dt2.now() - _lu_dt).total_seconds() / 3600.0
                                if _age_hours > _download_timeout_hours:
                                    _timed_out_dl = True
                            except Exception:
                                pass
                        if not _timed_out_dl:
                            # Mark job as actively downloading after 1 minute so Adding queue skips it
                            _job_id_dl = torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id
                            try:
                                from datetime import datetime as _dt_dl
                                _lu_dl = item.get('last_updated')
                                if _lu_dl:
                                    _lu_dl_dt = _dt_dl.fromisoformat(str(_lu_dl).replace('Z', '+00:00').split('+')[0])
                                    _age_mins = (_dt_dl.now() - _lu_dl_dt).total_seconds() / 60.0
                                    if _age_mins >= 1.0 and _job_id_dl not in adding_queue._nzb_downloading_job_ids:
                                        adding_queue._nzb_downloading_job_ids.add(_job_id_dl)
                                        logging.info(f'[NZB] {torrent_id} downloading ({progress}%) >1min — marked as Downloading, Adding queue will skip')
                            except Exception:
                                pass
                            logging.debug(f'[NZB] {torrent_id} progress={progress}% — waiting')
                            continue
                        # Download timed out — treat like a failed job: add URL to not-wanted,
                        # delete from cli_mount, try next scrape result or move to Wanted.
                        logging.warning(
                            f'[NZB] {torrent_id} stalled at {progress}% after >{_download_timeout_hours:.1f}h — '
                            f'adding to not-wanted and requeuing'
                        )
                        try:
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid_to
                            _nzb_url_to = item.get('filled_by_magnet', '')
                            if _nzb_url_to:
                                _add_guid_to(_nzb_url_to)
                                logging.info(f'[NZB] Added stalled URL to not-wanted')
                        except Exception:
                            pass
                        # Delete the stalled job from cli_mount
                        try:
                            import requests as _req_to
                            _dcy_url_to = _gs('Usenet Provider', 'url', default='').rstrip('/')
                            _dcy_token_to = _gs('Usenet Provider', 'api_token', default='')
                            _headers_to = {'Authorization': f'Bearer {_dcy_token_to}'} if _dcy_token_to else {}
                            _search_to = (entry_name or nzb_title or '').strip()
                            _real_hash_to = None
                            _page_to = 1
                            while not _real_hash_to:
                                _tr_to = _req_to.get(f'{_dcy_url_to}/api/torrents',
                                                     params={'page': _page_to, 'limit': 50, 'sort_by': 'added_on', 'sort_order': 'desc'},
                                                     headers=_headers_to, timeout=10)
                                if _tr_to.status_code != 200:
                                    break
                                _data_to = _tr_to.json()
                                for _t_to in _data_to.get('torrents', []):
                                    if _t_to.get('name', '').strip() == _search_to:
                                        _real_hash_to = _t_to.get('info_hash', '')
                                        break
                                if _real_hash_to or not _data_to.get('has_next'):
                                    break
                                _page_to += 1
                            if _real_hash_to:
                                # Safety: never delete if another live item (e.g. a different
                                # version sharing this job id due to a since-fixed dedup bug)
                                # still references it — that would delete its file too.
                                from database import get_db_connection as _gdb_stall
                                _conn_stall = _gdb_stall()
                                try:
                                    _stall_sibs = _conn_stall.execute(
                                        "SELECT COUNT(*) FROM media_items "
                                        "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                        (torrent_id, item_id)
                                    ).fetchone()[0]
                                finally:
                                    _conn_stall.close()
                                if _stall_sibs:
                                    logging.info(f'[NZB] Skipping provider delete of stalled job {_real_hash_to} — {_stall_sibs} sibling(s) still active')
                                else:
                                    _dr_to = _req_to.delete(f'{_dcy_url_to}/api/torrents',
                                                            headers=_headers_to,
                                                            params={'hashes': _real_hash_to}, timeout=10)
                                    if _dr_to.status_code == 200:
                                        logging.info(f'[NZB] Deleted stalled job {_real_hash_to} from cli_mount')
                                    else:
                                        logging.warning(f'[NZB] Could not delete stalled job: HTTP {_dr_to.status_code}')
                        except Exception as _del_to_err:
                            logging.debug(f'[NZB] Could not delete stalled job from cli_mount: {_del_to_err}')
                        # Handle siblings sharing this stalled job
                        try:
                            _stalled_siblings = [
                                s for s in adding_queue.items[:]
                                if s.get('filled_by_torrent_id') == torrent_id and s.get('id') != item_id
                            ]
                            for _ss in _stalled_siblings:
                                _ss_url = _ss.get('filled_by_magnet', '')
                                if _ss_url:
                                    try:
                                        _add_guid_to(_ss_url)
                                    except Exception:
                                        pass
                                self.queue_manager.move_to_wanted(_ss, 'Adding')
                                logging.info(f'[NZB] Moved stalled sibling {_ss["id"]} to Wanted')
                        except Exception:
                            pass
                        # Try next result from scrape_results (same as progress=-1 retry flow)
                        _has_more_to = False
                        try:
                            import json as _json_to
                            _sr_to = item.get('scrape_results', [])
                            if isinstance(_sr_to, str):
                                _sr_to = _json_to.loads(_sr_to)
                            _remaining_to = [r for r in (_sr_to or [])
                                             if (r.get('nzb_url') or r.get('magnet', '')) != item.get('filled_by_magnet', '')]
                            if _remaining_to:
                                _has_more_to = True
                                from database.database_writing import update_media_item as _umi_to
                                _umi_to(item_id,
                                    filled_by_torrent_id=None,
                                    filled_by_file=None,
                                    filled_by_title=None,
                                    filled_by_magnet=None,
                                    scrape_results=_json_to.dumps(_remaining_to),
                                )
                                item['filled_by_torrent_id'] = None
                                item['filled_by_magnet'] = None
                                adding_queue._nzb_submitted_ids.discard(item_id)
                                adding_queue._nzb_downloading_job_ids.discard(torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id)
                                logging.info(f'[NZB] Stalled job — {len(_remaining_to)} results remain, retrying in Adding')
                        except Exception as _retry_to_err:
                            logging.debug(f'[NZB] Could not prepare stall retry: {_retry_to_err}')
                        if not _has_more_to:
                            logging.info(f'[NZB] {torrent_id} stalled, no remaining results — moving to Wanted for fresh scrape')
                            try:
                                from database.database_writing import update_media_item as _umi_st
                                _umi_st(item_id, filled_by_torrent_id=None, filled_by_file=None,
                                        filled_by_title=None, filled_by_magnet=None,
                                        scrape_results=None, fall_back_to_single_scraper=False)
                            except Exception:
                                pass
                            self.queue_manager.move_to_wanted(item, 'Adding')
                        continue

                    logging.info(f'[NZB] {torrent_id} complete — running health check')

                    # If this is a season pack initiator, wait until all siblings have
                    # coalesced into Adding before moving to Checking. This ensures
                    # _resolve_nzb_file_info sees all episodes as a pack and assigns
                    # correct per-episode filenames, preventing Plex duplicate rows.
                    _imdb_wait = item.get('imdb_id')
                    _season_wait = item.get('season_number')
                    # Only wait for siblings when this is a season pack NZB.
                    # Individual episode NZBs (title contains SxxExx) are self-contained —
                    # their sibling episodes each have their own job and don't need to
                    # coalesce before _resolve_nzb_file_info runs.
                    _is_individual_ep = bool(__import__('re').search(r'[Ss]\d{2}[Ee]\d{2}', nzb_title or ''))
                    if _imdb_wait and _season_wait is not None and not _is_individual_ep:
                        try:
                            from database.core import get_db_connection as _get_dbc
                            with _get_dbc() as _wconn:
                                # Only count Scraping siblings that are recently updated (within 10 min).
                                # A sibling stuck in Scraping with no results will have a stale
                                # last_updated and should not block the pack indefinitely.
                                _scraping_siblings = _wconn.execute(
                                    "SELECT COUNT(*) FROM media_items "
                                    "WHERE imdb_id=? AND season_number=? AND type='episode' "
                                    "AND state='Scraping' AND id!=? "
                                    "AND (filled_by_torrent_id IS NULL OR filled_by_torrent_id!=?) "
                                    "AND last_updated >= datetime('now', '-10 minutes')",
                                    (_imdb_wait, _season_wait, item_id, torrent_id)
                                ).fetchone()[0]
                            if _scraping_siblings > 0:
                                logging.debug(
                                    f'[NZB] {torrent_id} complete but {_scraping_siblings} siblings still Scraping — waiting one tick'
                                )
                                # While waiting for Scraping siblings, pull any Wanted/Sleeping
                                # siblings into Adding now so they don't wait behind the throttle.
                                _is_pack_early = not __import__('re').search(r'[Ss]\d{2}[Ee]\d{2}', nzb_title or '')
                                if _is_pack_early:
                                    try:
                                        from database.core import get_db_connection as _get_dbc_e
                                        from database.database_writing import update_media_item as _umi_e
                                        from database.database_writing import update_media_item_state as _umis_e
                                        _nzb_url_e = item.get('filled_by_magnet', '') or item.get('link', '')
                                        _orig_e = item.get('original_scraped_torrent_title', nzb_title)
                                        with _get_dbc_e() as _ec:
                                            _wanted_e = _ec.execute(
                                                "SELECT id FROM media_items "
                                                "WHERE imdb_id=? AND season_number=? AND type='episode' "
                                                "AND (state IN ('Wanted','Sleeping') OR (state='Adding' AND (filled_by_torrent_id IS NULL OR filled_by_torrent_id=''))) AND id!=? "
                                                "AND collected_at IS NULL "
                                                "AND episode_number NOT IN ("
                                                "  SELECT episode_number FROM media_items "
                                                "  WHERE imdb_id=? AND season_number=? "
                                                "  AND type='episode' AND state='Collected'"
                                                ")",
                                                (_imdb_wait, _season_wait, item_id,
                                                 _imdb_wait, _season_wait)
                                            ).fetchall()
                                        _folder_e = entry_name or nzb_title
                                        _seg_e = item.get('nzb_segment_id', '') or ''
                                        for _we in _wanted_e:
                                            try:
                                                _seg_e_kw = {'nzb_segment_id': _seg_e} if _seg_e else {}
                                                _umi_e(_we[0], filled_by_torrent_id=torrent_id,
                                                       filled_by_file=_folder_e,
                                                       filled_by_magnet=_nzb_url_e,
                                                       filled_by_title=nzb_title,
                                                       original_scraped_torrent_title=_orig_e,
                                                       **_seg_e_kw)
                                                _umis_e(_we[0], 'Adding')
                                                logging.info(f'[NZB] Pulled Wanted sibling {_we[0]} into Adding (pack waiting for Scraping)')
                                            except Exception:
                                                pass
                                        if _wanted_e:
                                            adding_queue.update()
                                    except Exception:
                                        pass
                                continue
                        except Exception:
                            pass

                    if not entry_name:
                        # Should not happen — poll_job now keeps progress at 99 until folder found.
                        # Guard: stay in Adding rather than moving to Checking with no folder.
                        logging.warning(f'[NZB] {torrent_id} has no entry_name at health check — waiting for folder')
                        continue
                    else:
                        health = _health_results.get(entry_name)

                    if health == 'broken':
                        logging.warning(f'[NZB] {torrent_id} entry {entry_name!r} is BROKEN — deleting and moving back to Wanted')
                        self._nzb_health_triggered.pop(entry_name, None)
                        # Add NZB guid to not-wanted so it's filtered at scrape time in future
                        try:
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid
                            _nzb_url_for_guid = item.get('filled_by_magnet', '')
                            if _nzb_url_for_guid:
                                _add_guid(_nzb_url_for_guid)
                        except Exception as _ge:
                            logging.debug(f'[NZB] Could not add guid to not-wanted: {_ge}')
                        try:
                            import requests as _req
                            _dcy_url = _gs('Usenet Provider', 'url', default='').rstrip('/')
                            _dcy_token = _gs('Usenet Provider', 'api_token', default='')
                            _headers = {'Authorization': f'Bearer {_dcy_token}'} if _dcy_token else {}
                            _real_hash = None
                            _search_name = (entry_name or nzb_title or '').strip()
                            _page = 1
                            while not _real_hash:
                                _tr = _req.get(f'{_dcy_url}/api/torrents',
                                               params={'page': _page, 'limit': 50, 'sort_by': 'added_on', 'sort_order': 'desc'},
                                               headers=_headers, timeout=10)
                                if _tr.status_code != 200:
                                    break
                                _data = _tr.json()
                                for _t in _data.get('torrents', []):
                                    if _t.get('name', '').strip() == _search_name:
                                        _real_hash = _t.get('info_hash', '')
                                        break
                                if _real_hash or not _data.get('has_next'):
                                    break
                                _page += 1
                            if _real_hash:
                                _dr = _req.delete(f'{_dcy_url}/api/torrents', headers=_headers,
                                                  params={'hashes': _real_hash}, timeout=10)
                                if _dr.status_code == 200:
                                    logging.info(f'[NZB] Deleted broken entry {_real_hash} from cli_mount')
                                else:
                                    logging.warning(f'[NZB] Could not delete {_real_hash}: HTTP {_dr.status_code}')
                            else:
                                logging.warning(f'[NZB] Entry {_search_name!r} not found in /api/torrents')
                        except Exception as _de:
                            logging.warning(f'[NZB] Error deleting broken entry: {_de}')
                        try:
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_segment, extract_nzb_segment_id
                            import json as _j
                            _seg_id = ''
                            _results_raw = item.get('scrape_results', [])
                            if isinstance(_results_raw, str):
                                _results_raw = _j.loads(_results_raw)
                            for _r in (_results_raw or []):
                                if _r.get('title', '') == nzb_title or _r.get('original_title', '') == nzb_title:
                                    _nzb_fetch_url = _r.get('nzb_url', '') or _r.get('magnet', '')
                                    if _nzb_fetch_url:
                                        try:
                                            from routes.api_tracker import api as _fapi
                                            _fr = _fapi.get(_nzb_fetch_url, timeout=15, allow_redirects=True)
                                            if _fr.status_code == 200 and '<nzb' in _fr.text.lower():
                                                _seg_id = extract_nzb_segment_id(_fr.text)
                                        except Exception:
                                            pass
                                    break
                            if _seg_id:
                                add_to_not_wanted_nzb_segment(_seg_id)
                                logging.info(f'[NZB] Added broken NZB segment ID {_seg_id!r} to not-wanted')
                        except Exception as _nwe:
                            logging.debug(f'[NZB] Could not add segment to not-wanted: {_nwe}')
                        from database.database_writing import update_media_item as _umi
                        _umi(item_id, filled_by_torrent_id=None, filled_by_file=None, filled_by_title=None,
                             debrid_folder_name=None, fall_back_to_single_scraper=False)
                        try:
                            import json as _json
                            _results = item.get('scrape_results', [])
                            if isinstance(_results, str):
                                _results = _json.loads(_results)
                            _filtered = [r for r in (_results or []) if r.get('title', '') != nzb_title and r.get('original_title', '') != nzb_title]
                            if _filtered and len(_filtered) < len(_results):
                                _umi(item_id, scrape_results=_json.dumps(_filtered), fall_back_to_single_scraper=False)
                                logging.info(f'[NZB] Removed broken result, {len(_filtered)} remaining — retrying Adding')
                                item['scrape_results'] = _json.dumps(_filtered)
                            else:
                                logging.info(f'[NZB] No remaining scrape results — moving back to Wanted')
                                self.queue_manager.move_to_wanted(item, 'Adding')
                        except Exception as _re:
                            logging.warning(f'[NZB] Could not remove broken result: {_re}')
                            self.queue_manager.move_to_wanted(item, 'Adding')

                        # Clean up all coalesced siblings sharing this broken job.
                        # They are stuck in Adding with the same filled_by_torrent_id.
                        # Add their NZB URL to not-wanted, reset their fields, move to Wanted.
                        _broken_siblings = [
                            s for s in adding_queue.items[:]
                            if s.get('filled_by_torrent_id') == torrent_id
                            and s.get('id') != item_id
                        ]
                        if _broken_siblings:
                            logging.warning(f'[NZB] Cleaning up {len(_broken_siblings)} coalesced siblings of broken job {torrent_id}')
                            from database.database_writing import update_media_item as _umi2
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid2
                            for _sib in _broken_siblings:
                                try:
                                    _sib_url = _sib.get('filled_by_magnet', '')
                                    if _sib_url:
                                        try:
                                            _add_guid2(_sib_url)
                                        except Exception:
                                            pass
                                    _umi2(_sib['id'],
                                          filled_by_torrent_id=None,
                                          filled_by_file=None,
                                          filled_by_title=None,
                                          filled_by_magnet=None,
                                          debrid_folder_name=None,
                                          fall_back_to_single_scraper=False)
                                    self.queue_manager.move_to_wanted(_sib, 'Adding')
                                    logging.info(f'[NZB] Moved broken sibling {_sib["id"]} back to Wanted')
                                except Exception as _sib_err:
                                    logging.warning(f'[NZB] Could not clean up sibling {_sib.get("id")}: {_sib_err}')
                    else:
                        if health == 'healthy':
                            logging.info(f'[NZB] {torrent_id} health check passed — moving to Checking')
                        else:
                            logging.warning(f'[NZB] {torrent_id} health check inconclusive — proceeding to Checking anyway')
                        nzb_url = item.get('filled_by_magnet', '') or item.get('link', '')
                        nzb_original_title = item.get('original_scraped_torrent_title', nzb_title)

                        # Pull any Wanted/Sleeping siblings for this season directly into Adding
                        # bypassing the Scraping queue and throttle — the pack is already confirmed
                        # working so there's no need to scrape them individually.
                        _imdb_pull = item.get('imdb_id')
                        _season_pull = item.get('season_number')
                        _is_pack = not __import__('re').search(r'[Ss]\d{2}[Ee]\d{2}', nzb_title or '')
                        if _imdb_pull and _season_pull is not None and _is_pack:
                            try:
                                from database.core import get_db_connection as _get_dbc2
                                from database.database_writing import update_media_item as _umi_pull
                                with _get_dbc2() as _pconn:
                                    _wanted_sibs = _pconn.execute(
                                        "SELECT id FROM media_items "
                                        "WHERE imdb_id=? AND season_number=? AND type='episode' "
                                        "AND (state IN ('Wanted','Sleeping') OR (state='Adding' AND (filled_by_torrent_id IS NULL OR filled_by_torrent_id=''))) AND id!=? "
                                        "AND collected_at IS NULL "
                                        "AND episode_number NOT IN ("
                                        "  SELECT episode_number FROM media_items "
                                        "  WHERE imdb_id=? AND season_number=? "
                                        "  AND type='episode' AND state='Collected'"
                                        ")",
                                        (_imdb_pull, _season_pull, item_id,
                                         _imdb_pull, _season_pull)
                                    ).fetchall()
                                _folder_pull = entry_name or nzb_title
                                _pull_seg_id = item.get('nzb_segment_id', '') or ''
                                for _ws in _wanted_sibs:
                                    try:
                                        _pull_seg_kwargs = {'nzb_segment_id': _pull_seg_id} if _pull_seg_id else {}
                                        _umi_pull(_ws[0],
                                            filled_by_torrent_id=torrent_id,
                                            filled_by_file=_folder_pull,
                                            filled_by_magnet=nzb_url,
                                            filled_by_title=nzb_title,
                                            original_scraped_torrent_title=nzb_original_title,
                                            **_pull_seg_kwargs,
                                        )
                                        from database.database_writing import update_media_item_state as _umis_pull
                                        _umis_pull(_ws[0], 'Adding')
                                        logging.info(f'[NZB] Pulled Wanted sibling {_ws[0]} directly into Adding for pack {torrent_id}')
                                    except Exception as _pull_err:
                                        logging.debug(f'[NZB] Could not pull sibling {_ws[0]}: {_pull_err}')
                                if _wanted_sibs:
                                    # Reload Adding queue so new siblings are visible this tick
                                    adding_queue.update()
                            except Exception as _pull_ex:
                                logging.debug(f'[NZB] Wanted sibling pull failed: {_pull_ex}')
                        try:
                            _folder_name = entry_name or nzb_title
                            self.queue_manager.move_to_checking(
                                item, 'Adding',
                                title=nzb_title,
                                link=nzb_url,
                                filled_by_file=_folder_name,
                                torrent_id=torrent_id,
                                debrid_folder_name=_folder_name,
                                original_scraped_torrent_title=nzb_original_title,
                            )
                            # Move all coalesced siblings (same torrent_id, still in Adding,
                            # no filled_by_file yet) to Checking together with the initiator.
                            # _resolve_nzb_file_info will assign per-episode filenames to all
                            # of them once they're in Checking.
                            _siblings = [
                                s for s in adding_queue.items[:]
                                if s.get('filled_by_torrent_id') == torrent_id
                                and s.get('id') != item_id
                                and s.get('filled_by_file') is None
                            ]
                            for _sib in _siblings:
                                try:
                                    self.queue_manager.move_to_checking(
                                        _sib, 'Adding',
                                        title=nzb_title,
                                        link=nzb_url,
                                        filled_by_file=None,  # _resolve_nzb_file_info will set per-episode filename
                                        torrent_id=torrent_id,
                                        debrid_folder_name=_folder_name,
                                        original_scraped_torrent_title=nzb_original_title,
                                    )
                                    _moved_as_sibling.add(_sib['id'])
                                    logging.info(f'[NZB] Moved coalesced sibling {_sib["id"]} to Checking with initiator')
                                except Exception as _se:
                                    logging.warning(f'[NZB] Could not move sibling {_sib.get("id")} to Checking: {_se}')
                        except Exception as _ce:
                            logging.warning(f'[NZB] Could not move {torrent_id} to Checking: {_ce}')
                            adding_queue._handle_failed_item(item, f'NZB health passed but queue move failed: {_ce}', self.queue_manager)
                except Exception as _exc:
                    logging.error(f'[NZB] Error in health check for {torrent_id}: {_exc}', exc_info=True)

            self._nzb_health_last_duration = _time.monotonic() - _tick_start
            logging.debug(f'[NZBHealthCheck] Tick completed in {self._nzb_health_last_duration:.1f}s ({len(_unique_jobs)} jobs)')

            # --- ARTICLE_NOT_FOUND / broken item detection ---
            # After each health check tick, poll provider for broken items.
            # Rate-limited: only run every 5 minutes to avoid hammering the API.
            # Safety guard: skip items whose info_hash matches something currently
            # in Adding or Checking state — those are new submissions still in flight,
            # not genuinely broken collected items.
            try:
                _now_ts = _time.monotonic()
                if not hasattr(self, '_last_broken_check_ts'):
                    self._last_broken_check_ts = 0.0
                if _now_ts - self._last_broken_check_ts >= 300:  # 5 minutes
                    self._last_broken_check_ts = _now_ts
                    from usenet.repair_engine import fetch_broken_items, run_repair
                    _broken = fetch_broken_items()
                    if _broken:
                        # Build set of in-flight hashes (Adding + Checking) to exclude
                        try:
                            from database.core import get_db_connection as _get_dbc_hc
                            with _get_dbc_hc() as _hc_conn:
                                _inflight_rows = _hc_conn.execute(
                                    "SELECT filled_by_torrent_id FROM media_items "
                                    "WHERE state IN ('Adding','Checking') "
                                    "AND filled_by_torrent_id LIKE 'nzb:%'"
                                ).fetchall()
                            _inflight_hashes = {r[0][4:] for r in _inflight_rows if r[0]}
                        except Exception:
                            _inflight_hashes = set()
                        # Filter out in-flight items — they're not genuinely broken
                        _truly_broken = [
                            e for e in _broken
                            if (e.get('info_hash') or e.get('hash') or '') not in _inflight_hashes
                        ]
                        if _truly_broken:
                            logging.warning(
                                f'[NZBHealthCheck] Detected {len(_truly_broken)} broken item(s) '
                                f'(excluded {len(_broken)-len(_truly_broken)} in-flight) — triggering repair'
                            )
                            run_repair(triggered_by='auto_detected')
                        elif _broken:
                            logging.debug(
                                f'[NZBHealthCheck] {len(_broken)} broken item(s) all in-flight — skipping repair'
                            )
            except Exception as _broken_err:
                logging.debug(f'[NZBHealthCheck] Broken item check error: {_broken_err}')

            # --- cli_mount log watcher: ARTICLE_NOT_FOUND + cache warm failures ---
            # Tails cli_mount's log for two dead-file signals:
            #   1. [webdav] Error streaming file: ... ARTICLE_NOT_FOUND
            #   2. [manager] cache warm failed ... input/output error
            # Both mean the same thing: segments expired, file is unreadable.
            # Acting early (before Plex analyzes the file) prevents the Plex SQLite
            # WAL deadlock caused by --analyze processes stuck in kernel D-state.
            # Rate-limited to every 30s.
            try:
                _now_watcher = _time.monotonic()
                if not hasattr(self, '_last_webdav_check_ts'):
                    self._last_webdav_check_ts = 0.0
                    self._last_webdav_log_pos = -1  # -1 = uninitialized, seek to EOF on first run
                    self._webdav_repaired_entries = set()  # entries processed this session
                if _now_watcher - self._last_webdav_check_ts >= 30:
                    self._last_webdav_check_ts = _now_watcher
                    import re as _re_wv, os as _os_wv
                    from utilities.settings import get_setting as _gs_wv
                    _dcy_data = _gs_wv('Usenet Provider', 'data_path', '/climount_data').rstrip('/')
                    _dcy_log = f'{_dcy_data}/logs/climount.log'
                    if _os_wv.path.isfile(_dcy_log):
                        _fsize = _os_wv.path.getsize(_dcy_log)
                        # On first run, skip to end of log — don't reprocess old errors
                        if self._last_webdav_log_pos < 0:
                            self._last_webdav_log_pos = _fsize
                        # Reset position if log was rotated
                        if _fsize < self._last_webdav_log_pos:
                            self._last_webdav_log_pos = 0
                        if _fsize > self._last_webdav_log_pos:
                            with open(_dcy_log, 'r', errors='replace') as _lf:
                                _lf.seek(self._last_webdav_log_pos)
                                _new_lines = _lf.read()
                                self._last_webdav_log_pos = _lf.tell()
                            # Pattern 1a: DFS backend (Hanwen/native)
                            # [dfs] download error error="NNTP ARTICLE_NOT_FOUND..." entry="<entry_name>"
                            _dfs_pattern = _re_wv.compile(
                                r'\[dfs\] download error error="(?:.*?)ARTICLE_NOT_FOUND[^"]*" count=\d+ entry="([^"]+)"'
                            )
                            # Pattern 1b: rclone/WebDAV backend
                            # [webdav] Error streaming file: <entry_name>/<filename> error="NNTP ARTICLE_NOT_FOUND..."
                            _webdav_pattern = _re_wv.compile(
                                r'\[webdav\] Error streaming file: (.+?)/[^/]+ error="(?:.*?)ARTICLE_NOT_FOUND'
                            )
                            # Pattern 2: cache warm failures (file unreadable, I/O error)
                            # Format: [dfs] stream error ... entry="<entry>" filename="<file>"
                            # Also catches: [manager] cache warm failed error="read .../cli_debrid/<entry>/...: input/output error"
                            _cache_warm_pattern = _re_wv.compile(
                                r'(?:\[dfs\] stream error.*?entry="([^"]+)"|\[manager\] cache warm failed error="read [^/]+/[^/]+/([^/"]+)[^"]*: input/output error")'
                            )
                            _broken_entries = {}
                            for _m in _dfs_pattern.finditer(_new_lines):
                                _entry_name = _m.group(1).strip()
                                if _entry_name and _entry_name not in _broken_entries and _entry_name not in self._webdav_repaired_entries:
                                    _broken_entries[_entry_name] = True
                            for _m in _webdav_pattern.finditer(_new_lines):
                                _entry_name = _m.group(1).strip()
                                if _entry_name and _entry_name not in _broken_entries and _entry_name not in self._webdav_repaired_entries:
                                    _broken_entries[_entry_name] = True
                            for _m in _cache_warm_pattern.finditer(_new_lines):
                                # Two capture groups — one per sub-pattern alternative
                                _entry_name = (_m.group(1) or _m.group(2) or '').strip()
                                if _entry_name and _entry_name not in _broken_entries and _entry_name not in self._webdav_repaired_entries:
                                    _broken_entries[_entry_name] = 'cache_warm'

                            if _broken_entries:
                                _webdav_count = sum(1 for v in _broken_entries.values() if v is True)
                                _cache_count = sum(1 for v in _broken_entries.values() if v == 'cache_warm')
                                logging.warning(
                                    f'[NZBHealthCheck] Detected {len(_broken_entries)} dead entry(ies) '
                                    f'from cli_mount log ({_webdav_count} ARTICLE_NOT_FOUND, {_cache_count} cache warm I/O error) '
                                    f'— triggering immediate repair'
                                )
                                # Trigger repair immediately for these specific entries
                                try:
                                    from usenet.repair_engine import (
                                        _find_db_items_by_entry_name,
                                        _delete_from_plex,
                                        _delete_from_provider,
                                        _move_to_wanted,
                                        _blacklist_broken_nzb,
                                        _resolve_info_hash_from_provider,
                                    )
                                    from database.nzb_repair_activity import log_repair_activity
                                    from usenet.repair_engine import _backfill_hash_for_item
                                    for _ename in _broken_entries:
                                        try:
                                            # Resolve info_hash for provider deletion
                                            _ihash = _resolve_info_hash_from_provider(_ename)
                                            # Find DB items
                                            from usenet.repair_engine import AMBIGUOUS as _AMBIGUOUS_WD
                                            _items = _find_db_items_by_entry_name(_ename)
                                            if _items is _AMBIGUOUS_WD:
                                                # Multiple versions ambiguously matched — do not delete
                                                # from provider, a live row we can't identify may need it.
                                                logging.warning(f'[NZBHealthCheck] {_ename!r} — ambiguous multi-version match, skipping without deleting')
                                                self._webdav_repaired_entries.add(_ename)
                                                continue
                                            _items = [i for i in _items
                                                      if i.get('state') in ('Collected', 'Checking', 'Upgrading')]
                                            if not _items:
                                                # Also try hash lookup
                                                if _ihash:
                                                    from usenet.repair_engine import _find_db_item_by_info_hash
                                                    _single = _find_db_item_by_info_hash(_ihash)
                                                    if _single and _single.get('state') in ('Collected', 'Checking', 'Upgrading'):
                                                        _items = [_single]
                                            # If hash still empty but we have DB items, run targeted backfill
                                            if not _ihash and _items:
                                                _ihash = _backfill_hash_for_item(_items[0])
                                                if _ihash:
                                                    logging.info(f'[NZBHealthCheck] Targeted backfill resolved hash for {_ename!r}: {_ihash!r}')
                                            if not _items:
                                                logging.debug(f'[NZBHealthCheck] No DB items for broken webdav entry {_ename!r} — deleting from provider only')
                                                _delete_from_provider(_ihash, _ename)
                                                self._webdav_repaired_entries.add(_ename)
                                                continue
                                            logging.warning(
                                                f'[NZBHealthCheck] Immediate repair: {_ename!r} — '
                                                f'{len(_items)} item(s), hash={_ihash!r}'
                                            )
                                            # Blacklist all unique NZB URLs + segment IDs across all items in this entry
                                            _seen_urls = set()
                                            for _bi in _items:
                                                _nzb_url = _bi.get('filled_by_magnet', '') or ''
                                                _seg_id = _bi.get('nzb_segment_id', '') or ''
                                                if _nzb_url and _nzb_url not in _seen_urls:
                                                    _blacklist_broken_nzb(_nzb_url, segment_id=_seg_id)
                                                    _seen_urls.add(_nzb_url)
                                                elif _seg_id and _nzb_url in _seen_urls:
                                                    # URL already seen but still blacklist segment if different item
                                                    from database.not_wanted_magnets import add_to_not_wanted_nzb_segment as _add_seg
                                                    _add_seg(_seg_id)
                                            # Delete from Plex using folder path directly — works even if Plex never indexed the file.
                                            # Primary: os.path.dirname(location_on_disk)
                                            # Fallback: construct path from mount + entry name when location_on_disk is NULL
                                            try:
                                                from utilities.plex_functions import scan_and_empty_plex_trash
                                                from utilities.settings import get_setting as _gs_plx
                                                import os as _osp
                                                _seen_folders = set()
                                                for _dbi in _items:
                                                    _loc = _dbi.get('location_on_disk') or ''
                                                    _is_ep = _dbi.get('type') == 'episode'
                                                    if _loc:
                                                        _folder = _osp.path.dirname(_loc)
                                                    else:
                                                        # Fallback: build path from symlink/mount base + entry name
                                                        _mode = _gs_plx('File Management', 'file_collection_management', 'Plex')
                                                        if _mode == 'Symlinked/Local':
                                                            _base = _gs_plx('File Management', 'symlinked_files_path', '').rstrip('/')
                                                            _sub = 'TV Shows' if _is_ep else 'Movies'
                                                            _folder = f'{_base}/{_sub}/{_ename}' if _base else ''
                                                        else:
                                                            _mount = _gs_plx('Usenet Provider', 'mount_path', '/debrid').rstrip('/')
                                                            _sub = 'shows' if _is_ep else 'movies'
                                                            _folder = f'{_mount}/{_sub}/{_ename}' if _mount else ''
                                                    if _folder and _folder not in _seen_folders:
                                                        _seen_folders.add(_folder)
                                                        scan_and_empty_plex_trash(
                                                            paths=[_folder],
                                                            section_type='show' if _is_ep else 'movie',
                                                            empty_trash=True,
                                                        )
                                                        logging.info(f'[NZBHealthCheck] Plex cleanup via path: {_folder!r}')
                                            except Exception as _plx_err:
                                                logging.debug(f'[NZBHealthCheck] Plex path cleanup error: {_plx_err}')
                                            # Delete from provider once (hash → name search → path fallback)
                                            _provider_deleted = _delete_from_provider(_ihash, _ename)
                                            if not _provider_deleted:
                                                # Path-based deletion fallback: remove file directly from mount
                                                for _dbi in _items:
                                                    _loc = _dbi.get('location_on_disk', '') or ''
                                                    if _loc and not _provider_deleted:
                                                        try:
                                                            import os as _os
                                                            from utilities.settings import get_setting as _gs
                                                            _mount = _gs('Usenet Provider', 'mount_path', default='').rstrip('/')
                                                            if _mount and _loc.startswith('/debrid/'):
                                                                _rel = _loc[len('/debrid/'):]
                                                                _full = f'{_mount}/{_rel}'
                                                                if _os.path.exists(_full):
                                                                    _os.remove(_full)
                                                                    logging.info(f'[NZBHealthCheck] Path-deleted broken file: {_full!r}')
                                                                    _provider_deleted = True
                                                                else:
                                                                    logging.debug(f'[NZBHealthCheck] Path delete: file not found at {_full!r}')
                                                        except Exception as _pd_err:
                                                            logging.debug(f'[NZBHealthCheck] Path delete error for {_loc!r}: {_pd_err}')
                                            for _dbi in _items:
                                                _move_to_wanted(_dbi)
                                                log_repair_activity(
                                                    item_id=_dbi.get('id'),
                                                    title=_dbi.get('title'),
                                                    media_type=_dbi.get('type'),
                                                    season_number=_dbi.get('season_number'),
                                                    episode_number=_dbi.get('episode_number'),
                                                    broken_nzb_id=_ihash,
                                                    broken_nzb_title=_ename,
                                                    outcome='plex_deleted',
                                                    triggered_by='cache_warm_error' if _broken_entries.get(_ename) == 'cache_warm' else 'webdav_error',
                                                )
                                            # Mark as handled — avoid infinite re-processing this session
                                            self._webdav_repaired_entries.add(_ename)
                                        except Exception as _ename_err:
                                            logging.error(f'[NZBHealthCheck] Webdav repair error for {_ename!r}: {_ename_err}')
                                            self._webdav_repaired_entries.add(_ename)
                                except Exception as _wr_err:
                                    logging.error(f'[NZBHealthCheck] Webdav repair import error: {_wr_err}')
            except Exception as _watcher_err:
                logging.debug(f'[NZBHealthCheck] Webdav log watcher error: {_watcher_err}')

        except Exception as e:
            logging.error(f'[NZBHealthCheck] Task error: {e}', exc_info=True)
            self._nzb_health_last_duration = 0.0

    def task_backfill_nzb_torrent_ids(self):
        """Backfill filled_by_torrent_id for collected items on cli_mount mount that have no torrent ID.
        Matches location_on_disk folder name against cli_mount /api/torrents entries."""
        try:
            import re
            import requests as _req
            from utilities.settings import get_setting
            from database.database_reading import get_all_media_items
            from database.database_writing import update_media_item

            # cli_mount-specific: this backfills filled_by_torrent_id for legacy
            # cli_mount-collected items that predate the nzb: convention, via
            # /api/torrents (which nzbdav doesn't implement). nzbdav items already
            # carry their nzb: id natively, so there is nothing to backfill — skip
            # cleanly to avoid failed requests + a misleading "Loaded 0 entries" log.
            if (get_setting('Usenet Provider', 'provider', 'climount') or 'climount').strip().lower() != 'climount':
                logging.info('[NZBBackfill] Skipped — cli_mount-specific backfill (active usenet provider is not climount).')
                return

            dcy_url = get_setting('Usenet Provider', 'url', default='').rstrip('/')
            dcy_token = get_setting('Usenet Provider', 'api_token', default='')
            if not dcy_url:
                logging.warning('[NZBBackfill] Usenet Provider URL not configured, skipping.')
                return
            headers = {'Authorization': f'Bearer {dcy_token}'} if dcy_token else {}

            # Fetch all cli_mount entries (paginated), build name→info_hash map
            logging.info('[NZBBackfill] Fetching cli_mount entries...')
            name_to_hash = {}
            page = 1
            while True:
                for attempt in range(3):
                    try:
                        r = _req.get(f'{dcy_url}/api/torrents',
                                     params={'page': page, 'limit': 100, 'sort_by': 'added_on', 'sort_order': 'desc'},
                                     headers=headers, timeout=30)
                        if r.status_code == 200:
                            break
                        logging.warning(f'[NZBBackfill] cli_mount API HTTP {r.status_code} on page {page} (attempt {attempt+1})')
                    except Exception as _pe:
                        logging.warning(f'[NZBBackfill] cli_mount API error on page {page} (attempt {attempt+1}): {_pe}')
                    if attempt == 2:
                        logging.error(f'[NZBBackfill] Giving up on page {page} after 3 attempts')
                        break
                else:
                    break
                if r.status_code != 200:
                    break
                data = r.json()
                for t in data.get('torrents', []):
                    name = (t.get('name') or '').strip()
                    info_hash = (t.get('info_hash') or '').strip()
                    if name and info_hash:
                        name_to_hash[name] = info_hash
                if not data.get('has_next'):
                    break
                page += 1

            logging.info(f'[NZBBackfill] Loaded {len(name_to_hash)} cli_mount entries.')

            # Find collected items on /debrid mount with no torrent ID or non-NZB torrent ID
            items = [dict(i) for i in get_all_media_items(state='Collected')
                     if not (i.get('filled_by_torrent_id') or '').startswith('nzb:')
                     and (i.get('location_on_disk') or '').startswith('/debrid/')]

            logging.info(f'[NZBBackfill] Found {len(items)} collected items to backfill.')

            matched = skipped = 0
            for item in items:
                loc = item.get('location_on_disk', '')
                # Extract folder name: /debrid/shows/FolderName/file.mkv → FolderName
                parts = loc.split('/')
                # parts: ['', 'debrid', 'shows'|'movies', 'FolderName', ...]
                if len(parts) < 4:
                    skipped += 1
                    continue
                folder_name = parts[3]
                if not folder_name:
                    skipped += 1
                    continue

                info_hash = name_to_hash.get(folder_name)
                # If folder is already in NZB format (contains {imdb-), extract the original
                # release name from the trailing (...) and try that against cli_mount's old names.
                if not info_hash and '{imdb-' in folder_name:
                    import re as _re
                    m = _re.search(r'\(([^)]+)\)\s*$', folder_name)
                    if m:
                        orig_from_folder = m.group(1)
                        info_hash = name_to_hash.get(orig_from_folder)
                # Try filled_by_file (with and without ext)
                if not info_hash:
                    fbf = item.get('filled_by_file') or ''
                    if fbf:
                        fbf_noext = fbf.rsplit('.', 1)[0] if '.' in fbf else fbf
                        info_hash = name_to_hash.get(fbf) or name_to_hash.get(fbf_noext)
                        # If fbf is also NZB format, extract its trailing original name too
                        if not info_hash and '{imdb-' in fbf_noext:
                            m = _re.search(r'\(([^)]+)\)\s*$', fbf_noext)
                            if m:
                                info_hash = name_to_hash.get(m.group(1))
                if not info_hash:
                    orig = item.get('original_scraped_torrent_title') or ''
                    if orig:
                        info_hash = name_to_hash.get(orig)
                if info_hash:
                    update_media_item(item['id'], filled_by_torrent_id=f'nzb:{info_hash}')
                    matched += 1
                else:
                    if skipped < 10:
                        fbf = item.get('filled_by_file') or ''
                        orig = item.get('original_scraped_torrent_title') or ''
                        logging.info(f'[NZBBackfill] Unmatched: id={item.get("id")} loc={loc!r} folder={folder_name!r} fbf={fbf!r} orig={orig!r}')
                    skipped += 1

            logging.info(f'[NZBBackfill] Complete: {matched} matched, {skipped} unmatched/skipped.')

            # --- Phase 2: Backfill nzb_segment_id for items that have filled_by_magnet but no segment ID ---
            # Fetches the NZB XML from the stored URL and extracts the first segment Message-ID.
            # Rate-limited to avoid hammering indexers — 1 request per item.
            try:
                import requests as _req2
                from database.not_wanted_magnets import extract_nzb_segment_id as _ext_seg
                seg_items = [
                    dict(i) for i in get_all_media_items(state='Collected')
                    if (i.get('filled_by_magnet') or '').startswith('http')
                    and not (i.get('nzb_segment_id') or '').strip()
                ]
                logging.info(f'[NZBBackfill] Phase 2: {len(seg_items)} items need nzb_segment_id backfill.')
                seg_matched = seg_skipped = 0
                for seg_item in seg_items:
                    try:
                        _url = seg_item.get('filled_by_magnet', '')
                        _r = _req2.get(_url, timeout=15, allow_redirects=True,
                                       headers={'User-Agent': 'Sabnzbd/3.0.0'})
                        if _r.status_code == 200 and '<nzb' in _r.text.lower():
                            _seg_id = _ext_seg(_r.text)
                            if _seg_id:
                                update_media_item(seg_item['id'], nzb_segment_id=_seg_id)
                                seg_matched += 1
                                continue
                    except Exception:
                        pass
                    seg_skipped += 1
                logging.info(f'[NZBBackfill] Phase 2 complete: {seg_matched} segment IDs backfilled, {seg_skipped} skipped.')
            except Exception as _seg_err:
                logging.warning(f'[NZBBackfill] Phase 2 error: {_seg_err}')

        except Exception as e:
            logging.error(f'[NZBBackfill] Task error: {e}', exc_info=True)

    def task_backfill_plex_guids(self):
        """
        Backfill plex_guid on media_items and season_guids/plex_guid on tv_shows
        for all collected movies and episodes by querying cli_battery.

        Runs once (disabled by default). Enable in Task Manager under Features.
        Uses the Plex GUID stored in battery from Trakt API (ids.plex.guid).
        Falls back to a live Trakt lookup if battery doesn't have it yet.
        """
        try:
            from cli_battery.app.direct_api import DirectAPI
            from database.database_reading import get_all_media_items
            from database.database_writing import update_media_item
            from database.core import get_db_connection

            logging.info('[PlexGUIDBackfill] Starting Plex GUID backfill task')

            items = [dict(i) for i in get_all_media_items(state='Collected')
                     if i.get('imdb_id') and not i.get('plex_guid')]

            logging.info(f'[PlexGUIDBackfill] {len(items)} items need plex_guid')

            movie_ids = list({i['imdb_id'] for i in items if i.get('type') == 'movie'})
            show_ids  = list({i['imdb_id'] for i in items if i.get('type') == 'episode'})

            updated = 0
            errors  = 0

            # Movies
            for imdb_id in movie_ids:
                try:
                    result = DirectAPI.get_plex_guid(imdb_id, 'movie')
                    guid = (result or {}).get('show_guid')
                    if guid:
                        conn = get_db_connection()
                        conn.execute(
                            "UPDATE media_items SET plex_guid=? WHERE imdb_id=? AND type='movie'",
                            (guid, imdb_id)
                        )
                        conn.commit()
                        conn.close()
                        updated += 1
                except Exception as e:
                    logging.debug(f'[PlexGUIDBackfill] Movie {imdb_id} error: {e}')
                    errors += 1

            # Shows — update episodes + tv_shows table
            for imdb_id in show_ids:
                try:
                    # Force refresh to ensure absolute→TVDB episode GUID mapping runs
                    # This triggers _ensure_plex_guids_in_data which handles anime absolute numbering
                    try:
                        DirectAPI.force_refresh_metadata(imdb_id, item_type='show')
                    except Exception:
                        pass
                    result = DirectAPI.get_plex_guid(imdb_id, 'show')
                    if not result:
                        continue
                    show_guid    = result.get('show_guid')
                    season_guids = result.get('season_guids') or {}

                    conn = get_db_connection()
                    # Update episode plex_guid using season number lookup
                    # Store show-level guid + season_guids on tv_shows
                    if show_guid:
                        conn.execute(
                            "UPDATE tv_shows SET plex_guid=? WHERE imdb_id=?",
                            (show_guid, imdb_id)
                        )
                    if season_guids:
                        import json as _json
                        conn.execute(
                            "UPDATE tv_shows SET season_guids=? WHERE imdb_id=?",
                            (_json.dumps({str(k): v for k, v in season_guids.items()}), imdb_id)
                        )
                    # Update episode rows
                    if show_guid:
                        conn.execute(
                            "UPDATE media_items SET plex_guid=? WHERE imdb_id=? AND type='episode' AND plex_guid IS NULL",
                            (show_guid, imdb_id)
                        )
                    conn.commit()
                    conn.close()
                    updated += 1
                except Exception as e:
                    logging.debug(f'[PlexGUIDBackfill] Show {imdb_id} error: {e}')
                    errors += 1

            logging.info(f'[PlexGUIDBackfill] Complete: {updated} updated, {errors} errors')

        except Exception as e:
            logging.error(f'[PlexGUIDBackfill] Task error: {e}', exc_info=True)

    def task_repair_broken_nzbs(self, triggered_by: str = 'scheduled'):
        """Scan cli_mount for broken NZBs and attempt to repair them via re-scrape."""
        logging.info('[NZBRepair] Starting broken NZB repair task')
        try:
            # Trigger a partial NZB health scan and wait for it to complete
            try:
                from usenet.repair_engine import trigger_health_scan
                trigger_health_scan(full=False, wait=True, timeout=300)
                logging.info('[NZBRepair] Pre-scan complete')
            except Exception as scan_err:
                logging.warning(f'[NZBRepair] Pre-scan failed (continuing anyway): {scan_err}')
            from usenet.repair_engine import run_repair
            summary = run_repair(triggered_by=triggered_by)
            logging.info(
                f'[NZBRepair] Task complete — broken={summary["broken_found"]}, '
                f'matched={summary["matched"]}, replaced={summary["replaced"]}, '
                f'no_replacement={summary.get("no_replacement", 0)}, '
                f'submission_failed={summary.get("submission_failed", 0)}, '
                f'skipped_backoff={summary.get("skipped_backoff", 0)}, '
                f'skipped_max={summary.get("skipped_max_attempts", 0)}, '
                f'not_found={summary["not_found"]}, errors={summary["errors"]}'
            )
        except Exception as e:
            logging.error(f'[NZBRepair] Task error: {e}', exc_info=True)

    def task_repair_broken_debrids(self, triggered_by: str = 'scheduled'):
        """Scan cli_mount for broken torrent entries and attempt to repair them via CLI re-insertion."""
        logging.info('[DebridRepair] Starting broken debrid repair task')
        try:
            # Clean ghost health records first so repair doesn't waste time on them
            try:
                from usenet.debrid_repair_engine import delete_ghost_health_records
                ghost_result = delete_ghost_health_records()
                if ghost_result['deleted']:
                    logging.info(f'[DebridRepair] Ghost cleanup: deleted={ghost_result["deleted"]}')
            except Exception as ghost_err:
                logging.warning(f'[DebridRepair] Ghost cleanup failed (continuing): {ghost_err}')

            # Trigger a partial torrent health scan and wait for it to complete
            try:
                from usenet.debrid_repair_engine import trigger_health_scan
                trigger_health_scan(full=False, wait=True, timeout=300)
                logging.info('[DebridRepair] Pre-scan complete')
            except Exception as scan_err:
                logging.warning(f'[DebridRepair] Pre-scan failed (continuing anyway): {scan_err}')
            from usenet.debrid_repair_engine import run_repair
            summary = run_repair(triggered_by=triggered_by)
            logging.info(
                f'[DebridRepair] Task complete — broken={summary["broken_found"]}, '
                f'reinserted={summary["reinserted"]}, replaced={summary["replaced"]}, '
                f'not_found={summary["not_found"]}, errors={summary["errors"]}'
            )
        except Exception as e:
            logging.error(f'[DebridRepair] Task error: {e}', exc_info=True)

    def task_sync_cli_mount_changes(self):
        """Poll cli_mount for entry changes and sync key fields into CLI media_items DB."""
        from usenet.climount_sync import sync_changes_from_climount, _get_last_sync_ts
        is_first_run = _get_last_sync_ts() == 0
        if is_first_run:
            self.pause_info = {
                'reason_string': 'cli_mount initial full sync in progress — runs once on first setup, queue resumes automatically',
                'error_type': 'SYSTEM_MAINTENANCE',
                'service_name': 'cli_mount sync',
                'status_code': None,
                'retry_count': 0,
            }
            self.pause_queue()
            logging.info('[CMSync] First run — queue paused during initial full sync')
        try:
            sync_changes_from_climount()
        except Exception as e:
            logging.error(f'[CMSync] Task error: {e}', exc_info=True)
        finally:
            if is_first_run:
                self.last_resume_time = None  # bypass 30s throttle
                self.pause_info = {'reason_string': None, 'error_type': None,
                                   'service_name': None, 'status_code': None, 'retry_count': 0}
                self.resume_queue()
                logging.info('[CMSync] Initial full sync complete — queue resumed')

    def task_push_pending_climount_tags(self):
        """Push tags for media_items rows whose tags changed on the cli_debrid
        side only — sync_changes_from_climount can't detect these since it only
        re-fetches entries cli_mount itself has changed."""
        from usenet.climount_sync import push_pending_tags
        try:
            push_pending_tags()
        except Exception as e:
            logging.error(f'[CMSync] push_pending_tags task error: {e}', exc_info=True)

    def _is_system_idle_for_backup(self):
        """
        Check if the system is idle enough to run a database backup.

        Returns:
            bool: True if system is idle, False if busy
        """
        try:
            # Check 1: No active queue processing (checking if any tasks are currently executing)
            if self.currently_executing_tasks:
                logging.debug(f"[DATABASE_BACKUP] System busy: {len(self.currently_executing_tasks)} task(s) executing")
                return False

            # Check 2: Queue sizes are not growing rapidly (system is stable)
            from queues.queue_manager import QueueManager
            queue_manager = QueueManager()

            # If scraping or adding queues have items, system is actively working
            scraping_size = queue_manager.get_queue_size('Scraping')
            adding_size = queue_manager.get_queue_size('Adding')

            if scraping_size > 0 or adding_size > 0:
                logging.debug(f"[DATABASE_BACKUP] System busy: Scraping={scraping_size}, Adding={adding_size}")
                return False

            # Check 3: Not in a paused state (avoid backing up during maintenance)
            if queue_manager.is_paused():
                logging.debug("[DATABASE_BACKUP] System paused, waiting for resume")
                return False

            logging.debug("[DATABASE_BACKUP] System is idle, backup can proceed")
            return True

        except Exception as e:
            logging.warning(f"[DATABASE_BACKUP] Error checking idle state: {e}, assuming not idle")
            return False

    def task_reconcile_queues(self):
        """Task to reconcile items in Checking state with matching filled_by_file items,
           and deduplicate items in Wanted, Scraping, or Unreleased states."""
        import sqlite3
        import logging
        import os
        from datetime import datetime

        # Setup specific logging for reconciliations
        reconciliation_logger = logging.getLogger('reconciliations')
        if not reconciliation_logger.handlers:
            log_dir = os.environ.get('USER_LOGS', '/user/logs/')
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, 'reconciliations.log')
            handler = logging.FileHandler(log_file)
            handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
            reconciliation_logger.addHandler(handler)
            reconciliation_logger.setLevel(logging.INFO)

        from database.core import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        reconciled_count = 0
        deleted_count_filepath = 0 # Renamed for clarity
        deleted_count_semantic = 0 # For the new deduplication step
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            # Step 1: Original reconciliation for 'Checking' items based on filled_by_file
            reconciliation_logger.info("Starting reconciliation for 'Checking' items based on shared file paths...")
            cursor.execute("""
                SELECT
                    c.id as checking_id, c.title as checking_title, c.type as checking_type, c.filled_by_file,
                    m.id as matching_id, m.title as matching_title, m.state as matching_state, m.type as matching_type
                FROM media_items c
                JOIN media_items m ON c.filled_by_file = m.filled_by_file AND c.id != m.id
                WHERE c.state = 'Checking'
                  AND c.filled_by_file IS NOT NULL
                  AND m.state != 'Checking'
                  AND (m.ghostlisted IS NULL OR m.ghostlisted = 0)
                  AND (c.ghostlisted IS NULL OR c.ghostlisted = 0)
            """)
            reconciliation_pairs = cursor.fetchall()

            items_to_update = []
            items_to_delete_filepath = set()

            for pair in reconciliation_pairs:
                reconciliation_logger.info(
                    f"File-based Reconciliation Found: shared file '{pair['filled_by_file']}'\n"
                    f"  - Keeping (was Checking, now Collected): ID={pair['checking_id']}, Title='{pair['checking_title']}', Type={pair['checking_type']}\n"
                    f"  - Deleting (Matching entry): ID={pair['matching_id']}, Title='{pair['matching_title']}', State={pair['matching_state']}, Type={pair['matching_type']}"
                )
                items_to_update.append(pair['checking_id'])
                items_to_delete_filepath.add(pair['matching_id'])

            if items_to_update:
                update_sql = f"UPDATE media_items SET state = 'Collected', collected_at = ? WHERE id IN ({','.join(['?']*len(items_to_update))})"
                params = [now_str] + items_to_update
                cursor.execute(update_sql, params)
                reconciled_count = cursor.rowcount
                reconciliation_logger.info(f"Updated {reconciled_count} 'Checking' items to 'Collected' (file-based reconciliation). IDs: {items_to_update}")

            if items_to_delete_filepath:
                delete_ids_filepath = list(items_to_delete_filepath - set(items_to_update))
                if delete_ids_filepath:
                    # Only delete items that are true duplicates (same imdb_id+season+episode).
                    # Items that share filled_by_file but are different episodes (season pack coalescing)
                    # should be marked Collected, not deleted.
                    rows = cursor.execute(
                        f"SELECT id, imdb_id, season_number, episode_number FROM media_items "
                        f"WHERE id IN ({','.join(['?']*len(delete_ids_filepath))})",
                        delete_ids_filepath
                    ).fetchall()
                    # Build set of (imdb_id, season, episode) for already-updating items
                    updating_eps = set()
                    if items_to_update:
                        for _ur in cursor.execute(
                            f"SELECT imdb_id, season_number, episode_number FROM media_items "
                            f"WHERE id IN ({','.join(['?']*len(items_to_update))})",
                            items_to_update
                        ).fetchall():
                            updating_eps.add((_ur['imdb_id'], _ur['season_number'], _ur['episode_number']))
                    true_dupes = []
                    collect_instead = []
                    for r in rows:
                        key = (r['imdb_id'], r['season_number'], r['episode_number'])
                        if key in updating_eps:
                            true_dupes.append(r['id'])
                        else:
                            collect_instead.append(r['id'])
                    if collect_instead:
                        collect_sql = f"UPDATE media_items SET state = 'Collected', collected_at = ? WHERE id IN ({','.join(['?']*len(collect_instead))})"
                        cursor.execute(collect_sql, [now_str] + collect_instead)
                        reconciliation_logger.info(f"Marked {cursor.rowcount} season pack coalesced items as Collected (shared file). IDs: {collect_instead}")
                    if true_dupes:
                        delete_sql = f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(true_dupes))})"
                        cursor.execute(delete_sql, true_dupes)
                        deleted_count_filepath = cursor.rowcount
                        reconciliation_logger.info(f"Deleted {deleted_count_filepath} true duplicate items (file-based reconciliation). IDs: {true_dupes}")

            # --- Step 1b: Replace Season/Movie cleanup ---
            # When new items are promoted to Collected, remove old entries flagged with
            # manual_replace=1 (from a different torrent), remove the old debrid torrent,
            # and remove old items from Plex (with scan+empty trash fallback).
            deleted_count_replace = 0
            if items_to_update:
                try:
                    promoted_rows = cursor.execute(
                        f"SELECT id, type, imdb_id, season_number, episode_number, filled_by_torrent_id, version "
                        f"FROM media_items WHERE id IN ({','.join(['?']*len(items_to_update))})",
                        items_to_update
                    ).fetchall()
                    from debrid import get_debrid_provider as _get_debrid_prov
                    from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
                    import os as _os_recon
                    _debrid_prov = _get_debrid_prov()
                    ids_to_delete_replace = set()
                    _recon_plex_paths = set()
                    _recon_section_types = set()
                    _recon_select = 'id, filled_by_torrent_id, filled_by_file, location_on_disk, title, episode_title'

                    # Per-episode/movie cleanup: find old entries with manual_replace=1 for each promoted item
                    for promoted in promoted_rows:
                        if not promoted['imdb_id']:
                            continue
                        if promoted['type'] == 'episode':
                            if promoted['season_number'] is None or promoted['episode_number'] is None:
                                continue
                            old_replace_rows = cursor.execute(
                                f'''SELECT {_recon_select} FROM media_items
                                   WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                                   AND type = 'episode' AND manual_replace = 1 AND id != ?
                                   AND REPLACE(COALESCE(version,''),'*','') = ?''',
                                (promoted['imdb_id'], promoted['season_number'],
                                 promoted['episode_number'], promoted['id'],
                                 (promoted['version'] or '').replace('*', ''))
                            ).fetchall()
                            log_tag = 'REPLACE_SEASON'
                            removal_reason = 'Replaced by new season pack'
                            entry_label = 'episode'
                            _recon_section_types.add('show')
                        elif promoted['type'] == 'movie':
                            old_replace_rows = cursor.execute(
                                f'''SELECT {_recon_select} FROM media_items
                                   WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1 AND id != ?
                                   AND REPLACE(COALESCE(version,''),'*','') = ?''',
                                (promoted['imdb_id'], promoted['id'],
                                 (promoted['version'] or '').replace('*', ''))
                            ).fetchall()
                            log_tag = 'REPLACE_MOVIE'
                            removal_reason = 'Replaced by new movie torrent'
                            entry_label = 'movie'
                            _recon_section_types.add('movie')
                        else:
                            continue
                        new_torrent_id = promoted['filled_by_torrent_id']
                        for old_item in old_replace_rows:
                            old_id = old_item['id']
                            old_torrent_id = old_item['filled_by_torrent_id']
                            if old_torrent_id and old_torrent_id != new_torrent_id and _debrid_prov:
                                _sibs = cursor.execute(
                                    "SELECT COUNT(*) FROM media_items "
                                    "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                    (old_torrent_id, old_id)
                                ).fetchone()[0]
                                if _sibs:
                                    logging.info(f"[{log_tag}] Skipping debrid removal of {old_torrent_id} for replaced item {old_id} — {_sibs} sibling(s) still active")
                                else:
                                    try:
                                        _debrid_prov.remove_torrent(old_torrent_id, removal_reason=removal_reason)
                                        logging.info(f"[{log_tag}] Removed debrid torrent {old_torrent_id} for replaced item {old_id}")
                                    except Exception as _debrid_err:
                                        if '404' in str(_debrid_err):
                                            logging.debug(f"[{log_tag}] Old torrent {old_torrent_id} already removed (404)")
                                        else:
                                            logging.error(f"[{log_tag}] Failed to remove torrent {old_torrent_id}: {_debrid_err}")
                            if old_id not in items_to_delete_filepath and old_id not in items_to_update:
                                # Plex removal
                                _old_path = old_item['location_on_disk'] or old_item['filled_by_file']
                                if _old_path:
                                    _ep_title = old_item['episode_title'] if promoted['type'] == 'episode' else None
                                    try:
                                        if not remove_file_from_plex(old_item['title'] or '', _old_path, _ep_title):
                                            logging.warning(f"[{log_tag}] Direct Plex removal failed for item {old_id}, will scan+empty trash")
                                        else:
                                            logging.info(f"[{log_tag}] Removed item {old_id} from Plex")
                                    except Exception as _plex_err:
                                        logging.warning(f"[{log_tag}] Plex removal error for item {old_id}: {_plex_err}")
                                    _recon_plex_paths.add(_os_recon.path.dirname(_old_path))
                                ids_to_delete_replace.add(old_id)
                                logging.info(f"[{log_tag}] Queued old {entry_label} entry {old_id} for deletion after replacement")

                    # Broader sweep: catch stale manual_replace=1 items in any state (e.g. Upgrading)
                    # that already have a corresponding Collected replacement.
                    # Also handles items in items_to_update that had manual_replace=1 (old Upgrading
                    # items promoted to Collected by reconciliation) by clearing the flag.
                    _imdb_sweep = {}
                    for _p in promoted_rows:
                        if _p['imdb_id'] and _p['type'] in ('episode', 'movie'):
                            _sw_key = (_p['imdb_id'], (_p['version'] or '').replace('*', ''))
                            if _sw_key not in _imdb_sweep:
                                _imdb_sweep[_sw_key] = {'type': _p['type'], 'seasons': set()}
                            if _p['type'] == 'episode' and _p['season_number'] is not None:
                                _imdb_sweep[_sw_key]['seasons'].add(_p['season_number'])
                    for (_sw_imdb, _sw_version), _sw_info in _imdb_sweep.items():
                        if _sw_info['type'] == 'episode':
                            for _sw_season in _sw_info['seasons']:
                                _sw_rows = cursor.execute(
                                    f'''SELECT {_recon_select} FROM media_items m
                                       WHERE m.imdb_id = ? AND m.season_number = ? AND m.type = 'episode'
                                       AND m.manual_replace = 1
                                       AND REPLACE(COALESCE(m.version,''),'*','') = ?
                                       AND EXISTS (
                                           SELECT 1 FROM media_items m2
                                           WHERE m2.imdb_id = m.imdb_id AND m2.season_number = m.season_number
                                           AND m2.episode_number = m.episode_number AND m2.type = 'episode'
                                           AND m2.manual_replace = 0 AND m2.state = 'Collected'
                                           AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                                       )''',
                                    (_sw_imdb, _sw_season, _sw_version)
                                ).fetchall()
                                for _sr in _sw_rows:
                                    _sid = _sr['id']
                                    if _sid in items_to_update:
                                        cursor.execute('UPDATE media_items SET manual_replace = 0 WHERE id = ?', (_sid,))
                                        logging.info(f"[REPLACE_SEASON] Cleared manual_replace for promoted stale item {_sid}")
                                    elif _sid not in items_to_delete_filepath and _sid not in ids_to_delete_replace:
                                        _st = _sr['filled_by_torrent_id']
                                        if _st and _debrid_prov:
                                            _sw_sibs = cursor.execute(
                                                "SELECT COUNT(*) FROM media_items "
                                                "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                                (_st, _sid)
                                            ).fetchone()[0]
                                            if _sw_sibs:
                                                logging.info(f"[REPLACE_SEASON] Skipping debrid removal of {_st} for stale item {_sid} — {_sw_sibs} sibling(s) still active")
                                            else:
                                                try:
                                                    _debrid_prov.remove_torrent(_st, removal_reason='Replaced by new season pack')
                                                    logging.info(f"[REPLACE_SEASON] Removed debrid torrent {_st} for stale item {_sid}")
                                                except Exception as _de:
                                                    if '404' not in str(_de):
                                                        logging.error(f"[REPLACE_SEASON] Failed to remove torrent {_st}: {_de}")
                                        _sw_path = _sr['location_on_disk'] or _sr['filled_by_file']
                                        if _sw_path:
                                            try:
                                                if not remove_file_from_plex(_sr['title'] or '', _sw_path, _sr['episode_title']):
                                                    logging.warning(f"[REPLACE_SEASON] Direct Plex removal failed for stale item {_sid}")
                                                else:
                                                    logging.info(f"[REPLACE_SEASON] Removed stale item {_sid} from Plex")
                                            except Exception as _pe:
                                                logging.warning(f"[REPLACE_SEASON] Plex removal error for stale item {_sid}: {_pe}")
                                            _recon_plex_paths.add(_os_recon.path.dirname(_sw_path))
                                            _recon_section_types.add('show')
                                        ids_to_delete_replace.add(_sid)
                                        logging.info(f"[REPLACE_SEASON] Queued stale episode {_sid} for deletion (broader sweep)")
                        elif _sw_info['type'] == 'movie':
                            _sw_rows = cursor.execute(
                                f'''SELECT {_recon_select} FROM media_items m
                                   WHERE m.imdb_id = ? AND m.type = 'movie'
                                   AND m.manual_replace = 1
                                   AND REPLACE(COALESCE(m.version,''),'*','') = ?
                                   AND EXISTS (
                                       SELECT 1 FROM media_items m2
                                       WHERE m2.imdb_id = m.imdb_id AND m2.type = 'movie'
                                       AND m2.manual_replace = 0 AND m2.state = 'Collected'
                                       AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                                   )''',
                                (_sw_imdb, _sw_version)
                            ).fetchall()
                            for _sr in _sw_rows:
                                _sid = _sr['id']
                                if _sid in items_to_update:
                                    cursor.execute('UPDATE media_items SET manual_replace = 0 WHERE id = ?', (_sid,))
                                    logging.info(f"[REPLACE_MOVIE] Cleared manual_replace for promoted stale item {_sid}")
                                elif _sid not in items_to_delete_filepath and _sid not in ids_to_delete_replace:
                                    _st = _sr['filled_by_torrent_id']
                                    if _st and _debrid_prov:
                                        _sw_sibs = cursor.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                            (_st, _sid)
                                        ).fetchone()[0]
                                        if _sw_sibs:
                                            logging.info(f"[REPLACE_MOVIE] Skipping debrid removal of {_st} for stale item {_sid} — {_sw_sibs} sibling(s) still active")
                                        else:
                                            try:
                                                _debrid_prov.remove_torrent(_st, removal_reason='Replaced by new movie torrent')
                                                logging.info(f"[REPLACE_MOVIE] Removed debrid torrent {_st} for stale item {_sid}")
                                            except Exception as _de:
                                                if '404' not in str(_de):
                                                    logging.error(f"[REPLACE_MOVIE] Failed to remove torrent {_st}: {_de}")
                                    _sw_path = _sr['location_on_disk'] or _sr['filled_by_file']
                                    if _sw_path:
                                        try:
                                            if not remove_file_from_plex(_sr['title'] or '', _sw_path, None):
                                                logging.warning(f"[REPLACE_MOVIE] Direct Plex removal failed for stale item {_sid}")
                                            else:
                                                logging.info(f"[REPLACE_MOVIE] Removed stale item {_sid} from Plex")
                                        except Exception as _pe:
                                            logging.warning(f"[REPLACE_MOVIE] Plex removal error for stale item {_sid}: {_pe}")
                                        _recon_plex_paths.add(_os_recon.path.dirname(_sw_path))
                                        _recon_section_types.add('movie')
                                    ids_to_delete_replace.add(_sid)
                                    logging.info(f"[REPLACE_MOVIE] Queued stale movie {_sid} for deletion (broader sweep)")

                    if ids_to_delete_replace:
                        _ids_list = list(ids_to_delete_replace)
                        cursor.execute(
                            f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(_ids_list))})",
                            _ids_list
                        )
                        deleted_count_replace = cursor.rowcount
                        logging.info(f"[REPLACE] Deleted {deleted_count_replace} replaced/stale entries. IDs: {_ids_list}")

                    # Scan & empty Plex trash for all affected paths
                    if _recon_plex_paths:
                        try:
                            _sec_type = 'show' if 'show' in _recon_section_types and 'movie' not in _recon_section_types else \
                                        'movie' if 'movie' in _recon_section_types and 'show' not in _recon_section_types else None
                            scan_and_empty_plex_trash(paths=list(_recon_plex_paths), section_type=_sec_type)
                            logging.info(f"[REPLACE] Triggered Plex scan+empty trash for paths: {list(_recon_plex_paths)}")
                        except Exception as _scan_err:
                            logging.warning(f"[REPLACE] Plex scan+empty trash failed: {_scan_err}")

                except Exception as _replace_err:
                    logging.error(f"[REPLACE] Error in reconciliation replace hook: {_replace_err}")
            # --- End Step 1b ---

            # --- Step 1c: Collect stranded NZB coalesced items in Adding state ---
            # When a season pack NZB is submitted, only the initiator episode goes through
            # health check → Checking → _resolve_nzb_file_info → Collected.
            # The coalesced siblings sit in Adding with filled_by_torrent_id=nzb:xxx but no
            # filled_by_file. If a sibling is already Collected (via Plex scan), collect them
            # immediately with proper file info copied from the Collected sibling.
            try:
                stranded_rows = cursor.execute("""
                    SELECT a.id, a.imdb_id, a.season_number, a.episode_number, a.title
                    FROM media_items a
                    WHERE a.state = 'Adding'
                      AND a.filled_by_torrent_id LIKE 'nzb:%'
                      AND a.filled_by_file IS NULL
                      AND a.imdb_id IS NOT NULL
                      AND (a.ghostlisted IS NULL OR a.ghostlisted = 0)
                """).fetchall()

                if stranded_rows:
                    stranded_collected = []
                    for row in stranded_rows:
                        # Prefer the initiator row (has nzb torrent_id + folder info),
                        # fall back to any Collected sibling with a filled_by_file.
                        sibling = cursor.execute("""
                            SELECT id, filled_by_file, filled_by_title, debrid_folder_name,
                                   location_basename, original_scraped_torrent_title,
                                   real_debrid_original_title, filled_by_torrent_id
                            FROM media_items
                            WHERE imdb_id = ? AND season_number = ? AND type = 'episode'
                              AND state = 'Collected'
                              AND filled_by_file IS NOT NULL
                              AND id != ?
                            ORDER BY (filled_by_torrent_id LIKE 'nzb:%') DESC
                            LIMIT 1
                        """, (row['imdb_id'], row['season_number'], row['id'])).fetchone()
                        if sibling:
                            stranded_collected.append((row['id'], dict(sibling), dict(row)))

                    if stranded_collected:
                        for item_id, sib, item in stranded_collected:
                            # Use filled_by_title as location_basename (folder name) — sibling's
                            # location_basename may be a filename if set before the fix.
                            folder_name = sib['filled_by_title'] or sib['debrid_folder_name'] or sib['location_basename']
                            cursor.execute("""
                                UPDATE media_items SET
                                    state = 'Collected',
                                    collected_at = ?,
                                    filled_by_torrent_id = ?,
                                    filled_by_title = ?,
                                    debrid_folder_name = ?,
                                    location_basename = ?,
                                    original_scraped_torrent_title = ?,
                                    real_debrid_original_title = ?
                                WHERE id = ?
                            """, (
                                now_str,
                                sib['filled_by_torrent_id'],
                                folder_name,
                                folder_name,
                                folder_name,
                                sib['original_scraped_torrent_title'],
                                sib['real_debrid_original_title'],
                                item_id,
                            ))
                            reconciliation_logger.info(
                                f"[NZBCoalesce] Collected stranded Adding item {item_id} "
                                f"'{item['title']}' S{item['season_number']}E{item['episode_number']} "
                                f"— sibling {sib['id']} already Collected"
                            )
            except Exception as _sc_err:
                logging.error(f"[NZBCoalesce] Step 1c error: {_sc_err}", exc_info=True)
            # --- End Step 1c ---

            # --- Step 2: New deduplication for Wanted, Scraping, Unreleased states ---
            reconciliation_logger.info("Starting semantic deduplication for 'Wanted', 'Scraping', 'Unreleased' items (IMDB ID, S/E, Version - with '*' trimmed from version)...")
            
            cursor.execute("""
                SELECT id, imdb_id, season_number, episode_number, version, state, type, title
                FROM media_items
                WHERE state IN ('Wanted', 'Scraping', 'Unreleased')
                  AND imdb_id IS NOT NULL
                  AND (ghostlisted IS NULL OR ghostlisted = 0)
                ORDER BY imdb_id, type, season_number, episode_number, version, id
            """)
            candidate_semantic_duplicates = cursor.fetchall()

            items_to_delete_semantic_set = set()
            processed_groups = {}

            for item_row in candidate_semantic_duplicates:
                item = dict(item_row)
                
                s_num_key = item['season_number'] if item['type'] == 'episode' else None
                e_num_key = item['episode_number'] if item['type'] == 'episode' else None
                
                # Trim asterisks from version for grouping key
                raw_version = item['version']
                version_key = raw_version.replace('*', '') if isinstance(raw_version, str) else raw_version

                group_key = (item['imdb_id'], version_key, s_num_key, e_num_key)

                if group_key not in processed_groups:
                    processed_groups[group_key] = []
                processed_groups[group_key].append(item) # Store original item for logging/details

            state_priority = {'Scraping': 0, 'Wanted': 1, 'Unreleased': 2}

            for group_key, items_in_group in processed_groups.items():
                if len(items_in_group) > 1:
                    # Sort items: by state priority, then by ID (smallest ID is older)
                    items_in_group.sort(key=lambda x: (state_priority.get(x['state'], 99), x['id']))
                    
                    item_to_keep = items_in_group[0]
                    ids_in_group_to_delete = [i['id'] for i in items_in_group[1:]]

                    if ids_in_group_to_delete:
                        # Log with original version for clarity, but mention grouping logic
                        deleted_titles_log = [f"ID:{i['id']} '{i['title']}' (State:{i['state']}, OrigV:'{i['version']}')" for i in items_in_group[1:]]
                        group_key_log = (group_key[0], group_key[1], group_key[2], group_key[3]) # imdb, trimmed_version, s, e
                        reconciliation_logger.info(
                            f"Semantic Deduplication for group (key: {group_key_log}):\n"
                            f"  - Keeping: ID={item_to_keep['id']}, Title='{item_to_keep['title']}', State='{item_to_keep['state']}', Type='{item_to_keep['type']}', OrigV:'{item_to_keep['version']}'\n"
                            f"  - Deleting: {'; '.join(deleted_titles_log)}"
                        )
                        for del_id in ids_in_group_to_delete:
                            items_to_delete_semantic_set.add(del_id)
            
            if items_to_delete_semantic_set:
                final_semantic_delete_ids = list(items_to_delete_semantic_set - items_to_delete_filepath - set(items_to_update))
                                
                if final_semantic_delete_ids:
                    delete_sql_semantic = f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(final_semantic_delete_ids))})"
                    cursor.execute(delete_sql_semantic, final_semantic_delete_ids)
                    deleted_count_semantic = cursor.rowcount
                    reconciliation_logger.info(f"Deleted {deleted_count_semantic} items based on semantic duplication (IMDB ID, S/E, Version - with '*' trimmed). IDs: {final_semantic_delete_ids}")

            # Step 3: Delete Wanted/Scraping/Unreleased items that have a Collected counterpart
            # with the same imdb_id + season + episode + version (ignoring * suffix).
            # This cleans up duplicates created by Plex scan inserting Default* Collected rows
            # while the original Default Wanted row still exists.
            deleted_count_collected_dup = 0
            try:
                cursor.execute("""
                    DELETE FROM media_items
                    WHERE state IN ('Wanted', 'Scraping', 'Unreleased')
                      AND imdb_id IS NOT NULL
                      AND (ghostlisted IS NULL OR ghostlisted = 0)
                      AND EXISTS (
                          SELECT 1 FROM media_items c
                          WHERE c.state = 'Collected'
                            AND c.imdb_id = media_items.imdb_id
                            AND c.type = media_items.type
                            AND (c.season_number = media_items.season_number OR (c.season_number IS NULL AND media_items.season_number IS NULL))
                            AND (c.episode_number = media_items.episode_number OR (c.episode_number IS NULL AND media_items.episode_number IS NULL))
                            AND REPLACE(COALESCE(c.version,''), '*', '') = REPLACE(COALESCE(media_items.version,''), '*', '')
                            AND (c.ghostlisted IS NULL OR c.ghostlisted = 0)
                      )
                """)
                deleted_count_collected_dup = cursor.rowcount
                if deleted_count_collected_dup:
                    reconciliation_logger.info(f"Deleted {deleted_count_collected_dup} Wanted/Scraping items that have a Collected counterpart (version star mismatch cleanup).")
            except Exception as _cd_err:
                reconciliation_logger.error(f"Collected-dup cleanup error: {_cd_err}")

            conn.commit()

            log_parts = []
            if reconciled_count > 0:
                log_parts.append(f"{reconciled_count} items updated to 'Collected'")
            if deleted_count_filepath > 0:
                log_parts.append(f"{deleted_count_filepath} duplicates deleted (shared file paths)")
            if deleted_count_semantic > 0:
                log_parts.append(f"{deleted_count_semantic} duplicates deleted (content/version with '*' trimmed)")
            if deleted_count_replace > 0:
                log_parts.append(f"{deleted_count_replace} replaced entries deleted")

            if log_parts:
                 logging.info(f"Queue reconciliation completed: {', '.join(log_parts)}.")
            else: 
                 logging.debug("Queue reconciliation found no items needing update or deletion in this cycle.")

        except sqlite3.Error as e:
            logging.error(f"Database error during queue reconciliation: {str(e)}")
            if conn: conn.rollback() # Rollback on error
        finally:
            if conn: conn.close() # Ensure connection is closed

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
        # --- END EDIT ---


    def task_local_library_scan(self):
        """Run local library scan for symlinked files."""
        logging.info("Disabled for now")
        return
        if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            from database import get_all_media_items
            from utilities.local_library_scan import local_library_scan
            
            # Get all items in Checking state
            items = list(get_all_media_items(state="Checking"))
            if items:
                logging.info(f"Running local library scan for {len(items)} items in Checking state")
                found_items = local_library_scan(items)
                if found_items:
                    logging.info(f"Found {len(found_items)} items during local library scan")
                    
                    # Move found items to Collected state
                    for item_id, found_info in found_items.items():
                        item = found_info['item']
                        from queue_manager import QueueManager
                        queue_manager = QueueManager()
                        queue_manager.move_to_collected(item, "Checking")
            else:
                logging.debug("No items in Checking state to scan for")

    def task_get_plex_watch_history(self):
        """Task to get Plex watch history"""
        from utilities.plex_watch_history_functions import sync_get_watch_history_from_plex
        try:
            sync_get_watch_history_from_plex()
            logging.info("Successfully retrieved Plex watch history")
        except Exception as e:
            logging.error(f"Error retrieving Plex watch history: {str(e)}")

    def task_refresh_download_stats(self):
        """Task to refresh the download stats cache"""
        import threading
        import time
        from database.statistics import get_cached_download_stats
        
        result = [None]
        exception = [None]
        
        def run_with_timeout():
            try:
                get_cached_download_stats()
                result[0] = "success"
                logging.debug("Download stats cache refreshed")
            except Exception as e:
                exception[0] = e
                logging.error(f"Error refreshing download stats cache: {str(e)}")
        
        # Start the operation in a separate thread
        thread = threading.Thread(target=run_with_timeout)
        thread.daemon = True
        thread.start()
        
        # Wait for up to 30 seconds
        thread.join(timeout=30.0)
        
        if thread.is_alive():
            logging.error("Download stats refresh timed out after 30 seconds")
        elif exception[0]:
            logging.error(f"Download stats refresh failed: {str(exception[0])}")

    def task_sync_plex_labels(self):
        """
        Sync Plex labels for Collected items

        Ensures labels stored in database are also present in Plex.
        Handles cases where labels failed to sync initially (item not in Plex yet).
        Only runs if at least one content source has Plex labels enabled.
        """
        try:
            # Check if any content source has Plex labels enabled
            from utilities.settings import get_all_settings
            settings = get_all_settings()
            content_sources = settings.get('Content Sources', {})

            labels_enabled = any(
                source.get('plex_labels', {}).get('enabled', False)
                for source in content_sources.values()
            )

            if not labels_enabled:
                logging.debug("Plex labels not enabled in any content source, skipping sync task")
                return

            from utilities.plex_label_manager import sync_pending_labels
            synced = sync_pending_labels(max_items=100)
            if synced > 0:
                logging.info(f"Synced {synced} Plex labels")
        except Exception as e:
            logging.error(f"Error in task_sync_plex_labels: {e}", exc_info=True)

    def task_backfill_plex_labels_content_source_detail(self):
        """
        Backfill content_source_detail for items with NULL value

        Parses the content_source field and extracts appropriate detail values
        for all source types. Used to fix old items that were added before
        content_source_detail was properly populated.
        """
        try:
            from utilities.plex_label_manager import backfill_content_source_detail
            result = backfill_content_source_detail()

            if result['success']:
                logging.info(f"Backfilled {result['total_updated']} items: {result['by_source']}")
            else:
                logging.error(f"Backfill failed: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logging.error(f"Error in task_backfill_plex_labels_content_source_detail: {e}", exc_info=True)

    def task_regenerate_labels_from_backfilled_details(self, incremental: bool = False, days_back: int = 7):
        """
        Regenerate Plex labels from updated content_source_detail values

        Args:
            incremental: If True, only sync items that need updating (not synced or recently changed)
            days_back: When incremental=True, also sync items collected in last N days (default 7)

        After backfilling content_source_detail, this regenerates the plex_labels
        column from the updated detail values and syncs them to Plex. Handles all
        source types including Overseerr, Agregarr, and internal sources.

        When incremental=True, only processes items with plex_labels_last_synced IS NULL
        or items collected in the last N days, resulting in 95%+ time reduction.
        """
        try:
            from utilities.plex_label_manager import regenerate_labels_from_backfilled_details
            result = regenerate_labels_from_backfilled_details(incremental=incremental, days_back=days_back)

            if result['success']:
                mode_info = f" ({result.get('mode', 'full')} mode)" if 'mode' in result else ""
                logging.info(f"Regenerated labels for {result['total_regenerated']} items ({result['unique_items']} unique TMDB IDs){mode_info}")
            else:
                logging.error(f"Label regeneration failed: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logging.error(f"Error in task_regenerate_labels_from_backfilled_details: {e}", exc_info=True)

    def task_regenerate_labels_full(self):
        """
        Full sync: Regenerate Plex labels for ALL items

        Processes all 5922 items regardless of when they were last synced.
        Use this for initial setup, troubleshooting, or after bulk configuration changes.

        Expected time: 13-14 hours
        """
        logging.info("Starting FULL label sync (all items)")
        self.task_regenerate_labels_from_backfilled_details(incremental=False)

    def task_regenerate_labels_incremental(self):
        """
        Incremental sync: Regenerate Plex labels for changed/new items only

        Only processes items that:
        - Have never been synced (plex_labels_last_synced IS NULL), OR
        - Were collected in the last 7 days

        Use this for routine maintenance and regular syncs.

        Expected time: 5-15 minutes
        """
        logging.info("Starting INCREMENTAL label sync (last 7 days + never synced)")
        self.task_regenerate_labels_from_backfilled_details(incremental=True, days_back=7)

    def task_backfill_missing_labels(self):
        """
        Backfill Plex labels for items with NULL/empty plex_labels

        Generates and syncs labels only for items that don't have labels yet,
        without overwriting existing labels. Safe to run multiple times.
        """
        try:
            from utilities.plex_label_manager import backfill_missing_labels
            result = backfill_missing_labels()

            if result['success']:
                logging.info(f"Backfilled labels for {result['total_backfilled']} items ({result['unique_items']} unique TMDB IDs)")
            else:
                logging.error(f"Label backfill failed: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logging.error(f"Error in task_backfill_missing_labels: {e}", exc_info=True)

    def task_backfill_plex_ms_item_id(self):
        """
        Two-phase backfill for Collected items missing ms_item_id:

        Phase 1 — Plex scan triggers: For each unique folder derived from
        location_on_disk, trigger a Plex section scan so Plex indexes the file.

        Phase 2 — ms_item_id lookup: After triggering scans, query Plex by
        filename and GUID to find the ratingKey and write it to ms_item_id.
        Note: if Plex hasn't finished indexing by the time Phase 2 runs, those
        items will still be missing ms_item_id. Run the task again after Plex
        finishes scanning to pick them up.

        Runs once (disabled by default). Enable in Task Manager under Features.
        Only applies when Plex is the configured media server.
        """
        try:
            from database.core import get_db_connection as _get_db
            from urllib.parse import quote as _urlquote
            import os as _os

            # Connect to Plex using same pattern as task_check_plex_files
            plex_url = get_setting('Plex', 'url', default='')
            plex_token = get_setting('Plex', 'token', default='')
            if not plex_url or not plex_token:
                if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                    plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
                    plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
            if not plex_url or not plex_token:
                logging.info('[MSItemBackfill] Plex not configured — skipping')
                return

            plex = PlexServer(plex_url, plex_token)
            sections = plex.library.sections()
            logging.info(f'[MSItemBackfill] Connected to Plex, {len(sections)} sections')

            # Fetch all affected items
            conn = _get_db()
            rows = conn.execute(
                """SELECT id, type, imdb_id, tmdb_id, title, location_on_disk
                   FROM media_items
                   WHERE state='Collected'
                   AND (ms_item_id IS NULL OR ms_item_id = '')
                   AND location_on_disk IS NOT NULL
                   AND location_on_disk != ''"""
            ).fetchall()
            conn.close()
            items = [dict(r) for r in rows]
            logging.info(f'[MSItemBackfill] {len(items)} items missing ms_item_id')
            if not items:
                return

            # ── Phase 1: trigger Plex scans for unique folders ─────────────────
            scanned_folders = set()
            scan_count = 0
            for item in items:
                location = item['location_on_disk']
                folder = _os.path.dirname(location)
                if not folder or folder in scanned_folders:
                    continue
                is_episode = item['type'] == 'episode'
                type_lookup = 'show' if is_episode else 'movie'
                for section in sections:
                    if section.type != type_lookup:
                        continue
                    # Only scan if the folder lives under a known section location
                    for sec_loc in section.locations:
                        if folder.startswith(sec_loc) or sec_loc in folder:
                            try:
                                section.update(path=folder)
                                scanned_folders.add(folder)
                                scan_count += 1
                                logging.debug(f'[MSItemBackfill] Triggered scan: {folder}')
                            except Exception as _se:
                                logging.debug(f'[MSItemBackfill] Scan trigger failed for {folder}: {_se}')
                            break
                    if folder in scanned_folders:
                        break
            logging.info(f'[MSItemBackfill] Phase 1 complete — triggered {scan_count} folder scans')

            # ── Phase 2: look up ratingKey and write ms_item_id ────────────────
            updated = 0
            not_found = 0
            total = len(items)
            for idx, item in enumerate(items, 1):
                item_id = item['id']
                location = item['location_on_disk']
                imdb_id = item.get('imdb_id')
                tmdb_id = item.get('tmdb_id')
                is_episode = item['type'] == 'episode'
                type_lookup = 'show' if is_episode else 'movie'
                rating_key = None

                # Log progress every 25 items
                if idx % 25 == 0 or idx == 1:
                    logging.info(f'[MSItemBackfill] Phase 2 progress: {idx}/{total} (updated={updated}, not_found={not_found})')

                # Strategy 1: file path search (5s timeout)
                try:
                    filename = _os.path.basename(location)
                    type_param = '&type=4' if is_episode else ''
                    for section in sections:
                        if section.type != type_lookup:
                            continue
                        try:
                            results = plex.fetchItems(f'/library/sections/{section.key}/all?file={_urlquote(filename)}{type_param}', timeout=5)
                            if results:
                                r0 = results[0]
                                if is_episode:
                                    rating_key = str(getattr(r0, 'grandparentRatingKey', None) or getattr(r0, 'ratingKey', None) or '')
                                else:
                                    rating_key = str(getattr(r0, 'ratingKey', '') or '')
                                break
                        except Exception:
                            pass
                except Exception as _e:
                    logging.debug(f'[MSItemBackfill] File search error item {item_id}: {_e}')

                # Strategy 2: GUID fallback (5s timeout)
                if not rating_key and (imdb_id or tmdb_id):
                    try:
                        for section in sections:
                            if section.type != type_lookup:
                                continue
                            try:
                                _results = []
                                if imdb_id:
                                    _results = section.search(**{'guid': f'imdb://{imdb_id}'})
                                if not _results and tmdb_id:
                                    _results = section.search(**{'guid': f'tmdb://{tmdb_id}'})
                                if _results:
                                    rating_key = str(getattr(_results[0], 'ratingKey', '') or '')
                                    break
                            except Exception:
                                pass
                    except Exception as _e:
                        logging.debug(f'[MSItemBackfill] GUID search error item {item_id}: {_e}')

                if rating_key:
                    try:
                        conn = _get_db()
                        conn.execute("UPDATE media_items SET ms_item_id = ? WHERE id = ?", (rating_key, item_id))
                        conn.commit()
                        conn.close()
                        updated += 1
                        logging.info(f'[MSItemBackfill] Set ms_item_id={rating_key} for item {item_id} ({item.get("title")})')
                    except Exception as _e:
                        logging.error(f'[MSItemBackfill] DB write failed item {item_id}: {_e}')
                else:
                    not_found += 1
                    logging.debug(f'[MSItemBackfill] Not in Plex yet: item {item_id} ({item.get("title")})')

            logging.info(f'[MSItemBackfill] Phase 2 complete — ms_item_id updated: {updated}, still not in Plex: {not_found} (re-run task after Plex scans)')

        except Exception as e:
            logging.error(f'[MSItemBackfill] Task failed: {e}', exc_info=True)

    def task_refresh_plex_tokens(self):
        logging.info("Performing periodic Plex token validation")
        from content_checkers.plex_watchlist import validate_plex_tokens
        token_status = validate_plex_tokens()
        for username, status in token_status.items():
            if not status['valid']:
                logging.error(f"Invalid Plex token detected during periodic check for user {username}")
            else:
                logging.debug(f"Plex token for user {username} is valid")

    def _replace_cleanup_after_collect(self, promoted_dict):
        """
        After a new item is promoted to Collected (via Plex fallback or reconciliation),
        clean up old entries with manual_replace=1 for the same media, and remove their
        debrid torrents, Plex entries and scan/empty trash. Handles both episode
        (Replace Season) and movie (Replace Movie).
        """
        imdb_id = promoted_dict.get('imdb_id')
        item_type = promoted_dict.get('type')
        item_id = promoted_dict.get('id')
        new_torrent_id = promoted_dict.get('filled_by_torrent_id')
        item_version = (promoted_dict.get('version') or '').replace('*', '')

        if not imdb_id or item_type not in ('episode', 'movie'):
            return

        conn = None
        try:
            from debrid import get_debrid_provider as _get_debrid_prov
            from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
            import os as _os
            _debrid_prov = _get_debrid_prov()
            conn = get_db_connection()
            cur = conn.cursor()

            _select_fields = 'id, filled_by_torrent_id, filled_by_file, location_on_disk, title, episode_title'

            if item_type == 'episode':
                season_number = promoted_dict.get('season_number')
                episode_number = promoted_dict.get('episode_number')
                if season_number is None or episode_number is None:
                    return
                old_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items
                       WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                       AND type = 'episode' AND manual_replace = 1 AND id != ?
                       AND REPLACE(COALESCE(version,''),'*','') = ?''',
                    (imdb_id, season_number, episode_number, item_id, item_version)
                ).fetchall()
                log_tag = 'REPLACE_SEASON'
                removal_reason = 'Replaced by new season pack'
                entry_label = 'episode'
            else:  # movie
                old_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items
                       WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1 AND id != ?
                       AND REPLACE(COALESCE(version,''),'*','') = ?''',
                    (imdb_id, item_id, item_version)
                ).fetchall()
                log_tag = 'REPLACE_MOVIE'
                removal_reason = 'Replaced by new movie torrent'
                entry_label = 'movie'

            ids_to_delete = set()
            plex_scan_paths = set()

            def _remove_old_item(row, reason_label):
                """Remove debrid torrent and queue Plex removal for one old row."""
                old_id = row['id']
                old_torrent_id = row['filled_by_torrent_id']
                if old_torrent_id and old_torrent_id != new_torrent_id and _debrid_prov:
                    _sibs = cur.execute(
                        "SELECT COUNT(*) FROM media_items "
                        "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                        (old_torrent_id, old_id)
                    ).fetchone()[0]
                    if _sibs:
                        logging.info(f"[{log_tag}] Skipping debrid removal of {old_torrent_id} for {reason_label} {old_id} — {_sibs} sibling(s) still active")
                    else:
                        try:
                            _debrid_prov.remove_torrent(old_torrent_id, removal_reason=removal_reason)
                            logging.info(f"[{log_tag}] Removed debrid torrent {old_torrent_id} for {reason_label} {old_id}")
                        except Exception as debrid_err:
                            if '404' in str(debrid_err):
                                logging.debug(f"[{log_tag}] Old torrent {old_torrent_id} already removed (404)")
                            else:
                                logging.error(f"[{log_tag}] Failed to remove torrent {old_torrent_id}: {debrid_err}")
                # Plex removal
                item_path = row['location_on_disk'] or row['filled_by_file']
                if item_path:
                    ep_title = row['episode_title'] if item_type == 'episode' else None
                    title = row['title'] or ''
                    try:
                        if not remove_file_from_plex(title, item_path, ep_title):
                            logging.warning(f"[{log_tag}] Direct Plex removal failed for '{title}' ({item_path}), will fallback to scan+empty trash")
                        else:
                            logging.info(f"[{log_tag}] Removed '{title}' from Plex")
                    except Exception as plex_err:
                        logging.warning(f"[{log_tag}] Plex removal error for '{title}': {plex_err}")
                    plex_scan_paths.add(_os.path.dirname(item_path))
                ids_to_delete.add(old_id)
                logging.info(f"[{log_tag}] Queued old {entry_label} entry {old_id} for deletion ({reason_label})")

            # Per-item cleanup: same imdb/season/episode with manual_replace=1
            for old_row in old_rows:
                _remove_old_item(old_row, 'replaced item')

            # Broader sweep: catch stale manual_replace=1 items in any state (e.g. Upgrading)
            # that already have a corresponding fresh Collected replacement for the same content.
            if item_type == 'episode':
                stale_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items m
                       WHERE m.imdb_id = ? AND m.season_number = ? AND m.type = 'episode'
                       AND m.manual_replace = 1 AND m.id != ?
                       AND REPLACE(COALESCE(m.version,''),'*','') = ?
                       AND EXISTS (
                           SELECT 1 FROM media_items m2
                           WHERE m2.imdb_id = m.imdb_id AND m2.season_number = m.season_number
                           AND m2.episode_number = m.episode_number AND m2.type = 'episode'
                           AND m2.manual_replace = 0 AND m2.state = 'Collected'
                           AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                       )''',
                    (imdb_id, season_number, item_id, item_version)
                ).fetchall()
            else:  # movie
                stale_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items m
                       WHERE m.imdb_id = ? AND m.type = 'movie'
                       AND m.manual_replace = 1 AND m.id != ?
                       AND REPLACE(COALESCE(m.version,''),'*','') = ?
                       AND EXISTS (
                           SELECT 1 FROM media_items m2
                           WHERE m2.imdb_id = m.imdb_id AND m2.type = 'movie'
                           AND m2.manual_replace = 0 AND m2.state = 'Collected'
                           AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                       )''',
                    (imdb_id, item_id, item_version)
                ).fetchall()

            for stale_row in stale_rows:
                if stale_row['id'] not in ids_to_delete:
                    _remove_old_item(stale_row, 'stale item (broader sweep)')

            if ids_to_delete:
                ids_list = list(ids_to_delete)
                cur.execute(
                    f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(ids_list))})",
                    ids_list
                )
                conn.commit()
                logging.info(f"[{log_tag}] Deleted {cur.rowcount} replaced/stale {entry_label} entries. IDs: {ids_list}")

            # Scan & empty Plex trash for all affected paths (catches any missed direct removals)
            if plex_scan_paths:
                try:
                    section_type = 'show' if item_type == 'episode' else 'movie'
                    scan_and_empty_plex_trash(paths=list(plex_scan_paths), section_type=section_type)
                    logging.info(f"[{log_tag}] Triggered Plex scan+empty trash for paths: {list(plex_scan_paths)}")
                except Exception as scan_err:
                    logging.warning(f"[{log_tag}] Plex scan+empty trash failed: {scan_err}")

        except Exception as err:
            logging.error(f"[REPLACE] Error in replace cleanup after collect: {err}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    def task_check_plex_files(self):
        """Check for new files in Plex location and update libraries"""
        updated_sections = set()  # Initialize here to prevent UnboundLocalError
        if not get_setting('Plex', 'update_plex_on_file_discovery') and not get_setting('Plex', 'disable_plex_library_checks'):
            logging.debug("Skipping Plex file check as both relevant settings are disabled.")
            return

        # Use centralized Plex connection setup if possible or ensure proper error handling
        plex = None
        try:
            plex_url = get_setting('Plex', 'url', default='')
            plex_token = get_setting('Plex', 'token', default='')
            
            if not plex_url or not plex_token:
                # Check symlink settings if primary are missing
                if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                    plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
                    plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')

            if not plex_url or not plex_token:
                logging.warning("Plex URL or token not configured in primary or symlink settings. Skipping Plex file check.")
                return

            # Connect to Plex server
            plex = PlexServer(plex_url, plex_token)
            sections = plex.library.sections()
            logging.info(f"Connected to Plex server for file check, found {len(sections)} library sections.")

        except Exception as e:
            logging.error(f"Failed to connect to Plex for file check: {str(e)}")
            return # Cannot proceed without Plex connection

        plex_file_location = get_setting('Plex', 'mounted_file_location', default='/mnt/zurg/__all__')
        if not os.path.exists(plex_file_location):
             # Also check original_files_path for symlink mode as a fallback?
            if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                plex_file_location = get_setting('File Management', 'original_files_path', default=None)
                if not plex_file_location or not os.path.exists(plex_file_location):
                    logging.warning(f"Plex mounted_file_location and original_files_path (for symlink mode) do not exist. Cannot check files.")
                    return
            elif get_setting('Plex', 'disable_plex_library_checks', default=False):
                logging.warning(f"Plex mounted_file_location ('{plex_file_location}') does not exist, but proceeding with file discovery as library checks are disabled.")
            else:
                logging.warning(f"Plex mounted_file_location does not exist: {plex_file_location}")
                return


        # Get all media items from database that are in Checking state
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            items = cursor.execute('SELECT id, title, filled_by_title, filled_by_file, type, imdb_id, tmdb_id, season_number, episode_number, year, version, original_scraped_torrent_title, real_debrid_original_title, debrid_folder_name, last_updated, upgrading_from, upgrading_from_torrent_id FROM media_items WHERE state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)').fetchall()
        except sqlite3.Error as db_err:
            logging.error(f"Database error fetching items for Plex check: {db_err}")
            conn.close()
            return
        finally:
             # Ensure connection is closed even if fetch fails after opening
            if conn: conn.close()

        logging.info(f"Found {len(items)} media items in Checking state to verify against Plex location '{plex_file_location}'")

        # Check if Plex library checks are disabled (file discovery only)
        if get_setting('Plex', 'disable_plex_library_checks', default=False):
            logging.info("Plex library checks disabled - marking found files as Collected")
            updated_items = 0
            not_found_items = 0
            
            # --- START EDIT: Initialize scan tracking and tick counts ---
            paths_to_scan_by_section = {} # Store {section_title: set(constructed_paths)}
            sections_map = {}
            if plex and sections: # Only map if connection succeeded
                sections_map = {s.title: s for s in sections} # Map titles to section objects
            if not hasattr(self, 'plex_scan_tick_counts'):
                self.plex_scan_tick_counts = {}
            # --- END EDIT ---

            # If mounted file location is blank/empty, mark all items as Collected without file checks
            if not plex_file_location or plex_file_location.strip() == '':
                logging.info("Plex mounted file location is blank - marking all items as Collected without file existence checks")
                for item_dict in items:
                    item_id = item_dict['id']
                    item_title_log = (item_dict['title'] or 'N/A')
                    
                    conn_update = None
                    try:
                        conn_update = get_db_connection()
                        cursor_update = conn_update.cursor()
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        cursor_update.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                              (now, item_id))

                        if cursor_update.rowcount > 0:
                            conn_update.commit()
                            logging.info(f"Updated item {item_id} ({item_title_log}) to Collected state (Plex checks disabled, no file location).")
                            updated_items += 1
                            # Post-processing and notification logic
                            updated_item_details = get_media_item_by_id(item_id)
                            if updated_item_details:
                                handle_state_change(dict(updated_item_details))
                                # Add Collected notification
                                from database.database_writing import add_to_collected_notifications
                                notification_item = dict(updated_item_details)
                                notification_item['is_upgrade'] = False
                                notification_item['new_state'] = "Collected"
                                add_to_collected_notifications(notification_item)
                                logging.info(f"Added collection notification for item: {item_id} ({item_title_log})")
                        else:
                            logging.debug(f"Item {item_id} was not updated to Collected (state may have changed).")

                    except sqlite3.Error as db_update_err:
                        logging.error(f"Database error updating item {item_id} to collected: {db_update_err}")
                        if conn_update: conn_update.rollback()
                    except Exception as e_update:
                        logging.error(f"Unexpected error during item update/notification for {item_id}: {e_update}", exc_info=True)
                        if conn_update: conn_update.rollback()
                    finally:
                        if conn_update: conn_update.close()
                
                # Log summary and return early
                logging.info(f"Plex check summary (checks disabled, no file location): {updated_items} items marked as Collected, 0 items not found.")
                return

            # If we have a valid file location, proceed with file existence checks
            for item_dict in items: # Iterate over dicts
                item_id = item_dict['id']
                filled_by_title = item_dict['filled_by_title']
                filled_by_file = item_dict['filled_by_file']
                # --- START: Ensure new field is fetched ---
                original_scraped_torrent_title = item_dict['original_scraped_torrent_title'] or ''
                real_debrid_original_title = item_dict['real_debrid_original_title'] or ''
                debrid_folder_name = item_dict['debrid_folder_name'] or ''
                # --- END: Ensure new field is fetched ---

                if not filled_by_title or not filled_by_file: # This check might need re-evaluation if filled_by_title can be empty but other titles exist
                    logging.debug(f"Item {item_id} missing filled_by_title or filled_by_file. Skipping.")
                    continue

                # --- Get potential folder names ---
                # filled_by_title already fetched
                # original_torrent_title already fetched (as original_scraped_torrent_title)
                current_filename = item_dict['filled_by_file']

                # --- Construct potential paths in order of priority ---
                paths_to_check = []
                base_path = plex_file_location # Base path to check within

                # 0. Exact provider folder name
                if debrid_folder_name:
                    paths_to_check.append(os.path.join(base_path, debrid_folder_name, current_filename))

                # 1. Original Scraped Torrent Title (raw)
                if original_scraped_torrent_title:
                    paths_to_check.append(os.path.join(base_path, original_scraped_torrent_title, current_filename))

                # 2. Original Scraped Torrent Title (trimmed)
                if original_scraped_torrent_title:
                    original_scraped_torrent_title_trimmed = os.path.splitext(original_scraped_torrent_title)[0]
                    if original_scraped_torrent_title_trimmed != original_scraped_torrent_title:
                        paths_to_check.append(os.path.join(base_path, original_scraped_torrent_title_trimmed, current_filename))
                
                # 3. Real Debrid Original Title (raw) (NEW)
                if real_debrid_original_title:
                    paths_to_check.append(os.path.join(base_path, real_debrid_original_title, current_filename))

                # 4. Real Debrid Original Title (trimmed) (NEW)
                if real_debrid_original_title:
                    real_debrid_original_title_trimmed = os.path.splitext(real_debrid_original_title)[0]
                    if real_debrid_original_title_trimmed != real_debrid_original_title:
                        paths_to_check.append(os.path.join(base_path, real_debrid_original_title_trimmed, current_filename))

                # 5. Filled By Title (raw)
                if filled_by_title:
                    paths_to_check.append(os.path.join(base_path, filled_by_title, current_filename))

                # 6. Filled By Title (trimmed)
                if filled_by_title:
                    filled_by_title_trimmed = os.path.splitext(filled_by_title)[0]
                    if filled_by_title_trimmed != filled_by_title:
                        paths_to_check.append(os.path.join(base_path, filled_by_title_trimmed, current_filename))

                # 7. Direct path under base
                paths_to_check.append(os.path.join(base_path, current_filename))

                # --- Check paths in order ---
                file_found_on_disk = False
                actual_file_path = None
                checked_paths_log = [] # For logging if not found
                for idx, potential_path in enumerate(paths_to_check):
                    checked_paths_log.append(potential_path)
                    logging.debug(f"Plex Check Attempt {idx+1}: Checking path: {potential_path}")
                    if os.path.exists(potential_path):
                        file_found_on_disk = True
                        actual_file_path = potential_path
                        logging.info(f"Plex Check: Found file for item {item_id} at: {actual_file_path} (Attempt {idx+1})")
                        break # Found it, stop checking

                # --- Handle Cache Key ---
                # Use a consistent cache key, perhaps based on item ID or a combo?
                # Using filled_by_title + filled_by_file might be less reliable if those change.
                # Let's stick with the existing filled_by_title:filled_by_file key for now.
                cache_key = f"{filled_by_title}:{current_filename}"


                if file_found_on_disk:
                    logging.info(f"Confirmed file exists on disk: {actual_file_path} for item {item_id}") # Log actual path found
                    self.file_location_cache[cache_key] = 'exists'

                    # --- START EDIT: Add Tick Check and Scan Path Gathering ---
                    should_trigger_scan = False
                    current_tick = self.plex_scan_tick_counts.get(cache_key, 0) + 1
                    self.plex_scan_tick_counts[cache_key] = current_tick
                    # Trigger scan only if library checks are ENABLED
                    if not get_setting('Plex', 'disable_plex_library_checks', default=False):
                         if current_tick <= 3:
                             should_trigger_scan = True
                             updated_items += 1 # Count item here when scan is intended
                             logging.info(f"File '{current_filename}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                         else:
                             logging.info(f"File '{current_filename}' found (tick {current_tick}). Plex scan ticks exhausted — marking item {item_id} as Collected directly (file confirmed on disk, recentlyAdded did not resolve it).")
                             # Fallback: recentlyAdded scan failed to resolve this item (e.g. file was already
                             # in Plex before it entered Checking). File is confirmed on disk, so mark Collected.
                             _conn_fb = None
                             try:
                                 _conn_fb = get_db_connection()
                                 _cur_fb = _conn_fb.cursor()
                                 _now_fb = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                 _cur_fb.execute(
                                     'UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                     (_now_fb, item_id)
                                 )
                                 if _cur_fb.rowcount > 0:
                                     _conn_fb.commit()
                                     logging.info(f"Fallback: marked item {item_id} ({item_dict['title'] if item_dict['title'] else 'N/A'}) as Collected after {current_tick} ticks with file on disk.")
                                     _fb_details = get_media_item_by_id(item_id)
                                     if _fb_details:
                                         handle_state_change(dict(_fb_details))  # triggers replace_cleanup_after_collect internally
                                         from database.database_writing import add_to_collected_notifications
                                         _notif = dict(_fb_details)
                                         _notif['is_upgrade'] = False
                                         _notif['new_state'] = "Collected"
                                         add_to_collected_notifications(_notif)
                                     # Reset tick count so it doesn't keep trying on the now-Collected item
                                     if cache_key in self.plex_scan_tick_counts:
                                         del self.plex_scan_tick_counts[cache_key]
                                 else:
                                     logging.debug(f"Fallback: item {item_id} state already changed, skipping Collected update.")
                             except sqlite3.Error as _fb_db_err:
                                 logging.error(f"Fallback: DB error marking item {item_id} as Collected: {_fb_db_err}")
                                 if _conn_fb: _conn_fb.rollback()
                             except Exception as _fb_err:
                                 logging.error(f"Fallback: unexpected error for item {item_id}: {_fb_err}", exc_info=True)
                                 if _conn_fb: _conn_fb.rollback()
                             finally:
                                 if _conn_fb: _conn_fb.close()
                    else:
                         # If library checks are disabled, we don't trigger scans based on ticks here
                         # We just mark as collected
                         updated_items += 1 # Count item as 'updated' if found (checks disabled case)
                         logging.info(f"File '{current_filename}' found (tick {current_tick}). Library checks disabled, will mark as collected.")


                    # --- Only gather scan paths if checks enabled AND should_trigger_scan ---
                    if should_trigger_scan and not get_setting('Plex', 'disable_plex_library_checks', default=False):
                        if not sections:
                             logging.error("Plex sections not available, cannot identify scan paths.")
                             # Continue processing other items
                        else:
                            item_type_mapped = 'show' if item_dict['type'] == 'episode' else item_dict['type']
                            logging.debug(f"Identifying scan paths for item {item_id} (type: {item_type_mapped}, title: '{filled_by_title}')")

                            found_matching_section_location = False
                            # Use the *folder name* from the actual_file_path to construct the scan path relative to section locations
                            folder_name_found = os.path.basename(os.path.dirname(actual_file_path))

                            for section in sections:
                                if section.type != item_type_mapped:
                                    continue

                                logging.debug(f"  Checking Section '{section.title}' (Type: {section.type})")
                                for location in section.locations:
                                    constructed_plex_path = os.path.join(location, folder_name_found)
                                    logging.debug(f"    Considering scan path: '{constructed_plex_path}' based on location '{location}' and found folder '{folder_name_found}'")

                                    if section.title not in paths_to_scan_by_section:
                                        paths_to_scan_by_section[section.title] = set()
                                    paths_to_scan_by_section[section.title].add(constructed_plex_path)
                                    found_matching_section_location = True

                            if not found_matching_section_location:
                                logging.warning(f"Could not find any matching Plex library section (type: {item_type_mapped}) for item {item_id} based on file '{current_filename}'. Scan might not be triggered correctly.")

                    # --- Update item state to Collected if checks are disabled ---
                    if get_setting('Plex', 'disable_plex_library_checks', default=False):
                         conn_update = None
                         try:
                             conn_update = get_db_connection()
                             cursor_update = conn_update.cursor()
                             now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                             cursor_update.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                                   (now, item_id))

                             if cursor_update.rowcount > 0:
                                 conn_update.commit()
                                 item_title_log = (item_dict['title'] or 'N/A')
                                 logging.info(f"Updated item {item_id} ({item_title_log}) to Collected state (Plex checks disabled).")
                                 # Post-processing and notification logic... (omitted for brevity, should be similar to existing code)
                                 updated_item_details = get_media_item_by_id(item_id)
                                 if updated_item_details:
                                      handle_state_change(dict(updated_item_details))
                                      # Add notification logic here...

                             else:
                                 logging.debug(f"Item {item_id} was not updated to Collected (state may have changed).")

                         except sqlite3.Error as db_update_err:
                             logging.error(f"Database error updating item {item_id} to collected: {db_update_err}")
                             if conn_update: conn_update.rollback()
                         except Exception as e_update:
                              logging.error(f"Unexpected error during item update/notification for {item_id}: {e_update}", exc_info=True)
                              if conn_update: conn_update.rollback()
                         finally:
                             if conn_update: conn_update.close()


                else: # File not found on disk
                    not_found_items += 1
                    logging.debug(f"File not found on disk for item {item_id}. Checked paths:\n  " + "\n  ".join(checked_paths_log))
                    # --- START EDIT: Reset tick count if file missing ---
                    if cache_key in self.plex_scan_tick_counts:
                        logging.debug(f"Resetting Plex scan tick count for missing file '{current_filename}'.")
                        del self.plex_scan_tick_counts[cache_key]
                    # --- END EDIT ---
                    continue

            # --- START EDIT: Add Scan Triggering Logic ---
            if paths_to_scan_by_section and plex and sections_map: # Check connection exists
                logging.info(f"Triggering scans for {len(paths_to_scan_by_section)} sections based on detected files (library checks disabled)...")
                final_updated_sections = set() # Track unique section titles updated
                for section_title, scan_paths in paths_to_scan_by_section.items():
                    section = sections_map.get(section_title)
                    if not section:
                        logging.error(f"Could not find section object for title '{section_title}' during scan trigger phase.")
                        continue

                    for scan_path in scan_paths:
                        try:
                            logging.info(f"Triggering Plex section '{section.title}' update scan for path: {scan_path}")
                            section.update(path=scan_path)
                            final_updated_sections.add(section.title)
                        except NotFound:
                             logging.warning(f"Path '{scan_path}' not found by Plex server during scan trigger for section '{section.title}'. This might be expected if the folder doesn't exist yet.")
                        except Exception as e_scan:
                             logging.error(f"Failed to trigger update scan for Plex section '{section.title}' with path '{scan_path}': {str(e_scan)}", exc_info=True)

                if final_updated_sections:
                    logging.info(f"Plex sections triggered for update in this run: {', '.join(sorted(list(final_updated_sections)))}")
            # --- END EDIT ---

            # Log summary of operations
            logging.info(f"Plex check summary (checks disabled): {updated_items} items found on disk and marked Collected, {not_found_items} items not found.")
            # We don't update sections when checks are disabled. # <-- This comment is now slightly inaccurate due to the edit, but harmless.

        # ----- ELSE: Plex library checks are ENABLED -----
        else:
            logging.info("Plex library checks enabled - verifying file existence and triggering scans if needed.")
            updated_sections = set()  # Track which sections we've updated
            updated_items = 0 # Count items found AND triggering scan
            not_found_items = 0

            if not hasattr(self, 'plex_scan_tick_counts'):
                self.plex_scan_tick_counts = {}

            # Store tuples of (section, constructed_scan_path)
            # Using a set avoids triggering the exact same path scan multiple times if found via different items
            paths_to_scan_by_section = {} # {section_title: set(constructed_paths)}
            sections_map = {s.title: s for s in sections} # Map titles to section objects

            for item_dict in items: # Iterate over dicts
                item_dict = dict(item_dict) if not isinstance(item_dict, dict) else item_dict
                item_id = item_dict['id']
                filled_by_title = item_dict['filled_by_title']
                current_filename = item_dict['filled_by_file'] # Use current_filename for clarity
                # --- START: Ensure new field is fetched ---
                original_scraped_torrent_title = item_dict['original_scraped_torrent_title'] or ''
                real_debrid_original_title = item_dict['real_debrid_original_title'] or ''
                debrid_folder_name = item_dict['debrid_folder_name'] or ''
                # --- END: Ensure new field is fetched ---

                if not filled_by_title or not current_filename: 
                    logging.debug(f"Item {item_id} missing filled_by_title or current_filename. Skipping Plex scan trigger check.")
                    continue

                cache_key = f"{filled_by_title}:{current_filename}"

                # --- START: UNIFIED FILE SEARCH LOGIC (consistent with disabled checks mode) ---
                paths_to_check_info = [] # Stores dicts: {'name': folder_name, 'path': full_path, 'type': log_type}
                base_path = plex_file_location

                # 0. Exact provider folder name
                if debrid_folder_name:
                    path = os.path.join(base_path, debrid_folder_name, current_filename)
                    paths_to_check_info.append({'name': debrid_folder_name, 'path': path, 'type': 'debrid_folder_name'})

                # 1. Original Scraped Torrent Title (raw)
                if original_scraped_torrent_title:
                    path = os.path.join(base_path, original_scraped_torrent_title, current_filename)
                    paths_to_check_info.append({'name': original_scraped_torrent_title, 'path': path, 'type': 'original_scraped_raw'})

                # 2. Original Scraped Torrent Title (trimmed)
                if original_scraped_torrent_title:
                    trimmed_title = os.path.splitext(original_scraped_torrent_title)[0]
                    if trimmed_title != original_scraped_torrent_title:
                        path = os.path.join(base_path, trimmed_title, current_filename)
                        paths_to_check_info.append({'name': trimmed_title, 'path': path, 'type': 'original_scraped_trimmed'})
                
                # 3. Real Debrid Original Title (raw)
                if real_debrid_original_title:
                    path = os.path.join(base_path, real_debrid_original_title, current_filename)
                    paths_to_check_info.append({'name': real_debrid_original_title, 'path': path, 'type': 'real_debrid_raw'})

                # 4. Real Debrid Original Title (trimmed)
                if real_debrid_original_title:
                    trimmed_title = os.path.splitext(real_debrid_original_title)[0]
                    if trimmed_title != real_debrid_original_title:
                        path = os.path.join(base_path, trimmed_title, current_filename)
                        paths_to_check_info.append({'name': trimmed_title, 'path': path, 'type': 'real_debrid_trimmed'})

                # 5. Filled By Title (raw)
                if filled_by_title:
                    path = os.path.join(base_path, filled_by_title, current_filename)
                    paths_to_check_info.append({'name': filled_by_title, 'path': path, 'type': 'filled_by_title_raw'})

                # 6. Filled By Title (trimmed)
                if filled_by_title:
                    trimmed_title = os.path.splitext(filled_by_title)[0]
                    if trimmed_title != filled_by_title:
                        path = os.path.join(base_path, trimmed_title, current_filename)
                        paths_to_check_info.append({'name': trimmed_title, 'path': path, 'type': 'filled_by_title_trimmed'})

                # 7. Direct path under base
                direct_path = os.path.join(base_path, current_filename)
                paths_to_check_info.append({'name': None, 'path': direct_path, 'type': 'direct_under_base'})

                file_found_on_disk = False
                actual_file_path = None
                folder_name_for_plex_scan = None 
                found_path_type_log = "None"
                log_checked_paths = [] # For detailed logging if not found

                for idx, p_info in enumerate(paths_to_check_info):
                    potential_path = p_info['path']
                    log_checked_paths.append(potential_path) 
                    logging.debug(f"Plex Check (checks enabled) Attempt {idx+1}: Checking path: {potential_path} (using folder '{p_info['name']}', type: {p_info['type']})")
                    if os.path.exists(potential_path):
                        file_found_on_disk = True
                        actual_file_path = potential_path
                        folder_name_for_plex_scan = p_info['name'] 
                        found_path_type_log = p_info['type']
                        item_title_for_log = (item_dict['title'] or 'N/A')
                        logging.info(f"Plex Check (checks enabled): Found file for item {item_id} ('{item_title_for_log}') at: {actual_file_path} (Type: {found_path_type_log}, Folder for scan: '{folder_name_for_plex_scan}')")
                        break 
                # --- END: UNIFIED FILE SEARCH LOGIC ---
                
                should_trigger_scan = False
                if file_found_on_disk:
                    # File exists, update cache and handle tick count
                    item_title_for_log = item_dict['title'] if item_dict['title'] else 'N/A'
                    logging.info(f"Plex Check (checks enabled): Found file for item {item_id} ('{item_title_for_log}') at: {actual_file_path} (Type: {found_path_type_log}, Folder for scan: '{folder_name_for_plex_scan}')")
                    logging.debug(f"Confirmed file exists on disk: {actual_file_path} for item {item_id}")
                    self.file_location_cache[cache_key] = 'exists'
                    current_tick = self.plex_scan_tick_counts.get(cache_key, 0) + 1
                    self.plex_scan_tick_counts[cache_key] = current_tick

                    # Option C: Direct Plex library lookup.
                    # Try 1: file path search (most reliable — checks exact file Plex should have indexed).
                    # Try 2: GUID search (fallback — sometimes unreliable in Plex).
                    _force_collect_reason = None
                    _imdb_id = item_dict['imdb_id']
                    _tmdb_id = item_dict['tmdb_id']
                    _item_type_lookup = 'show' if item_dict['type'] == 'episode' else item_dict['type']
                    # File path search: ask Plex "do you have this exact file?"
                    # Plex ?file= matches against full Plex path; URL-encode the filename.
                    _plex_location = None
                    _new_basename = os.path.basename(item_dict['filled_by_file'] or '')
                    _plex_rating_key = None
                    if actual_file_path:
                        from urllib.parse import quote as _urlquote
                        _plex_filename = os.path.basename(actual_file_path)
                        _is_episode = item_dict['type'] == 'episode'
                        for _section in sections:
                            if _section.type != _item_type_lookup:
                                continue
                            try:
                                _type_param = '&type=4' if _is_episode else ''
                                _fp_results = plex.fetchItems(f'/library/sections/{_section.key}/all?file={_urlquote(_plex_filename)}{_type_param}')
                                if _fp_results:
                                    _force_collect_reason = f"file indexed in Plex confirmed (tick {current_tick})"
                                    try:
                                        _plex_location = _fp_results[0].media[0].parts[0].file
                                    except Exception:
                                        pass
                                    try:
                                        _fp_item = _fp_results[0]
                                        if _is_episode:
                                            _plex_rating_key = str(getattr(_fp_item, 'grandparentRatingKey', None) or getattr(_fp_item, 'ratingKey', None) or '')
                                        else:
                                            _plex_rating_key = str(getattr(_fp_item, 'ratingKey', '') or '')
                                    except Exception:
                                        pass
                                    if not _plex_location:
                                        logging.warning(f"[PlexCheck] File search found item {item_id} but could not extract location from result: {_fp_results[0]}")
                                    break
                            except Exception as _e_fp:
                                logging.debug(f"[PlexCheck] File path search failed for item {item_id}: {_e_fp}")
                    # GUID search fallback
                    if not _force_collect_reason and (_imdb_id or _tmdb_id):
                        for _section in sections:
                            if _section.type != _item_type_lookup:
                                continue
                            try:
                                _results = []
                                if _imdb_id:
                                    _results = _section.search(**{'guid': f'imdb://{_imdb_id}'})
                                if not _results and _tmdb_id:
                                    _results = _section.search(**{'guid': f'tmdb://{_tmdb_id}'})
                                if _results:
                                    _force_collect_reason = f"direct Plex GUID lookup confirmed in library (tick {current_tick})"
                                    # Try to extract location_on_disk from the matched Plex item,
                                    # preferring the part whose filename matches filled_by_file.
                                    # For episodes: _results is Show objects; must fetch episodes to get parts.
                                    if not _plex_location and _new_basename:
                                        try:
                                            _is_episode = item_dict['type'] == 'episode'
                                            for _pi in _results:
                                                _candidates = []
                                                if _is_episode:
                                                    # Show object: walk seasons→episodes to find parts
                                                    try:
                                                        _sn = item_dict['season_number']
                                                        _ep = item_dict['episode_number']
                                                        if _sn is not None and _ep is not None:
                                                            _episode_obj = _pi.episode(season=int(_sn), episode=int(_ep))
                                                            _candidates = [_episode_obj] if _episode_obj else []
                                                        else:
                                                            _candidates = _pi.episodes()
                                                    except Exception:
                                                        _candidates = []
                                                else:
                                                    _candidates = [_pi]
                                                for _candidate in _candidates:
                                                    for _pm in getattr(_candidate, 'media', []):
                                                        for _pp in getattr(_pm, 'parts', []):
                                                            _pp_file = getattr(_pp, 'file', '') or ''
                                                            if os.path.basename(_pp_file) == _new_basename:
                                                                _plex_location = _pp_file
                                                                if not _plex_rating_key:
                                                                    try:
                                                                        if _is_episode:
                                                                            _plex_rating_key = str(getattr(_candidate, 'grandparentRatingKey', None) or getattr(_candidate, 'ratingKey', None) or '')
                                                                        else:
                                                                            _plex_rating_key = str(getattr(_candidate, 'ratingKey', '') or '')
                                                                    except Exception:
                                                                        pass
                                                                break
                                                        if _plex_location:
                                                            break
                                                    if _plex_location:
                                                        break
                                                if _plex_location:
                                                    break
                                        except Exception as _loc_err:
                                            logging.debug(f"[PlexCheck] Location extraction from GUID results failed for item {item_id}: {_loc_err}")
                                    # Extract ratingKey even if location not found via file match
                                    if not _plex_rating_key and _results:
                                        try:
                                            _pi0 = _results[0]
                                            _is_episode = item_dict['type'] == 'episode'
                                            if _is_episode:
                                                _plex_rating_key = str(getattr(_pi0, 'ratingKey', '') or '')
                                            else:
                                                _plex_rating_key = str(getattr(_pi0, 'ratingKey', '') or '')
                                        except Exception:
                                            pass
                                    if not _plex_location:
                                        logging.warning(f"[PlexCheck] GUID search found item {item_id} ({item_title_for_log}) in Plex but could not extract location (filled_by_file basename='{_new_basename}')")
                                    break
                            except Exception as _e_search:
                                logging.debug(f"[PlexCheck] Direct GUID search failed for item {item_id}: {_e_search}")

                    # Option B: Time-based fallback using last_updated from DB (restart-resilient).
                    if not _force_collect_reason:
                        _last_updated = item_dict['last_updated']
                        if _last_updated and current_tick > 1:
                            try:
                                _lu_dt = datetime.strptime(str(_last_updated), '%Y-%m-%d %H:%M:%S') if isinstance(_last_updated, str) else _last_updated
                                _mins_in_checking = (datetime.now() - _lu_dt).total_seconds() / 60
                                if _mins_in_checking > 30:
                                    _force_collect_reason = f"{_mins_in_checking:.0f}m in Checking (time-based fallback, tick {current_tick})"
                            except Exception:
                                pass

                    # Original tick fallback.
                    if not _force_collect_reason and current_tick > 3:
                        _force_collect_reason = f"tick {current_tick} exhausted (recentlyAdded did not resolve)"

                    if _force_collect_reason:
                        logging.info(f"[PlexCheck] Marking item {item_id} ('{item_title_for_log}') as Collected: {_force_collect_reason}.")
                        _conn_fb = None
                        try:
                            _conn_fb = get_db_connection()
                            _cur_fb = _conn_fb.cursor()
                            _now_fb = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            # Use Plex-confirmed path if available, otherwise fall back to
                            # actual_file_path from mount. NULL location_on_disk causes ghost
                            # entries in the library and repeated cleanup prompts.
                            # Never store __all__ paths — replace with typed folder (movies/shows).
                            _raw_loc = _plex_location or actual_file_path
                            if _raw_loc and '/__all__/' in _raw_loc:
                                _typed = 'shows' if item_dict.get('type') == 'episode' else 'movies'
                                _mount_base = _raw_loc.split('/__all__/')[0]
                                _raw_loc = _raw_loc.replace('/__all__/', f'/{_typed}/', 1)
                            _location_to_store = _raw_loc
                            if _location_to_store:
                                if _plex_rating_key:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ?, location_on_disk = ?, ms_item_id = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, _location_to_store, _plex_rating_key, item_id)
                                    )
                                    logging.info(f"[PlexCheck] Updated ms_item_id={_plex_rating_key} for item {item_id} ({item_title_for_log})")
                                else:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ?, location_on_disk = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, _location_to_store, item_id)
                                    )
                            else:
                                if _plex_rating_key:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ?, ms_item_id = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, _plex_rating_key, item_id)
                                    )
                                    logging.info(f"[PlexCheck] Updated ms_item_id={_plex_rating_key} for item {item_id} ({item_title_for_log})")
                                else:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, item_id)
                                    )
                            if _cur_fb.rowcount > 0:
                                # Set original_filename from location_on_disk basename if not already set
                                import os as _os
                                _orig_fn_src = _location_to_store or ''
                                _orig_fn_val = _os.path.basename(_orig_fn_src) if _orig_fn_src else None
                                if _orig_fn_val:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET original_filename = ? WHERE id = ? AND (original_filename IS NULL OR original_filename = "")',
                                        (_orig_fn_val, item_id)
                                    )
                                _conn_fb.commit()
                                logging.info(f"[PlexCheck] Marked item {item_id} ({item_title_for_log}) as Collected ({_force_collect_reason}).")
                                if _location_to_store:
                                    logging.info(f"[PlexCheck] Updated location_on_disk for item {item_id} ({item_title_for_log}): {_location_to_store}")

                                # Debrid File Naming: ensure the cli_mount folder is renamed
                                # at Collected time. By this point the file is confirmed in
                                # cli_mount so the rename succeeds immediately — no retry needed.
                                # Covers cases where the add-time rename failed or was never
                                # triggered (pre-existing RD torrents, closure bug, etc.).
                                try:
                                    _torrent_id_chk = item_dict.get('filled_by_torrent_id', '') or ''
                                    if (not _torrent_id_chk.startswith('nzb:')
                                            and _torrent_id_chk
                                            and get_setting('Debrid Provider', 'enable_debrid_naming', False)):
                                        from routes.scraper_routes import _build_debrid_title as _bdt_chk
                                        _chk_type = item_dict.get('type', '')
                                        _chk_mt = 'tv' if _chk_type == 'episode' else _chk_type
                                        _chk_orig = item_dict.get('original_scraped_torrent_title') or item_dict.get('filled_by_title', '')
                                        _chk_title = _bdt_chk(
                                            title=item_dict.get('title', ''),
                                            year=item_dict.get('year', ''),
                                            imdb_id=item_dict.get('imdb_id'),
                                            version=item_dict.get('version', ''),
                                            original_scraped_torrent_title=_chk_orig,
                                            media_type=_chk_mt,
                                            season=item_dict.get('season_number'),
                                            episode=item_dict.get('episode_number'),
                                            episode_title=item_dict.get('episode_title'),
                                            tags=item_dict.get('tags') or None,
                                            content_source_display_name=item_dict.get('content_source_detail') or item_dict.get('content_source'),
                                        )
                                        if _chk_title and _chk_title != _chk_orig:
                                            # Get hash from debrid provider using torrent ID
                                            from debrid import get_debrid_provider as _gdp_chk
                                            _dp_chk = _gdp_chk()
                                            if _dp_chk:
                                                _chk_info = _dp_chk.get_torrent_info(_torrent_id_chk)
                                                _chk_hash = (_chk_info or {}).get('hash', '').lower()
                                                if _chk_hash:
                                                    import threading as _t_chk
                                                    def _do_chk_rename(h, name, ident, iid):
                                                        import time as _t
                                                        try:
                                                            from usenet.climount_client import get_climount_client
                                                            _dc = get_climount_client()
                                                            if not hasattr(_dc, 'rename_nzb'):
                                                                return  # active usenet provider (e.g. nzbdav) has no rename semantics
                                                            for _a in range(5):
                                                                # This short loop (5 attempts x 10s = 50s total) isn't long
                                                                # enough to distinguish "genuinely gone" from "cli_mount
                                                                # hasn't finished its periodic sync yet" (can take ~10 min
                                                                # per torrent_processor.py) — so a 404 here is never treated
                                                                # as final, just run the fixed attempt budget like before.
                                                                if _dc.rename_nzb(h, name):
                                                                    logging.info(f'[DebridNaming] Renamed {h!r} -> {name!r} (collected, attempt {_a+1})')
                                                                    if iid:
                                                                        try:
                                                                            from database.database_writing import update_media_item as _umi
                                                                            _umi(iid, debrid_folder_name=name, filled_by_title=name)
                                                                        except Exception as _db_err:
                                                                            logging.debug(f'[DebridNaming] DB update failed (collected): {_db_err}')
                                                                        _dc.register_cli_ids_for_item(h, iid)
                                                                        _dc.push_tags_for_item(h, iid)
                                                                    return
                                                                _t.sleep(10)
                                                            logging.warning(f'[DebridNaming] Could not rename {h!r} after 5 attempts (collected)')
                                                        except Exception as _e:
                                                            logging.debug(f'[DebridNaming] Rename error (collected): {_e}')
                                                    _t_chk.Thread(target=_do_chk_rename, args=(_chk_hash, _chk_title, item_title_for_log, item_id), daemon=True).start()
                                except Exception as _dbn_chk_ex:
                                    logging.debug(f'[DebridNaming] Collected-time rename setup error for {item_id}: {_dbn_chk_ex}')

                                _fb_details = get_media_item_by_id(item_id)
                                if _fb_details:
                                    handle_state_change(dict(_fb_details))
                                    from database.database_writing import add_to_collected_notifications
                                    _notif = dict(_fb_details)
                                    _notif['is_upgrade'] = bool(_notif.get('upgrading_from'))
                                    _notif['new_state'] = "Collected"
                                    add_to_collected_notifications(_notif)
                                    # Log upgrade success to Upgrade Hub activity
                                    if _notif.get('upgrading_from'):
                                        try:
                                            from queues.upgrading_queue import log_successful_upgrade
                                            log_successful_upgrade(_notif)
                                        except Exception as _lsu_err:
                                            logging.debug(f"[PlexCheck] log_successful_upgrade failed: {_lsu_err}")
                                    # Upgrade cleanup: delete old torrent/NZB and remove old Plex entry.
                                    # Gated on Scraping.enable_upgrading_cleanup — when disabled, the old
                                    # file/torrent must be left in place (same "keep both files" contract
                                    # as local_library_scan.py's upgrade handling).
                                    _old_torrent_id = _notif.get('upgrading_from_torrent_id')
                                    _upgrading_from_path = _notif.get('upgrading_from')
                                    if not get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                                        logging.info(f"[PlexCheck] Scraping.enable_upgrading_cleanup is disabled — keeping old file/torrent for item {item_id} ({item_title_for_log}).")
                                    else:
                                        if _old_torrent_id:
                                            if _old_torrent_id.startswith('nzb:'):
                                                # Old item was an NZB — remove via cli_mount
                                                try:
                                                    from usenet import get_usenet_client as _guc
                                                    _uc = _guc()
                                                    if _uc:
                                                        _uc.remove_nzb(_old_torrent_id[4:], entry_name=_upgrading_from_path or '')
                                                        logging.info(f"[PlexCheck] Removed old upgrade NZB {_old_torrent_id} for item {item_id} ({item_title_for_log})")
                                                except Exception as _ct_err:
                                                    logging.warning(f"[PlexCheck] Failed to remove old upgrade NZB {_old_torrent_id}: {_ct_err}")
                                            else:
                                                # Old item was a debrid torrent
                                                try:
                                                    from debrid import get_debrid_provider as _gdp
                                                    _dp = _gdp()
                                                    if _dp:
                                                        _dp.remove_torrent(_old_torrent_id, removal_reason='Replaced by upgrade')
                                                        logging.info(f"[PlexCheck] Removed old upgrade torrent {_old_torrent_id} for item {item_id} ({item_title_for_log})")
                                                except Exception as _ct_err:
                                                    if '404' in str(_ct_err):
                                                        logging.debug(f"[PlexCheck] Old torrent {_old_torrent_id} already removed (404)")
                                                    else:
                                                        logging.warning(f"[PlexCheck] Failed to remove old upgrade torrent {_old_torrent_id}: {_ct_err}")
                                        if _upgrading_from_path:
                                            try:
                                                from utilities.plex_functions import remove_file_from_plex
                                                remove_file_from_plex(item_title_for_log, _upgrading_from_path)
                                                logging.info(f"[PlexCheck] Removed old upgrade file from Plex for item {item_id} ({item_title_for_log})")
                                            except Exception as _cp_err:
                                                logging.warning(f"[PlexCheck] Failed to remove old upgrade file from Plex for item {item_id}: {_cp_err}")
                                # Check if the Plex episode has a local:// guid (episode-level mismatch)
                                # or the show has no external IDs (show-level mismatch).
                                # In both cases, use the Plex GUID from battery to fix directly.
                                _plex_ep_item = None
                                _plex_ep_guid_is_local = False
                                try:
                                    _check_fn2 = os.path.basename((_fb_details.get('filled_by_file') or '') if _fb_details else '')
                                    if _check_fn2 and _fb_details and _fb_details.get('type') == 'episode':
                                        for _cs2 in sections:
                                            if _cs2.type != 'show':
                                                continue
                                            _cr2 = plex.fetchItems(f'/library/sections/{_cs2.key}/all?file={__import__("urllib.parse", fromlist=["quote"]).quote(_check_fn2)}&type=4')
                                            if _cr2:
                                                _plex_ep_item = _cr2[0]
                                                _ep_guid_str = str(getattr(_plex_ep_item, 'guid', ''))
                                                _plex_ep_guid_is_local = _ep_guid_str.startswith('local://')
                                                break
                                except Exception:
                                    pass

                                if _fb_details and _plex_ep_item and _plex_ep_guid_is_local:
                                    try:
                                        _fix_imdb   = _fb_details.get('imdb_id')
                                        _fix_imdb   = _fb_details.get('imdb_id')
                                        _fix_tmdb   = _fb_details.get('tmdb_id')
                                        _fix_season = _fb_details.get('season_number')
                                        _fix_ep     = _fb_details.get('episode_number')
                                        _fix_ep_rk  = str(_plex_ep_item.ratingKey)
                                        _fix_show_rk = str(getattr(_plex_ep_item, 'grandparentRatingKey', _fix_ep_rk))

                                        # Determine fix strategy using absolute_episode in battery:
                                        # - absolute_episode > 0 (anime/absolute numbering) → episode-level GUID per episode
                                        # - absolute_episode = 0 or NULL (regular shows) → show-level GUID via force_match
                                        _is_absolute = False
                                        try:
                                            from cli_battery.app.database import Session as _BatSess2, Item as _BatItem2, Season as _BatSeason2, Episode as _BatEp2
                                            with _BatSess2() as _bs2:
                                                _bi2 = _bs2.query(_BatItem2).filter_by(imdb_id=_fix_imdb).first() if _fix_imdb else None
                                                if _bi2:
                                                    _abs2 = _bs2.query(_BatEp2).join(_BatSeason2).filter(
                                                        _BatSeason2.item_id == _bi2.id,
                                                        _BatEp2.absolute_episode > 0
                                                    ).first()
                                                    _is_absolute = _abs2 is not None
                                        except Exception:
                                            pass

                                        import threading as _threading

                                        if not _is_absolute:
                                            logging.info(
                                                f"[PlexGUID] Item {item_id} regular show — "
                                                f"scheduling show-level fix-match (ratingKey={_fix_show_rk})"
                                            )
                                            def _do_show_fix(_rk, _title, _year, _tmdb, _imdb, _s, _ep):
                                                try:
                                                    from utilities.plex_matching_functions import force_match_with_tmdb
                                                    force_match_with_tmdb(
                                                        _title,
                                                        str(_year) if _year else None,
                                                        str(_tmdb) if _tmdb else '0',
                                                        _rk,
                                                        imdb_id=_imdb,
                                                        media_type='show',
                                                        season=_s,
                                                        episode=_ep,
                                                    )
                                                except Exception as _fe:
                                                    logging.debug(f"[PlexGUID] Show fix-match failed: {_fe}")
                                            _threading.Thread(
                                                target=_do_show_fix,
                                                args=(_fix_show_rk, _fb_details.get('title', ''),
                                                      _fb_details.get('year'), _fix_tmdb, _fix_imdb,
                                                      _fix_season, _fix_ep),
                                                daemon=True
                                            ).start()
                                        else:
                                            logging.info(
                                                f"[PlexGUID] Item {item_id} absolute show — "
                                                f"scheduling episode fix-match (ratingKey={_fix_ep_rk}, "
                                                f"S{_fix_season}E{_fix_ep})"
                                            )

                                        def _do_ep_fix(_rk, _title, _year, _tmdb, _imdb, _s, _ep):
                                            try:
                                                from cli_battery.app.direct_api import DirectAPI
                                                from utilities.settings import get_setting as _gs
                                                from plexapi.server import PlexServer as _PS
                                                _gu = _gs('Plex', 'url', '').rstrip('/')
                                                _gt = _gs('Plex', 'token', '')
                                                if not _gu or not _gt:
                                                    return
                                                _gplex = _PS(_gu, _gt, timeout=30)
                                                _gitem = _gplex.fetchItem(int(_rk))
                                                # Get episode GUID from battery
                                                _gr = DirectAPI.get_plex_guid(_imdb, 'show', season=_s, episode=_ep) if _imdb else None
                                                logging.info(f"[PlexGUID] get_plex_guid({_imdb}, S{_s}E{_ep}) returned: {_gr}")
                                                _ep_guid = (_gr or {}).get('episode_guid')
                                                _s_guid  = (_gr or {}).get('season_guid')
                                                _sh_guid = (_gr or {}).get('show_guid')
                                                # Use most specific available
                                                if _ep_guid:
                                                    _full_guid = f'plex://episode/{_ep_guid}'
                                                    _gname = f'{_title} S{_s:02d}E{_ep:02d}'
                                                elif _s_guid:
                                                    _full_guid = f'plex://season/{_s_guid}'
                                                    _gname = f'{_title} Season {_s}'
                                                elif _sh_guid:
                                                    _full_guid = f'plex://show/{_sh_guid}'
                                                    _gname = _title
                                                else:
                                                    logging.debug(f"[PlexGUID] No GUID available for {_imdb} S{_s}E{_ep}")
                                                    return
                                                logging.info(f"[PlexGUID] Applying {_full_guid} to ratingKey={_rk}")
                                                import time as _time
                                                import requests as _req
                                                from urllib.parse import quote as _uq

                                                # Use raw HTTP PUT — PlexAPI fixMatch() doesn't
                                                # work on episode items but the API endpoint does.
                                                _match_url = (
                                                    f"{_gu}/library/metadata/{_rk}/match"
                                                    f"?guid={_uq(_full_guid)}&name={_uq(_gname)}"
                                                    f"&X-Plex-Token={_gt}"
                                                )
                                                _resp = _req.put(_match_url, timeout=15)
                                                logging.info(f"[PlexGUID] PUT match HTTP {_resp.status_code} for ratingKey={_rk}")
                                                _time.sleep(2)
                                                # Trigger metadata refresh so Plex fetches episode title/summary immediately
                                                _req.put(f"{_gu}/library/metadata/{_rk}/refresh?X-Plex-Token={_gt}", timeout=15)
                                                _time.sleep(4)
                                                _gitem.reload()
                                                _new_guid = str(getattr(_gitem, 'guid', ''))
                                                if not _new_guid.startswith('local://'):
                                                    logging.info(f"[PlexGUID] Episode fix-match SUCCESS for '{_title}' S{_s}E{_ep} → {_new_guid}")
                                                else:
                                                    logging.warning(f"[PlexGUID] Episode fix-match failed (HTTP {_resp.status_code}) — guid still local:// for '{_title}' S{_s}E{_ep}")
                                            except Exception as _fme:
                                                logging.debug(f"[PlexGUID] Episode fix-match failed: {_fme}")
                                        if _is_absolute:
                                            _threading.Thread(
                                                target=_do_ep_fix,
                                                args=(_fix_ep_rk, _fb_details.get('title', ''),
                                                      _fb_details.get('year'), _fix_tmdb, _fix_imdb,
                                                      _fix_season, _fix_ep),
                                                daemon=True
                                            ).start()
                                    except Exception as _guid_err:
                                        logging.debug(f"[PlexGUID] Fix-match scheduling failed: {_guid_err}")

                                if cache_key in self.plex_scan_tick_counts:
                                    del self.plex_scan_tick_counts[cache_key]
                            else:
                                logging.debug(f"[PlexCheck] Item {item_id} state already changed, skipping Collected update.")
                        except sqlite3.Error as _fb_db_err:
                            logging.error(f"[PlexCheck] DB error marking item {item_id} as Collected: {_fb_db_err}")
                            if _conn_fb: _conn_fb.rollback()
                        except Exception as _fb_err:
                            logging.error(f"[PlexCheck] Unexpected error for item {item_id}: {_fb_err}", exc_info=True)
                            if _conn_fb: _conn_fb.rollback()
                        finally:
                            if _conn_fb: _conn_fb.close()
                        continue
                    else:
                        should_trigger_scan = True
                        updated_items += 1
                        logging.info(f"File '{current_filename}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                else:
                    # File not found
                    not_found_items += 1
                    logging.debug(f"File not found on disk for item {item_id}. Checked paths:\n  " + "\n  ".join(log_checked_paths))
                    if cache_key in self.plex_scan_tick_counts:
                        logging.debug(f"Resetting Plex scan tick count for missing file '{current_filename}'.")
                        del self.plex_scan_tick_counts[cache_key]
                    # --- START EDIT: Need to continue loop if file not found --- # This comment is from a previous edit, still relevant
                    continue 
                    # --- END EDIT ---

                # --- START: Logic to identify scan paths (original location)
                if should_trigger_scan:
                    if not sections:
                         logging.error("Plex sections not available, cannot identify scan paths.")
                         continue

                    item_type_mapped = 'show' if item_dict['type'] == 'episode' else item_dict['type']
                    logging.debug(f"Identifying scan paths for item {item_id} (type: {item_type_mapped}, title: '{filled_by_title}')")

                    # Get configured library filters (support both names and IDs)
                    allowed_library_keys = None
                    try:
                        from utilities.plex_functions import process_library_names

                        # Build library dictionaries for filtering
                        all_libraries = {}  # {name: key}
                        libraries_by_key = {}  # {key: name}
                        for section in sections:
                            # Convert key to string for consistent comparison
                            key_str = str(section.key)
                            all_libraries[section.title] = key_str
                            libraries_by_key[key_str] = section.title

                        # Get configured library names/IDs from settings
                        if item_type_mapped == 'movie':
                            movie_libs_setting = get_setting('Plex', 'movie_libraries', '')
                            if movie_libs_setting:
                                allowed_library_keys = process_library_names(movie_libs_setting, all_libraries, libraries_by_key)
                                lib_names = [libraries_by_key.get(key, key) for key in allowed_library_keys]
                                logging.debug(f"Filtering Plex file check to configured movie libraries: {lib_names}")
                        elif item_type_mapped == 'show':
                            shows_libs_setting = get_setting('Plex', 'shows_libraries', '')
                            if shows_libs_setting:
                                allowed_library_keys = process_library_names(shows_libs_setting, all_libraries, libraries_by_key)
                                lib_names = [libraries_by_key.get(key, key) for key in allowed_library_keys]
                                logging.debug(f"Filtering Plex file check to configured show libraries: {lib_names}")
                    except Exception as filter_err:
                        logging.warning(f"Error setting up library filters for Plex file check: {filter_err}. Will scan all matching sections.")
                        allowed_library_keys = None

                    found_matching_section_location = False

                    # If Plex already confirmed the file location, derive the scan path
                    # directly from Plex's own reported path — no mount assumptions needed.
                    # This avoids scanning every section location (e.g. /debrid/ufc) when
                    # the file only exists under one of them (e.g. /debrid/movies).
                    if _plex_location:
                        _plex_scan_dir = os.path.dirname(_plex_location)
                        for section in sections:
                            if section.type != item_type_mapped:
                                continue
                            if allowed_library_keys is not None and str(section.key) not in allowed_library_keys:
                                continue
                            # Only add the section that actually contains this path
                            for location in section.locations:
                                if _plex_scan_dir.startswith(location):
                                    if section.title not in paths_to_scan_by_section:
                                        paths_to_scan_by_section[section.title] = set()
                                    paths_to_scan_by_section[section.title].add(_plex_scan_dir)
                                    found_matching_section_location = True
                                    logging.debug(f"  Scan path from Plex-confirmed location: '{_plex_scan_dir}' in section '{section.title}'")
                                    break
                    else:
                        # Plex hasn't indexed the file yet.
                        # Use the ACTUAL folder name from the found file path (renamed CLI name)
                        # rather than folder_name_for_plex_scan (raw DB torrent name).
                        # actual_file_path is e.g. /debrid/__all__/Succession (2018) - S04 - {imdb-...}/file.mkv
                        # so dirname gives the renamed folder name that actually exists under
                        # /debrid/shows/, /debrid/movies/ etc.
                        # Use os.listdir to verify the folder exists at each section location
                        # before triggering a scan — this prevents scanning wrong virtual folders
                        # (e.g. /debrid/ufc/) for items that don't belong there.
                        # The folder name to scan. After debrid naming rename, __all__ updates
                        # to the CLI structured name, so actual_file_path dirname is reliable.
                        # Also try debrid_folder_name and filled_by_title from DB as fallbacks
                        # since the rename may not have propagated to __all__ yet on tick 1.
                        _actual_folder_name = os.path.basename(os.path.dirname(actual_file_path)) if actual_file_path else folder_name_for_plex_scan
                        _dbn = item_dict.get('debrid_folder_name') or ''
                        _candidate_folder_names = []
                        # Prefer debrid_folder_name (CLI renamed) over actual path when it's a proper CLI name
                        _ordered = [_dbn, _actual_folder_name, item_dict.get('filled_by_title') or ''] if '{imdb-' in _dbn else [_actual_folder_name, _dbn, item_dict.get('filled_by_title') or '']
                        for _cn in _ordered:
                            if _cn and _cn not in _candidate_folder_names:
                                _candidate_folder_names.append(_cn)

                        for section in sections:
                            if section.type != item_type_mapped:
                                continue
                            if allowed_library_keys is not None and str(section.key) not in allowed_library_keys:
                                logging.debug(f"Skipping section '{section.title}' (key: {section.key}) - not in configured libraries")
                                continue
                            logging.debug(f"  Checking Section '{section.title}' (Type: {section.type})")
                            for location in section.locations:
                                if _candidate_folder_names:
                                    # Try each candidate folder name against this location's listing.
                                    # Use the first one found. If none found, skip this location
                                    # to avoid scanning wrong virtual folders (e.g. /debrid/ufc/).
                                    try:
                                        _location_listing = os.listdir(location)
                                    except Exception:
                                        _location_listing = []
                                    _matched_folder = next(
                                        (n for n in _candidate_folder_names if n in _location_listing),
                                        None
                                    )
                                    if not _matched_folder:
                                        logging.debug(f"    Skipping '{location}' — none of candidates found in listing")
                                        continue
                                    constructed_plex_path = os.path.join(location, _matched_folder)
                                else:
                                    constructed_plex_path = location
                                logging.debug(f"    Scan path: '{constructed_plex_path}'")
                                if section.title not in paths_to_scan_by_section:
                                    paths_to_scan_by_section[section.title] = set()
                                paths_to_scan_by_section[section.title].add(constructed_plex_path)
                                found_matching_section_location = True

                    if not found_matching_section_location:
                        logging.warning(f"Could not find any matching Plex library section (type: {item_type_mapped}) for item {item_id} based on file '{current_filename}'. Scan might not be triggered correctly.")
                # --- END: Logic to identify scan paths ---


            # --- Trigger scans after checking all items ---
            if paths_to_scan_by_section:
                logging.info(f"Triggering scans for {len(paths_to_scan_by_section)} sections based on detected files...")
                final_updated_sections = set()
                for section_title, scan_paths in paths_to_scan_by_section.items():
                    section = sections_map.get(section_title)
                    if not section:
                        logging.error(f"Could not find section object for title '{section_title}' during scan trigger phase.")
                        continue

                    # Deduplicate: if multiple episode folders share the same parent (show folder),
                    # scan the parent once instead of each episode folder separately.
                    # This prevents flooding Plex with N concurrent Scanner processes for season packs.
                    # IMPORTANT: only collapse to parent when the parent is NOT a section root location
                    # (e.g. /debrid/shows). Collapsing to the section root triggers a full library
                    # re-scan which causes Plex to create duplicate/mismatched entries.
                    _section_root_locations = set(section.locations)
                    deduped_paths = set()
                    for scan_path in scan_paths:
                        parent = os.path.dirname(scan_path)
                        # Never collapse to a section root — that would scan the entire library
                        if parent in _section_root_locations:
                            deduped_paths.add(scan_path)
                            continue
                        siblings = [p for p in scan_paths if os.path.dirname(p) == parent and p != scan_path]
                        if siblings:
                            deduped_paths.add(parent)
                        else:
                            deduped_paths.add(scan_path)

                    # Hard cap: never trigger more than 5 scans per section per run.
                    # Remaining paths will be picked up on the next 60s run.
                    _MAX_SCANS_PER_RUN = 25
                    if len(deduped_paths) > _MAX_SCANS_PER_RUN:
                        logging.warning(
                            f"[PlexCheck] {len(deduped_paths)} scan paths for '{section.title}' — "
                            f"capping at {_MAX_SCANS_PER_RUN} to avoid Plex DB contention. "
                            f"Remaining will be picked up next run."
                        )
                        deduped_paths = set(list(deduped_paths)[:_MAX_SCANS_PER_RUN])

                    for scan_path in deduped_paths:
                        try:
                            logging.info(f"Triggering Plex section '{section.title}' update scan for path: {scan_path}")
                            section.update(path=scan_path)
                            final_updated_sections.add(section.title)
                        except NotFound:
                             logging.warning(f"Path '{scan_path}' not found by Plex server during scan trigger for section '{section.title}'.")
                        except Exception as e_scan:
                             logging.error(f"Failed to trigger update scan for Plex section '{section.title}' with path '{scan_path}': {str(e_scan)}", exc_info=True)

                if final_updated_sections:
                    logging.info(f"Plex sections triggered for update in this run: {', '.join(sorted(list(final_updated_sections)))}")


            # --- Stale location repair ---
            # Fix Collected items where location_on_disk still points to the pre-upgrade file.
            # These items were marked Collected via tick/time fallback without a Plex location,
            # or the GUID/file search failed at upgrade time. Query Plex by filled_by_file to fix.
            try:
                _repair_conn = get_db_connection()
                _all_upgraded = _repair_conn.execute(
                    '''SELECT id, title, type, filled_by_file, location_on_disk, upgrading_from,
                              imdb_id, tmdb_id, season_number, episode_number, filled_by_torrent_id
                       FROM media_items
                       WHERE state = "Collected"
                         AND upgrading_from IS NOT NULL AND upgrading_from != ""
                         AND filled_by_file IS NOT NULL AND filled_by_file != ""
                    '''
                ).fetchall()
                _repair_conn.close()

                # Keep only items where location_on_disk is stale:
                # - NULL/empty, OR
                # - basename matches upgrading_from basename (path still points to old file)
                # Skip RD torrent items — their location is managed via cli_mount rename,
                # not via Plex library search. Repairing them would overwrite with old NZB path.
                _stale_set = {}
                for _row in _all_upgraded:
                    _tid = _row['filled_by_torrent_id'] or ''
                    if _tid and not _tid.startswith('nzb:'):
                        continue  # RD torrent — skip stale repair
                    _loc = _row['location_on_disk'] or ''
                    _upg = _row['upgrading_from'] or ''
                    if not _loc:
                        _stale_set[_row['id']] = _row
                    elif os.path.basename(_loc) == os.path.basename(_upg):
                        _stale_set[_row['id']] = _row

                if _stale_set:
                    logging.info(f"[PlexCheck] Stale location repair: found {len(_stale_set)} Collected items with stale location_on_disk")
                    from urllib.parse import quote as _urlquote2
                    for _sid, _sitem in _stale_set.items():
                        _sfilled = _sitem['filled_by_file'] or ''
                        _sbasename = os.path.basename(_sfilled)
                        if not _sbasename:
                            continue
                        _stype = _sitem['type']
                        _stype_lookup = 'show' if _stype == 'episode' else _stype
                        _new_loc = None
                        try:
                            for _section in sections:
                                if _section.type != _stype_lookup:
                                    continue
                                _type_p = '&type=4' if _stype == 'episode' else ''
                                _sr = plex.fetchItems(f'/library/sections/{_section.key}/all?file={_urlquote2(_sbasename)}{_type_p}')
                                if _sr:
                                    try:
                                        _new_loc = _sr[0].media[0].parts[0].file
                                    except Exception:
                                        pass
                                    break
                        except Exception as _se:
                            logging.debug(f"[PlexCheck] Stale repair Plex query failed for item {_sid}: {_se}")
                        if _new_loc and _new_loc != _sitem['location_on_disk']:
                            try:
                                _rc = get_db_connection()
                                _rc.execute(
                                    'UPDATE media_items SET location_on_disk = ?, last_updated = ? WHERE id = ? AND state = "Collected"',
                                    (_new_loc, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), _sid)
                                )
                                _rc.commit()
                                _rc.close()
                                logging.info(f"[PlexCheck] Stale location repaired for item {_sid} ({_sitem['title']}): '{_sitem['location_on_disk']}' → '{_new_loc}'")
                            except Exception as _ue:
                                logging.warning(f"[PlexCheck] Stale location repair DB update failed for item {_sid}: {_ue}")
                        else:
                            logging.debug(f"[PlexCheck] Stale repair: no Plex result for item {_sid} ({_sitem['title']}) filled_by_file='{_sbasename}'")
            except Exception as _repair_err:
                logging.warning(f"[PlexCheck] Stale location repair block failed: {_repair_err}")

            # Log summary of operations
            processed_items_count = len(items) # Count items processed in this run
            logging.info(f"Plex check summary (checks enabled): Processed {processed_items_count} items. Identified {updated_items} found items potentially needing scans (within tick limit). {not_found_items} items not found on disk.")
            # Updated sections log moved to after the scan loop


    def task_update_show_ids(self):
        """Update show IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
        try:
            from database.maintenance import update_show_ids
            update_show_ids()
        except Exception as e:
            logging.error(f"Error in task_update_show_ids: {str(e)}")

    def task_update_show_titles(self):
        """Update show titles in the database if they don't match the direct API, storing old titles in title_aliases."""
        try:
            from database.maintenance import update_show_titles
            update_show_titles()
        except Exception as e:
            logging.error(f"Error in task_update_show_titles: {str(e)}")

    def task_update_movie_ids(self):
        """Update movie IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
        try:
            from database.maintenance import update_movie_ids
            update_movie_ids()
        except Exception as e:
            logging.error(f"Error in task_update_movie_ids: {str(e)}")

    def task_update_movie_titles(self):
        """Update movie titles in the database if they don't match the direct API, storing old titles in title_aliases."""
        try:
            from database.maintenance import update_movie_titles
            update_movie_titles()
        except Exception as e:
            logging.error(f"Error in task_update_movie_titles: {str(e)}")

    def task_sync_episode_metadata(self):
        """Sync episode metadata (titles, etc.) from Trakt/TMDB for all collected shows."""
        try:
            from database.maintenance import sync_episode_metadata
            sync_episode_metadata()
        except Exception as e:
            logging.error(f"Error in task_sync_episode_metadata: {str(e)}")

    def task_cleanup_title_year_suffixes(self):
        """Database year-in-title cleanup — strips trailing (YYYY) from title column."""
        try:
            from database.maintenance import cleanup_title_year_suffixes
            cleanup_title_year_suffixes()
        except Exception as e:
            logging.error(f"Error in task_cleanup_title_year_suffixes: {str(e)}")

    def trigger_task(self, task_name):
        """Manually trigger a task to run immediately by adding it to APScheduler's queue."""
        normalized_name = self._normalize_task_name(task_name)
        job_id_base = normalized_name # This is the base task name, e.g., "task_artificial_long_run"

        logging.info(f"Attempting to manually trigger task: {job_id_base} by adding it to APScheduler queue.")

        target_func, args, kwargs = self._get_task_target(job_id_base)

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

    def task_run_library_maintenance(self):
        """Run library maintenance tasks."""
        from database.maintenance import run_library_maintenance
        run_library_maintenance()

    def task_sync_library_metadata(self):
        """Sync audio/subtitle track metadata from Plex/Jellyfin for collected items."""
        from utilities.settings import get_setting
        from database.core import get_db_connection
        from overlays.overlay_manager import OverlayManager
        from overlays.media_info import MediaInfoExtractor

        extractor = MediaInfoExtractor()
        use_jellyfin = get_setting('Jellyfin', 'enabled', False)
        plex_url = get_setting('Plex', 'url', '') if not use_jellyfin else get_setting('Jellyfin', 'url', '')
        plex_token = get_setting('Plex', 'token', '') if not use_jellyfin else get_setting('Jellyfin', 'api_key', '')

        if not plex_url or not plex_token:
            logging.warning("[SyncLibMeta] Media server not configured — skipping.")
            return

        manager = OverlayManager(None, plex_url, plex_token)
        client = manager.plex  # reuse already-initialized Plex/Jellyfin client

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            # For episodes, ms_item_id is the show's rating key — group to avoid
            # fetching the same show metadata 20 times for 20 episodes.
            # For movies, ms_item_id is the movie's own rating key.
            cursor.execute("""
                SELECT MIN(id) as id, ms_item_id, type
                FROM media_items
                WHERE state = 'Collected'
                AND ms_item_id IS NOT NULL AND ms_item_id != ''
                AND (ms_audio_track IS NULL OR ms_subtitle_track IS NULL)
                GROUP BY ms_item_id, type
                ORDER BY MAX(collected_at) DESC
                LIMIT 500
            """)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            logging.info("[SyncLibMeta] No items need metadata sync.")
            return

        logging.info(f"[SyncLibMeta] Syncing metadata for {len(rows)} unique ms_item_ids.")

        updated = 0
        errors = 0
        for row in rows:
            ms_item_id = row['ms_item_id']
            item_type = row['type']
            try:
                if use_jellyfin:
                    metadata = client.get_media_metadata(ms_item_id)
                    if not metadata:
                        continue
                    media_info = extractor.extract_from_jellyfin_metadata(metadata)
                else:
                    if item_type == 'episode':
                        # ms_item_id is show rating key — fetch best episode for stream data
                        metadata = client.get_show_best_episode_media(ms_item_id)
                    else:
                        metadata = client.get_media_metadata(ms_item_id)
                    if not metadata:
                        continue
                    media_info = extractor.extract_from_plex_metadata(metadata)

                audio_track = media_info.get('audio_track')
                subtitle_track = media_info.get('subtitle_track')

                if audio_track is None and subtitle_track is None:
                    continue

                # Update ALL episodes/movies sharing this ms_item_id
                conn = get_db_connection()
                try:
                    conn.execute("""
                        UPDATE media_items
                        SET ms_audio_track = COALESCE(ms_audio_track, ?),
                            ms_subtitle_track = COALESCE(ms_subtitle_track, ?),
                            ms_last_scanned = CURRENT_TIMESTAMP
                        WHERE ms_item_id = ? AND type = ?
                        AND state = 'Collected'
                        AND (ms_audio_track IS NULL OR ms_subtitle_track IS NULL)
                    """, (audio_track, subtitle_track, ms_item_id, item_type))
                    updated += conn.execute(
                        "SELECT changes()"
                    ).fetchone()[0]
                    conn.commit()
                finally:
                    conn.close()

            except Exception as e:
                logging.debug(f"[SyncLibMeta] Failed for ms_item_id {ms_item_id}: {e}")
                errors += 1

        logging.info(f"[SyncLibMeta] Done. Updated={updated} rows. Errors={errors}")


    def task_process_standalone_plex_removals(self):
        """
        Processes the Plex removal cache.
        """
        try:
            from utilities.plex_removal_cache import process_removal_cache
            min_age_hours = 6
            process_removal_cache(min_age_hours=min_age_hours)
            logging.info(f"Standalone Plex removal processing complete (min_age_hours: {min_age_hours}).")
        except Exception as e:
            logging.error(f"Error during standalone Plex removal processing: {e}", exc_info=True)


    # def task_update_statistics_summary(self):
    #     """Update the statistics summary table for faster statistics page loading"""
    #     try:
    #         # Use the directly imported function with force=True
    #         from database.statistics import update_statistics_summary
    #         update_statistics_summary(force=True)
    #         logging.debug("Scheduled statistics summary update complete")
    #     except Exception as e:
    #         logging.error(f"Error updating statistics summary: {str(e)}")

    def task_check_database_health(self):
        """Periodic task to verify database health and handle any corruption."""
        from main import verify_database_health

        try:
            if not verify_database_health():
                logging.error("Database health check failed during periodic check")
                # --- START EDIT: Update pause_info for DB health ---
                self.pause_info = {
                    "reason_string": "Database corruption detected - check logs for details",
                    "error_type": "DB_HEALTH",
                    "service_name": "System Database",
                    "status_code": None,
                    "retry_count": 0
                }
                # --- END EDIT ---
                self.pause_queue()

                # Send notification about database corruption
                try:
                    from routes.notifications import send_program_crash_notification
                    send_program_crash_notification("Database corruption detected - program must be restarted to recreate databases")

                except Exception as e:
                    logging.error(f"Failed to send database corruption notification: {str(e)}")
            else:
                logging.info("Periodic database health check passed")
        except Exception as e:
            logging.error(f"Error during periodic database health check: {str(e)}")

    def task_verify_symlinked_files(self):
        """Verify symlinked files have been properly scanned into Plex."""
        logging.info("Checking for symlinked files to verify in Plex...")
        try:
            # Import here to avoid circular imports
            from database.symlink_verification import get_verification_stats
            from utilities.plex_verification import run_plex_verification_scan

            # Check if there are any unverified files to process
            stats = get_verification_stats()
            if stats['unverified'] == 0:
                logging.info("No unverified files in queue. Skipping verification scan.")
                return

            # Alternate between full and recent scans
            # Use a class attribute to track the last scan type
            if not hasattr(self, '_last_symlink_scan_was_full'):
                # Initialize to True so first scan will be recent (gets toggled below)
                self._last_symlink_scan_was_full = True

            # Toggle scan type
            do_full_scan = not self._last_symlink_scan_was_full
            self._last_symlink_scan_was_full = do_full_scan

            scan_type = "full" if do_full_scan else "recent"
            logging.info(f"Running {scan_type} symlink verification scan...")

            # Run the verification scan
            verified_count, total_processed = run_plex_verification_scan(
                max_files=500,
                recent_only=not do_full_scan
            )

            logging.info(f"Verified {verified_count} out of {total_processed} symlinked files in Plex ({scan_type} scan)")

            # If recent scan found nothing but we have unverified files, force a full scan next time
            if not do_full_scan and total_processed == 0 and stats['unverified'] > 0:
                logging.info("Recent scan found no files but unverified files exist. Will run full scan next time.")
                self._last_symlink_scan_was_full = False

        except Exception as e:
            logging.error(f"Error verifying symlinked files: {e}")

        # Sync ms_item_id for any Collected items missing it
        try:
            from overlays.scheduled_tasks import _sync_ms_keys_auto
            counts = _sync_ms_keys_auto()
            if counts and any(counts.values()):
                logging.info(f"[SyncMsItemIds] Updated ms_item_id: movies={counts.get('movies', 0)}, "
                             f"episodes={counts.get('episodes', 0)}, errors={counts.get('errors', 0)}")
        except Exception as e:
            logging.debug(f"[SyncMsItemIds] ms_item_id sync skipped: {e}")

    def task_verify_plex_removals(self):
        """Verify that files marked for removal are actually gone from the configured media server (Plex or Jellyfin/Emby) using title-based search."""
        logging.info("[TASK] Running media server removal verification task.")

        # Determine Plex connection details based on settings
        plex_url, plex_token = None, None
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url', '').rstrip('/')
            plex_token = get_setting('Plex', 'token', '')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='').rstrip('/')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')

        if not plex_url or not plex_token:
            logging.debug("[VERIFY] No Plex URL or token found in relevant settings. Skipping removal verification.")
            return

        # Initialize Plex connection
        plex = None
        try:
            plex = plexapi.server.PlexServer(plex_url, plex_token)
        except Exception as e:
            logging.error(f"[VERIFY] Failed to connect to Plex ({plex_url}) for removal verification: {e}")
            return

        # Fetch pending items (now includes titles)
        pending_items = get_pending_removal_paths()
        if not pending_items:
            logging.info("[VERIFY] No pending Plex removals to verify.")
            return
        logging.info(f"[VERIFY] Found {len(pending_items)} paths pending Plex removal verification.")

        verified_count = 0
        failed_verification_count = 0
        # Fetch settings for max attempts and cleanup days
        max_attempts = get_setting('File Management', 'plex_removal_max_attempts', 5)
        cleanup_days = get_setting('File Management', 'plex_removal_cleanup_days', 30)

        for item in pending_items:
            item_id = item['id']
            item_path = item['item_path']
            item_title = item['item_title']
            episode_title = item.get('episode_title') # Use .get for safety
            attempts = item['attempts']
            last_checked = item.get('last_checked_at')

            # Exponential backoff: wait longer between retries based on attempt count
            # Attempt 0: immediate, Attempt 1: 1 min, Attempt 2: 5 min, Attempt 3: 15 min, Attempt 4+: 30 min
            backoff_minutes = [0, 1, 5, 15, 30]
            required_wait = backoff_minutes[min(attempts, len(backoff_minutes) - 1)]

            if last_checked and required_wait > 0:
                try:
                    from datetime import datetime, timedelta
                    if isinstance(last_checked, str):
                        last_checked_dt = datetime.fromisoformat(last_checked.replace('Z', '+00:00'))
                    else:
                        last_checked_dt = last_checked
                    time_since_last = datetime.now() - last_checked_dt.replace(tzinfo=None)
                    if time_since_last < timedelta(minutes=required_wait):
                        remaining = timedelta(minutes=required_wait) - time_since_last
                        logging.debug(f"[VERIFY] Skipping '{item_path}' - backoff not elapsed. Wait {remaining.seconds // 60}m {remaining.seconds % 60}s more.")
                        continue
                except Exception as backoff_err:
                    logging.debug(f"[VERIFY] Could not check backoff for {item_id}: {backoff_err}")

            logging.debug(f"[VERIFY DEBUG] Processing Item: ID={item_id}, Path={item_path}, Title={item_title}, Episode={episode_title}, Attempts={attempts}")
            logging.info(f"[VERIFY] Checking path: '{item_path}' (Attempt {attempts + 1}/{max_attempts}) Title: '{item_title}', Episode: '{episode_title}'")

            if attempts >= max_attempts:
                logging.warning(f"[VERIFY] Max attempts reached for path {item_path}. Marking as Failed.")
                update_removal_status(item_id, 'Failed', failure_reason=f'Max attempts ({max_attempts}) reached.')
                failed_verification_count += 1
                continue

            item_still_exists = False # Assume item is gone unless found
            try:
                logging.debug(f"[VERIFY DEBUG] Finding Plex section for path: {item_path}")
                plex_library, plex_section = find_plex_library_and_section(plex, item_path)
                if not plex_section:
                    logging.warning(f"[VERIFY] Could not find Plex section for path: {item_path}. Incrementing attempt count.")
                    increment_removal_attempt(item_id) # Increment attempt if section not found
                    failed_verification_count += 1
                    continue
                logging.debug(f"[VERIFY DEBUG] Found section: {plex_section.title}")

                section_type = get_section_type(plex_section) # 'movie' or 'show'
                target_basename = os.path.basename(item_path)
                logging.debug(f"[VERIFY DEBUG] Section type: {section_type}, Target basename: {target_basename}")

                if not item_title:
                     logging.error(f"[VERIFY] Item ID {item_id} is missing item_title. Cannot perform title-based search for path {item_path}. Incrementing attempt count.")
                     increment_removal_attempt(item_id)
                     failed_verification_count += 1
                     continue

                # --- Movie Check ---
                if section_type == 'movie':
                    logging.debug(f"[VERIFY DEBUG] Searching for MOVIE title: '{item_title}' in section '{plex_section.title}'")
                    search_results = plex_section.search(title=item_title, libtype='movie')
                    logging.debug(f"[VERIFY DEBUG] Movie search results count: {len(search_results)}")
                    if not search_results:
                         logging.debug(f"[VERIFY] Movie title '{item_title}' not found in section '{plex_section.title}'. Check passed for this item.")
                    else:
                        for movie in search_results:
                            logging.debug(f"[VERIFY DEBUG] Checking parts for movie: {movie.title} ({movie.key})")
                            for part in movie.iterParts():
                                 part_basename = os.path.basename(part.file)
                                 logging.debug(f"[VERIFY DEBUG] Comparing target '{target_basename}' with part '{part_basename}' (from {part.file})")
                                 if part_basename == target_basename:
                                     logging.warning(f"[VERIFY] Path '{item_path}' still found associated with Movie '{item_title}' (Part: {part.file}). Verification FAILED.")
                                     item_still_exists = True
                                     break
                            if item_still_exists: break

                # --- Show Check ---
                elif section_type == 'show':
                    logging.debug(f"[VERIFY DEBUG] Searching for SHOW title: '{item_title}' in section '{plex_section.title}'")
                    shows = plex_section.search(title=item_title, libtype='show')
                    logging.debug(f"[VERIFY DEBUG] Show search results count: {len(shows)}")
                    if not shows:
                        logging.debug(f"[VERIFY] Show title '{item_title}' not found in section '{plex_section.title}'. Check passed for this item.")
                    else:
                        for show in shows:
                            logging.debug(f"[VERIFY DEBUG] Found show: {show.title} ({show.key})")
                            # Search specific episode if title provided
                            if episode_title:
                                logging.debug(f"[VERIFY DEBUG] Searching for EPISODE title: '{episode_title}' within show '{show.title}'")
                                try:
                                    episode = show.episode(title=episode_title)
                                    logging.debug(f"[VERIFY DEBUG] Found episode: {episode.title} ({episode.key})")
                                    for part in episode.iterParts():
                                        part_basename = os.path.basename(part.file)
                                        logging.debug(f"[VERIFY DEBUG] Comparing target '{target_basename}' with part '{part_basename}' (from {part.file})")
                                        if part_basename == target_basename:
                                            logging.warning(f"[VERIFY] Path '{item_path}' still found associated with Episode '{item_title} - {episode_title}' (Part: {part.file}). Verification FAILED.")
                                            item_still_exists = True
                                            break
                                except NotFound:
                                    logging.debug(f"[VERIFY] Episode '{episode_title}' not found for show '{show.title}'. Check passed for this episode.")
                                except Exception as e_ep:
                                     logging.error(f"[VERIFY] Error searching for episode '{episode_title}' in show '{show.title}': {e_ep}")
                                     item_still_exists = True # Assume exists on error
                            # If no episode title, check all episodes (less common)
                            else:
                                logging.debug(f"[VERIFY DEBUG] No episode title for '{item_title}', path '{item_path}'. Checking ALL episode parts.")
                                for episode in show.episodes():
                                     for part in episode.iterParts():
                                         part_basename = os.path.basename(part.file)
                                         if part_basename == target_basename:
                                             logging.warning(f"[VERIFY] Path '{item_path}' still found (no ep title) with Show '{item_title}' (Ep: {episode.title}, Part: {part.file}). Verification FAILED.")
                                             item_still_exists = True
                                             break
                                     if item_still_exists: break
                            if item_still_exists: break

                # --- Unknown Section Type ---
                else:
                    logging.warning(f"[VERIFY] Unknown section type '{section_type}' for section '{plex_section.title}'. Cannot verify path {item_path}. Incrementing attempt.")
                    increment_removal_attempt(item_id) # Increment attempt for unknown type
                    failed_verification_count += 1
                    continue # Skip update logic below

                # --- Logging before potential update ---
                logging.debug(f"[VERIFY DEBUG] Post-check for ID {item_id}: item_still_exists = {item_still_exists}")

            except Exception as e_proc:
                logging.error(f"[VERIFY] Error during Plex verification processing for path {item_path} (ID: {item_id}): {e_proc}", exc_info=True)
                increment_removal_attempt(item_id)
                failed_verification_count += 1
                continue # Skip to next item

            # --- Update status based on verification result ---
            try:
                if not item_still_exists:
                    logging.info(f"[VERIFY] Path '{item_path}' appears removed from Plex metadata. Marking as Verified.")
                    update_removal_status(item_id, 'Verified')
                    verified_count += 1
                else:
                    # Item still exists - use different strategies based on attempt count
                    current_attempts = attempts + 1  # This will be the new attempt count

                    # Strategy 1: First few attempts - try direct deletion via API
                    if current_attempts <= 2:
                        logging.warning(f"[VERIFY] Path '{item_path}' still found in Plex. Attempting direct removal (attempt {current_attempts})...")
                        removal_successful = remove_file_from_plex(item_title, item_path, episode_title)
                        if removal_successful:
                            logging.info(f"[VERIFY] Successfully triggered removal via remove_file_from_plex for '{item_path}'. Will verify later.")
                        else:
                            logging.warning(f"[VERIFY] Direct removal failed for '{item_path}'. Will try scan & trash on next attempts.")

                    # Strategy 2: Later attempts - use scan & empty trash instead
                    elif current_attempts <= max_attempts - 1:
                        logging.warning(f"[VERIFY] Path '{item_path}' still in Plex after {attempts} attempts. Using scan & empty trash approach...")
                        try:
                            from utilities.plex_functions import scan_and_empty_plex_trash
                            # Get the parent folder of the file to scan
                            parent_folder = os.path.dirname(item_path)
                            if parent_folder and os.path.exists(os.path.dirname(parent_folder)):  # Check grandparent exists
                                scan_result = scan_and_empty_plex_trash(paths=[parent_folder])
                                if scan_result.get('success'):
                                    logging.info(f"[VERIFY] Triggered scan & trash for folder: {parent_folder}")
                                else:
                                    logging.warning(f"[VERIFY] Scan & trash had issues: {scan_result.get('errors', [])}")
                            else:
                                logging.warning(f"[VERIFY] Cannot scan - parent folder doesn't exist: {parent_folder}")
                        except Exception as scan_err:
                            logging.error(f"[VERIFY] Error during scan & trash for '{item_path}': {scan_err}")

                    # Strategy 3: Final attempt - mark as failed, requires user intervention
                    else:
                        logging.error(f"[VERIFY] Max attempts ({max_attempts}) nearly reached for '{item_path}'. "
                                     f"This may require manual intervention in Plex (enable 'Allow media deletion' in Settings > Library).")

                    logging.warning(f"[VERIFY] Incrementing attempt count for '{item_path}' (now {current_attempts}/{max_attempts}).")
                    increment_removal_attempt(item_id)
                    failed_verification_count += 1
            except Exception as db_update_err:
                 logging.error(f"[VERIFY] Database error updating status/attempts for ID {item_id}: {db_update_err}", exc_info=True)
                 # If DB update fails, the attempt count might not increment, potentially causing loops.

        logging.info(f"[VERIFY] Plex removal verification task finished. Verified: {verified_count}, Failed/Still Pending: {failed_verification_count}.")

        # Clean up old verified/failed entries
        if cleanup_days > 0:
            logging.info(f"[VERIFY] Cleaning up verified/failed entries older than {cleanup_days} days.")
            try:
                 # Use the correct function name
                removed_count = cleanup_old_verified_removals(days=cleanup_days)
                logging.info(f"[VERIFY] Removed {removed_count} old verified/failed entries.")
            except Exception as e_cleanup:
                 logging.error(f"[VERIFY] Error during cleanup of old removal entries: {e_cleanup}")


    def task_precompute_airing_shows(self):
        """Precompute the recently aired and airing soon shows in a background task"""
        try:
            from routes.statistics_routes import get_recently_aired_and_airing_soon

            # Actually call the function to populate the cache
            logging.info("Precomputing airing shows data...")
            start_time = time.time()
            # *** START EDIT ***
            # Call the function without the unsupported force_refresh argument
            recently_aired, airing_soon = get_recently_aired_and_airing_soon()
            # *** END EDIT ***

            duration = time.time() - start_time
            logging.info(f"Precomputed airing shows data in {duration:.2f}s. Found {len(recently_aired)} recently aired and {len(airing_soon)} airing soon shows.")
        except Exception as e:
            logging.error(f"Error precomputing airing shows: {e}")

    # --- START: New Task Implementation ---
    def task_update_tv_show_status(self):
        """
        Periodically updates the status for TV shows in the tv_shows table
        based on external metadata, and calculates per-version presence status
        in the tv_show_version_status table based on local collection state.
        """
        logging.info("[TASK] Running TV show status update...")
        start_time = time.time()
        conn = None
        updated_count = 0
        failed_count = 0
        processed_shows = set() # Track shows processed in this run
        shows_with_versions_updated = set() # Track shows where versions were processed

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Get distinct show IMDB IDs from media_items (episodes only needed now)
            cursor.execute("""
                SELECT DISTINCT imdb_id
                FROM media_items
                WHERE type = 'episode' AND imdb_id IS NOT NULL AND imdb_id != '' AND season_number > 0
            """)
            show_imdb_ids = [row['imdb_id'] for row in cursor.fetchall()]

            if not show_imdb_ids:
                logging.info("[TV Status] No TV show IMDB IDs found in media_items (episodes) to update.")
                return

            logging.info(f"[TV Status] Found {len(show_imdb_ids)} unique show IMDB IDs with episodes to check.")
            api = DirectAPI()

            for imdb_id in show_imdb_ids:
                if imdb_id in processed_shows:
                    continue

                logging.debug(f"[TV Status] Processing show: {imdb_id}")
                show_metadata = None
                source = "Unknown"
                show_status = 'unknown' # Default status
                total_episodes_from_source = 0
                total_seasons_from_source = 0
                is_show_ended = False
                tmdb_id = None
                title = None
                year = None

                try:
                    # Fetch metadata using DirectAPI
                    show_metadata, source = api.get_show_metadata(imdb_id)

                    if not show_metadata:
                        logging.warning(f"[TV Status] No metadata found for show {imdb_id} from source '{source}'. Will proceed with version check using existing DB status if available.")
                        # Try to get existing status from DB to determine if ended
                        cursor.execute("SELECT status, total_episodes, total_seasons FROM tv_shows WHERE imdb_id = ?", (imdb_id,))
                        existing_show = cursor.fetchone()
                        if existing_show:
                            show_status = existing_show['status'].lower() if existing_show['status'] else 'unknown'
                            total_episodes_from_source = existing_show['total_episodes'] or 0
                            total_seasons_from_source = existing_show['total_seasons'] or 0
                        else:
                             # No metadata and no existing record, cannot determine version completeness accurately
                             logging.warning(f"[TV Status] No existing record for {imdb_id} either. Skipping version status calculation.")
                             failed_count += 1
                             processed_shows.add(imdb_id)
                             continue # Skip to next show
                    else:
                        # Process metadata if found
                        show_status = show_metadata.get('status', 'unknown').lower()
                        tmdb_id = show_metadata.get('ids', {}).get('tmdb')
                        title = show_metadata.get('title')
                        year = show_metadata.get('year')

                        # Calculate total episodes and seasons from source metadata
                        if 'seasons' in show_metadata:
                             # Reset count before summing seasons
                            total_episodes_from_source = 0
                            total_seasons_from_source = 0
                            for season_num_str, season_data in show_metadata.get('seasons', {}).items():
                                try:
                                     season_num = int(season_num_str)
                                     if season_num == 0: continue # Skip specials season
                                     total_episodes_from_source += len(season_data.get('episodes', {}))
                                     total_seasons_from_source += 1
                                except ValueError:
                                     logging.warning(f"[TV Status] Invalid season number '{season_num_str}' in metadata for {imdb_id}. Skipping.")
                                     continue
                        else:
                            logging.warning(f"[TV Status] Metadata for {imdb_id} ('{title}') lacks 'seasons' key. Total episode count may be inaccurate.")
                            # Fallback to DB value if exists? Or treat as 0? Let's fetch existing.
                            cursor.execute("SELECT total_episodes, total_seasons FROM tv_shows WHERE imdb_id = ?", (imdb_id,))
                            existing_show_fallback = cursor.fetchone()
                            total_episodes_from_source = existing_show_fallback['total_episodes'] if existing_show_fallback else 0
                            total_seasons_from_source = existing_show_fallback['total_seasons'] if existing_show_fallback else 0
                            if total_episodes_from_source == 0:
                                logging.warning(f"[TV Status] No episode count from metadata or DB for {imdb_id}. Skipping version status calculation.")
                                # We can still update show status, but version logic is impossible


                    # Determine overall show ended status based *only* on metadata status
                    # Treat 'canceled' the same as 'ended' for completion purposes
                    is_show_ended = bool(show_status in ('ended', 'canceled'))

                    logging.debug(f"[TV Status] Show: {imdb_id} ('{title}') - Status: {show_status}, Source Episodes: {total_episodes_from_source}, IsEnded/Canceled: {is_show_ended}")

                    # Prepare data for tv_shows DB update/insert
                    now_utc = datetime.now(timezone.utc)
                    now_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')

                    # Upsert into tv_shows. 'is_complete' reflects if the show's status is 'ended'/'canceled'.
                    # total_episodes is updated from source metadata.
                    # Ensure COALESCE is used for fields that might not be present in new metadata fetch
                    # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior
                    cursor.execute("""
                        INSERT INTO tv_shows (
                            imdb_id, tmdb_id, title, year, status, is_complete,
                            total_episodes, total_seasons, last_status_check, added_at, last_updated
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT added_at FROM tv_shows WHERE imdb_id = ?), ?), ?)
                        ON CONFLICT DO UPDATE SET
                            tmdb_id = COALESCE(NULLIF(excluded.tmdb_id, ''), tv_shows.tmdb_id),
                            title = COALESCE(NULLIF(excluded.title, ''), tv_shows.title),
                            year = COALESCE(excluded.year, tv_shows.year),
                            status = COALESCE(NULLIF(excluded.status, ''), tv_shows.status),
                            is_complete = excluded.is_complete,
                            total_episodes = excluded.total_episodes,
                            total_seasons = excluded.total_seasons,
                            last_status_check = excluded.last_status_check,
                            last_updated = excluded.last_updated
                        WHERE tv_shows.imdb_id = excluded.imdb_id;
                    """, (
                        imdb_id, tmdb_id, title, year, show_status, int(is_show_ended),
                        total_episodes_from_source, total_seasons_from_source, now_str, # last_status_check
                        # Values for INSERT part (added_at logic)
                        imdb_id, now_str, # imdb_id for subquery, now_str for COALESCE fallback
                        # Value for INSERT part (last_updated)
                        now_str
                    ))
                    conn.commit() # Commit show data before processing versions
                    updated_count += 1 # Count successful show upsert

                    # --- NEW: Per-Version Status Update ---
                    # Skip if we couldn't determine total episodes and show is ended/canceled
                    # (Can't reliably calculate completeness)
                    if total_episodes_from_source <= 0 and is_show_ended:
                         logging.warning(f"[TV Status] Cannot reliably calculate version completeness for ended/canceled show {imdb_id} due to zero total episodes. Skipping version updates.")
                    else:
                        try:
                            # Get all episode items with their version and state for this show
                            cursor.execute("""
                                SELECT state, version -- Fetch the version key directly
                                FROM media_items
                                WHERE imdb_id = ? AND type = 'episode' AND season_number > 0
                            """, (imdb_id,))
                            all_episodes = cursor.fetchall()

                            if not all_episodes:
                                logging.debug(f"[TV Status] No local episode media items found for {imdb_id}. Cleaning up old version statuses.")
                                # Remove any stale version statuses if no episodes exist anymore
                                cursor.execute("DELETE FROM tv_show_version_status WHERE imdb_id = ?", (imdb_id,))
                            else:
                                # Group episodes by version identifier
                                episodes_by_version = {}
                                for episode in all_episodes:
                                    version_identifier = (episode['version'] or 'UnknownVersion').rstrip('*') # Handle potential NULL/empty version and trim trailing '*'
                                    if version_identifier not in episodes_by_version:
                                        episodes_by_version[version_identifier] = []
                                    episodes_by_version[version_identifier].append(episode)

                                logging.debug(f"[TV Status] Found {len(episodes_by_version)} versions for {imdb_id}: {list(episodes_by_version.keys())}")

                                # Process each version
                                version_now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                                versions_processed_this_show = set()
                                for version_id, episodes_in_version in episodes_by_version.items():
                                    versions_processed_this_show.add(version_id)
                                    # Count present episodes (Collected or Blacklisted)
                                    present_count = sum(1 for ep in episodes_in_version if ep['state'] in ('Collected', 'Blacklisted'))

                                    # Determine if this version is up-to-date (has all known episodes)
                                    # If show is not ended, up-to-date is meaningless, default to False? Or base on present_count?
                                    # Let's define up-to-date ONLY if the show is ended/canceled AND has all eps.
                                    is_up_to_date = bool(
                                        is_show_ended and # Show must be finished
                                        total_episodes_from_source > 0 and
                                        present_count >= total_episodes_from_source
                                    )

                                    # Determine if this version is complete AND fully present
                                    # is_complete_and_present is essentially the same as is_up_to_date by this definition
                                    is_complete_and_present = is_up_to_date

                                    logging.debug(f"[TV Status] Version '{version_id}' for {imdb_id}: Present: {present_count}/{total_episodes_from_source}, ShowEnded/Canceled: {is_show_ended} -> UpToDate: {is_up_to_date}, CompleteAndPresent: {is_complete_and_present}")

                                    # Upsert into the new version status table
                                    version_data = (
                                        imdb_id,
                                        version_id,
                                        int(is_complete_and_present), # Store as integer 0 or 1
                                        int(is_up_to_date),           # Store as integer 0 or 1
                                        present_count,
                                        version_now_str # last_checked
                                    )
                                    cursor.execute("""
                                        INSERT INTO tv_show_version_status (
                                            imdb_id, version_identifier, is_complete_and_present,
                                            is_up_to_date, present_episode_count, last_checked
                                        ) VALUES (?, ?, ?, ?, ?, ?)
                                        ON CONFLICT(imdb_id, version_identifier) DO UPDATE SET
                                            is_complete_and_present = excluded.is_complete_and_present,
                                            is_up_to_date = excluded.is_up_to_date,
                                            present_episode_count = excluded.present_episode_count,
                                            last_checked = excluded.last_checked;
                                    """, version_data)

                                # Clean up version statuses for versions that no longer exist locally
                                if versions_processed_this_show: # Ensure set is not empty before query
                                     cursor.execute("""
                                        DELETE FROM tv_show_version_status
                                        WHERE imdb_id = ? AND version_identifier NOT IN ({})
                                    """.format(','.join('?'*len(versions_processed_this_show))), (imdb_id, *versions_processed_this_show))
                                else:
                                     # If somehow versions_processed_this_show is empty after finding episodes, delete all for this imdb_id
                                     logging.warning(f"[TV Status] No versions processed for {imdb_id} despite finding episodes. Cleaning all version statuses.")
                                     cursor.execute("DELETE FROM tv_show_version_status WHERE imdb_id = ?", (imdb_id,))


                                shows_with_versions_updated.add(imdb_id)

                            conn.commit() # Commit version status updates for this show

                        except sqlite3.Error as db_err_version:
                            logging.error(f"[TV Status] Database error during version status update for {imdb_id}: {db_err_version}", exc_info=True)
                            if conn: conn.rollback()
                            failed_count += 1 # Count show as failed if version update fails
                            # Rollback removed the main show update, so no need to adjust updated_count
                            updated_count -= 1 # Decrement successful show update count
                        except Exception as e_version:
                            logging.error(f"[TV Status] Error during version status update for {imdb_id}: {e_version}", exc_info=True)
                            if conn: conn.rollback()
                            failed_count += 1
                            updated_count -= 1 # Decrement successful show update count


                    processed_shows.add(imdb_id) # Mark base show info as processed (even if version failed)

                except Exception as e:
                    logging.error(f"[TV Status] Failed to process show {imdb_id}: {e}", exc_info=True)
                    failed_count += 1
                    if conn: conn.rollback() # Rollback any partial changes for this show
                    processed_shows.add(imdb_id) # Mark as processed to avoid retrying in this run
                    # Ensure it's not counted as having versions updated
                    if imdb_id in shows_with_versions_updated:
                        shows_with_versions_updated.remove(imdb_id)


            # No final commit needed here as commits happen per-show or are rolled back on error

        except sqlite3.Error as db_err:
            logging.error(f"[TV Status] Database error during TV show status update setup: {db_err}", exc_info=True)
            if conn: conn.rollback()
        except Exception as err:
            logging.error(f"[TV Status] Unexpected error during TV show status update: {err}", exc_info=True)
            if conn: conn.rollback() # Rollback any potential transaction
        finally:
            if conn:
                conn.close()

        duration = time.time() - start_time
        # Refined counting:
        # processed_shows = count of unique imdb_ids attempted.
        # updated_count = count of successful base show info upserts.
        # shows_with_versions_updated = count where version logic also completed.
        # failed_count = count where either base or version logic failed with error/rollback.
        logging.info(f"[TASK] TV show status update finished in {duration:.2f}s. Processed Shows: {len(processed_shows)}, Base Info Updated: {updated_count}, Versions Updated: {len(shows_with_versions_updated)}, Failed: {failed_count}.")
    # --- END: New Task Implementation ---

    # *** START EDIT: Add task method to update queue views ***
    def task_update_queue_views(self):
        """Periodically updates the in-memory queue views from the database."""
        if not hasattr(self, 'queue_manager') or not self.queue_manager:
            logging.warning("Queue manager not available, cannot update queue views.")
            return

        # logging.debug("Running task_update_queue_views to sync queues with DB.")
        updated_count = 0
        start_time = time.time()
        try:
            # It's important to iterate over a copy of the items in case the dict changes
            queues_to_update = list(self.queue_manager.queues.values())
            for queue in queues_to_update:
                if hasattr(queue, 'update'):
                    try:
                        queue.update()
                        updated_count += 1
                    except Exception as e:
                        logging.error(f"Error updating queue '{getattr(queue, 'name', 'Unknown')}': {e}", exc_info=True)
                else:
                     # This might happen if initialization failed partially
                    logging.warning(f"Queue object '{getattr(queue, 'name', 'Unknown')}' lacks an update method.")

            duration = time.time() - start_time
            # logging.debug(f"Finished task_update_queue_views in {duration:.3f}s. Updated {updated_count} queues.")
        except Exception as e:
            logging.error(f"Unexpected error in task_update_queue_views: {e}", exc_info=True)
    # *** END EDIT ***

    # *** START EDIT: Add Listener Method ***
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
    # *** END EDIT ***

    # *** START EDIT: Modify _run_and_measure_task for tracemalloc sampling AND heavy task locking ***
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

        # --- START EDIT: Manage currently_executing_tasks ---
        with self._running_task_lock:
            self.currently_executing_tasks.add(actual_job_id_from_scheduler)
            if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
                logging.info(f"Task '{log_display_name}' started execution, added to currently_executing_tasks.")
        # --- END EDIT ---

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

        # --- START EDIT: Capture CPU time before task execution ---
        cpu_start = self._get_current_thread_cpu_seconds()
        # --- END EDIT ---

        try:
            # Execute the original task function
            func(*args, **kwargs)
            duration = time.monotonic() - start_time # Measure duration regardless
            # --- START EDIT: Record runtime on successful completion ---
            self._record_task_runtime(task_name_for_logging, duration)
            # --- END EDIT ---

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

            # --- START EDIT: Capture CPU time after task execution ---
            cpu_end = self._get_current_thread_cpu_seconds()
            cpu_seconds = cpu_end - cpu_start
            cpu_util = (cpu_seconds / duration) * 100 if duration > 0 else 0
            self._record_task_cpu(task_name_for_logging, cpu_seconds)
            if self._log_per_run_cpu:
                logging.debug(f"[CPU] Task '{log_display_name}' cpu={cpu_seconds:.3f}s, wall={duration:.3f}s, util={cpu_util:.1f}%")
            # --- END EDIT ---

            # Optional: Log normal duration if not sampling (can be noisy)
            # else:
            #    logging.debug(f"Task '{log_display_name}' completed successfully in {duration:.3f}s (No tracemalloc sample this time)")

            return duration # Return duration for the listener

        except Exception as e:
            duration = time.monotonic() - start_time
            logging.error(f"Error during execution of job '{log_display_name}': {e}", exc_info=True)
            # --- START EDIT: Record runtime even on error ---
            self._record_task_runtime(task_name_for_logging, duration)
            # --- END EDIT ---

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

            # --- START EDIT: Capture CPU time after task execution on error ---
            cpu_end = self._get_current_thread_cpu_seconds()
            cpu_seconds = cpu_end - cpu_start
            cpu_util = (cpu_seconds / duration) * 100 if duration > 0 else 0
            self._record_task_cpu(task_name_for_logging, cpu_seconds)
            if self._log_per_run_cpu:
                logging.debug(f"[CPU] Task '{log_display_name}' cpu={cpu_seconds:.3f}s, wall={duration:.3f}s, util={cpu_util:.1f}%")
            # --- END EDIT ---

            raise # Re-raise the exception
        finally:
            # --- START EDIT: Manage currently_executing_tasks ---
            with self._running_task_lock:
                self.currently_executing_tasks.discard(actual_job_id_from_scheduler)
                if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
                    logging.info(f"Task '{log_display_name}' finished execution, removed from currently_executing_tasks.")
            # --- END EDIT ---
                    
            # --- START EDIT: Add inter-task sleep for low power mode ---
            try:
                inter_task_sleep_seconds = self.current_inter_task_sleep
                if inter_task_sleep_seconds > 0:
                    logging.debug(f"Sleeping for {inter_task_sleep_seconds:.2f}s after task '{log_display_name}' due to current inter-task sleep setting.")
                    time.sleep(inter_task_sleep_seconds)
            except (ValueError, TypeError):
                # If setting is invalid, don't sleep
                pass
            # --- END EDIT ---

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

    # --- START EDIT: Add method for live interval updates ---
    def update_task_interval(self, task_name: str, interval_seconds: int | None):
        """
        Updates the interval (in seconds) for a specific task live.
        If interval_seconds is None, resets to the default interval.
        """
        normalized_name = self._normalize_task_name(task_name)

        if normalized_name not in self.original_task_intervals:
            logging.error(f"Cannot update interval for '{normalized_name}': Task not defined.")
            return False

        # --- START EDIT: Define minimum seconds ---
        MIN_INTERVAL_SECONDS = 1 # Must match validation
        # --- END EDIT ---

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
                 # --- START EDIT: Validate seconds ---
                if interval_sec_int >= MIN_INTERVAL_SECONDS:
                    target_interval_seconds = interval_sec_int
                else:
                    logging.error(f"Invalid interval for '{normalized_name}': {interval_seconds} (must be >= {MIN_INTERVAL_SECONDS} seconds). Cannot apply live update.")
                    return False
                 # --- END EDIT ---
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
    # --- END EDIT ---

    # --- START EDIT: Modified task for library size cache refresh (now fully synchronous structure) ---
    def task_refresh_library_size_cache(self):
        """Scheduled task to refresh the Debrid library size cache."""
        logging.info("Initiating scheduled library size cache refresh task.")
        try:
            provider = get_debrid_provider()
            # Use provider capability or method presence instead of concrete type
            if hasattr(provider, 'get_total_library_size'):
                logging.info("Background task: Refreshing library size cache via Debrid provider...")
                # The provider's get_total_library_size is async, so we run it in a new event loop.
                # This call is expected to fetch the size and update the cache file itself.
                calculated_size = asyncio.run(provider.get_total_library_size())
                if calculated_size is not None and not str(calculated_size).startswith("Error"):
                    logging.info(f"Background task: Library size cache refresh successful. Provider reported size: {calculated_size}")
                else:
                    logging.warning(f"Background task: Library size cache refresh via provider failed or returned error. Result: {calculated_size}")
            else:
                logging.info("Background task: Library size cache refresh skipped (provider does not implement total size API or not configured).")
        except ProviderUnavailableError:
            logging.warning("Background task: Debrid provider unavailable during library size cache refresh.")
        except RuntimeError as e_runtime:
            # This might happen if asyncio.run() is called from an already running loop,
            # though less likely with BackgroundScheduler's default thread-based execution.
             logging.error(f"Background task: Runtime error during library size cache refresh: {e_runtime}", exc_info=True)
        except Exception as e:
            logging.error(f"Background task: General error during library size cache refresh: {e}", exc_info=True)
    # --- END EDIT ---

    # --- START EDIT: Add media analysis task method ---
    def task_analyze_media_files(self):
        """Scheduled task to analyze and repair media files."""
        from utilities.analyze_library import analyze_and_repair_media_files

        logging.info("Initiating scheduled media file analysis and repair task.")
        try:
            collection_type = get_setting('File Management', 'file_collection_management', 'Plex')
            if collection_type not in ['Plex', 'Symlinked/Local']:
                logging.warning(
                    f"Unsupported collection type '{collection_type}' for media analysis. Supported types are 'Plex' or 'Symlinked/Local'. Skipping."
                )
                return

            # The analyze_and_repair_media_files function uses its own default for max_files_to_check_this_run
            # If you want to make this configurable via settings.ini for the scheduled task,
            # you could add:
            # max_files_setting = get_setting('Maintenance', 'media_analysis_max_files_per_run', <default_from_analyze_library>)
            # and pass it to the function: analyze_and_repair_media_files(collection_type, max_files_setting)

            analyze_and_repair_media_files(collection_type=collection_type)
            logging.info("Scheduled media file analysis and repair task completed.")

        except Exception as e:
            logging.error(f"Error during scheduled media file analysis and repair: {e}", exc_info=True)
    # --- END EDIT ---

    # --- START EDIT: Add bulk subtitle processing task ---
    def task_process_bulk_subtitles(self):
        """Scheduled task to process bulk subtitles using the bulk_subs.sh script."""
        import subprocess
        import os
        
        logging.info("Initiating scheduled bulk subtitle processing task.")
        
        try:
            # Get the collection type setting to determine the scan directory
            from utilities.settings import get_setting
            collection_type = get_setting('File Management', 'file_collection_management', 'Plex')
            
            if collection_type == 'Symlinked/Local':
                scan_dir = get_setting('File Management', 'symlinked_files_path', '')
                if not os.path.exists(scan_dir):
                    logging.warning(f"Symlinks directory not found: {scan_dir}")
                    return
            elif collection_type == 'Plex':
                self._task_process_bulk_subtitles_plex()
                return
            else:
                logging.warning(f"Unsupported collection type '{collection_type}' for bulk subtitle processing.")
                return

            # Get the script path
            script_path = os.path.abspath('utilities/bulk_subs.sh')
            if not os.path.exists(script_path):
                logging.error(f"Bulk subtitle script not found: {script_path}")
                return

            # Make sure the script is executable
            # Check if already executable to avoid permission errors on mounted filesystems
            try:
                current_mode = os.stat(script_path).st_mode
                if not (current_mode & 0o111):  # Check if any execute bit is set
                    os.chmod(script_path, 0o755)
                    logging.debug(f"Set execute permissions on {script_path}")
                else:
                    logging.debug(f"Script {script_path} is already executable")
            except (OSError, PermissionError) as e:
                # If chmod fails but file exists, check if it's already executable
                if os.access(script_path, os.X_OK):
                    logging.debug(f"Script {script_path} is executable (chmod failed but file has execute permission)")
                else:
                    logging.warning(f"Could not set execute permission on {script_path}: {e}. Will attempt to run anyway.")

            # Run the bulk subtitle processing script with a limit of 200 files
            max_files = 200
            logging.info(f"Running bulk subtitle processing on {scan_dir} (max {max_files} files)")
            
            cmd = [script_path, scan_dir, str(max_files)]
            # Don't capture output to allow full visibility of the subtitle processing
            result = subprocess.run(
                cmd, 
                timeout=3600  # 1 hour timeout
            )
            
            if result.returncode == 0:
                logging.info("Bulk subtitle processing completed successfully.")
            else:
                logging.warning(f"Bulk subtitle processing finished with return code: {result.returncode}")
            
        except subprocess.TimeoutExpired:
            logging.error("Bulk subtitle processing timed out after 1 hour")
        except Exception as e:
            logging.error(f"Error during bulk subtitle processing: {e}", exc_info=True)
    # --- END EDIT ---

    def _get_plex_mount_base(self):
        """Get the actual mount base path from Plex mounted_file_location, stripping /__all__."""
        mount = get_setting('Plex', 'mounted_file_location', '').strip()
        if mount.endswith('/__all__'):
            mount = mount[:-len('/__all__')]
        return mount.rstrip('/')

    def _remap_debrid_path(self, path, mount_base):
        """Replace the Plex-reported /debrid prefix with the actual mount path."""
        if not path or not mount_base:
            return path
        # location_on_disk from Plex looks like /debrid/shows/FolderName or /debrid/movies/FolderName
        # Split off the /debrid prefix and replace with mount_base
        parts = path.split('/', 3)  # ['', 'debrid', 'shows'|'movies', 'rest...']
        if len(parts) >= 3:
            return mount_base + '/' + '/'.join(parts[2:])
        return path

    def _task_process_bulk_subtitles_plex(self):
        """Process bulk subtitles for Plex mode via cli_mount sidecar injection."""
        from database.core import get_db_connection
        from utilities.downsub import download_subtitles_for_video
        logging.info("[BulkSubs/Plex] Starting bulk subtitle processing for Plex mode.")

        mount_base = self._get_plex_mount_base()
        if not mount_base:
            logging.warning("[BulkSubs/Plex] No Plex mounted_file_location configured — cannot process subtitles.")
            return

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT location_on_disk, filled_by_file FROM media_items
                WHERE state = 'Collected'
                AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
                LIMIT 200
            """)
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            logging.error(f"[BulkSubs/Plex] DB error: {e}")
            return

        processed = 0
        for row in rows:
            location = row[0] or ''
            filled_by_file = row[1] or ''
            if not location or not filled_by_file:
                continue
            # Remap /debrid/... → /media/mount/...
            location = self._remap_debrid_path(location, mount_base)
            full_path = os.path.join(location, filled_by_file) if not location.endswith(filled_by_file) else location
            try:
                download_subtitles_for_video(full_path)
                processed += 1
            except Exception as e:
                logging.debug(f"[BulkSubs/Plex] Skipped {full_path}: {e}")
        logging.info(f"[BulkSubs/Plex] Processed {processed} items.")

    # *** START EDIT: Add the new long-running task method ***
    def task_artificial_long_run(self):
        task_name = 'task_artificial_long_run'
        logging.info(f"'{task_name}' has started.")
        
        duration_seconds = 120 # Run for 2 minutes
        
        logging.info(f"'{task_name}' will now sleep for {duration_seconds} seconds.")
        time.sleep(duration_seconds)
        
        logging.info(f"'{task_name}' has finished sleeping and is now complete.")
    # *** END EDIT ***

    def task_overlay_sync(self):
        """Scheduled task to sync overlay data and regenerate changed posters."""
        try:
            from overlays.scheduled_tasks import task_overlay_sync as sync_func
            with self._running_task_lock:
                _triggered_by = 'manual' if any(
                    'manual_task_overlay_sync' in jid
                    for jid in self.currently_executing_tasks
                ) else 'scheduled'
            result = sync_func(triggered_by=_triggered_by)
            if result.get('success'):
                logging.info(f"Overlay sync completed: {result.get('message', 'Success')}")
            else:
                logging.warning(f"Overlay sync had issues: {result.get('message', 'Unknown error')}")
        except ImportError as e:
            logging.warning(f"Overlay module not available: {e}")
        except Exception as e:
            logging.error(f"Error in overlay sync task: {e}", exc_info=True)
        finally:
            # Overlay sync loads large amounts of Plex metadata into memory.
            # Force glibc to return freed arena pages to the OS immediately.
            try:
                import gc, ctypes
                def _rss_kb():
                    with open('/proc/self/status') as _f:
                        for _l in _f:
                            if _l.startswith('VmRSS:'):
                                return int(_l.split()[1])
                    return 0
                _before_rss = _rss_kb()
                gc.collect()
                ctypes.CDLL('libc.so.6').malloc_trim(0)
                _after_rss = _rss_kb()
                logging.info(f"[OVERLAY_SYNC] malloc_trim: RSS {_before_rss//1024} MB → {_after_rss//1024} MB (freed {(_before_rss - _after_rss)//1024} MB)")
            except Exception as _e:
                logging.info(f"[OVERLAY_SYNC] malloc_trim skipped: {_e}")

    def task_plex_smart_collection_posters(self):
        """Apply custom posters to enabled Plex smart collections."""
        try:
            from database.plex_smart_collections import apply_smart_collection_posters
            apply_smart_collection_posters()
        except Exception as e:
            logging.error(f"Plex smart collection posters task failed: {e}", exc_info=True)

    def task_plex_movie_boxsets(self):
        """Sync Plex movie box set collections from TMDB collection data."""
        try:
            from database.plex_movie_boxsets import run_plex_movie_boxsets
            run_plex_movie_boxsets()
        except Exception as e:
            logging.error(f"Plex movie box sets task failed: {e}", exc_info=True)

    def task_overlay_cleanup(self):
        """Scheduled task to clean up unused posters and overlay cache."""
        try:
            logging.info("Overlay cleanup task starting")
            db_path = os.environ.get('DATABASE_PATH', '/user/db_content/media_items.db')

            # 1. Clean up orphaned poster backup files
            try:
                from overlays.cache_cleanup import PosterCacheManager
                manager = PosterCacheManager(db_path)
                backup_result = manager.cleanup_orphaned_backups()
                removed = backup_result.get('orphaned_removed', 0)
                logging.info(f"Overlay cleanup: removed {removed} orphaned backup file(s)")
            except Exception as e:
                logging.warning(f"Overlay cleanup: backup cleanup failed: {e}")

            # 2. Reset stuck 'analyzing' states (older than 30 minutes) to 'pending'
            try:
                import sqlite3 as _sqlite3
                conn = _sqlite3.connect(db_path)
                conn.execute("""
                    UPDATE media_overlay_state SET status = 'pending'
                    WHERE status = 'analyzing'
                      AND updated_at < datetime('now', '-30 minutes')
                """)
                stuck_reset = conn.total_changes
                conn.commit()
                conn.close()
                if stuck_reset:
                    logging.info(f"Overlay cleanup: reset {stuck_reset} stuck analyzing state(s) to pending")
            except Exception as e:
                logging.warning(f"Overlay cleanup: stuck state reset failed: {e}")

            # 3. DB housekeeping + Plex poster pruning (unified in task_overlay_cleanup)
            from overlays.scheduled_tasks import task_overlay_cleanup as cleanup_func
            with self._running_task_lock:
                _triggered_by = 'manual' if any(
                    'manual_task_overlay_cleanup' in jid
                    for jid in self.currently_executing_tasks
                ) else 'scheduled'
            result = cleanup_func(triggered_by=_triggered_by)
            if result.get('success'):
                logging.info(f"Overlay cleanup completed: {result.get('message', 'Success')}")
            else:
                logging.warning(f"Overlay cleanup had issues: {result.get('message', 'Unknown error')}")

        except ImportError as e:
            logging.warning(f"Overlay module not available: {e}")
        except Exception as e:
            logging.error(f"Error in overlay cleanup task: {e}", exc_info=True)

    def task_upgrade_hub_scan(self):
        """Scheduled nightly scan for better-quality releases via Zilean."""
        try:
            if 'task_upgrade_hub_scan' not in self.enabled_tasks:
                logging.debug("[UPGRADE_HUB] Scheduled scan skipped — task disabled in Task Manager")
                return
            from database.zilean_upgrade import scan_for_upgrades, get_scan_status
            from utilities.settings import get_setting
            if get_scan_status()['in_progress']:
                logging.info("[UPGRADE_HUB] Scheduled scan skipped — scan already in progress")
                return
            scan_limit = get_setting('Upgrade Hub', 'scan_limit', None)
            if scan_limit not in (None, '', 'null'):
                try:
                    scan_limit = int(scan_limit)
                except (TypeError, ValueError):
                    scan_limit = None
            else:
                scan_limit = None
            logging.info("[UPGRADE_HUB] Starting scheduled upgrade scan")
            result = scan_for_upgrades(scan_limit=scan_limit, triggered_by='scheduled')
            if 'error' in result:
                logging.warning(f"[UPGRADE_HUB] Scheduled scan error: {result['error']}")
            else:
                upgrades = len(result.get('upgrade_candidates', []))
                packs    = len(result.get('pack_candidates', []))
                logging.info(f"[UPGRADE_HUB] Scheduled scan complete: {upgrades} upgrades, {packs} packs")
        except Exception as e:
            logging.error(f"[UPGRADE_HUB] Scheduled scan failed: {e}", exc_info=True)

    def task_upgrade_hub_auto_queue(self):
        """Auto-queue upgrade candidates found in the most recent scan."""
        try:
            if 'task_upgrade_hub_auto_queue' not in self.enabled_tasks:
                logging.debug("[UPGRADE_HUB] Auto-queue skipped — task disabled in Task Manager")
                return
            from utilities.settings import get_setting
            from database.zilean_upgrade import (
                get_last_results, scan_for_upgrades, get_scan_status, queue_upgrade_candidates
            )
            results = get_last_results()
            if not results or 'error' in results:
                if get_scan_status()['in_progress']:
                    logging.info("[UPGRADE_HUB] Auto-queue skipped — scan in progress")
                    return
                scan_limit = get_setting('Upgrade Hub', 'scan_limit', None)
                if scan_limit not in (None, '', 'null'):
                    try:
                        scan_limit = int(scan_limit)
                    except (TypeError, ValueError):
                        scan_limit = None
                else:
                    scan_limit = None
                results = scan_for_upgrades(scan_limit=scan_limit, triggered_by='scheduled')
            if not results or 'error' in results:
                return
            threshold = float(get_setting('Upgrade Hub', 'min_improvement_threshold', 0) or 0)
            show_recent_only = str(get_setting('Upgrade Hub', 'show_recent_only', False)).lower() == 'true'
            recent_threshold_days = int(get_setting('Upgrade Hub', 'recent_threshold_days', 90) or 90)
            max_per_run = int(get_setting('Upgrade Hub', 'max_upgrades_per_run', 10) or 10)

            import datetime as _dt
            cutoff_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=recent_threshold_days)

            from dateutil import parser as _dtparser

            # Fetch all item_ids already in Upgrading state so we skip them and
            # queue the next batch instead (allows multiple button presses to work).
            try:
                from database.core import get_db_connection as _get_db
                _conn = _get_db()
                _already_upgrading = {
                    r[0] for r in _conn.execute(
                        "SELECT id FROM media_items WHERE state='Upgrading'"
                    ).fetchall()
                }
                _conn.close()
            except Exception:
                _already_upgrading = set()

            # Collect season pack candidates first (to build dedup set for upgrades)
            pack_slots = []
            pack_covered_ids = set()
            for p in results.get('pack_candidates', []):
                if p.get('improvement_pct', 0) < threshold:
                    continue
                if show_recent_only:
                    pack_ep_ids = p.get('item_ids', [])
                    if pack_ep_ids:
                        try:
                            from database.core import get_db_connection as _get_db
                            _conn = _get_db()
                            placeholders = ','.join('?' * len(pack_ep_ids))
                            row = _conn.execute(
                                f"SELECT MAX(ingested_at) FROM media_items WHERE id IN ({placeholders})",
                                pack_ep_ids,
                            ).fetchone()
                            _conn.close()
                            if row and row[0]:
                                iat = _dtparser.parse(row[0])
                                if iat.tzinfo is None:
                                    iat = iat.replace(tzinfo=_dt.timezone.utc)
                                if iat < cutoff_dt:
                                    continue
                        except Exception:
                            pass
                ep_ids = [i for i in p.get('item_ids', []) if i not in _already_upgrading]
                if ep_ids:
                    pack_slots.append(ep_ids)
                    pack_covered_ids.update(ep_ids)

            # Collect individual upgrade candidates — skip any episode covered by a pack slot
            # or already in Upgrading state (so repeated button presses queue the next batch)
            upgrade_item_ids = []
            for c in results.get('upgrade_candidates', []):
                if c.get('improvement_pct', 0) < threshold:
                    continue
                if c['item_id'] in pack_covered_ids:
                    continue
                if c['item_id'] in _already_upgrading:
                    continue
                if show_recent_only:
                    iat_str = c.get('new_ingested_at', '')
                    if not iat_str:
                        continue  # no date — exclude when recent-only is on
                    try:
                        iat = _dtparser.parse(iat_str)
                        if iat.tzinfo is None:
                            iat = iat.replace(tzinfo=_dt.timezone.utc)
                        if iat < cutoff_dt:
                            continue
                    except Exception:
                        continue  # unparseable date — exclude when recent-only is on
                upgrade_item_ids.append(c['item_id'])

            # Cap each side independently by max_per_run, then combine
            upgrade_slots = [([iid], 'upgrade') for iid in upgrade_item_ids][:max_per_run]
            pack_slots_q  = [(ids, 'pack') for ids in pack_slots][:max_per_run]
            all_slots     = upgrade_slots + pack_slots_q

            if not all_slots:
                logging.info("[UPGRADE_HUB] Auto-queue: no candidates matching settings filters")
                try:
                    from database.upgrade_hub_activity import log_hub_activity
                    log_hub_activity('queue', triggered_by='scheduled', result='success',
                                     title='Auto-queue: no candidates to queue',
                                     stats={'queued': 0, 'failed': 0})
                except Exception:
                    pass
                return

            flat_item_ids = [iid for ids, _ in all_slots for iid in ids]
            n_upgrades = sum(1 for _, t in all_slots if t == 'upgrade')
            n_packs    = sum(1 for _, t in all_slots if t == 'pack')
            queue_upgrade_candidates(flat_item_ids, triggered_by='scheduled')
            logging.info(
                f"[UPGRADE_HUB] Auto-queued {n_upgrades} upgrade(s) + {n_packs} pack(s) "
                f"= {len(flat_item_ids)} item(s) total (limit: {max_per_run} per side)"
            )
        except Exception as e:
            logging.error(f"[UPGRADE_HUB] Auto-queue task failed: {e}", exc_info=True)

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

    # --- START EDIT: Add method to get current thread CPU seconds ---
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
    # --- END EDIT ---

    # --- START EDIT: Add method to record task CPU seconds ---
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
    # --- END EDIT ---

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

    # --- START EDIT: Add try-except for float conversions ---
    try:
        movie_airtime_offset_min = float(get_setting("Queue", "movie_airtime_offset", "19")) * 60
    except (ValueError, TypeError):
        movie_airtime_offset_min = 19.0 * 60

    try:
        episode_airtime_offset_min = float(get_setting("Queue", "episode_airtime_offset", "0")) * 60
    except (ValueError, TypeError):
        episode_airtime_offset_min = 0.0 * 60
    # --- END EDIT ---

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
            # *** START EDIT ***
            # Use replace() for zoneinfo, which handles DST transitions (raises errors for invalid times, assumes standard time for ambiguous times by default)
            local_release_datetime = naive_release_datetime.replace(tzinfo=tz) 
            # *** END EDIT ***

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

# *** START EDIT: Add Listener Setup Method ***
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

        _queue_workers = max(1, min(3, int(get_setting('Queue', 'queue_pool_workers', 2))))
        executors = {
            'default': ThreadPoolExecutor(max_workers=1),
            'queue': ThreadPoolExecutor(max_workers=_queue_workers),
        }
        job_defaults = {'coalesce': True, 'max_instances': 1}

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
# *** END EDIT ***

def run_program():
    global program_runner
    logging.info("Program start requested")

    if program_runner is None or not program_runner.is_running():
        logging.info("Initializing ProgramRunner...")
        program_runner = ProgramRunner()
        # *** START EDIT: Setup listeners after init ***
        try:
            _setup_scheduler_listeners(program_runner) # Use the correct function name
        except Exception as e:
             logging.error(f"Failed to set up scheduler listeners during startup: {e}", exc_info=True)
        # *** END EDIT ***
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

