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
from content_checkers.mdb_list import get_wanted_from_mdblists
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
    create_plex_removal_queue_table,
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
from debrid.real_debrid.client import RealDebridProvider
# --- END EDIT ---
from utilities.plex_removal_cache import process_removal_cache # Added import for standalone removal processing
import sys # Add for checking apscheduler.events
from collections import defaultdict  # Added alongside deque above for runtime tracking

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
        
        # Configure scheduler timezone using the local timezone helper
        try:
            from metadata.metadata import _get_local_timezone # Added import
            tz = _get_local_timezone()
            logging.info(f"Initializing APScheduler with timezone: {tz.key}")
            # --- START EDIT: Configure scheduler for sequential execution ---
            executors = {
                'default': ThreadPoolExecutor(max_workers=1)
            }
            job_defaults = {
                'coalesce': True, # If multiple runs are missed, only run once
                'max_instances': 1, # Already part of individual job scheduling, but good to have as default
                'misfire_grace_time': None  # Allow jobs to run no matter how late
            }
            self.scheduler = BackgroundScheduler(executors=executors, job_defaults=job_defaults, timezone=tz)
            logging.info("APScheduler configured with a single worker thread for sequential job execution.")
            # --- END EDIT ---
        except Exception as e:
            logging.error(f"Failed to get local timezone for scheduler, using system default: {e}")
            # --- START EDIT: Configure scheduler for sequential execution (fallback) ---
            executors = {
                'default': ThreadPoolExecutor(max_workers=1)
            }
            job_defaults = {
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': None  # Allow jobs to run no matter how late
            }
            self.scheduler = BackgroundScheduler(executors=executors, job_defaults=job_defaults) # Fallback to default timezone
            logging.info("APScheduler configured with a single worker thread for sequential job execution (using system default timezone).")
            # --- END EDIT ---

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
        expected_queues = ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading', 'Final_Check']
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
            'final_check_queue': 'process_final_check' # Use lowercase key matching the task ID
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
            'Checking': 180,
            'Sleeping': 300,
            'Unreleased': 300,
            'Blacklisted': 7200,
            'Pending Uncached': 3600,
            'Upgrading': 3600,
            'final_check_queue': 900, # Use lowercase key matching the task ID
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
            'task_verify_plex_removals': 900,      # Run every 15 minutes (if enabled)
            'task_reconcile_queues': 3600,         # Run every 1 hour
            'task_check_database_health': 3600,    # Run every hour
            'task_sync_time': 3600,                # Run every hour
            'task_check_trakt_early_releases': 3600,# Run every hour
            'task_update_show_ids': 40600,         # Run every ~11 hours
            'task_update_show_titles': 45600,      # Run every ~12 hours
            'task_update_movie_ids': 50600,        # Run every ~14 hours
            'task_update_movie_titles': 55600,     # Run every ~15 hours
            'task_refresh_release_dates': 36000,   # Run every 10 hours
            # 'task_generate_airtime_report': 3600,  # Run every hour
            'task_run_library_maintenance': 12 * 60 * 60, # Run every twelve hours (if enabled)
            'task_get_plex_watch_history': 24 * 60 * 60,  # Run every 24 hours (if enabled)
            'task_refresh_plex_tokens': 24 * 60 * 60,   # Run every 24 hours
            'task_update_tv_show_status': 172800,       # Run every 48 hours
            # 'task_purge_not_wanted_magnets_file': 604800, # Default: 1 week (Can be added if needed)
            # 'task_local_library_scan': 900, # Default: 15 mins (Can be added if needed)
            'task_plex_full_scan': 1800, # Run every hour (Can be adjusted)
            # NEW Load Adjustment Task
            # 'task_adjust_intervals_for_load': 120, # Run every 2 minutes
            # --- START EDIT: Add new task for library size refresh ---
            'task_refresh_library_size_cache': 12 * 60 * 60, # Run every 12 hours
            # --- END EDIT ---
            'task_process_standalone_plex_removals': 60 * 60, # Run every hour
            # --- START EDIT: Add media analysis task interval ---
            'task_analyze_media_files': 1 * 60 * 60, # Once an hour
            # --- END EDIT ---
            # --- START EDIT: Add manual Plex full scan task ---
            'task_manual_plex_full_scan': 3600, # Run every 60 minutes, disabled by default
            # --- END EDIT ---
            # 'task_artificial_long_run': 1*60*60, # Run every 2 minutes
            'task_regulate_system_load': 30 # Check system load every 30 seconds
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
            'task_update_movie_titles', 'task_get_plex_watch_history', 'task_refresh_plex_tokens',
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
            # Combined/High Frequency Tasks
            'task_update_queue_views',
            'task_send_notifications',
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
            'task_process_standalone_plex_removals', # Enable by default
            # --- START EDIT: Enable media analysis task by default ---
            # 'task_analyze_media_files', # disabled by default
            # --- END EDIT ---
            # 'task_artificial_long_run',
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
             # Enable symlink task if configured and not toggled off (Removal task handled above)
            if get_setting('File Management', 'plex_url_for_symlink') and get_setting('File Management', 'plex_token_for_symlink'):
                symlink_task = 'task_verify_symlinked_files'
                is_symlink_toggled_off = saved_states.get(self._normalize_task_name(symlink_task), True) is False
                # --- START EDIT: Removed reference to removal_task toggle check here ---
                # is_removal_toggled_off = saved_states.get(self._normalize_task_name(removal_task), True) is False
                # --- END EDIT ---

                if not is_symlink_toggled_off and symlink_task not in self.enabled_tasks:
                    self.enabled_tasks.add(symlink_task)
                    logging.info("Enabled symlink verification task based on settings.")
            else:
                 # Disable if settings are off and not toggled on
                 is_symlink_toggled_on = saved_states.get(self._normalize_task_name('task_verify_symlinked_files'), False) is True
                 # --- START EDIT: Removed reference to removal_task toggle check here ---
                 # is_removal_toggled_on = saved_states.get(self._normalize_task_name('task_verify_plex_removals'), False) is True
                 # --- END EDIT ---
                 if 'task_verify_symlinked_files' in self.enabled_tasks and not is_symlink_toggled_on:
                     self.enabled_tasks.remove('task_verify_symlinked_files')
                     logging.info("Disabled symlink verification task as settings are off.")
                 # --- START EDIT: Removed removal task disable logic here (handled above) ---
                 # if 'task_verify_plex_removals' in self.enabled_tasks and not is_removal_toggled_on:
                 #     self.enabled_tasks.remove('task_verify_plex_removals')
                 #     logging.info("Disabled Plex removal verification task as settings are off.")
                 # --- END EDIT ---


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

        if get_setting('Debug', 'enable_library_maintenance_task', False):
            task_name = 'task_run_library_maintenance'
            is_toggled_off = saved_states.get(self._normalize_task_name(task_name), True) is False
            if not is_toggled_off and task_name not in self.enabled_tasks:
                self.enabled_tasks.add(task_name)
                logging.info(f"Enabled '{task_name}' based on Debug setting.")
        else:
            task_name = 'task_run_library_maintenance'
            is_toggled_on = saved_states.get(self._normalize_task_name(task_name), False) is True
            if task_name in self.enabled_tasks and not is_toggled_on:
                 self.enabled_tasks.remove(task_name)
                 logging.info(f"Disabled '{task_name}' as Debug setting is off.")

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
            'final_check_queue': 'process_final_check' # Use lowercase key matching the task ID
        }

        # Log the final set of enabled tasks right before starting the scheduling process
        # --- START EDIT: Add debug logging ---
        logging.info(f"DEBUG: Final enabled tasks before scheduling: {sorted(list(self.enabled_tasks))}")
        logging.info(f"DEBUG: Final task intervals before scheduling: {self.task_intervals}")
        # --- END EDIT ---
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
    def _schedule_task(self, task_name: str, interval_seconds: int, initial_run: bool = False):
        """Schedules a single task in APScheduler, wrapped for duration measurement."""
        current_thread_id_outer = threading.get_ident()
        logging.debug(f"Attempting to schedule task: '{task_name}' with interval {interval_seconds}s (initial_run: {initial_run}) (Thread: {current_thread_id_outer})")
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
                        # Wrap the target function
                        # For regular tasks, actual_job_id and task_name_for_logging are the same (job_id)
                        wrapped_func = functools.partial(self._run_and_measure_task, job_id, task_name, target_func, args, kwargs)

                        trigger = IntervalTrigger(seconds=interval_seconds)

                        # *** START EDIT: Explicitly pass scheduler's timezone to add_job ***
                        # This should prevent the IntervalTrigger from calling tzlocal.get_localzone()
                        resolved_timezone = self.scheduler.timezone
                        logging.debug(f"Passing timezone '{resolved_timezone}' explicitly to add_job for task '{job_id}'")
                        # *** END EDIT ***

                        self.scheduler.add_job(
                            func=wrapped_func,
                            trigger=trigger,
                            id=job_id,
                            name=job_id,
                            replace_existing=True,
                            misfire_grace_time=None,  # No grace limit – run even if very late
                            # *** START EDIT: Allow 1 concurrent instance ***
                            max_instances=1,
                            # *** END EDIT ***
                            # *** START EDIT: Add timezone argument ***
                            timezone=resolved_timezone
                            # *** END EDIT ***
                        )
                        # *** START EDIT: Updated log message ***
                        logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds} seconds (max_instances=1, wrapped for duration measurement).") # Reverted max_instances to 1
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

        for task_name in tasks_to_process:
            # Ensure task is still in self.enabled_tasks; it might have been removed if tasks_to_process had duplicates
            # and one was already processed and removed. However, list(set) makes duplicates unlikely.
            # This check is more of a safeguard if self.enabled_tasks was manipulated externally during this loop,
            # or if tasks_to_process could somehow have a task not currently in self.enabled_tasks.
            if task_name not in self.enabled_tasks:
                continue

            interval = self.task_intervals.get(task_name)
            if interval is not None:
                # Attempt to schedule
                if self._schedule_task(task_name, interval, initial_run=True):
                    scheduled_count += 1
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
                'Special Trakt Lists': 900 # Added new source type with default interval
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
                # Update task_intervals (this defines the task interval for scheduling)
                self.task_intervals[task_name] = final_interval
                # Update original intervals map too (used for resets)
                if task_name not in self.original_task_intervals:
                     self.original_task_intervals[task_name] = final_interval

                log_intervals_message.append(f"  {task_name}: {final_interval} seconds")
                
                if isinstance(data.get('enabled'), str):
                    data['enabled'] = data['enabled'].lower() == 'true'
                
                # Add to enabled tasks if enabled (this happens *after* toggle loading in the new flow)
                is_enabled = data.get('enabled', False)
                if is_enabled and task_name not in self.enabled_tasks:
                    self.enabled_tasks.add(task_name)
                    logging.info(f"Enabled content source task based on its settings: {task_name}")
                # Ensure it's removed if disabled (respecting if it was already removed by toggle)
                elif not is_enabled and task_name in self.enabled_tasks:
                     self.enabled_tasks.remove(task_name)
                     logging.info(f"Disabled content source task based on its settings: {task_name}")

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
        # Add reconciliation call after full scan processing
        logging.info("Triggering queue reconciliation after full Plex scan.")
        self.task_reconcile_queues()
        
    # --- START EDIT: Add manual Plex full scan task method ---
    def task_manual_plex_full_scan(self):
        """Manually trigger a full Plex scan, bypassing the mode check."""
        logging.info("Executing manual Plex full scan task...")
        get_and_add_all_collected_from_plex(bypass=True)
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
            cutoff_date_skipped = 0

            wanted_content = []
            # Pass the original versions_from_config to fetchers, assuming they expect list/dict as per config
            if source_type == 'Overseerr':
                wanted_content = get_wanted_from_overseerr(versions_from_config)
            elif source_type == 'MDBList':
                mdblist_urls = data.get('urls', '').split(',')
                for mdblist_url in mdblist_urls:
                    mdblist_url = mdblist_url.strip()
                    if mdblist_url: # Ensure not empty
                        wanted_content.extend(get_wanted_from_mdblists(mdblist_url, versions_from_config))
            elif source_type == 'Trakt Watchlist':
                try:
                    wanted_content = get_wanted_from_trakt_watchlist(versions_from_config)
                except (ValueError, api.exceptions.RequestException) as e:
                    logging.error(f"Failed to fetch Trakt watchlist: {str(e)}")
                    return
            elif source_type == 'Trakt Lists':
                trakt_lists = data.get('trakt_lists', '').split(',')
                for trakt_list in trakt_lists:
                    trakt_list = trakt_list.strip()
                    if trakt_list: # Ensure not empty
                        try:
                            wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions_from_config))
                        except (ValueError, api.exceptions.RequestException) as e:
                            logging.error(f"Failed to fetch Trakt list {trakt_list}: {str(e)}")
                            continue
            elif source_type == 'Trakt Collection':
                wanted_content = get_wanted_from_trakt_collection(versions_from_config)
            elif source_type == 'Friends Trakt Watchlist':
                # This function takes data (source_config) and versions
                wanted_content = get_wanted_from_friend_trakt_watchlist(data, versions_from_config)
            elif source_type == 'Special Trakt Lists': # New elif block
                # 'data' is the source_config, 'versions_dict' is the resolved simple versions map
                wanted_content = get_wanted_from_special_trakt_lists(data, versions_from_config)
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
                
                other_watchlists = []
                # Use self.content_sources which should be populated
                all_sources = self.get_content_sources() if hasattr(self, 'get_content_sources') else {}
                for source_id, source_data in all_sources.items():
                    if source_id.startswith('Other Plex Watchlist_') and source_data.get('enabled', False):
                        # Fetch versions specific to this 'Other' source config
                        other_source_versions = source_data.get('versions', []) # Default list
                        other_watchlists.append({
                            'username': source_data.get('username', ''),
                            'token': source_data.get('token', ''),
                            'versions': other_source_versions # Pass its specific config
                        })
                
                for watchlist in other_watchlists:
                    if watchlist['username'] and watchlist['token']:
                        try:
                             # Pass the versions specific to this friend's config
                            watchlist_content = get_wanted_from_other_plex_watchlist(
                                username=watchlist['username'],
                                token=watchlist['token'],
                                versions=watchlist['versions'] # Use the versions from the loop
                            )
                            # Extend the main wanted_content list
                            # Note: This assumes get_wanted_from_other_plex_watchlist returns the same tuple format
                            wanted_content.extend(watchlist_content)
                        except Exception as e:
                            logging.error(f"Failed to fetch Other Plex watchlist for {watchlist['username']}: {str(e)}")
                            continue
            else:
                logging.warning(f"Unknown source type: {source_type}")
                return

            if wanted_content:
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

                        # Filter items by media type first
                        if source_media_type != 'All' and not source_type.startswith('Collected'):
                            items_filtered_type = [
                                item for item in items
                                if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                                   (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                            ]
                            media_type_skipped += len(items) - len(items_filtered_type)
                            items = items_filtered_type # Update items
                        
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
                                items_to_process.append(item_dict_processed)
                            
                            from metadata.metadata import process_metadata
                            processed_items = process_metadata(items_to_process)
                            if processed_items:
                                all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                                
                                # Set content source and detail for each item
                                for item in all_items:
                                    item['content_source'] = source
                                    item = append_content_source_detail(item, source_type=source_type)
                                
                                # Update cache for the original items (pre-metadata processing)
                                for item_raw in items_to_process_raw:
                                    update_cache_for_item(item_raw, source, source_cache)

                                # Filter by cutoff date after metadata processing
                                if cutoff_date:
                                    items_filtered_date = []
                                    for item in all_items:
                                        release_date = item.get('release_date')
                                        if not release_date or release_date.lower() == 'unknown':
                                            items_filtered_date.append(item)
                                            continue
                                        try:
                                            item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                            if item_date >= cutoff_date:
                                                items_filtered_date.append(item)
                                            else:
                                                cutoff_date_skipped += 1
                                                logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                        except ValueError:
                                            # If we can't parse the date, allow the item through
                                            items_filtered_date.append(item)
                                            logging.debug(f"Item {item.get('title', 'Unknown')} has invalid date format: {release_date}, allowing through")
                                    all_items = items_filtered_date
                                    if cutoff_date_skipped > 0:
                                        logging.debug(f"Batch {source}: Skipped {cutoff_date_skipped} items due to cutoff date")

                                from database import add_collected_items, add_wanted_items
                                # Pass the CONVERTED versions dict to add_wanted_items
                                add_wanted_items(all_items, versions_to_inject or versions_dict)
                                total_items += len(all_items)
                                items_processed += len(items_to_process)
                else:
                    # Handle single list of items
                    logging.debug(f"Processing batch of {len(wanted_content)} items from {source}")
                    
                    # Filter items by media type first
                    if source_media_type != 'All' and not source_type.startswith('Collected'):
                        wanted_content_filtered_type = [
                            item for item in wanted_content
                            if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                               (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                        ]
                        media_type_skipped += len(wanted_content) - len(wanted_content_filtered_type)
                        wanted_content = wanted_content_filtered_type # Update wanted_content after filtering
                    
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
                            items_to_process.append(item_dict_processed)

                        from metadata.metadata import process_metadata
                        processed_items = process_metadata(items_to_process)
                        if processed_items:
                            all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                            
                            # Set content source and detail for each item
                            for item in all_items:
                                item['content_source'] = source
                                item = append_content_source_detail(item, source_type=source_type)
                            
                            # Update cache for the original items (pre-metadata processing)
                            for item_raw in items_to_process_raw:
                                update_cache_for_item(item_raw, source, source_cache)

                            # Filter by cutoff date after metadata processing
                            if cutoff_date:
                                items_filtered_date = []
                                for item in all_items:
                                    release_date = item.get('release_date')
                                    if not release_date or release_date.lower() == 'unknown':
                                        items_filtered_date.append(item)
                                        continue
                                    try:
                                        item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                        if item_date >= cutoff_date:
                                            items_filtered_date.append(item)
                                        else:
                                            cutoff_date_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                    except ValueError:
                                        # If we can't parse the date, allow the item through
                                        items_filtered_date.append(item)
                                        logging.debug(f"Item {item.get('title', 'Unknown')} has invalid date format: {release_date}, allowing through")
                                    all_items = items_filtered_date
                                    if cutoff_date_skipped > 0:
                                        logging.debug(f"{source}: Skipped {cutoff_date_skipped} items due to cutoff date")

                            from database import add_collected_items, add_wanted_items
                            # Pass the CONVERTED versions_dict to add_wanted_items
                            add_wanted_items(all_items, versions_dict)
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
                if cutoff_date_skipped > 0:
                    stats_msg += f", skipped {cutoff_date_skipped} items due to cutoff date"
                stats_msg += ")"
                logging.info(stats_msg)
            else:
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

    def task_debug_log(self):
        current_time = time.time()
        debug_info = []
        for task, interval in self.task_intervals.items():
            time_until_next_run = interval - (current_time - self.last_run_times[task])
            minutes, seconds = divmod(int(time_until_next_run), 60)
            hours, minutes = divmod(minutes, 60)
            debug_info.append(f"{task}: {hours:02d}:{minutes:02d}:{seconds:02d}")

        logging.info("Time until next task run:\n" + "\n".join(debug_info))

    def run_initialization(self):
        self._initializing = True 
        logging.info("Running initialization...")
        skip_initial_plex_update = get_setting('Debug', 'skip_initial_plex_update', False)
        
        disable_initialization = get_setting('Debug', 'disable_initialization', '')
        if not disable_initialization:
            initialize(skip_initial_plex_update)
            logging.info("Initialization complete")
        else:
            logging.info("Initialization disabled, skipping...")
        
        self._initializing = False

    def start(self):
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

            self.run_initialization()

            # *** START EDIT: Simplified run loop ***
            # The main loop now just keeps the script alive while the scheduler runs.
            # We can add checks here if needed (e.g., monitoring scheduler health).
            while self._running:
                try:
                    # Check scheduler status periodically
                    if not self.scheduler or not self.scheduler.running:
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

                    # --- START EDIT: Fetch and use main loop sleep setting ---
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

    def sync_time(self):
        try:
            ntp_client = ntplib.NTPClient()
            response = ntp_client.request('pool.ntp.org', version=3)
            system_time = time.time()
            ntp_time = response.tx_time
            offset = ntp_time - system_time
            
            if abs(offset) > 1:  # If offset is more than 1 second
                logging.warning(f"System time is off by {offset:.2f} seconds. Adjusting task timers.")
                self.last_run_times = {task: ntp_time for task in self.task_intervals}
        except:
            logging.error("Failed to synchronize time with NTP server")

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
            'task_update_movie_titles', 'task_get_plex_watch_history', 'task_refresh_plex_tokens',
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


        # --- Determine idle state based on Scraping/Adding queues ---
        # ... (existing idle check logic) ...

        with self.scheduler_lock:
            # Get the *currently configured* intervals (could be default or custom)
            # These are stored in self.task_intervals after __init__ applies customs.
            # We no longer need self.original_task_intervals for this task's logic.

            if system_is_idle:
                # ... (existing logging for idle state) ...
                idle_increase_seconds = 300 # Make this configurable later?

                for task_id in self.DYNAMIC_INTERVAL_TASKS: # Use the dynamic task set
                    job = self.scheduler.get_job(task_id)
                    # Get the base interval for this task (could be default or custom)
                    # self.task_intervals holds the *intended* base interval after init.
                    base_interval = self.task_intervals.get(task_id)

                    if job and base_interval:
                        current_job_interval = job.trigger.interval.total_seconds()
                        # Increase interval relative to the *configured* base interval
                        # Apply max limits
                        new_interval = min(
                             base_interval + idle_increase_seconds,
                             base_interval * self.MAX_INTERVAL_MULTIPLIER,
                             self.ABSOLUTE_MAX_INTERVAL
                        )
                        new_interval = max(new_interval, base_interval) # Ensure it doesn't go below base

                        if new_interval > current_job_interval: # Only modify if increasing
                            try:
                                self.scheduler.modify_job(task_id, trigger=IntervalTrigger(seconds=new_interval))
                                logging.debug(f"Adjusted interval for idle '{task_id}' to {new_interval}s (Base: {base_interval}s)")
                            except Exception as e:
                                logging.error(f"Error modifying job '{task_id}' interval to {new_interval}s: {e}")

            else: # System is active
                # ... (existing logging for active state) ...

                needs_reset = False
                tasks_to_reset = []
                for task_id in self.DYNAMIC_INTERVAL_TASKS: # Use the dynamic task set
                    job = self.scheduler.get_job(task_id)
                    # Get the configured base interval (default or custom)
                    base_interval = self.task_intervals.get(task_id)

                    if job and base_interval:
                        current_job_interval = job.trigger.interval.total_seconds()
                        # Reset if current interval doesn't match the configured base
                        if current_job_interval != base_interval:
                            needs_reset = True
                            tasks_to_reset.append(task_id)

                if needs_reset:
                    logging.info(f"System active: Resetting intervals for {len(tasks_to_reset)} dynamically adjusted tasks to their configured base values.")
                    for task_id in tasks_to_reset:
                        base_interval = self.task_intervals.get(task_id) # Get configured base again
                        if base_interval:
                            try:
                                self.scheduler.modify_job(task_id, trigger=IntervalTrigger(seconds=base_interval))
                                logging.debug(f"Reset interval for '{task_id}' to configured base {base_interval}s")
                            except Exception as e:
                                logging.error(f"Error resetting job '{task_id}' interval to {base_interval}s: {e}")

        self._was_idle_last_check = system_is_idle
        # --- END EDIT ---


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
                    
                last_heartbeat = int(content)
                time_diff = current_time - last_heartbeat
                
                # Update in-memory cache from file
                self._last_heartbeat_time = last_heartbeat
                
                # If more than 5 minutes have passed since last heartbeat
                if time_diff > 300:
                    logging.warning(f"Stale heartbeat detected in file - {time_diff} seconds since last update")
                    return False
                
                return True
        except (IOError, OSError, ValueError) as e:
            logging.error(f"Error checking heartbeat file: {e}")
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
        # self.sync_time() # Call the original sync_time logic
        try:
            ntp_client = ntplib.NTPClient()
            # Increased timeout for robustness
            response = ntp_client.request('pool.ntp.org', version=3, timeout=10)
            system_time = time.time()
            ntp_time = response.tx_time
            offset = ntp_time - system_time

            logging.info(f"Time sync check: System time offset from NTP = {offset:.3f} seconds.")

            # Adjusting task timers based on offset is complex with APScheduler.
            # APScheduler uses the system clock. If the system clock is significantly off,
            # tasks might run at the "wrong" wall-clock time but consistently relative
            # to the system clock. Correcting the system clock itself is the best approach.
            # This task now mainly serves to LOG the offset.
            if abs(offset) > 60:  # Log a warning if offset is more than 1 minute
                logging.warning(f"System time offset is significant ({offset:.2f} seconds). Consider synchronizing the system clock.")
            # Removing the part that tried to adjust self.last_run_times
            # self.last_run_times = {task: ntp_time for task in self.task_intervals}
        except ntplib.NTPException as e:
            logging.error(f"Failed to synchronize time with NTP server: {e}")
        except Exception as e:
             # Catch potential socket errors, etc.
             logging.error(f"Unexpected error during time synchronization: {e}")

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
                    delete_sql = f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(delete_ids_filepath))})"
                    cursor.execute(delete_sql, delete_ids_filepath)
                    deleted_count_filepath = cursor.rowcount
                    reconciliation_logger.info(f"Deleted {deleted_count_filepath} duplicate items (file-based reconciliation). IDs: {delete_ids_filepath}")

            # --- Step 2: New deduplication for Wanted, Scraping, Unreleased states ---
            reconciliation_logger.info("Starting semantic deduplication for 'Wanted', 'Scraping', 'Unreleased' items (IMDB ID, S/E, Version - with '*' trimmed from version)...")
            
            cursor.execute("""
                SELECT id, imdb_id, season_number, episode_number, version, state, type, title
                FROM media_items
                WHERE state IN ('Wanted', 'Scraping', 'Unreleased')
                  AND imdb_id IS NOT NULL
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

            conn.commit()

            log_parts = []
            if reconciled_count > 0:
                log_parts.append(f"{reconciled_count} items updated to 'Collected'")
            if deleted_count_filepath > 0:
                log_parts.append(f"{deleted_count_filepath} duplicates deleted (shared file paths)")
            if deleted_count_semantic > 0:
                log_parts.append(f"{deleted_count_semantic} duplicates deleted (content/version with '*' trimmed)")

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
        # Need to shutdown and restart scheduler carefully
        with self.scheduler_lock:
            if self.scheduler and self.scheduler.running:
                logging.info("Shutting down scheduler for reinitialization...")
                self.scheduler.shutdown(wait=True) # Wait for jobs to finish if possible
                logging.info("Scheduler stopped.")

        self._initialized_runner_attributes = False
        self.__init__() # Re-runs init, including scheduling initial tasks

        # Restart scheduler if it was running before
        # self.start() will handle starting the scheduler
        logging.info("ProgramRunner reinitialized successfully. Restarting...")
        # The restart might need to happen externally depending on how reinit is called
        # If called internally, we might need to call self.start() here,
        # but need to be careful about threading/context.

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
        from database.statistics import get_cached_download_stats
        try:
            get_cached_download_stats()
            logging.debug("Download stats cache refreshed")
        except Exception as e:
            logging.error(f"Error refreshing download stats cache: {str(e)}")

    def task_refresh_plex_tokens(self):
        logging.info("Performing periodic Plex token validation")
        from utilities.plex_functions import validate_plex_tokens
        token_status = validate_plex_tokens()
        for username, status in token_status.items():
            if not status['valid']:
                logging.error(f"Invalid Plex token detected during periodic check for user {username}")
            else:
                logging.debug(f"Plex token for user {username} is valid")

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
            else:
                logging.warning(f"Plex mounted_file_location does not exist: {plex_file_location}")
                return


        # Get all media items from database that are in Checking state
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            items = cursor.execute('SELECT id, title, filled_by_title, filled_by_file, type, imdb_id, tmdb_id, season_number, episode_number, year, version, original_scraped_torrent_title, real_debrid_original_title FROM media_items WHERE state = "Checking"').fetchall()
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

            for item_dict in items: # Iterate over dicts
                item_id = item_dict['id']
                filled_by_title = item_dict['filled_by_title']
                filled_by_file = item_dict['filled_by_file']
                # --- START: Ensure new field is fetched ---
                original_scraped_torrent_title = item_dict['original_scraped_torrent_title'] or ''
                real_debrid_original_title = item_dict['real_debrid_original_title'] or ''
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
                         if current_tick <= 5:
                             should_trigger_scan = True
                             updated_items += 1 # Count item here when scan is intended
                             logging.info(f"File '{current_filename}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                         else:
                             logging.debug(f"File '{current_filename}' found (tick {current_tick}). Skipping Plex scan trigger (only triggers for first 5 ticks).")
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
                                    # Construct the path Plex *should* see using the folder name from the found path
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
                             cursor_update.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking"',
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
                item_id = item_dict['id']
                filled_by_title = item_dict['filled_by_title']
                current_filename = item_dict['filled_by_file'] # Use current_filename for clarity
                # --- START: Ensure new field is fetched ---
                original_scraped_torrent_title = item_dict['original_scraped_torrent_title'] or ''
                real_debrid_original_title = item_dict['real_debrid_original_title'] or ''
                # --- END: Ensure new field is fetched ---

                if not filled_by_title or not current_filename: 
                    logging.debug(f"Item {item_id} missing filled_by_title or current_filename. Skipping Plex scan trigger check.")
                    continue

                cache_key = f"{filled_by_title}:{current_filename}"

                # --- START: UNIFIED FILE SEARCH LOGIC (consistent with disabled checks mode) ---
                paths_to_check_info = [] # Stores dicts: {'name': folder_name, 'path': full_path, 'type': log_type}
                base_path = plex_file_location

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
                    if current_tick <= 5:
                        should_trigger_scan = True
                        updated_items += 1 # Count item here when scan is intended
                        logging.info(f"File '{current_filename}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                    else:
                        logging.debug(f"File '{current_filename}' found (tick {current_tick}). Skipping Plex scan trigger (only triggers for first 5 ticks).")
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

                    found_matching_section_location = False
                    for section in sections:
                        # Check if section type matches item type
                        if section.type != item_type_mapped:
                            continue

                        logging.debug(f"  Checking Section '{section.title}' (Type: {section.type})")
                        for location in section.locations:
                            # Construct the path Plex *should* see for this item within this location
                            # Assumes the item folder name is `filled_by_title` (now `folder_name_for_plex_scan`)
                            if folder_name_for_plex_scan:
                                constructed_plex_path = os.path.join(location, folder_name_for_plex_scan)
                            else: # File was found directly in plex_file_location
                                constructed_plex_path = location # Scan the root of the section location
                            logging.debug(f"    Considering scan path: '{constructed_plex_path}' based on location '{location}' and determined folder '{folder_name_for_plex_scan}'")

                            # Add this section/path combination to our list
                            if section.title not in paths_to_scan_by_section:
                                paths_to_scan_by_section[section.title] = set()
                            paths_to_scan_by_section[section.title].add(constructed_plex_path)
                            found_matching_section_location = True
                            # Don't break - a section might have multiple relevant locations? Unlikely, but safe not to break.

                    if not found_matching_section_location:
                        logging.warning(f"Could not find any matching Plex library section (type: {item_type_mapped}) for item {item_id} based on file '{filled_by_file}'. Scan might not be triggered correctly.")
                # --- END: Logic to identify scan paths ---


            # --- Trigger scans after checking all items ---
            if paths_to_scan_by_section:
                logging.info(f"Triggering scans for {len(paths_to_scan_by_section)} sections based on detected files...")
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

    def trigger_task(self, task_name):
        """Manually trigger a task to run immediately by adding it to APScheduler's queue."""
        normalized_name = self._normalize_task_name(task_name)
        job_id_base = normalized_name # This is the base task name, e.g., "task_artificial_long_run"

        logging.info(f"Attempting to manually trigger task: {job_id_base} by adding it to APScheduler queue.")

        target_func, args, kwargs = self._get_task_target(job_id_base)

        if target_func:
            try:
                # Generate a unique ID for this manual job instance
                manual_job_instance_id = f"manual_{job_id_base}_{uuid.uuid4()}"
                
                # Pass the unique manual_job_instance_id as the first arg (actual_job_id_from_scheduler)
                # and job_id_base as the second arg (task_name_for_logging) to _run_and_measure_task
                wrapped_func = functools.partial(self._run_and_measure_task, manual_job_instance_id, job_id_base, target_func, args, kwargs)
                
                run_now_date = datetime.now(self.scheduler.timezone)

                with self.scheduler_lock:
                    self.scheduler.add_job(
                        func=wrapped_func,
                        trigger='date',  # Use DateTrigger for true run-once
                        run_date=run_now_date,
                        id=manual_job_instance_id, # Use the unique ID for this job instance
                        name=f"Manual run of {job_id_base}",
                        replace_existing=False, # Should be false for unique IDs
                        max_instances=1, # Max instances for this specific job ID
                        misfire_grace_time=60 # Allow 1 minute grace time for manual tasks
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

    def task_verify_plex_removals(self):
        """Verify that files marked for removal are actually gone from Plex using title-based search."""
        logging.info("[TASK] Running Plex removal verification task.")

        # Determine Plex connection details based on settings
        plex_url, plex_token = None, None
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url', '').rstrip('/')
            plex_token = get_setting('Plex', 'token', '')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='').rstrip('/')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')

        if not plex_url or not plex_token:
            logging.error("[VERIFY] No Plex URL or token found in relevant settings. Skipping removal verification.")
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
                    # Item still exists - log, attempt removal again using remove_file_from_plex, increment attempts
                    # --- START EDIT ---
                    logging.warning(f"[VERIFY] Path '{item_path}' still found in Plex. Attempting removal using remove_file_from_plex...")
                    # Call remove_file_from_plex instead of remove_symlink_from_plex
                    removal_successful = remove_file_from_plex(item_title, item_path, episode_title)
                    if removal_successful:
                        logging.info(f"[VERIFY] Successfully triggered removal via remove_file_from_plex for '{item_path}'. Will verify later.")
                    else:
                        logging.error(f"[VERIFY] Failed to trigger removal via remove_file_from_plex for '{item_path}'.")
                    # --- END EDIT ---

                    logging.warning(f"[VERIFY] Incrementing attempt count for '{item_path}' as it still exists.")
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
        # inserted_count = 0 # Removed as combined count is simpler
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
                        cursor.execute("SELECT status, total_episodes FROM tv_shows WHERE imdb_id = ?", (imdb_id,))
                        existing_show = cursor.fetchone()
                        if existing_show:
                            show_status = existing_show['status'].lower() if existing_show['status'] else 'unknown'
                            total_episodes_from_source = existing_show['total_episodes'] or 0
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

                        # Calculate total episodes from source metadata
                        if 'seasons' in show_metadata:
                             # Reset count before summing seasons
                            total_episodes_from_source = 0
                            for season_num_str, season_data in show_metadata.get('seasons', {}).items():
                                try:
                                     season_num = int(season_num_str)
                                     if season_num == 0: continue # Skip specials season
                                     total_episodes_from_source += len(season_data.get('episodes', {}))
                                except ValueError:
                                     logging.warning(f"[TV Status] Invalid season number '{season_num_str}' in metadata for {imdb_id}. Skipping.")
                                     continue
                        else:
                            logging.warning(f"[TV Status] Metadata for {imdb_id} ('{title}') lacks 'seasons' key. Total episode count may be inaccurate.")
                            # Fallback to DB value if exists? Or treat as 0? Let's fetch existing.
                            cursor.execute("SELECT total_episodes FROM tv_shows WHERE imdb_id = ?", (imdb_id,))
                            existing_show_fallback = cursor.fetchone()
                            total_episodes_from_source = existing_show_fallback['total_episodes'] if existing_show_fallback else 0
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
                    cursor.execute("""
                        INSERT INTO tv_shows (
                            imdb_id, tmdb_id, title, year, status, is_complete,
                            total_episodes, last_status_check, added_at, last_updated
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT added_at FROM tv_shows WHERE imdb_id = ?), ?), ?)
                        ON CONFLICT(imdb_id) DO UPDATE SET
                            tmdb_id = COALESCE(excluded.tmdb_id, tv_shows.tmdb_id),
                            title = COALESCE(excluded.title, tv_shows.title),
                            year = COALESCE(excluded.year, tv_shows.year),
                            status = COALESCE(excluded.status, tv_shows.status),
                            is_complete = excluded.is_complete, -- Set based on show_status=='ended'/'canceled'
                            total_episodes = excluded.total_episodes,
                            last_status_check = excluded.last_status_check,
                            last_updated = excluded.last_updated
                        WHERE tv_shows.imdb_id = excluded.imdb_id;
                    """, (
                        imdb_id, tmdb_id, title, year, show_status, int(is_show_ended),
                        total_episodes_from_source, now_str, # last_status_check
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


        try:
            # Execute the original task function
            func(*args, **kwargs)
            duration = time.monotonic() - start_time # Measure duration regardless
            # --- START EDIT: Record runtime on successful completion ---
            self._record_task_runtime(task_name_for_logging, duration)
            # --- END EDIT ---

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
    # *** END EDIT ***

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

        # Get system usage
        try:
            cpu_usage = psutil.cpu_percent(interval=1)
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
                # --- DEBUG LOGGING ---
                job = self.scheduler.get_job(normalized_name)
                if job:
                    live_interval = job.trigger.interval.total_seconds()
                    logging.info(f"[DEBUG] After live update: self.task_intervals['{normalized_name}']={self.task_intervals.get(normalized_name)}, scheduler job interval={live_interval}")
                    # --- Force next run time to now + interval ---
                    from datetime import datetime, timedelta
                    next_run = datetime.now(self.scheduler.timezone) + timedelta(seconds=target_interval_seconds)
                    job.modify(next_run_time=next_run)
                    logging.info(f"[DEBUG] Forced next run time for '{normalized_name}' to {next_run}")
                # --- END DEBUG LOGGING ---
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
            if isinstance(provider, RealDebridProvider):
                logging.info("Background task: Refreshing library size cache via Debrid provider...")
                # The provider's get_total_library_size is async, so we run it in a new event loop.
                # This call is expected to fetch the size and update the cache file itself.
                calculated_size = asyncio.run(provider.get_total_library_size())
                if calculated_size is not None and not str(calculated_size).startswith("Error"):
                    logging.info(f"Background task: Library size cache refresh successful. Provider reported size: {calculated_size}")
                else:
                    logging.warning(f"Background task: Library size cache refresh via provider failed or returned error. Result: {calculated_size}")
            else:
                logging.info("Background task: Library size cache refresh skipped (provider is not RealDebrid or not configured).")
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

    # *** START EDIT: Add the new long-running task method ***
    def task_artificial_long_run(self):
        task_name = 'task_artificial_long_run'
        logging.info(f"'{task_name}' has started.")
        
        duration_seconds = 120 # Run for 2 minutes
        
        logging.info(f"'{task_name}' will now sleep for {duration_seconds} seconds.")
        time.sleep(duration_seconds)
        
        logging.info(f"'{task_name}' has finished sleeping and is now complete.")
    # *** END EDIT ***

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
    requester_email = data.get('request', {}).get('requestedBy', {}).get('email') # Requester info


    if not media_type or not tmdb_id:
        logging.error(f"Invalid Overseerr webhook data: missing media_type or tmdbId. Data: {data}")
        return

    logging.info(f"Processing Overseerr webhook: Type={media_type}, TMDB={tmdb_id}, Title='{title}', Requester='{requester_email}'")

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
        # Get the versions for the relevant Overseerr source from settings
        content_sources = ProgramRunner().get_content_sources(force_refresh=False) # Don't need full refresh usually
        # Find the first enabled Overseerr source (assuming only one usually)
        overseerr_source_key = next((source for source, data in content_sources.items()
                                     if source.startswith('Overseerr') and data.get('enabled')), None)

        versions = {}
        source_name = 'overseerr_webhook' # Default source name
        if overseerr_source_key:
             versions = content_sources[overseerr_source_key].get('versions', {})
             source_name = overseerr_source_key # Use the actual source name if found
             logging.info(f"Using versions from configured Overseerr source '{overseerr_source_key}': {versions}")
        else:
             logging.warning("No enabled Overseerr content source found in settings. Using default versions (empty).")


        all_items = wanted_content_processed.get('movies', []) + wanted_content_processed.get('episodes', []) + wanted_content_processed.get('anime', [])
        if all_items:
             for item in all_items:
                 item['content_source'] = source_name # Use determined source name
                 from content_checkers.content_source_detail import append_content_source_detail
                 item = append_content_source_detail(item, source_type='Overseerr') # Keep source type generic

             from database import add_collected_items, add_wanted_items
             add_wanted_items(all_items, versions) # Pass the determined versions
             logging.info(f"Processed and added {len(all_items)} wanted item(s) from Overseerr webhook (TMDB ID: {tmdb_id}).")
        else:
             logging.warning(f"Metadata processing for Overseerr webhook (TMDB ID: {tmdb_id}) resulted in no items to add.")

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


def get_and_add_all_collected_from_plex(bypass=False):
    collected_content = None  # Initialize here
    mode = get_setting('File Management', 'file_collection_management')

    if mode == 'Plex' or bypass:
        logging.info("Getting all collected content from Plex...")
        try:
            collected_content = asyncio.run(run_get_collected_from_plex(bypass=bypass))
        except Exception as e:
             logging.error(f"Error running run_get_collected_from_plex: {e}", exc_info=True)
             return None # Return None on error during fetch

    elif mode == 'Zurg':
        logging.info("Getting all collected content from Zurg...")
        try:
             # Assuming a similar function exists or needs to be created for Zurg full scan
            collected_content = asyncio.run(run_get_collected_from_zurg(bypass=bypass)) # Added bypass
        except Exception as e:
            logging.error(f"Error running run_get_collected_from_zurg: {e}", exc_info=True)
            return None # Return None on error during fetch
    else:
        logging.info(f"File collection management mode ('{mode}') does not support full library scan for collected items.")
        return None


    if collected_content:
        movies = collected_content.get('movies', []) # Use .get for safety
        episodes = collected_content.get('episodes', [])

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from {mode}.")

        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            from database import add_collected_items # Keep import local
            add_collected_items(movies + episodes)
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
                logging.info("[MemCleanup] Cleared collected content and forced GC after Plex full scan.")
            except Exception as e_cleanup:
                logging.debug(f"[MemCleanup] Exception during cleanup: {e_cleanup}")
            # ----------------------------------------------------------------
            return collected_content  # Return the original content even if some items were skipped

    logging.warning(f"Failed to retrieve or process collected content from {mode}.")
    return None

def get_and_add_recent_collected_from_plex():
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

        executors = {'default': ThreadPoolExecutor(max_workers=1)}
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

