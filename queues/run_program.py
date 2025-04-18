import logging
import random
import time
import os
import sqlite3
import plexapi # Added import
# *** START EDIT ***
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import threading # For scheduler lock AND concurrent queue processing
import functools # Added for partial
import apscheduler.events # Added for listener events
# *** END EDIT ***
from queues.initialization import initialize
from utilities.settings import get_setting, get_all_settings
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss, get_wanted_from_friends_plex_rss
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist, get_wanted_from_trakt_collection, get_wanted_from_friend_trakt_watchlist
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
from utilities.rclone_processing import handle_rclone_file # Add this import
from cli_battery.app.direct_api import DirectAPI # Import DirectAPI

queue_logger = logging.getLogger('queue_logger')
program_runner = None

# Database migration check at startup
migrate_plex_removal_database()

class ProgramRunner:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ProgramRunner, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.running = False
        self.initializing = False
        self.pause_reason = None  # Track why the queue is paused
        self.connectivity_failure_time = None  # Track when connectivity failed
        self.connectivity_retry_count = 0  # Track number of retries
        self.queue_paused = False # Initialize the pause state flag
        
        # Add a queue for pending rclone paths (using deque for efficiency)
        self.pending_rclone_paths = deque() 
        
        # Configure scheduler timezone using the local timezone helper
        from metadata.metadata import _get_local_timezone
        try:
            tz = _get_local_timezone()
            logging.info(f"Initializing APScheduler with timezone: {tz.key}")
            self.scheduler = BackgroundScheduler(timezone=tz)
        except Exception as e:
            logging.error(f"Failed to get local timezone for scheduler, using system default: {e}")
            self.scheduler = BackgroundScheduler() # Fallback to default

        self.scheduler_lock = threading.Lock() # Lock for modifying scheduler jobs
        self.paused_jobs_by_queue = set() # Keep track of jobs paused by pause_queue
        
        from queues.queue_manager import QueueManager
        
        # Initialize queue manager with logging
        logging.info("Initializing QueueManager")
        self.queue_manager = QueueManager()
        
        # Verify queue initialization
        expected_queues = ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading']
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
            # --- START REVERT ---
            'Scraping': 'process_scraping', # Restore Scraping
            'Adding': 'process_adding',     # Restore Adding
            # --- END REVERT ---
            'Checking': 'process_checking',
            'Sleeping': 'process_sleeping',
            'Unreleased': 'process_unreleased',
            'Blacklisted': 'process_blacklisted',
            'Pending Uncached': 'process_pending_uncached',
            'Upgrading': 'process_upgrading'
        }
        # --- END EDIT ---

        # Base Task Intervals
        self.task_intervals = {
            # Queue Processing Tasks (intervals for individual queues are less critical now)
            'Wanted': 5,
            # --- START REVERT ---
            'Scraping': 5, # Restore Scraping interval (adjust if needed)
            'Adding': 5,   # Restore Adding interval (adjust if needed)
            # --- END REVERT ---
            'Checking': 180,
            'Sleeping': 1800,
            'Unreleased': 300,
            'Blacklisted': 7200,
            'Pending Uncached': 3600,
            'Upgrading': 3600,
            # Combined/High Frequency Tasks
            # --- START REVERT ---
            # 'task_process_scraping_adding': 10, # Remove combined task
            # --- END REVERT ---
            'task_update_queue_views': 30,     # Update queue views every 30 seconds
            'task_process_pending_rclone_paths': 10, # Check pending rclone paths every 10 seconds
            'task_send_notifications': 15,       # Run every 15 seconds
            'task_check_plex_files': 60,         # Run every 60 seconds (if enabled)
            # Periodic Maintenance/Update Tasks
            'task_check_service_connectivity': 60, # Run every 60 seconds
            'task_heartbeat': 120,               # Run every 2 minutes
            'task_update_statistics_summary': 300, # Run every 5 minutes
            'task_refresh_download_stats': 300,    # Run every 5 minutes
            'task_precompute_airing_shows': 600,   # Precompute airing shows every 10 minutes
            'task_verify_symlinked_files': 900,    # Run every 15 minutes (if enabled)
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
            'task_generate_airtime_report': 3600,  # Run every hour
            'task_run_library_maintenance': 12 * 60 * 60, # Run every twelve hours (if enabled)
            'task_get_plex_watch_history': 24 * 60 * 60,  # Run every 24 hours (if enabled)
            'task_refresh_plex_tokens': 24 * 60 * 60,   # Run every 24 hours
            'task_update_tv_show_status': 172800,       # Run every 48 hours
            # 'task_purge_not_wanted_magnets_file': 604800, # Default: 1 week (Can be added if needed)
            # 'task_local_library_scan': 900, # Default: 15 mins (Can be added if needed)
            'task_plex_full_scan': 3600, # Run every hour (Can be adjusted)
            # NEW Load Adjustment Task
            'task_adjust_intervals_for_load': 120, # Run every 2 minutes
        }
        # Store original intervals for reference (will be updated after content sources)
        self.original_task_intervals = self.task_intervals.copy()
        
        # Initialize content_sources attribute FIRST
        self.content_sources = None
        self.file_location_cache = {}  # Cache to store known file locations

        self.start_time = time.time()

        # --- START: Task Enabling Logic Reorder ---

        # 1. Initialize enabled_tasks with base/essential tasks
        self.enabled_tasks = {
            # Core Queue Processing (Individual queues are less important to enable here)
            'Wanted',
            # --- START REVERT ---
            'Scraping', # Restore Scraping
            'Adding',   # Restore Adding
            # --- END REVERT ---
            'Checking',
            'Sleeping',
            'Unreleased',
            'Blacklisted',
            'Pending Uncached',
            'Upgrading',
            # Combined/High Frequency Tasks
            # --- START REVERT ---
            # 'task_process_scraping_adding', # Remove combined task
            # --- END REVERT ---
            'task_update_queue_views',
            'task_process_pending_rclone_paths',
            'task_send_notifications',
            # Essential Periodic Tasks
            'task_check_service_connectivity',
            'task_heartbeat',
            'task_update_statistics_summary',
            'task_refresh_download_stats',
            'task_precompute_airing_shows',
            'task_reconcile_queues',
            'task_check_database_health',
            'task_sync_time',
            'task_check_trakt_early_releases',
            'task_update_show_ids',
            'task_update_show_titles',
            'task_update_movie_ids',
            'task_update_movie_titles',
            'task_refresh_release_dates',
            'task_generate_airtime_report',
            'task_refresh_plex_tokens',
            'task_update_tv_show_status',
            # NEW Load Adjustment Task
            'task_adjust_intervals_for_load',
            # --- START EDIT: Add 'task_verify_plex_removals' back to default set ---
            # 'task_plex_full_scan', # Removed from default set
            'task_verify_plex_removals' # Added back to default set
            # --- END EDIT ---
        }
        logging.info("Initialized base enabled tasks.")

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

                for task_name, enabled in saved_states.items():
                    normalized_name = self._normalize_task_name(task_name)
                    # Check if the task from the JSON file actually exists in our defined intervals
                    # --- START EDIT: Check against original_task_intervals which is more complete early on ---
                    if normalized_name not in self.original_task_intervals and normalized_name not in self.task_intervals:
                    # --- END EDIT ---
                        logging.warning(f"Task '{normalized_name}' found in task_toggles.json but not defined in task_intervals/original_task_intervals. Skipping toggle.")
                        continue

                    if enabled:
                        if normalized_name not in self.enabled_tasks:
                            self.enabled_tasks.add(normalized_name)
                            logging.info(f"Enabled task from saved settings: {normalized_name}")
                    elif not enabled:
                        if normalized_name in self.enabled_tasks:
                            self.enabled_tasks.remove(normalized_name)
                            logging.info(f"Disabled task from saved settings: {normalized_name}")
            else:
                logging.info("No task_toggles.json found, using default enabled tasks.")
        except Exception as e:
            logging.error(f"Error loading saved task toggle states: {str(e)}")

        # 3. Get Content Sources (populates intervals AND updates enabled_tasks based on source settings)
        logging.info("Populating content source intervals and updating enabled tasks based on source settings...")
        self.get_content_sources(force_refresh=True) # This populates intervals and toggles sources
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
        self.original_task_intervals = self.task_intervals.copy()
        logging.info("Finalized original task intervals after all task definitions and settings.")

        # --- END: Task Enabling Logic Reorder ---


        # Define queue processing map EARLIER
        self.queue_processing_map = {
            'Wanted': 'process_wanted',
            # --- START REVERT ---
            'Scraping': 'process_scraping', # Restore Scraping
            'Adding': 'process_adding',     # Restore Adding
            # --- END REVERT ---
            'Checking': 'process_checking',
            'Sleeping': 'process_sleeping',
            'Unreleased': 'process_unreleased',
            'Blacklisted': 'process_blacklisted',
            'Pending Uncached': 'process_pending_uncached',
            'Upgrading': 'process_upgrading'
        }

        # Log the final set of enabled tasks right before starting the scheduling process
        logging.info(f"Final enabled tasks before initial scheduling: {sorted(list(self.enabled_tasks))}")

        # Schedule initial tasks
        self._schedule_initial_tasks()

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
                logging.error(f"Content source data not found for source ID '{source_id}' derived from task '{task_name}'")

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
        logging.debug(f"Attempting to schedule task: '{task_name}' with interval {interval_seconds}s")

        with self.scheduler_lock:
            job_id = task_name # Use task name as job ID

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
                    wrapped_func = functools.partial(self._run_and_measure_task, target_func, args, kwargs)

                    trigger = IntervalTrigger(seconds=interval_seconds)
                    self.scheduler.add_job(
                        func=wrapped_func,
                        trigger=trigger,
                        id=job_id,
                        name=job_id,
                        replace_existing=True,
                        misfire_grace_time=max(60, interval_seconds // 4)
                    )
                    logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds} seconds (wrapped for duration measurement).")
                    return True
                except Exception as e:
                    logging.error(f"Error scheduling task '{job_id}': {e}", exc_info=True)
                    return False
            else:
                 logging.error(f"Failed to determine target function for task '{task_name}'. Cannot schedule.")
                 return False
    # *** END EDIT ***


    def _schedule_initial_tasks(self):
        """Schedules all enabled tasks based on initial configuration."""
        logging.info("Scheduling initial tasks...")
        scheduled_count = 0
        failed_count = 0
        for task_name in self.enabled_tasks:
            interval = self.task_intervals.get(task_name)
            if interval is not None:
                if self._schedule_task(task_name, interval, initial_run=True):
                    scheduled_count += 1
                else:
                    failed_count += 1
            else:
                logging.warning(f"Task '{task_name}' is enabled but has no interval defined in task_intervals. Skipping scheduling.")
                failed_count += 1
        logging.info(f"Initial task scheduling complete. Scheduled: {scheduled_count}, Failed/Skipped: {failed_count}")


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
        if self.running:
            if random_number < 100:
                logging.info("Program running...")
            else:
                logging.info("Program running...is your fridge?")

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
                'My Friends Trakt Watchlist': 900
            }
            
            log_intervals_message = ["Content source intervals:"] # Prepare log message
            
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
        
    def task_check_service_connectivity(self):
        """Check connectivity to required services"""
        from routes.program_operation_routes import check_service_connectivity
        connectivity_ok, failed_services = check_service_connectivity()
        if connectivity_ok:
            logging.debug("Service connectivity check passed")
        else:
            logging.error("Service connectivity check failed")
            self.handle_connectivity_failure(failed_services)

    def handle_connectivity_failure(self, failed_services=None):
        from routes.program_operation_routes import stop_program, check_service_connectivity
        from routes.extensions import app  # Import the Flask app

        # Create a descriptive message about which services failed
        if failed_services and len(failed_services) > 0:
            failed_services_str = ", ".join(failed_services)
            reason = f"Connectivity failure - {failed_services_str} unavailable"
        else:
            reason = "Connectivity failure - waiting for services to be available"
            
        logging.warning(f"Pausing program queue due to connectivity failure: {reason}")
        self.pause_reason = reason
        self.pause_queue()
        
        # Set the initial failure time if not already set
        if not self.connectivity_failure_time:
            self.connectivity_failure_time = time.time()
            self.connectivity_retry_count = 0

    def check_connectivity_status(self):
        """Check connectivity status during normal program cycle"""
        from routes.program_operation_routes import stop_program, check_service_connectivity
        from routes.extensions import app

        if not self.connectivity_failure_time:
            return
            
        # Check if we should retry connectivity check
        time_since_failure = time.time() - self.connectivity_failure_time
        if time_since_failure >= 60 * (self.connectivity_retry_count + 1):
            self.connectivity_retry_count += 1
            
            try:
                connectivity_ok, failed_services = check_service_connectivity()
                if connectivity_ok:
                    logging.info("Service connectivity restored")
                    self.connectivity_failure_time = None
                    self.connectivity_retry_count = 0
                    self.resume_queue()
                    return
            except Exception as e:
                logging.error(f"Error checking service connectivity: {str(e)}")
                
            logging.warning(f"Service connectivity check failed. Retry {self.connectivity_retry_count}/5")
            if failed_services and len(failed_services) > 0:
                failed_services_str = ", ".join(failed_services)
                self.pause_reason = f"Connectivity failure - {failed_services_str} unavailable (Retry {self.connectivity_retry_count}/5)"
            else:
                self.pause_reason = f"Connectivity failure - waiting for services to be available (Retry {self.connectivity_retry_count}/5)"

            # After 5 minutes (5 retries), stop the program
            if self.connectivity_retry_count >= 5:
                logging.error("Service connectivity not restored after 5 minutes. Stopping the program.")
                with app.app_context():
                    stop_result = stop_program()
                    logging.info(f"Program stop result: {stop_result}")
                self.connectivity_failure_time = None
                self.connectivity_retry_count = 0

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

            for job in all_jobs:
                job_id = job.id
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
        QueueManager().pause_queue(reason=self.pause_reason) # Keep for status reporting

        self.queue_paused = True # Keep internal flag if used elsewhere
        # Updated log to reflect pausing *all* running jobs
        logging.info(f"Queue paused. Attempted to pause all running jobs, newly paused: {paused_count}. Tracked paused jobs: {len(self.paused_jobs_by_queue)}. Reason: {self.pause_reason}")
        # *** END EDIT ***

    def resume_queue(self):
        # *** START EDIT: Resume logic remains the same, but update log context ***
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
        QueueManager().resume_queue() # Keep for status reporting

        self.queue_paused = False # Keep internal flag
        self.pause_reason = None  # Clear pause reason on resume
        # Updated log to reflect resuming *all* tracked jobs
        logging.info(f"Queue resumed. Attempted to resume {len(jobs_to_resume)} tracked jobs, successfully resumed: {resumed_count}.")
        # *** END EDIT ***

    def task_plex_full_scan(self):
        get_and_add_all_collected_from_plex()
        # Add reconciliation call after full scan processing
        logging.info("Triggering queue reconciliation after full Plex scan.")
        self.task_reconcile_queues()
        
    def process_content_source(self, source, data):
        source_type = source.split('_')[0]
        versions = data.get('versions', {})
        source_media_type = data.get('media_type', 'All')

        logging.debug(f"Processing content source: {source} (type: {source_type}, media_type: {source_media_type})")

        try:
            # Load cache for this source
            source_cache = load_source_cache(source)
            logging.debug(f"Initial cache state for {source}: {len(source_cache)} entries")
            cache_skipped = 0
            items_processed = 0
            total_items = 0
            media_type_skipped = 0

            wanted_content = []
            if source_type == 'Overseerr':
                wanted_content = get_wanted_from_overseerr(versions)
            elif source_type == 'MDBList':
                mdblist_urls = data.get('urls', '').split(',')
                for mdblist_url in mdblist_urls:
                    mdblist_url = mdblist_url.strip()
                    wanted_content.extend(get_wanted_from_mdblists(mdblist_url, versions))
            elif source_type == 'Trakt Watchlist':
                try:
                    wanted_content = get_wanted_from_trakt_watchlist(versions)
                except (ValueError, api.exceptions.RequestException) as e:
                    logging.error(f"Failed to fetch Trakt watchlist: {str(e)}")
                    return
            elif source_type == 'Trakt Lists':
                trakt_lists = data.get('trakt_lists', '').split(',')
                for trakt_list in trakt_lists:
                    trakt_list = trakt_list.strip()
                    try:
                        wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions))
                    except (ValueError, api.exceptions.RequestException) as e:
                        logging.error(f"Failed to fetch Trakt list {trakt_list}: {str(e)}")
                        continue
            elif source_type == 'Trakt Collection':
                wanted_content = get_wanted_from_trakt_collection(versions)
            elif source_type == 'Friends Trakt Watchlist':
                wanted_content = get_wanted_from_friend_trakt_watchlist(data, versions)
            elif source_type == 'Collected':
                wanted_content = get_wanted_from_collected()
            elif source_type == 'My Plex Watchlist':
                from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist
                wanted_content = get_wanted_from_plex_watchlist(versions)
            elif source_type == 'My Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions)
            elif source_type == 'My Friends Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_friends_plex_rss(plex_rss_url, versions)
            elif source_type == 'Other Plex Watchlist':
                # Import the function here
                from content_checkers.plex_watchlist import get_wanted_from_other_plex_watchlist
                
                other_watchlists = []
                for source_id, source_data in self.get_content_sources().items():
                    if source_id.startswith('Other Plex Watchlist_') and source_data.get('enabled', False):
                        other_watchlists.append({
                            'username': source_data.get('username', ''),
                            'token': source_data.get('token', ''),
                            'versions': source_data.get('versions', versions)
                        })
                
                for watchlist in other_watchlists:
                    if watchlist['username'] and watchlist['token']:
                        try:
                            watchlist_content = get_wanted_from_other_plex_watchlist(
                                username=watchlist['username'],
                                token=watchlist['token'],
                                versions=watchlist['versions']
                            )
                            wanted_content.extend(watchlist_content)
                        except Exception as e:
                            logging.error(f"Failed to fetch Other Plex watchlist for {watchlist['username']}: {str(e)}")
                            continue
            else:
                logging.warning(f"Unknown source type: {source_type}")
                return

            if wanted_content:
                if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                    # Handle list of tuples (e.g., from Plex sources)
                    for items, item_versions in wanted_content:
                        logging.debug(f"Processing batch of {len(items)} items from {source}")
                        
                        # Filter items by media type first
                        if source_media_type != 'All' and not source_type.startswith('Collected'):
                            items = [
                                item for item in items
                                if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                                   (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                            ]
                            media_type_skipped += len(items) - len(items)
                        
                        # Then filter items based on cache
                        items_to_process = [
                            item for item in items 
                            if should_process_item(item, source, source_cache)
                        ]
                        items_skipped = len(items) - len(items_to_process)
                        cache_skipped += items_skipped
                        
                        if items_to_process:
                            from metadata.metadata import process_metadata
                            processed_items = process_metadata(items_to_process)
                            if processed_items:
                                all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                                
                                # Set content source and detail for each item
                                for item in all_items:
                                    item['content_source'] = source
                                    item = append_content_source_detail(item, source_type=source_type)
                                
                                # Update cache for the original items (pre-metadata processing)
                                for item in items_to_process:
                                    update_cache_for_item(item, source, source_cache)

                                from database import add_collected_items, add_wanted_items
                                add_wanted_items(all_items, item_versions or versions)
                                total_items += len(all_items)
                                items_processed += len(items_to_process)
                else:
                    # Handle single list of items
                    logging.debug(f"Processing batch of {len(wanted_content)} items from {source}")
                    
                    # Filter items by media type first
                    if source_media_type != 'All' and not source_type.startswith('Collected'):
                        wanted_content = [
                            item for item in wanted_content
                            if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                               (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                        ]
                        media_type_skipped += len(wanted_content) - len(wanted_content)
                    
                    # Then filter items based on cache
                    items_to_process = [
                        item for item in wanted_content 
                        if should_process_item(item, source, source_cache)
                    ]
                    items_skipped = len(wanted_content) - len(items_to_process)
                    cache_skipped += items_skipped
                    
                    if items_to_process:
                        processed_items = process_metadata(items_to_process)
                        if processed_items:
                            all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                            
                            # Set content source and detail for each item
                            for item in all_items:
                                item['content_source'] = source
                                item = append_content_source_detail(item, source_type=source_type)
                            
                            # Update cache for the original items (pre-metadata processing)
                            for item in items_to_process:
                                update_cache_for_item(item, source, source_cache)

                            from database import add_collected_items, add_wanted_items
                            add_wanted_items(all_items, versions)
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

    def task_generate_airtime_report(self):
        generate_airtime_report()

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
        self.initializing = True
        logging.info("Running initialization...")
        skip_initial_plex_update = get_setting('Debug', 'skip_initial_plex_update', False)
        
        disable_initialization = get_setting('Debug', 'disable_initialization', '')
        if not disable_initialization:
            initialize(skip_initial_plex_update)
            logging.info("Initialization complete")
        else:
            logging.info("Initialization disabled, skipping...")
        
        self.initializing = False

    def start(self):
        if not self.running:
            self.running = True
            # *** START EDIT: Start Scheduler ***
            try:
                 logging.info("Starting APScheduler...")
                 self.scheduler.start(paused=False) # Start scheduler, ensure it's not paused initially
                 logging.info("APScheduler started.")
            except Exception as e:
                 logging.error(f"Failed to start APScheduler: {e}", exc_info=True)
                 self.running = False # Indicate startup failure
                 return # Don't proceed if scheduler fails
            # *** END EDIT ***
            self.run()

    def stop(self):
        logging.warning("Program stop requested")
        self.running = False
        self.initializing = False
        # *** START EDIT: Shutdown Scheduler ***
        try:
            if self.scheduler and self.scheduler.running:
                 logging.info("Shutting down APScheduler...")
                 # wait=False allows the stop command to return faster,
                 # but background jobs might still be finishing.
                 # Set wait=True for a cleaner shutdown if blocking is acceptable.
                 self.scheduler.shutdown(wait=False)
                 logging.info("APScheduler shut down.")
        except Exception as e:
            logging.error(f"Error shutting down APScheduler: {e}", exc_info=True)
        # *** END EDIT ***

    def is_running(self):
        # *** EDIT: Check scheduler state as well ***
        return self.running and self.scheduler and self.scheduler.running
        # *** END EDIT ***

    def is_initializing(self):  # Add this method
        return self.initializing

    def run(self):
        try:
            logging.info("Starting program run loop (monitoring scheduler state)")
            self.running = True  # Make sure running flag is set

            self.run_initialization()

            # *** START EDIT: Simplified run loop ***
            # The main loop now just keeps the script alive while the scheduler runs.
            # We can add checks here if needed (e.g., monitoring scheduler health).
            while self.running:
                try:
                    # Check scheduler status periodically
                    if not self.scheduler or not self.scheduler.running:
                         logging.error("APScheduler is not running. Stopping program.")
                         self.stop() # Trigger stop if scheduler died
                         break
            
                    # Perform checks that still need to run outside scheduled tasks
                    # e.g., connectivity checks that might pause/resume scheduler jobs
                    self.check_connectivity_status()

                    # Check scheduled pause (pauses/resumes scheduler jobs)
                    is_scheduled_pause = self._is_within_pause_schedule()
                    # Use the internal self.queue_paused flag which is set by pause_queue/resume_queue
                    if is_scheduled_pause and not self.queue_paused:
                        pause_start = get_setting('Queue', 'pause_start_time', '00:00')
                        pause_end = get_setting('Queue', 'pause_end_time', '00:00')
                        new_reason = f"Scheduled pause active ({pause_start} - {pause_end})"
                        # Check if reason needs update (or if already paused for another reason)
                        if self.pause_reason != new_reason:
                             self.pause_reason = new_reason
                             self.pause_queue() # Pauses scheduler jobs
                             logging.info(f"Queue automatically paused due to schedule: {self.pause_reason}")
                    elif not is_scheduled_pause and self.queue_paused and self.pause_reason and "Scheduled pause active" in self.pause_reason:
                        logging.info("Scheduled pause period ended. Resuming queue.")
                        self.resume_queue() # Resumes scheduler jobs

                    # Main loop sleep
                    time.sleep(5) # Check status every 5 seconds

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
            'task_precompute_airing_shows', 'task_process_pending_rclone_paths',
            'task_update_tv_show_status'
        }
        for task, interval in self.original_task_intervals.items():
             if task.endswith('_wanted') and interval > 900:
                 slowdown_candidates.add(task)

        idle_increase_seconds = 300
        # DELAY_THRESHOLD = 3 # Remove delay threshold

        # --- Determine idle state based on Scraping/Adding queues ---
        system_is_idle = False
        if hasattr(self, 'queue_manager') and self.queue_manager:
            scraping_queue = self.queue_manager.queues.get('Scraping')
            adding_queue = self.queue_manager.queues.get('Adding')
            if scraping_queue and adding_queue:
                try:
                    # Use get_contents with limit 1 for efficiency
                    scraping_empty = len(scraping_queue.get_contents()) == 0
                    adding_empty = len(adding_queue.get_contents()) == 0
                    system_is_idle = scraping_empty and adding_empty
                except Exception as e:
                    logging.error(f"Error checking Scraping/Adding queue state for idle check: {e}")
                    # Default to not idle on error
                    system_is_idle = False
            else:
                logging.warning("Scraping or Adding queue not found for idle check.")
                system_is_idle = False # Assume not idle if queues are missing
        else:
             logging.warning("Queue manager not available for idle check.")
             system_is_idle = False # Assume not idle if manager is missing

        # --- End Determine idle state ---

        with self.scheduler_lock:
            if system_is_idle:
                if not hasattr(self, '_last_idle_adjustment_log') or current_time - self._last_idle_adjustment_log >= 600:
                     # Updated log message
                     logging.info(f"System idle (Scraping/Adding queues empty) - increasing non-critical task intervals by {idle_increase_seconds}s.")
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
                 active_reason = []
                 # Update active reason based on new check
                 if not system_is_idle: active_reason.append("Scraping or Adding queue has items")
                 # Remove delayed task check from reason
                 # if delayed_tasks_count >= DELAY_THRESHOLD: active_reason.append(f"{delayed_tasks_count} potentially delayed tasks >= threshold {DELAY_THRESHOLD}")

                 # Logging logic (same as before)
                 # ...
                 log_now = False # Determine if logging is needed
                 if not hasattr(self, '_last_active_state_log'):
                      self._last_active_state_log = 0
                      self._was_idle_last_check = True # Assume initially idle so first active state logs

                 if not self._was_idle_last_check: # Only log transition to active or periodically
                      if current_time - self._last_active_state_log >= 600:
                           log_now = True
                 else: # Was idle, now active
                      log_now = True


                 if log_now:
                      # Updated log message
                      logging.info(f"System active ({', '.join(active_reason)}) - ensuring default task intervals.")
                      self._last_active_state_log = current_time

                 needs_reset = False
                 tasks_to_reset = []
                 for task_id in slowdown_candidates:
                      job = self.scheduler.get_job(task_id)
                      original_interval = self.original_task_intervals.get(task_id)
                      if job and original_interval:
                           current_job_interval = job.trigger.interval.total_seconds()
                           if current_job_interval != original_interval:
                                needs_reset = True
                                tasks_to_reset.append(task_id)

                 if needs_reset:
                      logging.info(f"Resetting intervals for {len(tasks_to_reset)} tasks to default values.")
                      for task_id in tasks_to_reset:
                           original_interval = self.original_task_intervals.get(task_id)
                           if original_interval:
                                try:
                                    self.scheduler.modify_job(task_id, trigger=IntervalTrigger(seconds=original_interval))
                                    logging.debug(f"Reset interval for '{task_id}' to {original_interval}s")
                                except Exception as e:
                                    logging.error(f"Error resetting job '{task_id}' interval to {original_interval}s: {e}")


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
        
        if notifications_file.exists():
            try:
                # Use a temporary file to read and then delete original safely
                temp_notifications_file = notifications_file.with_suffix(".pkl.tmp")
                notifications = []
                try:
                    # Ensure the temp file doesn't exist before renaming
                    if temp_notifications_file.exists():
                        try:
                            temp_notifications_file.unlink()
                        except OSError as e_unlink:
                            logging.warning(f"Could not remove pre-existing temp notifications file {temp_notifications_file}: {e_unlink}")
                            # Decide if we should proceed or return. Returning might be safer.
                            return 

                    # Move/rename the file first
                    notifications_file.rename(temp_notifications_file)
                    with open(temp_notifications_file, "rb") as f:
                        notifications = pickle.load(f)
                        # Delete the temp file after successful read
                        temp_notifications_file.unlink()
                except FileNotFoundError:
                    # If the original file disappeared between exists() and rename()
                    logging.debug("Notifications file disappeared before processing.")
                    return
                except (pickle.UnpicklingError, EOFError) as pe:
                    logging.error(f"Error reading notifications pickle file: {pe}. Discarding file.")
                    try: temp_notifications_file.unlink() # Attempt removal of corrupt file
                    except OSError: pass
                    return # Don't proceed with empty/corrupt data
                except Exception as e_read:
                    logging.error(f"Error handling notifications file read/rename: {e_read}", exc_info=True)
                    # Attempt to put the file back? Or just leave the temp? Leaving temp might be safer.
                    return # Avoid processing potentially partial data
                
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

                # else: # No notifications loaded, log removed for less noise
                    # logging.debug("No notifications to send")

            except Exception as e:
                logging.error(f"Error processing notifications task: {str(e)}", exc_info=True)
        # else: # File doesn't exist, log removed for less noise
            # logging.debug("No notifications file found")

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
        """Task to reconcile items in Checking state with matching filled_by_file items"""
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
        deleted_count = 0
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            # Find pairs of (Checking item, Matching non-Checking item) with the same filled_by_file
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
            items_to_delete = set() # Use set to avoid duplicates

            for pair in reconciliation_pairs:
                # Log the reconciliation
                reconciliation_logger.info(
                    f"Reconciliation Found based on shared file: '{pair['filled_by_file']}'\n"
                    f"  - Keeping (was Checking): ID={pair['checking_id']}, Title='{pair['checking_title']}', Type={pair['checking_type']}, File={pair['filled_by_file']}\n"
                    f"  - Deleting (Matching):    ID={pair['matching_id']}, Title='{pair['matching_title']}', State={pair['matching_state']}, Type={pair['matching_type']}, File={pair['filled_by_file']}"
                )
                items_to_update.append(pair['checking_id'])
                items_to_delete.add(pair['matching_id'])

            if items_to_update:
                # Bulk update Checking items to Collected state
                update_sql = f"UPDATE media_items SET state = 'Collected', collected_at = ? WHERE id IN ({','.join(['?']*len(items_to_update))})"
                params = [now_str] + items_to_update
                cursor.execute(update_sql, params)
                reconciled_count = cursor.rowcount
                # Add criteria to update log
                reconciliation_logger.info(f"Updated {reconciled_count} 'Checking' items to 'Collected' state due to matching file paths. IDs: {items_to_update}")

            if items_to_delete:
                # Bulk delete the matching items
                # Ensure items being updated aren't accidentally deleted if IDs overlap somehow (unlikely)
                delete_ids = list(items_to_delete - set(items_to_update))
                if delete_ids:
                    delete_sql = f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(delete_ids))})"
                    cursor.execute(delete_sql, delete_ids)
                    deleted_count = cursor.rowcount
                     # Add criteria to delete log
                    reconciliation_logger.info(f"Deleted {deleted_count} duplicate items (non-Checking state) that shared a file path with reconciled items. IDs: {delete_ids}")

            conn.commit()
            if reconciled_count > 0 or deleted_count > 0:
                 # Make this log more informative
                 logging.info(f"Queue reconciliation completed: {reconciled_count} items updated to 'Collected', {deleted_count} duplicate items deleted based on shared file paths.")
            else:
                 logging.debug("Queue reconciliation found no items needing reconciliation.")

        except sqlite3.Error as e:
            logging.error(f"Database error during queue reconciliation: {str(e)}")
            conn.rollback() # Rollback on error
        finally:
            conn.close() # Ensure connection is closed


    def reinitialize(self):
        """Force reinitialization of the program runner to pick up new settings"""
        logging.info("Reinitializing ProgramRunner...")
        # Need to shutdown and restart scheduler carefully
        with self.scheduler_lock:
            if self.scheduler and self.scheduler.running:
                logging.info("Shutting down scheduler for reinitialization...")
                self.scheduler.shutdown(wait=True) # Wait for jobs to finish if possible
                logging.info("Scheduler stopped.")

        self._initialized = False
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

            paused_count = 0
            for job_id in debrid_related_ids:
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
        self.pause_reason = f"Debrid Rate Limit - Resuming tasks around {resume_time.strftime('%H:%M:%S')}"
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
        if self.pause_reason and "Rate Limit" in self.pause_reason:
             self.pause_reason = None
             self.queue_paused = False
             # Optionally resume the entire queue manager status reporting
             # from queues.queue_manager import QueueManager
             # QueueManager().resume_queue()


    def task_local_library_scan(self):
        """Run local library scan for symlinked files."""
        logging.info("Disabled for now")
        return
        if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            from database import get_all_media_items
            from utilities.local_library_scan import local_library_scan
            
            # Get all items in Checking state
            items = get_all_media_items(state="Checking")
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
            # *** START EDIT ***
            get_cached_download_stats() # Removed unsupported force_refresh=True argument
            # *** END EDIT ***
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
            items = cursor.execute('SELECT id, title, filled_by_title, filled_by_file, type, imdb_id, tmdb_id, season_number, episode_number, year, version FROM media_items WHERE state = "Checking"').fetchall()
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

                if not filled_by_title or not filled_by_file:
                    logging.debug(f"Item {item_id} missing filled_by_title or filled_by_file. Skipping.")
                    continue

                # Construct potential paths
                file_path = os.path.join(plex_file_location, filled_by_title, filled_by_file)
                # Handle cases where filled_by_title might have an extension (less common now?)
                title_without_ext = os.path.splitext(filled_by_title)[0]
                file_path_no_ext = os.path.join(plex_file_location, title_without_ext, filled_by_file)
                # Add check for file directly under plex_file_location (less common)
                file_path_direct = os.path.join(plex_file_location, filled_by_file) # Direct path


                # Check if we've already found this file before (cache)
                cache_key = f"{filled_by_title}:{filled_by_file}"
                # --- START EDIT: Remove early cache skip - needs tick logic first ---
                # if self.file_location_cache.get(cache_key) == 'exists':
                #     logging.debug(f"Skipping previously verified file via cache: {filled_by_title}/{filled_by_file}")
                #     continue
                # --- END EDIT ---


                file_found_on_disk = False
                actual_file_path = None
                if os.path.exists(file_path):
                    file_found_on_disk = True
                    actual_file_path = file_path
                elif os.path.exists(file_path_no_ext):
                    file_found_on_disk = True
                    actual_file_path = file_path_no_ext
                # Add check for file directly under plex_file_location (less common)
                elif os.path.exists(file_path_direct):
                     file_found_on_disk = True
                     actual_file_path = file_path_direct


                if file_found_on_disk:
                    logging.info(f"Found file on disk: {actual_file_path} for item {item_id}")
                    self.file_location_cache[cache_key] = 'exists' # Update cache

                    # --- START EDIT: Add Tick Check and Scan Path Gathering ---
                    should_trigger_scan = False
                    current_tick = self.plex_scan_tick_counts.get(cache_key, 0) + 1
                    self.plex_scan_tick_counts[cache_key] = current_tick
                    if current_tick <= 5:
                        should_trigger_scan = True
                        logging.info(f"File '{filled_by_file}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                    else:
                        logging.debug(f"File '{filled_by_file}' found (tick {current_tick}). Skipping Plex scan trigger (only triggers for first 5 ticks).")

                    if should_trigger_scan and plex and sections_map: # Check connection exists
                        item_type_mapped = 'show' if item_dict['type'] == 'episode' else item_dict['type']
                        logging.debug(f"Identifying scan paths for item {item_id} (type: {item_type_mapped}, title: '{filled_by_title}')")
                        found_matching_section_location = False
                        for section in sections:
                            if section.type != item_type_mapped:
                                continue
                            logging.debug(f"  Checking Section '{section.title}' (Type: {section.type})")
                            for location in section.locations:
                                constructed_plex_path = os.path.join(location, filled_by_title)
                                logging.debug(f"    Considering scan path: '{constructed_plex_path}' based on location '{location}'")
                                if section.title not in paths_to_scan_by_section:
                                    paths_to_scan_by_section[section.title] = set()
                                paths_to_scan_by_section[section.title].add(constructed_plex_path)
                                found_matching_section_location = True
                        if not found_matching_section_location:
                            logging.warning(f"Could not find any matching Plex library section (type: {item_type_mapped}) for item {item_id} based on file '{filled_by_file}'. Scan might not be triggered correctly.")
                    # --- END EDIT ---
                            
                    updated_items += 1 # Count item as 'updated' if found

                    # Update item state to Collected if found
                    conn_update = None
                    try:
                        conn_update = get_db_connection()
                        cursor_update = conn_update.cursor()
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        # --- START EDIT: Remove redundant first update ---
                        # cursor_update.execute('UPDATE media_items SET state = "Collected", collected_at = ?, filled_by_file = ?, filled_by_title = ? WHERE id = ? AND state = "Checking"',
                        #                       (now, os.path.basename(actual_file_path), os.path.basename(os.path.dirname(actual_file_path)), item_id)) # Update file/title too? Maybe safer not to here. Use original filled_by.
                        # Let's stick to only updating state and time here for safety
                        # --- END EDIT ---
                        cursor_update.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking"',
                                              (now, item_id))

                        if cursor_update.rowcount > 0: # Check if the update actually happened
                            conn_update.commit()
                            item_title_log = item_dict['title'] if item_dict['title'] else 'N/A'
                            logging.info(f"Updated item {item_id} ({item_title_log}) to Collected state.")

                            # Add post-processing call after state update
                            # Fetch updated details AFTER commit
                            updated_item_details = get_media_item_by_id(item_id)
                            if updated_item_details:
                                handle_state_change(dict(updated_item_details)) # Pass as dict

                            # Send notification for collected item
                            try:
                                from routes.notifications import send_notifications # Keep import local
                                from routes.settings_routes import get_enabled_notifications_for_category # Keep import local
                                from routes.extensions import app # Keep import local

                                with app.app_context(): # Ensure Flask app context
                                    # Fetch enabled notifications (simplified call)
                                    enabled_notifications = get_enabled_notifications_for_category('collected').get_json().get('enabled_notifications', {})

                                    if enabled_notifications and updated_item_details:
                                        # Construct notification data from fetched details
                                        notification_data = {
                                            'id': updated_item_details['id'],
                                            'title': updated_item_details.get('title', 'Unknown Title'),
                                            'type': updated_item_details.get('type', 'unknown'),
                                            'year': updated_item_details.get('year', ''),
                                            'version': updated_item_details.get('version', ''),
                                            'season_number': updated_item_details.get('season_number'),
                                            'episode_number': updated_item_details.get('episode_number'),
                                            'new_state': 'Collected'
                                        }
                                        send_notifications([notification_data], enabled_notifications, notification_category='collected')
                            except Exception as e_notify:
                                logging.error(f"Failed to send collected notification for item {item_id}: {str(e_notify)}", exc_info=True)
                        else:
                             # Log if the item wasn't updated (e.g., state changed concurrently)
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
                    logging.debug(f"File not found on disk for item {item_id} in primary locations:\n  {file_path}\n  {file_path_no_ext}\n  {file_path_direct}")
                    # --- START EDIT: Reset tick count if file missing ---
                    if cache_key in self.plex_scan_tick_counts:
                        logging.debug(f"Resetting Plex scan tick count for missing file '{filled_by_file}'.")
                        del self.plex_scan_tick_counts[cache_key]
                    # --- END EDIT ---
                    # Optional: Clear cache if file is confirmed missing? Might cause re-checks later if transient.
                    # if cache_key in self.file_location_cache:
                    #     del self.file_location_cache[cache_key]

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
                filled_by_file = item_dict['filled_by_file']

                if not filled_by_title or not filled_by_file:
                    logging.debug(f"Item {item_id} missing filled_by_title or filled_by_file. Skipping Plex scan trigger check.")
                    continue

                # Generate cache key and potential file paths (remains the same)
                cache_key = f"{filled_by_title}:{filled_by_file}"
                file_path = os.path.join(plex_file_location, filled_by_title, filled_by_file)
                title_without_ext = os.path.splitext(filled_by_title)[0]
                file_path_no_ext = os.path.join(plex_file_location, title_without_ext, filled_by_file)
                file_path_direct = os.path.join(plex_file_location, filled_by_file) # Direct path

                # Check if the file exists on disk (remains the same)
                file_found_on_disk = False
                actual_file_path = None
                if os.path.exists(file_path):
                    file_found_on_disk = True
                    actual_file_path = file_path
                elif os.path.exists(file_path_no_ext):
                    file_found_on_disk = True
                    actual_file_path = file_path_no_ext
                elif os.path.exists(file_path_direct):
                    file_found_on_disk = True
                    actual_file_path = file_path_direct

                should_trigger_scan = False
                if file_found_on_disk:
                    # File exists, update cache and handle tick count (remains the same)
                    logging.debug(f"Confirmed file exists on disk: {actual_file_path} for item {item_id}")
                    self.file_location_cache[cache_key] = 'exists'
                    current_tick = self.plex_scan_tick_counts.get(cache_key, 0) + 1
                    self.plex_scan_tick_counts[cache_key] = current_tick
                    if current_tick <= 5:
                        should_trigger_scan = True
                        updated_items += 1 # Count item here when scan is intended
                        logging.info(f"File '{filled_by_file}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                    else:
                        logging.debug(f"File '{filled_by_file}' found (tick {current_tick}). Skipping Plex scan trigger (only triggers for first 5 ticks).")
                else:
                    # File not found (remains the same)
                    not_found_items += 1
                    logging.debug(f"File not found on disk for item {item_id} in primary locations:\n  {file_path}\n  {file_path_no_ext}\n  {file_path_direct}")
                    if cache_key in self.plex_scan_tick_counts:
                        logging.debug(f"Resetting Plex scan tick count for missing file '{filled_by_file}'.")
                        del self.plex_scan_tick_counts[cache_key]
                    # --- START EDIT: Need to continue loop if file not found ---
                    continue 
                    # --- END EDIT ---

                # --- START: Logic to identify scan paths (original location) ---
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
                            # Assumes the item folder name is `filled_by_title`
                            constructed_plex_path = os.path.join(location, filled_by_title)
                            logging.debug(f"    Considering scan path: '{constructed_plex_path}' based on location '{location}'")

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
        """Manually trigger a task to run immediately by running its job now."""
        normalized_name = self._normalize_task_name(task_name) # Use existing normalization
        job_id = normalized_name # Job ID should match normalized name

        logging.info(f"Attempting to manually trigger task: {job_id}")

        # --- Resolve target function using helper ---
        target_func, args, kwargs = self._get_task_target(job_id)
        # ------------------------------------------

        if target_func:
            logging.info(f"Executing task '{job_id}' manually...")
            try:
                # Execute directly in the current thread (API request thread)
                target_func(*args, **kwargs)
                logging.info(f"Manual trigger for task '{job_id}' completed.")
                return True # Indicate success
            except Exception as e:
                 logging.error(f"Error during manual execution of task '{job_id}': {e}", exc_info=True)
                 # Raise a more specific error or wrap the original
                 raise RuntimeError(f"Error executing task {job_id} manually: {e}") from e
        else:
            logging.error(f"Could not determine target function for manual trigger of '{job_id}'")
            # Check scheduler for job existence to provide better error
            with self.scheduler_lock:
                 job = self.scheduler.get_job(job_id)
                 if job_id not in self.task_intervals:
                      raise ValueError(f"Task '{job_id}' is not defined.")
                 elif not job:
                      raise ValueError(f"Task '{job_id}' exists but is not currently scheduled (likely disabled). Cannot trigger.")
                 else:
                      # Should not happen if target_func is None but job exists and is defined
                      raise ValueError(f"Task function for '{job_id}' not found despite job existing.")

    def enable_task(self, task_name):
        """Enable a task by adding/resuming its job in the scheduler."""
        normalized_name = self._normalize_task_name(task_name)
        job_id = normalized_name

        if normalized_name not in self.task_intervals:
             logging.error(f"Cannot enable task '{normalized_name}': No interval defined.")
             return False # Return False for failure

        with self.scheduler_lock:
            job = self.scheduler.get_job(job_id)
            if job:
                 if job.next_run_time is not None: # Job exists and is scheduled (not paused indefinitely)
                     logging.info(f"Task '{normalized_name}' is already scheduled and enabled.")
                     # Ensure it's in our enabled_tasks set
                     if normalized_name not in self.enabled_tasks: self.enabled_tasks.add(normalized_name)
                     return True
                 else: # Job exists but is paused
                     try:
                         self.scheduler.resume_job(job_id)
                         self.enabled_tasks.add(normalized_name) # Add to set
                         # Remove from manual pause set if it was there
                         if job_id in self.paused_jobs_by_queue: self.paused_jobs_by_queue.remove(job_id)
                         logging.info(f"Resumed existing paused job for task: {normalized_name}")
                         return True
                     except Exception as e:
                         logging.error(f"Error resuming job '{job_id}': {e}")
                         return False
            else: # Job doesn't exist, need to add it
                 interval = self.task_intervals.get(normalized_name)
                 if interval:
                     if self._schedule_task(normalized_name, interval): # Use the schedule method
                         self.enabled_tasks.add(normalized_name) # Add to set
                         logging.info(f"Scheduled and enabled new task: {normalized_name}")
                         return True
                     else:
                         logging.error(f"Failed to schedule new job for task: {normalized_name}")
                         return False
                 else:
                     # This case should be caught by the initial check
                     logging.error(f"Interval not found for task '{normalized_name}' during enable.")
                     return False

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

    def task_update_statistics_summary(self):
        """Update the statistics summary table for faster statistics page loading"""
        try:
            # Use the directly imported function with force=True
            from database.statistics import update_statistics_summary
            update_statistics_summary(force=True)
            logging.debug("Scheduled statistics summary update complete")
        except Exception as e:
            logging.error(f"Error updating statistics summary: {str(e)}")

    def task_check_database_health(self):
        """Periodic task to verify database health and handle any corruption."""
        from main import verify_database_health

        try:
            if not verify_database_health():
                logging.error("Database health check failed during periodic check")
                # Pause the queue if database is corrupted
                self.pause_reason = "Database corruption detected - check logs for details"
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
                max_files=50,
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

    # Method to add path received from webhook
    def add_pending_rclone_path(self, path: str):
        """Adds a path received from the rclone webhook to the pending queue."""
        if path not in self.pending_rclone_paths:
            self.pending_rclone_paths.append(path)
            logging.info(f"Added '{path}' to pending rclone queue. Size: {len(self.pending_rclone_paths)}")
        else:
            logging.debug(f"Path '{path}' is already in the pending rclone queue.")

    # New task to process paths from the pending queue
    def task_process_pending_rclone_paths(self):
        """
        Processes relative file paths (e.g., 'Folder/file.mkv') added by the
        rclone webhook. Checks for the file's existence and calls
        handle_rclone_file to process it. Includes retry logic.
        """
        if not self.pending_rclone_paths:
            return

        relative_file_path = None # Initialize to handle potential errors before assignment
        processing_result = {'success': False} # Default to failure

        try:
            # Peek at the path (e.g., 'Movie Title (Year)/Movie.Title.Year.mkv')
            # This path includes folder and filename as queued by the webhook
            relative_file_path = self.pending_rclone_paths[0]
            logging.info(f"Processing pending rclone path: '{relative_file_path}'. Remaining: {len(self.pending_rclone_paths)}")
        except IndexError:
            logging.debug("Pending rclone path queue was empty when trying to peek.")
            return # Should not happen if initial check passed, but safety first

        original_files_path = None
        target_full_path = None

        try:
            # Determine base path based on mode (handle_rclone_file uses its own settings)
            file_management_mode = get_setting('File Management', 'file_collection_management', 'Symlinked/Local')
            if file_management_mode == 'Plex':
                 # Use Plex mount path for existence check if mode is Plex
                original_files_path = get_setting('Plex', 'mounted_file_location')
            else: # Symlinked/Local or other modes potentially
                original_files_path = get_setting('File Management', 'original_files_path')

            if not original_files_path:
                logging.error(f"Source files path setting is missing for mode '{file_management_mode}'. Cannot process rclone path '{relative_file_path}'. Discarding.")
                # Discard the path if config is bad
                try: self.pending_rclone_paths.popleft()
                except IndexError: pass
                return

            # Construct the full path to the target file
            target_full_path = os.path.join(original_files_path, relative_file_path)
            logging.debug(f"Checking for file existence: {target_full_path}")

            # --- Retry Logic for File Existence ---
            found = False
            # Adjusted delays for slightly longer wait if needed
            delays = [1, 2, 3] # Seconds - Total ~6s wait after initial check
            attempt = 1

            if os.path.exists(target_full_path):
                found = True
                logging.info(f"File found on initial check (attempt 1): {target_full_path}")
            else:
                logging.info(f"File '{target_full_path}' not found on initial check, starting retries...")
                for delay in delays:
                    attempt += 1
                    logging.info(f"Retrying attempt {attempt} for file '{target_full_path}' in {delay} seconds...")
                    time.sleep(delay)
                    if os.path.exists(target_full_path):
                        found = True
                        logging.info(f"File found on attempt {attempt}: {target_full_path}")
                        break
                    else:
                        logging.info(f"File still not found after attempt {attempt}.")

            if not found:
                logging.error(f"File '{target_full_path}' not found or not accessible after {attempt} attempts for relative path '{relative_file_path}'. Path will be retried later.")
                # --- START EDIT: Add logic to potentially discard after N attempts ---
                # Track retry attempts per path (requires storing attempts with the path)
                # For simplicity now, keep infinite retries. If this becomes problematic,
                # a dictionary could store {'path': attempt_count} in the class.
                # --- END EDIT ---
                return # Keep in queue

            # --- Call handle_rclone_file ---
            logging.info(f"File '{target_full_path}' found. Calling handle_rclone_file with relative path: '{relative_file_path}'")
            try:
                # Call the handler function and store its result
                processing_result = handle_rclone_file(relative_file_path)
                log_level = logging.INFO if processing_result.get('success') else logging.WARNING
                logging.log(log_level, f"Result of handle_rclone_file for '{relative_file_path}': {processing_result}")
                # The 'success' key in processing_result now determines if the path should be removed

            except Exception as handle_err:
                logging.error(f"Error calling handle_rclone_file for path '{relative_file_path}': {handle_err}", exc_info=True)
                processing_result['success'] = False # Ensure success is false on exception
                processing_result['message'] = f"Error in handle_rclone_file: {handle_err}"
                # Keep path in queue if the handler itself fails

        except Exception as e:
            logging.error(f"Unexpected error processing pending rclone path '{relative_file_path}': {str(e)}", exc_info=True)
            processing_result['success'] = False # Ensure success is false on general error
            processing_result['message'] = f"Unexpected error: {e}"
            # Keep path in queue on general error

        finally:
            # --- Remove from queue ONLY if handle_rclone_file indicated success ---
            if processing_result.get('success'): # Check the success flag from the result dict
                try:
                    removed_path = self.pending_rclone_paths.popleft()
                    # Verify the removed path is the one we intended to process
                    if removed_path != relative_file_path:
                        logging.warning(f"Removed path '{removed_path}' from queue, but expected '{relative_file_path}'. Queue state might be inconsistent.")
                        # Potential issue: If another thread modified the queue between peek and pop.
                        # Consider using locks if multi-threading is a concern here.
                    else:
                        logging.info(f"Successfully processed path '{relative_file_path}' (Result: {processing_result.get('message', 'OK')}) and removed it from queue.")
                except IndexError:
                    logging.error(f"Tried to remove path '{relative_file_path}' from queue after successful processing, but queue was empty.")
            else:
                 # Log why it's being kept (using message from processing_result)
                logging.info(f"Processing failed or incomplete for '{relative_file_path}' (Reason: {processing_result.get('message', 'Unknown')}). Leaving it in queue for retry.")


    # --- REMOVE apply_dynamic_interval_adjustment method ---
    # def apply_dynamic_interval_adjustment(self, task_name: str, duration: float):
    # ... remove this method ...


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
    def _job_listener(self, event: apscheduler.events.JobExecutionEvent):
        """Listener called after a job executes. Adjusts interval based on duration."""
        if event.exception:
            # Optionally handle job errors here, but for now just skip adjustment
            logging.warning(f"Job '{event.job_id}' failed with exception: {event.exception}")
            return

        task_name = event.job_id
        duration = event.retval # Assuming the wrapper returns duration

        # Ensure duration is a number (it might be None if job failed before wrapper could return)
        if not isinstance(duration, (int, float)) or duration < 0:
            # logging.debug(f"Skipping interval adjustment for '{task_name}': Invalid duration ({duration}) returned by wrapper.")
            return

        # --- Start of logic moved from apply_dynamic_interval_adjustment ---
        # Check if this task is eligible for dynamic adjustment
        is_dynamic_eligible = task_name in self.DYNAMIC_INTERVAL_TASKS or \
                              (task_name.startswith('task_') and task_name.endswith('_wanted'))

        if not is_dynamic_eligible:
            # logging.debug(f"Task '{task_name}' not eligible for duration-based interval adjustment.")
            return # Not eligible for this adjustment type

        with self.scheduler_lock: # Ensure thread safety when accessing intervals/scheduler
            current_job = self.scheduler.get_job(task_name)
            if not current_job:
                logging.warning(f"Cannot adjust interval for task '{task_name}': Job not found in scheduler (might have been removed).")
                return

            # Get current interval from the job's trigger
            try:
                # Check if trigger is IntervalTrigger first
                if not isinstance(current_job.trigger, IntervalTrigger):
                     logging.debug(f"Cannot adjust interval for task '{task_name}': Trigger is not IntervalTrigger ({type(current_job.trigger)}).")
                     return
                current_interval = current_job.trigger.interval.total_seconds()
            except AttributeError:
                 # Should not happen after the type check, but safety first
                logging.warning(f"Cannot adjust interval for task '{task_name}': Failed to get interval from trigger.")
                return

            original_interval = self.original_task_intervals.get(task_name)

            if current_interval is None or original_interval is None:
                logging.warning(f"Cannot apply dynamic interval adjustment for task '{task_name}': Missing original interval data.")
                return

            # Avoid division by zero or negative intervals
            if current_interval <= 0:
                 logging.warning(f"Cannot adjust interval for task '{task_name}': Current interval is zero or negative ({current_interval}s).")
                 return

            threshold = current_interval * 0.10 # 10% threshold
            new_interval_seconds = None # Variable to store the new interval if changed

            if duration > threshold:
                # Task took longer than 10% of its interval, increase the interval (double it)
                potential_new_interval = current_interval * 2

                # Apply maximum caps: original * multiplier AND absolute max
                max_interval_by_multiplier = original_interval * self.MAX_INTERVAL_MULTIPLIER
                capped_interval = min(potential_new_interval, max_interval_by_multiplier, self.ABSOLUTE_MAX_INTERVAL)

                # Ensure capped interval is actually greater than current
                if capped_interval > current_interval:
                    new_interval_seconds = capped_interval
                    logging.info(f"Task '{task_name}' took {duration:.2f}s (> {threshold:.2f}s threshold). Increasing interval to {new_interval_seconds}s.")
                # else: # Log if capped interval didn't result in an increase
                #     logging.debug(f"Task '{task_name}' duration {duration:.2f}s exceeded threshold, but interval increase was capped. Interval remains {current_interval}s.")


            elif current_interval != original_interval:
                # Task was fast enough and interval was previously increased, reset to default
                new_interval_seconds = original_interval
                logging.info(f"Task '{task_name}' took {duration:.2f}s (<= {threshold:.2f}s threshold). Resetting interval to default {new_interval_seconds}s.")

            # Modify the job if the interval changed
            if new_interval_seconds is not None:
                try:
                    # Create a new trigger with the adjusted interval
                    new_trigger = IntervalTrigger(seconds=new_interval_seconds)
                    # Reschedule the job with the new trigger
                    # Use reschedule_job which is safer than modify_job for trigger changes
                    self.scheduler.reschedule_job(job_id=task_name, trigger=new_trigger)
                    logging.debug(f"Successfully rescheduled job '{task_name}' with new interval {new_interval_seconds}s")
                except Exception as e:
                    logging.error(f"Error rescheduling job '{task_name}' with new interval {new_interval_seconds}s: {e}", exc_info=True)
         # --- End of moved logic ---
    # *** END EDIT ***

    # *** START EDIT: Add Wrapper Method ***
    def _run_and_measure_task(self, func, args, kwargs):
        """Wraps a task function to measure and return its execution duration."""
        start_time = time.time()
        task_name_for_log = getattr(func, '__name__', 'unknown_function')
        # Add args/kwargs to log for better context? Be careful with sensitive data.
        # logging.debug(f"Executing wrapped task: '{task_name_for_log}'")
        try:
            # Execute the original task function
            func(*args, **kwargs)
            # Duration calculation happens after successful execution
            duration = time.time() - start_time
            # Log duration for debugging
            # logging.debug(f"Task '{task_name_for_log}' completed successfully in {duration:.3f}s")
            return duration # Return duration for the listener
        except Exception as e:
            # Log the error
            duration = time.time() - start_time
            logging.error(f"Error during execution of wrapped task '{task_name_for_log}' after {duration:.3f}s: {e}", exc_info=True)
            # Re-raise the exception so APScheduler handles it as an error
            raise
    # *** END EDIT ***

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

    movie_airtime_offset_min = float(get_setting("Queue", "movie_airtime_offset", "19")) * 60
    episode_airtime_offset_min = float(get_setting("Queue", "episode_airtime_offset", "0")) * 60

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
            return collected_content  # Return the original content even if some items were skipped
        else:
            logging.info("No collected movies or episodes retrieved or processed.")
            return collected_content # Return empty dict if nothing found

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
    """Adds necessary event listeners to the scheduler."""
    if runner_instance and runner_instance.scheduler:
        try:
            logging.info("Setting up APScheduler job execution listener...")
            runner_instance.scheduler.add_listener(
                runner_instance._job_listener, # Ensure this uses the correct method name
                apscheduler.events.EVENT_JOB_EXECUTED | apscheduler.events.EVENT_JOB_ERROR # Listen for errors too
            )
            logging.info("APScheduler job execution/error listener added successfully.")
        except Exception as e:
            logging.error(f"Failed to add APScheduler listener: {e}", exc_info=True)
    else:
        logging.error("Cannot setup scheduler listeners: ProgramRunner instance or scheduler not found.")
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
