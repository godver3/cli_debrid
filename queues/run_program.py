import logging
import random
import time
import os
import sqlite3
import plexapi # Added import
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
from routes.notifications import send_notifications
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
    remove_symlink_from_plex, # Added import
)
from plexapi.exceptions import NotFound
import pytz # Added import
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError # Keep this if needed elsewhere, or remove if only _get_local_timezone uses it
from database.core import get_db_connection # Add DB connection import
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
        self.currently_running_tasks = set()  # Track which tasks are currently running
        self.pause_reason = None  # Track why the queue is paused
        self.connectivity_failure_time = None  # Track when connectivity failed
        self.connectivity_retry_count = 0  # Track number of retries
        
        # Add a queue for pending rclone paths (using deque for efficiency)
        self.pending_rclone_paths = deque() 
        
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
        
        self.tick_counter = 0
        self.task_intervals = {
            'Wanted': 5,       # Keep these responsive
            'Scraping': 5,     # Keep these responsive
            'Adding': 5,       # Keep these responsive
            'Checking': 180,   # 3 minutes (was already good)
            'Sleeping': 1800,  # 30 minutes (increased from 15)
            'Unreleased': 300,  # 5 minutes (reduced from 1 hour to ensure regular checking)
            'Blacklisted': 7200,  # 2 hours (increased from 1)
            'Pending Uncached': 3600,  # 1 hour (no change)
            'Upgrading': 3600,  # 1 hour (no change)
            'task_plex_full_scan': 3600,
            #'task_debug_log': 60,
            'task_refresh_release_dates': 36000,
            #'task_purge_not_wanted_magnets_file': 604800,
            'task_generate_airtime_report': 3600,
            'task_check_service_connectivity': 60,
            'task_send_notifications': 15,  # Run every 0.25 minutes (15 seconds)
            'task_sync_time': 3600,  # Run every hour
            'task_check_trakt_early_releases': 3600,  # Run every hour
            'task_reconcile_queues': 3600,  # Run every 1 hour (was 5 minutes)
            'task_heartbeat': 120,  # Run every 2 minutes
            #'task_local_library_scan': 900,  # Run every 5 minutes
            'task_refresh_download_stats': 300,  # Run every 5 minutes
            'task_check_plex_files': 60,  # Run every 60 seconds
            'task_update_show_ids': 40600,  # Run every six hours
            'task_update_show_titles': 45600,  # Run every six hours
            'task_update_movie_ids': 50600,  # Run every six hours
            'task_update_movie_titles': 55600,  # Run every six hours
            'task_get_plex_watch_history': 24 * 60 * 60,  # Run every 24 hours
            'task_refresh_plex_tokens': 24 * 60 * 60,  # Run every 24 hours
            'task_check_database_health': 3600,  # Run every hour
            'task_run_library_maintenance': 12 * 60 * 60,  # Run every twelve hours
            'task_verify_symlinked_files': 900,  # Run every 15 minutes
            'task_verify_plex_removals': 900, # NEW: Run every 15 minutes
            'task_update_statistics_summary': 300,  # Run every 5 minutes
            'task_precompute_airing_shows': 600,  # Precompute airing shows every 10 minutes
            'task_process_pending_rclone_paths': 10, # Add new task: check pending rclone paths every 10 seconds (was 60)
            'task_update_tv_show_status': 172800, # NEW: 48 hours (48 * 60 * 60)
        }
        # Store original intervals for reference
        self.original_task_intervals = self.task_intervals.copy()
        
        # Initialize content_sources attribute FIRST
        self.content_sources = None
        self.file_location_cache = {}  # Cache to store known file locations

        self.start_time = time.time()
        # Initialize with base intervals FIRST
        self.last_run_times = {task: self.start_time for task in self.task_intervals}
        self.original_task_intervals = self.task_intervals.copy() # Keep original intervals

        # Initialize enabled_tasks with base tasks FIRST
        self.enabled_tasks = {
            'Wanted', 
            'Scraping', 
            'Adding', 
            'Checking', 
            'Sleeping', 
            'Unreleased', 
            'Blacklisted',
            'Pending Uncached',
            'Upgrading',
            'task_refresh_release_dates',
            'task_generate_airtime_report',
            'task_check_service_connectivity',
            'task_send_notifications',
            'task_sync_time',
            'task_check_trakt_early_releases',
            'task_reconcile_queues',
            'task_heartbeat',
            'task_refresh_download_stats',
            'task_update_show_ids',
            'task_update_show_titles',
            'task_update_movie_ids',
            'task_update_movie_titles',
            'task_refresh_plex_tokens',
            'task_check_database_health',
            'task_update_statistics_summary',
            'task_precompute_airing_shows',
            'task_update_tv_show_status', # NEW: Enable the task by default
        }

        # *** START EDIT ***
        # Define the set of tasks eligible for dynamic interval adjustment
        self.DYNAMIC_INTERVAL_TASKS = {
            'task_refresh_release_dates',
            'task_generate_airtime_report',
            'task_check_trakt_early_releases',
            'task_reconcile_queues',
            'task_refresh_download_stats',
            'task_check_plex_files',
            'task_update_show_ids',
            'task_update_show_titles',
            'task_update_movie_ids',
            'task_update_movie_titles',
            'task_get_plex_watch_history',
            'task_run_library_maintenance',
            'task_verify_symlinked_files',
            'task_verify_plex_removals',
            'task_update_statistics_summary',
            'task_precompute_airing_shows',
            'task_plex_full_scan'
        }
        # Define a maximum multiplier for dynamic intervals (e.g., 16x original)
        self.MAX_INTERVAL_MULTIPLIER = 16
        # Define an absolute maximum interval (e.g., 24 hours in seconds)
        self.ABSOLUTE_MAX_INTERVAL = 48 * 60 * 60
        # *** END EDIT ***

        # THEN populate content source intervals AND add enabled content source tasks
        logging.info("Performing initial population of content source intervals and enabled tasks...")
        self.get_content_sources(force_refresh=True) # This call will now work as self.enabled_tasks exists
        logging.info("Initial content source interval/task population complete.")
        
        # Enable Plex removal task if symlink verification is enabled
        if 'task_verify_symlinked_files' in self.enabled_tasks:
            self.enabled_tasks.add('task_verify_plex_removals')
            logging.info("Enabled Plex removal verification task as symlink verification is active.")

        # FINALLY load saved task toggle states from JSON file (AFTER intervals are populated)
        try:
            import os
            import json

            # Get the user_db_content directory from environment variable
            db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
            toggles_file_path = os.path.join(db_content_dir, 'task_toggles.json')

            # Check if file exists
            if os.path.exists(toggles_file_path):
                # Load from JSON file
                with open(toggles_file_path, 'r') as f:
                    saved_states = json.load(f)

                # Apply saved states
                for task_name, enabled in saved_states.items():
                    normalized_name = self._normalize_task_name(task_name)
                    # --- START EDIT ---
                    # Check if the task from the JSON file actually exists in our defined intervals
                    if normalized_name not in self.task_intervals:
                        logging.warning(f"Task '{normalized_name}' found in task_toggles.json but not defined in task_intervals. Skipping toggle.")
                        continue # Skip this task if it's not defined in the code
                    # --- END EDIT ---

                    if enabled and normalized_name not in self.enabled_tasks:
                        self.enabled_tasks.add(normalized_name)
                        logging.info(f"Enabled task from saved settings: {normalized_name}")
                    elif not enabled and normalized_name in self.enabled_tasks:
                        self.enabled_tasks.remove(normalized_name)
                        logging.info(f"Disabled task from saved settings: {normalized_name}")
        except Exception as e:
            logging.error(f"Error loading saved task toggle states: {str(e)}")

        if get_setting('File Management', 'file_collection_management') == 'Plex' and (
            get_setting('Plex', 'update_plex_on_file_discovery') or 
            get_setting('Plex', 'disable_plex_library_checks')
        ):
            self.enabled_tasks.add('task_check_plex_files')

        if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local' and (
            get_setting('File Management', 'plex_url_for_symlink') and 
            get_setting('File Management', 'plex_token_for_symlink')
        ):
            self.enabled_tasks.add('task_verify_symlinked_files')

        if get_setting('Debug', 'not_add_plex_watch_history_items_to_queue', False):
            self.enabled_tasks.add('task_get_plex_watch_history')

        if get_setting('Debug', 'enable_library_maintenance_task', False):
            self.enabled_tasks.add('task_run_library_maintenance')
        
        # Add this line to store content sources
        self.content_sources = None
        self.file_location_cache = {}  # Cache to store known file locations

        # *** Add this line to update the original intervals AFTER dynamic tasks are added ***
        self.original_task_intervals = self.task_intervals.copy()
        logging.info("Finalized original task intervals after content source and toggle loading.")

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

    # Modify this method to cache content sources
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
            
            for source, data in self.content_sources.items():
                if isinstance(data, str):
                    data = {'enabled': data.lower() == 'true'}
                
                if not isinstance(data, dict):
                    logging.error(f"Unexpected data type for content source {source}: {type(data)}")
                    continue
                
                source_type = source.split('_')[0]

                # Use custom check period if present, otherwise use default
                custom_interval = custom_check_periods.get(source)
                if custom_interval is not None:
                    # Convert minutes to seconds, handling decimals
                    data['interval'] = int(float(custom_interval) * 60)  # First multiply by 60, then convert to int
                else:
                    data['interval'] = int(data.get('interval', default_intervals.get(source_type, 3600)))

                task_name = f'task_{source}_wanted'
                self.task_intervals[task_name] = data['interval']
                self.last_run_times[task_name] = self.start_time
                
                if isinstance(data.get('enabled'), str):
                    data['enabled'] = data['enabled'].lower() == 'true'
                
                if data.get('enabled', False):
                    self.enabled_tasks.add(task_name)
            
            # Log the intervals only once
            logging.info("Content source intervals:")
            for task, interval in self.task_intervals.items():
                if task.startswith('task_') and task.endswith('_wanted'):
                    logging.info(f"{task}: {interval} seconds")

        return self.content_sources
        
    def should_run_task(self, task_name):
        if task_name not in self.enabled_tasks or task_name in self.currently_running_tasks:
            return False
        
        # Defensive check: Ensure task exists in interval/timing dictionaries
        if task_name not in self.task_intervals or task_name not in self.last_run_times:
            logging.error(f"Task '{task_name}' found in enabled_tasks but missing from task_intervals or last_run_times. Skipping run.")
            # Optionally, try to re-initialize content sources here if it's a source task
            if task_name.endswith('_wanted'):
                 logging.warning(f"Attempting to re-initialize content sources due to missing task interval for {task_name}")
                 try:
                     self.get_content_sources(force_refresh=True)
                 except Exception as e:
                     logging.error(f"Error during forced content source refresh: {e}")
            return False
            
        current_time = time.time()
        time_since_last_run = current_time - self.last_run_times[task_name]
        should_run = time_since_last_run >= self.task_intervals[task_name]
        if should_run:
            self.last_run_times[task_name] = current_time
            self.currently_running_tasks.add(task_name)  # Mark task as running
        return should_run

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
        from queues.queue_manager import QueueManager
        
        QueueManager().pause_queue(reason=self.pause_reason)
        self.queue_paused = True
        logging.info("Queue paused")

    def resume_queue(self):
        from queues.queue_manager import QueueManager

        QueueManager().resume_queue()
        self.queue_paused = False
        self.pause_reason = None  # Clear pause reason on resume
        logging.info("Queue resumed")

    def process_queues(self):
        try:
            # Check connectivity status if we're in a failure state
            self.check_connectivity_status()

            # Check scheduled pause
            is_scheduled_pause = self._is_within_pause_schedule()
            queue_manager = self.queue_manager # Get manager instance

            if is_scheduled_pause and not queue_manager.is_paused():
                pause_start = get_setting('Queue', 'pause_start_time', '00:00')
                pause_end = get_setting('Queue', 'pause_end_time', '00:00')
                self.pause_reason = f"Scheduled pause active ({pause_start} - {pause_end})"
                self.pause_queue()
                logging.info(f"Queue automatically paused due to schedule: {self.pause_reason}")
            elif not is_scheduled_pause and queue_manager.is_paused() and self.pause_reason and "Scheduled pause active" in self.pause_reason:
                 # Only resume if the current pause reason is the scheduled one
                logging.info("Scheduled pause period ended. Resuming queue.")
                self.resume_queue()

            # Get current time once for this cycle
            current_time = time.time()
            
            # Initialize timing tracking attributes if they don't exist
            if not hasattr(self, '_last_heartbeat_check'):
                self._last_heartbeat_check = 0
                self._last_task_health_check = 0
                self._last_full_update = 0
                
            # Only update heartbeat every second at most
            if current_time - self._last_heartbeat_check >= 1.0:
                self.update_heartbeat()
                
                # Only check heartbeat every 30 seconds
                if current_time - self._last_heartbeat_check >= 30:
                    self.check_heartbeat()
                    
                self._last_heartbeat_check = current_time
            
            # Only check task health and adjust intervals every 60 seconds
            if current_time - self._last_task_health_check >= 60:
                stale_tasks_count = self.check_task_health()
                # Determine queue state for adjustment
                queues_are_empty = self.queue_manager.are_main_queues_empty() if hasattr(self, 'queue_manager') else True
                # Call adjustment function
                self.adjust_task_intervals_based_on_load(queues_are_empty, stale_tasks_count)
                
                self._last_task_health_check = current_time
            
            # Update frequently polled queues first (to check for new items)
            frequent_queues = ['Wanted', 'Scraping', 'Adding']
            for queue_name in frequent_queues:
                if queue_name in self.queue_manager.queues:
                    self.queue_manager.queues[queue_name].update()
            
            # Only do a full update of all queues periodically to reduce database load
            if not hasattr(self, '_last_full_update') or current_time - self._last_full_update >= 30:  # 30 seconds
                logging.debug("Performing full queue update")
                for queue_name, queue in self.queue_manager.queues.items():
                    if queue_name not in frequent_queues:  # Skip queues we already updated
                        queue.update()
                self._last_full_update = current_time
            # Always track the last Unreleased queue update separately
            elif not hasattr(self, '_last_unreleased_update') or current_time - self._last_unreleased_update >= 300:  # 5 minutes
                if 'Unreleased' in self.queue_manager.queues:
                    self.queue_manager.queues['Unreleased'].update()  # Update queue contents
                    self._last_unreleased_update = current_time
            
            # Process queue tasks (check for pause *before* processing each queue)
            for queue_name in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading']:
                if self.queue_manager.is_paused(): # Check pause state again
                    #logging.debug(f"Skipping queue {queue_name} due to pause state.")
                    continue # Skip processing this queue if paused

                should_run = self.should_run_task(queue_name)
                if should_run:
                    self.safe_process_queue(queue_name)

            # Process content source tasks (check for pause *before* processing each source)
            for source, data in self.get_content_sources().items():
                if self.queue_manager.is_paused(): # Check pause state again
                    #logging.debug(f"Skipping content source {source} due to pause state.")
                    continue # Skip processing this source if paused

                task_name = f'task_{source}_wanted'
                should_run = self.should_run_task(task_name)
                if should_run:
                    try:
                        self.process_content_source(source, data)
                    except Exception as e:
                        logging.error(f"Error processing content source {source}: {str(e)}")
                        logging.error(traceback.format_exc())
                    finally:
                        self.currently_running_tasks.discard(task_name)

            # Process other enabled tasks (check for pause *before* processing each task)
            for task_name in list(self.enabled_tasks): # Use list to allow modification during iteration if needed
                if self.queue_manager.is_paused(): # Check pause state again
                    #logging.debug(f"Skipping task {task_name} due to pause state.")
                    continue # Skip processing this task if paused

                # Check if it's NOT a standard queue or a content source task
                is_standard_queue = task_name in [
                    'Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping',
                    'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading'
                ]
                is_content_source = task_name.startswith('task_') and task_name.endswith('_wanted')
                
                if not is_standard_queue and not is_content_source:
                    if self.should_run_task(task_name):
                        task_start_time = time.time() # Start timer
                        try:
                            logging.debug(f"Running task: {task_name}")
                            task_method = getattr(self, task_name)
                            task_method()
                        except Exception as e:
                            logging.error(f"Error running task {task_name}: {str(e)}")
                            logging.error(traceback.format_exc())
                        finally:
                            task_duration = time.time() - task_start_time # Calculate duration
                            logging.debug(f"Task {task_name} finished in {task_duration:.2f}s")
                            # --- START EDIT: Apply dynamic interval logic ---
                            self.apply_dynamic_interval_adjustment(task_name, task_duration)
        # --- END EDIT ---
                            self.currently_running_tasks.discard(task_name)

        except Exception as e:
            logging.error(f"Error in process_queues: {str(e)}")
            logging.error(traceback.format_exc())

    def safe_process_queue(self, queue_name: str):
        try:
            start_time = time.time()
            
            if not hasattr(self, 'queue_manager') or not hasattr(self.queue_manager, 'queues'):
                logging.error("Queue manager not properly initialized")
                return None
                
            if queue_name not in self.queue_manager.queues:
                logging.error(f"Queue '{queue_name}' not found in queue manager!")
                return None
            
            # Convert queue name to method name, replacing spaces with underscores
            method_name = f'process_{queue_name.lower().replace(" ", "_")}'
            if not hasattr(self.queue_manager, method_name):
                logging.error(f"Process method '{method_name}' not found in queue manager!")
                return None
                
            process_method = getattr(self.queue_manager, method_name)
            
            queue_contents = self.queue_manager.queues[queue_name].get_contents()
            
            if self.queue_manager.is_paused():
                logging.warning(f"Queue processing is paused. Skipping {queue_name} queue.")
                return None
            
            try:
                if queue_name == "Checking":
                    result = process_method(self) # Pass self as program_runner
                else:
                    result = process_method()
            except RateLimitError:
                logging.warning("Rate limit exceeded on Debrid API")
                self.handle_rate_limit()
                return None
            finally:
                # Always remove the task from currently_running_tasks
                self.currently_running_tasks.discard(queue_name)
            
            queue_contents = self.queue_manager.queues[queue_name].get_contents()
            
            duration = time.time() - start_time
            
            return result
        
        except Exception as e:
            logging.error(f"Error processing {queue_name} queue: {str(e)}")
            logging.error(traceback.format_exc())
            return None
        finally:
            # Double ensure task is removed from currently_running_tasks
            self.currently_running_tasks.discard(queue_name)

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
            self.run()

    def stop(self):
        logging.warning("Program stop requested")
        self.running = False
        self.initializing = False

    def is_running(self):
        return self.running

    def is_initializing(self):  # Add this method
        return self.initializing

    def run(self):
        try:
            logging.info("Starting program run")
            self.running = True  # Make sure running flag is set
            logging.info(f"Program running state: {self.running}")

            self.run_initialization()

            # Track activity to adjust sleep times
            all_empty_count = 0
            unreleased_check_interval = 300  # Check Unreleased queue every 5 minutes (300 seconds)

            # Initialize sleep_time before the loop
            sleep_time = 0.1

            while self.running:
                try:
                    cycle_start = time.time()
                    current_time = cycle_start  # Store current time for later use

                    # Check if all relevant queues are empty before processing
                    all_queues_empty = True
                    if hasattr(self, 'queue_manager'):
                        all_queues_empty = self.queue_manager.are_main_queues_empty()

                    # Process all queues and tasks
                    self.process_queues()

                    # If all main queues are empty, we still need to periodically check the Unreleased queue
                    # since it contains items that might be ready to move to Wanted based on release dates
                    if all_queues_empty and hasattr(self, 'queue_manager') and 'Unreleased' in self.queue_manager.queues:
                        if not hasattr(self, '_last_unreleased_check'):
                            self._last_unreleased_check = 0

                        # Check if it's time to process the Unreleased queue
                        if current_time - self._last_unreleased_check >= unreleased_check_interval:
                            logging.debug("Checking Unreleased queue despite empty main queues")
                            if 'Unreleased' in self.queue_manager.queues: # Check again as update might remove it
                                self.queue_manager.queues['Unreleased'].update()  # Update queue contents
                            self.safe_process_queue('Unreleased')  # Process the queue
                            self._last_unreleased_check = current_time

                    # Track empty queue state
                    if all_queues_empty:
                        all_empty_count = min(all_empty_count + 1, 120)  # Cap at 120 (60 seconds with 0.5s sleep)
                    else:
                        all_empty_count = 0  # Reset when any queue has items

                    # Simplified sleep time logic
                    if all_queues_empty:
                        # Use a fixed longer sleep when main queues are idle
                        sleep_time = 5.0 # 5000ms sleep when idle
                        # Log occasionally if queues remain empty
                        if all_empty_count > 0 and all_empty_count % 12 == 0: # Log every 60 seconds (5s sleep * 12)
                             logging.debug(f"All critical queues empty for {all_empty_count} cycles, sleeping for {sleep_time:.2f}s (still checking Unreleased queue every {unreleased_check_interval/60:.1f} minutes)")
                    else:
                        # Use a fixed short sleep when active
                        sleep_time = 0.1 # 100ms sleep when active

                    # Check queue manager state (log pause only occasionally)
                    if hasattr(self, 'queue_manager'):
                        paused = self.queue_manager.is_paused()
                        # Use current_time already captured
                        if paused:
                            # Initialize last_pause_log if it doesn't exist
                            if not hasattr(self, 'last_pause_log'):
                                self.last_pause_log = 0

                            # Log only every 30 seconds
                            if current_time - self.last_pause_log >= 30:
                                logging.warning("Queue manager is currently paused")
                                self.last_pause_log = current_time

                except Exception as e:
                    logging.error(f"Unexpected error in main loop: {str(e)}")
                    logging.error(traceback.format_exc())
                    sleep_time = 1.0  # Longer sleep after error
                finally:
                    time.sleep(sleep_time)  # Use the calculated sleep time

            logging.warning("Program has stopped running")
        except Exception as e:
            logging.error(f"Fatal error in run method: {str(e)}")
            logging.error(traceback.format_exc())

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

    def adjust_task_intervals_based_on_load(self, queues_are_empty: bool, delayed_tasks_count: int):
        """
        Dynamically adjust task intervals based on queue status and task health.
        When main queues are empty and few tasks are delayed, slightly increase intervals for long-running tasks.
        Otherwise, ensure intervals are reset to their defaults.
        
        Args:
            queues_are_empty: Whether the main processing queues are empty.
            delayed_tasks_count: The number of tasks currently detected as potentially delayed.
        """
        if not hasattr(self, '_interval_adjustment_time'):
            self._interval_adjustment_time = 0
        
        current_time = time.time()
        # Only adjust every 60 seconds to avoid thrashing
        if current_time - self._interval_adjustment_time < 60:
            return
            
        self._interval_adjustment_time = current_time
        
        # Define non-critical tasks that can be slowed down when idle
        # (Exclude frequent checks, content sources with short intervals, etc.)
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
        # Also add content sources with intervals > 15 minutes (900 seconds)
        for task, interval in self.original_task_intervals.items():
             if task.endswith('_wanted') and interval > 900:
                 slowdown_candidates.add(task)

        idle_increase_seconds = 300 # Increase interval by 5 minutes when idle
        DELAY_THRESHOLD = 3 # Number of allowed delayed tasks before considering the system busy

        # Determine if system is truly idle (empty queues AND few delayed tasks)
        system_is_idle = queues_are_empty and (delayed_tasks_count < DELAY_THRESHOLD)

        if system_is_idle:
            if not hasattr(self, '_last_idle_adjustment_log') or current_time - self._last_idle_adjustment_log >= 600: # Log every 10 mins
                 logging.info(f"System idle (main queues empty, {delayed_tasks_count} delayed tasks < {DELAY_THRESHOLD}) - increasing non-critical task intervals.")
                 self._last_idle_adjustment_log = current_time
                 
            # Apply slight increase to candidate tasks
            for task in slowdown_candidates:
                if task in self.original_task_intervals and task in self.task_intervals:
                    self.task_intervals[task] = self.original_task_intervals[task] + idle_increase_seconds
        else:
            active_reason = []
            if not queues_are_empty:
                 active_reason.append("main queues have items")
            if delayed_tasks_count >= DELAY_THRESHOLD:
                 active_reason.append(f"{delayed_tasks_count} potentially delayed tasks >= threshold {DELAY_THRESHOLD}")
                 
            # Log only when state changes back to active or periodically if remaining active
            if not hasattr(self, '_last_active_state_log'): self._last_active_state_log = 0
            if not hasattr(self, '_was_idle_last_check'): self._was_idle_last_check = False
            
            log_now = False
            if not system_is_idle and self._was_idle_last_check: # Changed from idle to active
                log_now = True
            elif not system_is_idle and current_time - self._last_active_state_log >= 600: # Still active, log every 10 mins
                log_now = True
                
            if log_now:
                logging.info(f"System active ({', '.join(active_reason)}) - ensuring default task intervals.")
                self._last_active_state_log = current_time
                 
            # Reset all intervals to original values if they were changed
            needs_reset = False
            for task in slowdown_candidates:
                 if task in self.original_task_intervals and task in self.task_intervals:
                     if self.task_intervals[task] != self.original_task_intervals[task]:
                         needs_reset = True
                         break
            
            if needs_reset:
                 logging.info("Resetting task intervals to default values.")
                 # Create a fresh copy to avoid modifying the original
                 self.task_intervals = self.original_task_intervals.copy()
                 # Re-apply any custom intervals that might not be in slowdown_candidates (just in case)
                 # Although this shouldn't be necessary if original_task_intervals is the true source
                 # for task, interval in self.original_task_intervals.items():
                 #     self.task_intervals[task] = interval
            
        # Update idle state tracking for next check
        self._was_idle_last_check = system_is_idle

    def check_task_health(self):
        """Check task health, log potential delays, and return the count of delayed tasks."""
        current_time = time.time()
        delayed_tasks_info = []
        
        for task, last_run_time in self.last_run_times.items():
            if task not in self.enabled_tasks:
                continue
                
            time_since_last_run = current_time - last_run_time
            interval = self.task_intervals.get(task) # Use .get for safety
            
            if interval is None:
                logging.warning(f"Task {task} found in last_run_times but missing interval. Skipping health check.")
                continue

            # Check if the task is behind schedule by 1.5x its interval
            if time_since_last_run > interval * 1.5:
                delayed_tasks_info.append((task, time_since_last_run, interval))
                # Don't reset the timer here anymore, just log the potential delay.
        
        delayed_count = len(delayed_tasks_info)

        # Only log if there are potentially delayed tasks
        if delayed_count > 0:
            logging.info(f"Potential task delays detected ({delayed_count} tasks):")
            for task, time_since_last_run, interval in delayed_tasks_info:
                logging.info(f"  - Task '{task}' overdue: ran {time_since_last_run:.2f}s ago (interval: {interval}s)")
            
            # Log if multiple tasks are delayed
            if delayed_count >= 3:
                logging.info(f"Multiple ({delayed_count}) potentially delayed tasks detected. System might be busy or tasks taking longer.")
                
        return delayed_count # Return the count of potentially delayed tasks

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
                with open(notifications_file, "rb") as f:
                    notifications = pickle.load(f)
                
                if notifications:
                    # Fetch enabled notifications using CLI_DEBRID_PORT
                    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
                    response = requests.get(f'http://localhost:{port}/settings/notifications/enabled')
                    if response.status_code == 200:
                        enabled_notifications = response.json().get('enabled_notifications', {})
                        
                        # Send notifications
                        send_notifications(notifications, enabled_notifications)
                        
                        # Clear the notifications file
                        with open(notifications_file, "wb") as f:
                            pickle.dump([], f)
                        
                        logging.info(f"Sent {len(notifications)} notifications and cleared the notifications file")
                    else:
                        logging.error(f"Failed to fetch enabled notifications: {response.text}")
                else:
                    # logging.debug("No notifications to send")
                    pass
            except Exception as e:
                logging.error(f"Error processing notifications: {str(e)}")
        else:
            logging.debug("No notifications file found")

    def task_sync_time(self):
        self.sync_time()

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

    def reinitialize(self):
        """Force reinitialization of the program runner to pick up new settings"""
        logging.info("Reinitializing ProgramRunner...")
        self._initialized = False
        self.__init__()
        # Force refresh content sources
        self.get_content_sources(force_refresh=True)
        logging.info("ProgramRunner reinitialized successfully")

    def handle_rate_limit(self):
        """Handle rate limit by pausing the queue for 30 minutes"""
        logging.warning("Rate limit exceeded. Pausing queue for 30 minutes.")
        self.pause_reason = "Rate limit exceeded - resuming in 30 minutes"
        self.pause_queue()
        
        # Schedule queue resume after 30 minutes
        resume_time = datetime.now() + timedelta(minutes=30)
        logging.info(f"Queue will resume at {resume_time}")
        
        # Sleep for 30 minutes
        time.sleep(1800)  # 30 minutes in seconds
        
        logging.info("Rate limit pause period complete. Resuming queue.")
        self.resume_queue()

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
            get_cached_download_stats()  # This will refresh the cache if needed
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
            logging.debug("Skipping Plex file check")
            return

        from database import get_media_item_by_id

        plex_file_location = get_setting('Plex', 'mounted_file_location', default='/mnt/zurg/__all__')
        if not os.path.exists(plex_file_location):
            logging.warning(f"Plex file location does not exist: {plex_file_location}")
            return

        # Get all media items from database that are in Checking state
        from database.core import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        items = cursor.execute('SELECT id, title, filled_by_title, filled_by_file FROM media_items WHERE state = "Checking"').fetchall()
        conn.close()
        logging.info(f"Found {len(items)} media items in Checking state to verify")

        # Check if Plex library checks are disabled
        if get_setting('Plex', 'disable_plex_library_checks', default=False):
            logging.info("Plex library checks disabled - marking found files as Collected")
            updated_items = 0
            not_found_items = 0

            sections_to_update = {}  # Store {section_object: set_of_paths}

            for item in items:
                filled_by_title = item['filled_by_title']
                filled_by_file = item['filled_by_file']
                
                if not filled_by_title or not filled_by_file:
                    continue

                # Check if the file exists in the expected location
                file_path = os.path.join(plex_file_location, filled_by_title, filled_by_file)
                title_without_ext = os.path.splitext(filled_by_title)[0]
                file_path_no_ext = os.path.join(plex_file_location, title_without_ext, filled_by_file)
                
                # Check if we've already found this file before
                cache_key = f"{filled_by_title}:{filled_by_file}"
                if cache_key in self.file_location_cache and self.file_location_cache[cache_key] == 'exists':
                    logging.debug(f"Skipping previously verified file: {filled_by_title}")
                    continue

                file_found_on_disk = False
                actual_file_path = None
                if os.path.exists(file_path):
                    file_found_on_disk = True
                    actual_file_path = file_path
                elif os.path.exists(file_path_no_ext):
                    file_found_on_disk = True
                    actual_file_path = file_path_no_ext
                else:
                    not_found_items += 1
                    logging.debug(f"File not found on disk in primary locations:\n  {file_path}\n  {file_path_no_ext}")
                    continue

                if file_found_on_disk:
                    logging.info(f"Found file on disk: {actual_file_path}")
                    if cache_key not in self.file_location_cache:
                        self.file_location_cache[cache_key] = 'exists'
                    updated_items += 1 # Count item as 'updated' if found
                    
                    # Update item state to Collected if found
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    # Important: Only update if the item is still in 'Checking' state
                    cursor.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking"', (now, item['id']))
                    if cursor.rowcount > 0: # Check if the update actually happened
                        conn.commit()
                        # Use dictionary access for title, provide a default if None or empty
                        item_title = item['title'] if item['title'] else 'N/A'
                        logging.info(f"Updated item {item['id']} ({item_title}) to Collected state.")

                        # Add post-processing call after state update
                        updated_item_details = get_media_item_by_id(item['id']) # Fetch the updated details
                        if updated_item_details:
                            handle_state_change(dict(updated_item_details))

                        # Send notification for collected item
                        try:
                            # Imports moved inside to avoid potential top-level issues if routes aren't ready
                            from routes.notifications import send_notifications
                            from routes.settings_routes import get_enabled_notifications_for_category
                            from routes.extensions import app

                            with app.app_context():
                                response = get_enabled_notifications_for_category('collected')
                                # Check status code and content type for safety
                                if response.status_code == 200 and response.is_json:
                                    response_data = response.get_json()
                                    if response_data.get('success'):
                                        enabled_notifications = response_data.get('enabled_notifications')
                                        if enabled_notifications:
                                            # Use updated_item_details fetched above
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
                                else:
                                     logging.error(f"Failed to get enabled notifications: Status {response.status_code}, Response: {response.text}")
                        except Exception as e:
                            logging.error(f"Failed to send collected notification for item {item['id']}: {str(e)}")
                    else:
                        # Log if the item wasn't updated (e.g., it was already Collected or state changed concurrently)
                        logging.debug(f"Item {item['id']} was not updated to Collected (possibly already in correct state or state changed concurrently).")
                    conn.close()

                    # Identify relevant Plex sections and paths to update for this file
                    for section in sections:
                        for location in section.locations:
                            # Check if the found file path is within this library location
                            # This check needs refinement based on how Zurg maps files
                            # For now, let's assume the file *should* be based on filled_by_title
                            expected_path = os.path.join(location, filled_by_title)
                            # More robust check: is actual_file_path under location?
                            try:
                                if Path(actual_file_path).resolve().is_relative_to(Path(location).resolve()):
                                    if section not in sections_to_update:
                                        sections_to_update[section] = set()
                                    # We might want to update the specific file path or the parent directory
                                    # Updating the parent dir (expected_path) seems more aligned with Plex behavior
                                    sections_to_update[section].add(expected_path)
                                    # break # Found the correct section for this file
                            except ValueError: # Can happen if paths are on different drives on Windows
                                pass # Ignore this section if paths can't be compared
                            except Exception as e:
                                logging.warning(f"Error checking path relation for {actual_file_path} and {location}: {e}")

            # Now, update the relevant Plex sections once per section, using one relevant path
            if sections_to_update:
                logging.info(f"Updating {len(sections_to_update)} Plex sections...")
                for section, paths in sections_to_update.items():
                    # Decide which path to use for the update - maybe the first one found?
                    path_to_scan = next(iter(paths)) if paths else None
                    if path_to_scan:
                        try:
                            logging.info(f"Updating Plex section '{section.title}' for path: {path_to_scan}")
                            section.update(path=path_to_scan)
                            updated_sections.add(section) # Keep track for final summary
                        except Exception as e:
                            logging.error(f"Failed to update Plex section '{section.title}' with path '{path_to_scan}': {str(e)}", exc_info=True)
                    else:
                         logging.warning(f"No valid paths found to update section '{section.title}'")

            # Log summary of operations
            logging.info(f"Plex check summary: {updated_items} items found on disk, {not_found_items} items not found")
            if updated_sections:
                logging.info("Plex sections updated in this run:")

        if not get_setting('Plex', 'url', default=False):
                return

        try:
            plex_url = get_setting('Plex', 'url', default='')
            plex_token = get_setting('Plex', 'token', default='')
            
            if not plex_url or not plex_token:
                logging.warning("Plex URL or token not configured")
                return

            # Connect to Plex server and log library information
            plex = PlexServer(plex_url, plex_token)
            sections = plex.library.sections()
            logging.info(f"Connected to Plex server, found {len(sections)} library sections")
            
            # Log detailed information about each library section
            for section in sections:
                logging.info(f"Library Section: {section.title}")
                logging.info(f"  Type: {section.type}")
                logging.info(f"  Locations:")
                for location in section.locations:
                    logging.info(f"    - {location}")
            # <<< remove the 'if not os.path.exists...' check here

            updated_sections = set()  # Track which sections we've updated
            updated_items = 0 # This counter seems unused in the original logic, removing its increment
            not_found_items = 0

            # Ensure the tick count dictionary exists
            if not hasattr(self, 'plex_scan_tick_counts'):
                self.plex_scan_tick_counts = {}

            for item in items:
                filled_by_title = item['filled_by_title']
                filled_by_file = item['filled_by_file']

                if not filled_by_title or not filled_by_file:
                    continue

                # Generate cache key and potential file paths
                cache_key = f"{filled_by_title}:{filled_by_file}"
                file_path = os.path.join(plex_file_location, filled_by_title, filled_by_file)
                title_without_ext = os.path.splitext(filled_by_title)[0]
                file_path_no_ext = os.path.join(plex_file_location, title_without_ext, filled_by_file)

                # Check if the file exists on disk
                file_found_on_disk = False
                actual_file_path = None
                if os.path.exists(file_path):
                    file_found_on_disk = True
                    actual_file_path = file_path
                elif os.path.exists(file_path_no_ext):
                    file_found_on_disk = True
                    actual_file_path = file_path_no_ext

                should_trigger_scan = False
                if file_found_on_disk:
                    # File exists, update cache and handle tick count
                    logging.debug(f"Confirmed file exists on disk: {actual_file_path}")
                    self.file_location_cache[cache_key] = 'exists'

                    # Increment tick count
                    current_tick = self.plex_scan_tick_counts.get(cache_key, 0) + 1
                    self.plex_scan_tick_counts[cache_key] = current_tick

                    # Determine if scan should be triggered (first 5 times found)
                    if current_tick <= 5: # Changed condition
                        should_trigger_scan = True
                        # Updated log message
                        logging.info(f"File '{filled_by_file}' found (tick {current_tick}). Triggering Plex scan for all {len(sections)} sections (will trigger for first 5 ticks).")
                    else:
                        should_trigger_scan = False
                        # Updated log message
                        logging.debug(f"File '{filled_by_file}' found (tick {current_tick}). Skipping Plex scan trigger (only triggers for first 5 ticks).")

                else:
                    # File not found
                    not_found_items += 1
                    logging.debug(f"File not found on disk in primary locations for item {item['id']}:\n  {file_path}\n  {file_path_no_ext}")
                    # Reset tick count if file is not found
                    if cache_key in self.plex_scan_tick_counts:
                        logging.debug(f"Resetting Plex scan tick count for missing file '{filled_by_file}'.")
                        del self.plex_scan_tick_counts[cache_key]
                    # No scan if file not found
                    should_trigger_scan = False
                    continue # Move to the next item

                # Perform Plex scan only if the file was found and it's within the first 5 ticks
                if should_trigger_scan:
                    scan_triggered_for_item = False
                    for section in sections:
                        try:
                            # Update each library location with the expected path structure
                            for location in section.locations:
                                # Calculate path relative to this specific library location
                                expected_path = os.path.join(location, filled_by_title)
                                logging.debug(f"Attempting Plex section '{section.title}' scan trigger:")
                                logging.debug(f"  Location: {location}")
                                logging.debug(f"  Scan Path: {expected_path}")

                                # Trigger update for this section using the calculated path
                                section.update(path=expected_path)
                                scan_triggered_for_item = True
                                # Optimization: If a file path matches one location, we might not need
                                # to trigger scans based on *other* locations in the same section for the *same* file.
                                # However, the current logic triggers based on filled_by_title under *each* location.
                                # We'll keep the original behavior of trying all locations per section.

                        except Exception as e:
                            # Construct expected_path for logging even if loop didn't run
                            expected_path_for_log = f"location/{filled_by_title}" if not 'expected_path' in locals() else expected_path
                            logging.error(f"Failed to trigger update scan for Plex section '{section.title}' with expected path like '{expected_path_for_log}': {str(e)}", exc_info=True)

                    if scan_triggered_for_item:
                        logging.info(f"Completed Plex scan trigger attempt in relevant sections for item {item['id']}.")
                # No need for an else block here, skip message is logged above

            # Log summary of operations
            processed_items_count = len(items) # Count items processed in this run
            logging.info(f"Plex check summary: Processed {processed_items_count} items. {not_found_items} items not found on disk during this check.")
            # Removed logging block for updated_sections as it wasn't used consistently

        except Exception as e:
            logging.error(f"Error during Plex library update: {str(e)}", exc_info=True)

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
        """Manually trigger a task to run immediately."""
        if task_name not in self.enabled_tasks:
            # Convert task name to match how it's stored in enabled_tasks
            task_name_normalized = task_name
            if task_name.startswith('task_'):
                task_name_normalized = task_name[5:]  # Remove task_ prefix
            
            # For content source tasks, they're stored with spaces in enabled_tasks
            if '_wanted' in task_name_normalized:
                task_name_normalized = task_name_normalized.replace('_', ' ')
                if not task_name_normalized.startswith('task_'):
                    task_name_normalized = f'task_{task_name_normalized}'
                
            if task_name_normalized not in self.enabled_tasks:
                raise ValueError(f"Task {task_name} is not enabled")
            task_name = task_name_normalized
        
        # Handle queue tasks (which don't have task_ prefix)
        queue_tasks = [
            'process_wanted',
            'process_checking',
            'process_scraping',
            'process_adding',
            'process_unreleased',
            'process_sleeping',
            'process_blacklisted',
            'process_pending_uncached',
            'process_upgrading'
        ]
        
        # Remove process_ prefix and convert to lower case for comparison
        task_name_lower = task_name.lower()
        if task_name_lower.startswith('process_'):
            task_name_lower = task_name_lower[8:]  # Remove 'process_'
        elif task_name_lower.startswith('task_'):
            task_name_lower = task_name_lower[5:]  # Remove 'task_'
            
        # Check if this task name (without prefix) matches any queue task
        for queue_task in queue_tasks:
            queue_task_lower = queue_task.lower()[8:]  # Remove 'process_' prefix
            if task_name_lower == queue_task_lower:
                try:
                    queue_method = getattr(self.queue_manager, queue_task)  # Use original queue task name
                    # Check if the method is process_checking and pass self if it is
                    if queue_task == 'process_checking':
                        queue_method(self) # Pass self (ProgramRunner instance)
                    else:
                        queue_method()
                    logging.info(f"Manually triggered queue task: {queue_task}")
                    return
                except Exception as e:
                    logging.error(f"Error running queue task {queue_task}: {str(e)}")
                    raise
                    
        # Handle content source tasks
        if '_wanted' in task_name_lower or ' wanted' in task_name_lower:
            content_sources = self.get_content_sources()
            source_id = None
            
            # Try to match the task name to a content source
            task_name_parts = task_name_lower.replace('task_', '').replace('_wanted', ' wanted').split(' wanted')[0]
            
            for source in content_sources:
                if source.lower() == task_name_parts:
                    source_id = source
                    break
                    
            if source_id and source_id in content_sources:
                try:
                    self.process_content_source(source_id, content_sources[source_id])
                    logging.info(f"Manually triggered content source: {source_id}")
                    return
                except Exception as e:
                    logging.error(f"Error running content source {source_id}: {str(e)}")
                    raise
        
        # If we get here, it's a regular task - ensure it has task_ prefix
        if not task_name.startswith('task_'):
            task_name = f'task_{task_name}'
            
        task_method = getattr(self, task_name, None)
        if task_method is None:
            raise ValueError(f"Task {task_name} does not exist")
            
        try:
            task_method()
            logging.info(f"Manually triggered task: {task_name}")
        except Exception as e:
            logging.error(f"Error running task {task_name}: {str(e)}")
            raise
            
    def enable_task(self, task_name):
        """Enable a task that was previously disabled."""
        # Normalize task name to match how it's stored in enabled_tasks
        normalized_name = self._normalize_task_name(task_name)
        
        if normalized_name in self.enabled_tasks:
            logging.info(f"Task {normalized_name} is already enabled")
            return True
            
        # Add to enabled tasks
        self.enabled_tasks.add(normalized_name)
        logging.info(f"Enabled task: {normalized_name}")
        return True
        
    def disable_task(self, task_name):
        """Disable a task to prevent it from running."""
        # Normalize task name to match how it's stored in enabled_tasks
        normalized_name = self._normalize_task_name(task_name)
        
        if normalized_name not in self.enabled_tasks:
            logging.info(f"Task {normalized_name} is already disabled")
            return True
            
        # Remove from enabled tasks
        self.enabled_tasks.remove(normalized_name)
        logging.info(f"Disabled task: {normalized_name}")
        return True
        
    def _normalize_task_name(self, task_name):
        """Normalize task name to match how it's stored in enabled_tasks."""
        task_name_normalized = task_name
        
        # Handle queue tasks (which don't have task_ prefix)
        queue_names = [
            'Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 
            'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading'
        ]
        
        # Check if this is a queue name
        for queue_name in queue_names:
            if task_name.lower() == queue_name.lower():
                return queue_name
                
        # Handle task_ prefix
        if task_name.startswith('task_'):
            task_name_normalized = task_name
        else:
            # Check if it's a content source task
            if '_wanted' in task_name.lower():
                # Content source tasks have spaces, not underscores
                task_name_normalized = task_name.replace('_', ' ')
                if not task_name_normalized.startswith('task_'):
                    task_name_normalized = f'task_{task_name_normalized}'
            else:
                # Regular task - add task_ prefix if not present
                if not task_name.startswith('task_'):
                    task_name_normalized = f'task_{task_name}'
                    
        return task_name_normalized

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

        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            logging.error("No Plex URL or token found in settings")
            return False
    
        # Initialize Plex connection centrally if possible, or handle per task run
        plex = plexapi.server.PlexServer(plex_url, plex_token)
        if not plex:
            logging.error("[VERIFY] Failed to connect to Plex for removal verification.")
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
                    logging.warning(f"[VERIFY] Could not find Plex section for path: {item_path}. Skipping verification for now.")
                    # Don't increment attempts if the section isn't found, might be temporary issue
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

                if section_type == 'movie':
                    # Search for the movie by title
                    logging.debug(f"[VERIFY DEBUG] Searching for MOVIE title: '{item_title}' in section '{plex_section.title}'")
                    search_results = plex_section.search(title=item_title, libtype='movie')
                    logging.debug(f"[VERIFY DEBUG] Movie search results count: {len(search_results)}")
                    if not search_results:
                         logging.info(f"[VERIFY] Movie title '{item_title}' not found in section '{plex_section.title}'. Verification check passed for this item (so far).")
                    else:
                        # Check if any media parts match the original filename
                        for movie in search_results:
                            logging.debug(f"[VERIFY DEBUG] Checking parts for movie: {movie.title} ({movie.key})")
                            for part in movie.iterParts():
                                 part_basename = os.path.basename(part.file)
                                 logging.debug(f"[VERIFY DEBUG] Comparing target '{target_basename}' with part '{part_basename}' (from {part.file})")
                                 if part_basename == target_basename:
                                     logging.warning(f"[VERIFY] Path '{item_path}' still found associated with Movie '{item_title}' (Part: {part.file}). Verification FAILED.")
                                     item_still_exists = True
                                     break # Found a match, no need to check other parts of this movie
                            if item_still_exists: break # Found a match, no need to check other movies with the same title

                elif section_type == 'show':
                     # Search for the show by title
                    logging.debug(f"[VERIFY DEBUG] Searching for SHOW title: '{item_title}' in section '{plex_section.title}'")
                    shows = plex_section.search(title=item_title, libtype='show')
                    logging.debug(f"[VERIFY DEBUG] Show search results count: {len(shows)}")
                    if not shows:
                        logging.info(f"[VERIFY] Show title '{item_title}' not found in section '{plex_section.title}'. Verification check passed for this item (so far).")
                    else:
                        # Check all shows matching the title (rare, but possible)
                        for show in shows:
                            logging.debug(f"[VERIFY DEBUG] Found show: {show.title} ({show.key})")
                            # If episode_title is provided, search for the specific episode
                            if episode_title:
                                logging.debug(f"[VERIFY DEBUG] Searching for EPISODE title: '{episode_title}' within show '{show.title}'")
                                try:
                                    # Use show.episode() which handles season/episode numbers or titles
                                    episode = show.episode(title=episode_title)
                                    logging.debug(f"[VERIFY DEBUG] Found episode: {episode.title} ({episode.key})")
                                    for part in episode.iterParts():
                                        part_basename = os.path.basename(part.file)
                                        logging.debug(f"[VERIFY DEBUG] Comparing target '{target_basename}' with part '{part_basename}' (from {part.file})")
                                        if part_basename == target_basename:
                                            logging.warning(f"[VERIFY] Path '{item_path}' still found associated with Episode '{item_title} - {episode_title}' (Part: {part.file}). Verification FAILED.")
                                            item_still_exists = True
                                            break # Found match, stop checking parts
                                except NotFound:
                                    logging.info(f"[VERIFY] Episode '{episode_title}' not found for show '{show.title}'. Verification check passed for this episode (so far).")
                                except Exception as e:
                                     logging.error(f"[VERIFY] Error searching for episode '{episode_title}' in show '{show.title}': {e}")
                                     # Treat error as potentially still existing to be safe
                                     item_still_exists = True 
                            else:
                                # If no episode title, maybe it was a whole show removal? Check all episodes (less common case)
                                logging.warning(f"[VERIFY DEBUG] No episode title provided for show '{item_title}' path '{item_path}'. Checking ALL episode parts (this might be slow).")
                                for episode in show.episodes():
                                     logging.debug(f"[VERIFY DEBUG] Checking parts for episode: {episode.title} ({episode.key})")
                                     for part in episode.iterParts():
                                         part_basename = os.path.basename(part.file)
                                         logging.debug(f"[VERIFY DEBUG] Comparing target '{target_basename}' with part '{part_basename}' (from {part.file})")
                                         if part_basename == target_basename:
                                             logging.warning(f"[VERIFY] Path '{item_path}' still found associated with Show '{item_title}' (Episode: {episode.title}, Part: {part.file}). Verification FAILED.")
                                             item_still_exists = True
                                             break # Found match, stop checking parts
                                     if item_still_exists: break # Stop checking episodes for this show
                            # If match found in this show, no need to check other shows with same title
                            if item_still_exists: break 

                else:
                    logging.warning(f"[VERIFY] Unknown section type '{section_type}' for section '{plex_section.title}'. Cannot verify path {item_path}.")
                    # Don't increment attempts for unknown section types
                
                # Log the final decision logic before potential errors
                logging.debug(f"[VERIFY DEBUG] Pre-update check for ID {item_id}: item_still_exists = {item_still_exists}")

            except Exception as e:
                logging.error(f"[VERIFY] Error during Plex verification processing for path {item_path} (ID: {item_id}): {e}", exc_info=True)
                # Increment attempts on general errors during processing
                increment_removal_attempt(item_id)
                failed_verification_count +=1 # Treat errors as failed attempts for now
                continue # Skip to next item

            # Update status based on whether the item was found
            try:
                if not item_still_exists:
                    logging.info(f"[VERIFY] Path '{item_path}' appears removed from Plex metadata based on title/part search. Marking as Verified.")
                    update_removal_status(item_id, 'Verified')
                    verified_count += 1
                else:
                    logging.warning(f"[VERIFY] Path '{item_path}' still found in Plex based on title/part search. Attempting removal...")
                    # Attempt removal again
                    removal_successful = remove_symlink_from_plex(item_title, item_path, episode_title)
                    if removal_successful:
                        logging.info(f"[VERIFY] Successfully triggered removal for '{item_path}'. Will verify again later.")
                    else:
                        logging.error(f"[VERIFY] Failed to trigger removal again for '{item_path}'.")
                    
                    # Increment attempt count regardless of removal attempt success
                    logging.warning(f"[VERIFY] Incrementing attempt count for '{item_path}'.")
                    increment_removal_attempt(item_id)
                    failed_verification_count += 1 
            except Exception as db_update_err:
                 logging.error(f"[VERIFY] Database error updating status/attempts for ID {item_id}: {db_update_err}", exc_info=True)

        logging.info(f"[VERIFY] Plex removal verification task finished. Verified: {verified_count}, Failed/Pending: {failed_verification_count}.")

        # Clean up verified entries older than a certain period (optional, good practice)
        # cleanup_days is now defined above using get_setting
        if cleanup_days > 0:
            logging.info(f"[VERIFY] Cleaning up verified entries older than {cleanup_days} days.")
            # Use remove_verified_paths (assuming it replaced cleanup_old_verified_removals)
            removed_count = cleanup_old_verified_removals(days=cleanup_days) # Corrected function name and parameter
            logging.info(f"[VERIFY] Removed {removed_count} old verified entries.")

    def task_precompute_airing_shows(self):
        """Precompute the recently aired and airing soon shows in a background task"""
        try:
            from routes.statistics_routes import get_recently_aired_and_airing_soon
            
            # Actually call the function to populate the cache
            logging.info("Precomputing airing shows data...")
            start_time = time.time()
            recently_aired, airing_soon = get_recently_aired_and_airing_soon()
            
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
            else: # Symlinked/Local
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

    # --- START EDIT: New method for dynamic adjustment ---
    def apply_dynamic_interval_adjustment(self, task_name: str, duration: float):
        """
        Adjusts the interval for a task based on its execution duration.
        Increases interval if duration exceeds 10% of current interval.
        Resets interval if duration is within threshold and interval was previously increased.
        """
        # Check if this task is eligible for dynamic adjustment
        # Also include content source tasks in this logic
        is_dynamic_eligible = task_name in self.DYNAMIC_INTERVAL_TASKS or \
                              (task_name.startswith('task_') and task_name.endswith('_wanted'))

        if not is_dynamic_eligible:
            return # Not eligible for this adjustment type

        current_interval = self.task_intervals.get(task_name)
        original_interval = self.original_task_intervals.get(task_name)

        if current_interval is None or original_interval is None:
            logging.warning(f"Cannot apply dynamic interval adjustment for task '{task_name}': Missing interval data.")
            return

        # Avoid division by zero or negative intervals
        if current_interval <= 0:
             return

        threshold = current_interval * 0.10

        if duration > threshold:
            # Task took longer than 10% of its interval, increase the interval (double it)
            new_interval = current_interval * 2

            # Apply maximum caps
            max_interval_by_multiplier = original_interval * self.MAX_INTERVAL_MULTIPLIER
            capped_interval = min(new_interval, max_interval_by_multiplier, self.ABSOLUTE_MAX_INTERVAL)

            if capped_interval > current_interval: # Only update if it actually increases
                 self.task_intervals[task_name] = capped_interval
                 logging.info(f"Task '{task_name}' took {duration:.2f}s (> {threshold:.2f}s threshold). Increasing interval to {capped_interval}s.")
            elif new_interval > current_interval: # Log even if capped, indicating cap was hit
                 logging.info(f"Task '{task_name}' took {duration:.2f}s (> {threshold:.2f}s threshold). Interval increase capped at {capped_interval}s.")

        elif current_interval != original_interval:
            # Task was fast enough and interval was previously increased, reset to default
            self.task_intervals[task_name] = original_interval
            logging.info(f"Task '{task_name}' took {duration:.2f}s (<= {threshold:.2f}s threshold). Resetting interval to default {original_interval}s.")
    # --- END EDIT ---

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
                        for season_num, season_data in show_metadata.get('seasons', {}).items():
                            if int(season_num) == 0: continue # Skip specials
                            total_episodes_from_source += len(season_data.get('episodes', {}))
                    else:
                            logging.warning(f"[TV Status] Metadata for {imdb_id} ('{title}') lacks 'seasons' key. Total episode count may be inaccurate.")
                            # Fallback to DB value if exists? Or treat as 0? Let's fetch existing.
                            cursor.execute("SELECT total_episodes FROM tv_shows WHERE imdb_id = ?", (imdb_id,))
                            existing_show = cursor.fetchone()
                            total_episodes_from_source = existing_show['total_episodes'] or 0
                            if total_episodes_from_source == 0:
                                logging.warning(f"[TV Status] No episode count from metadata or DB for {imdb_id}. Skipping version status calculation.")
                                # We can still update show status, but version logic is impossible
                                # Let the main show update proceed, but skip version logic later


                    # Determine overall show ended status based *only* on metadata status
                    # Treat 'canceled' the same as 'ended' for completion purposes
                    is_show_ended = bool(show_status in ('ended', 'canceled'))

                    logging.debug(f"[TV Status] Show: {imdb_id} ('{title}') - Status: {show_status}, Source Episodes: {total_episodes_from_source}, IsEnded/Canceled: {is_show_ended}")

                    # Prepare data for tv_shows DB update/insert
                    now_utc = datetime.now(timezone.utc)
                    now_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')

                    # Upsert into tv_shows. 'is_complete' only reflects if the show's status is 'ended'.
                    # total_episodes is updated from source metadata.
                    # Ensure COALESCE is used for fields that might not be present in new metadata fetch
                    cursor.execute("""
                        INSERT INTO tv_shows (
                            imdb_id, tmdb_id, title, year, status, is_complete,
                            total_episodes, last_status_check, added_at, last_updated
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(imdb_id) DO UPDATE SET
                            tmdb_id = COALESCE(excluded.tmdb_id, tv_shows.tmdb_id),
                            title = COALESCE(excluded.title, tv_shows.title),
                            year = COALESCE(excluded.year, tv_shows.year),
                            status = COALESCE(excluded.status, tv_shows.status),
                            is_complete = excluded.is_complete, -- Set based on show_status=='ended'
                            total_episodes = excluded.total_episodes,
                            last_status_check = excluded.last_status_check,
                            last_updated = excluded.last_updated
                        WHERE imdb_id = excluded.imdb_id;
                    """, (
                        imdb_id, tmdb_id, title, year, show_status, int(is_show_ended),
                        total_episodes_from_source, now_str, now_str, now_str # last_status_check, added_at, last_updated
                    ))
                    conn.commit() # Commit show data before processing versions

                    # --- NEW: Per-Version Status Update ---
                    # Skip if we couldn't determine total episodes
                    if total_episodes_from_source <= 0 and is_show_ended:
                         logging.warning(f"[TV Status] Cannot reliably calculate version completeness for ended show {imdb_id} due to zero total episodes. Skipping version updates.")
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
                                    is_up_to_date = bool(
                                        total_episodes_from_source > 0 and
                                        present_count >= total_episodes_from_source
                                    )

                                    # Determine if this version is complete AND fully present
                                    # Requires the show to be ended/canceled AND have enough present episodes
                                    is_complete_and_present = bool(
                                        is_show_ended and is_up_to_date # Simplified using is_up_to_date
                                    )
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
                                cursor.execute("""
                                    DELETE FROM tv_show_version_status
                                    WHERE imdb_id = ? AND version_identifier NOT IN ({})
                                """.format(','.join('?'*len(versions_processed_this_show))), (imdb_id, *versions_processed_this_show))

                                shows_with_versions_updated.add(imdb_id)

                            conn.commit() # Commit version status updates for this show

                        except sqlite3.Error as db_err_version:
                            logging.error(f"[TV Status] Database error during version status update for {imdb_id}: {db_err_version}", exc_info=True)
                            if conn: conn.rollback()
                            failed_count += 1 # Count show as failed if version update fails
                            # Ensure we don't count it as successfully processed below
                            if imdb_id in shows_with_versions_updated:
                                shows_with_versions_updated.remove(imdb_id)
                        except Exception as e_version:
                            logging.error(f"[TV Status] Error during version status update for {imdb_id}: {e_version}", exc_info=True)
                            if conn: conn.rollback()
                            failed_count += 1 # Count show as failed if version update fails
                             # Ensure we don't count it as successfully processed below
                            if imdb_id in shows_with_versions_updated:
                                shows_with_versions_updated.remove(imdb_id)

                    processed_shows.add(imdb_id) # Mark base show info as processed

                except Exception as e:
                    logging.error(f"[TV Status] Failed to process show {imdb_id}: {e}", exc_info=True)
                    failed_count += 1
                    if conn: conn.rollback() # Rollback any partial changes for this show
                    # Ensure we don't process this ID again in this run if it failed
                    processed_shows.add(imdb_id)
                    # Also ensure it's not counted as successfully updated for versions
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
        # Refined counting: processed shows = total unique imdb_ids attempted.
        # successful updates = shows where version status was processed without error (or skipped cleanly)
        successful_updates = len(shows_with_versions_updated) + (len(processed_shows) - failed_count - len(shows_with_versions_updated))
        logging.info(f"[TASK] TV show status update finished in {duration:.2f}s. Processed Shows: {len(processed_shows)}, Successful Updates (incl. versions): {successful_updates}, Failed: {failed_count}.")
    # --- END: New Task Implementation ---

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

    if not media_type or not tmdb_id:
        logging.error("Invalid webhook data: missing media_type or tmdbId")
        return

    wanted_item = {
        'tmdb_id': tmdb_id,
        'media_type': media_type
    }

    # Add requested_seasons if present in media data
    if media_type == 'tv' and media.get('requested_seasons'):
        wanted_item['requested_seasons'] = media['requested_seasons']
        logging.info(f"Added requested seasons to wanted item: {media['requested_seasons']}")
    else:
        logging.debug(f"No requested seasons found in media data: {media}")

    wanted_content = [wanted_item]
    logging.debug(f"Processing wanted content with item: {wanted_item}")
    from metadata.metadata import process_metadata
    wanted_content_processed = process_metadata(wanted_content)
    if wanted_content_processed:
        # Get the versions for Overseerr from settings
        content_sources = ProgramRunner().get_content_sources(force_refresh=True)
        overseerr_settings = next((data for source, data in content_sources.items() if source.startswith('Overseerr')), {})
        versions = overseerr_settings.get('versions', {})
        
        logging.info(f"Versions: {versions}")

        all_items = wanted_content_processed.get('movies', []) + wanted_content_processed.get('episodes', []) + wanted_content_processed.get('anime', [])
        for item in all_items:
            item['content_source'] = 'overseerr_webhook'
            from content_checkers.content_source_detail import append_content_source_detail
            item = append_content_source_detail(item, source_type='Overseerr')

        from database import add_collected_items, add_wanted_items
        add_wanted_items(all_items, versions)
        logging.info(f"Processed and added wanted item from webhook: {wanted_item}")

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

    current_datetime = datetime.now()
    report = []

    logging.info(f"Movie airtime offset: {get_setting('Queue', 'movie_airtime_offset', '19')}")
    logging.info(f"Episode airtime offset: {get_setting('Queue', 'episode_airtime_offset', '0')}")

    for item in items:
        item_id, title, item_type, release_date, airtime, state = item
        
        if not release_date or release_date.lower() == "unknown":
            report.append(f"{title} ({item_type}): Unknown release date")
            continue

        try:
            release_date = datetime.strptime(release_date, '%Y-%m-%d').date()
        except ValueError:
            report.append(f"{title} ({item_type}): Invalid release date format")
            continue

        if item_type == 'movie':
            airtime_offset = (float(get_setting("Queue", "movie_airtime_offset", "19"))*60)
            airtime = datetime.strptime("00:00", '%H:%M').time()
        elif item_type == 'episode':
            airtime_offset = (float(get_setting("Queue", "episode_airtime_offset", "0"))*60)
            airtime = datetime.strptime(airtime or "00:00", '%H:%M').time()
        else:
            airtime_offset = 0
            airtime = datetime.now().time()

        release_datetime = datetime.combine(release_date, airtime)
        scrape_datetime = release_datetime + timedelta(minutes=airtime_offset)
        time_until_scrape = scrape_datetime - current_datetime

        if time_until_scrape > timedelta(0):
            report.append(f"{title} ({item_type}): Start scraping at {scrape_datetime}, in {time_until_scrape}")
        else:
            report.append(f"{title} ({item_type}): Ready to scrape (Release date: {release_date}, Current state: {state})")

    conn.close()

    # Log the report
    logging.info("Airtime Report:\n" + "\n".join(report))

def append_runtime_airtime(items):
    from metadata.metadata import get_runtime, get_episode_airtime # Added import here
    logging.info(f"Starting to append runtime and airtime for {len(items)} items")
    for index, item in enumerate(items, start=1):
        imdb_id = item.get('imdb_id')
        media_type = item.get('type')
        
        if not imdb_id or not type:
            logging.warning(f"Item {index} is missing imdb_id or type: {item}")
            continue
        
        try:
            if media_type == 'movie':
                runtime = get_runtime(imdb_id, 'movie')
                item['runtime'] = runtime
            elif media_type == 'episode':
                runtime = get_runtime(imdb_id, 'episode')
                airtime = get_episode_airtime(imdb_id)
                item['runtime'] = runtime
                item['airtime'] = airtime
            else:
                logging.warning(f"Unknown media type for item {index}: {media_type}")
        except Exception as e:
            logging.error(f"Error processing item {index} (IMDb: {imdb_id}): {str(e)}")
            logging.error(f"Item details: {item}")
            logging.error(traceback.format_exc())
    
def get_and_add_all_collected_from_plex(bypass=False):
    collected_content = None  # Initialize here
    if get_setting('File Management', 'file_collection_management') == 'Plex' or bypass:
        logging.info("Getting all collected content from Plex")
        if bypass:
            collected_content = asyncio.run(run_get_collected_from_plex(bypass=True))
        else:
            collected_content = asyncio.run(run_get_collected_from_plex())

    if collected_content:
        movies = collected_content['movies']
        episodes = collected_content['episodes']
        
        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes")
        
        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            from database import add_collected_items, add_wanted_items
            add_collected_items(movies + episodes)
            return collected_content  # Return the original content even if some items were skipped
        
    logging.error("Failed to retrieve content")
    return None

def get_and_add_recent_collected_from_plex():
    if get_setting('File Management', 'file_collection_management') == 'Plex':
        logging.info("Getting recently added content from Plex")
        collected_content = asyncio.run(run_get_recent_from_plex())
    elif get_setting('File Management', 'file_collection_management') == 'Zurg':
        logging.info("Getting recently added content from Zurg")
        collected_content = asyncio.run(run_get_recent_from_zurg())
    
    if collected_content:
        movies = collected_content['movies']
        episodes = collected_content['episodes']
        
        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes")
        
        # Check and fix any unmatched items before adding to database if enabled
        if get_setting('Debug', 'enable_unmatched_items_check', True):
            logging.info("Checking and fixing unmatched items before adding to database")
            from utilities.plex_matching_functions import check_and_fix_unmatched_items
            collected_content = check_and_fix_unmatched_items(collected_content)
            # Get updated counts after matching check
            movies = collected_content['movies']
            episodes = collected_content['episodes']
        
        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            from database import add_collected_items
            add_collected_items(movies + episodes, recent=True)
            return collected_content  # Return the original content even if some items were skipped
    
    logging.error("Failed to retrieve content")
    return None

def run_local_library_scan():
    from utilities.local_library_scan import local_library_scan
    logging.info("Full library scan disabled for now")
    #local_library_scan()

def run_recent_local_library_scan():
    from utilities.local_library_scan import recent_local_library_scan
    logging.info("Recent library scan disabled for now")
    #recent_local_library_scan()

def run_program():
    global program_runner
    logging.info("Program started")

    if program_runner is None or not program_runner.is_running():
        program_runner = ProgramRunner()
        # Update the program runner in program_operation_routes
        from routes.program_operation_routes import program_operation_bp
        program_operation_bp.program_runner = program_runner
        program_runner.start()  # Start the program runner
    else:
        logging.info("Program is already running")
    return program_runner

if __name__ == "__main__":
    run_program()
