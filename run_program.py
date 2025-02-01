import logging
import random
import time
import os
import sqlite3
from initialization import initialize
from settings import get_setting, get_all_settings
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss, get_wanted_from_friends_plex_rss
from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist, get_wanted_from_other_plex_watchlist, validate_plex_tokens
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist, get_wanted_from_trakt_collection
from metadata.metadata import process_metadata, refresh_release_dates, get_runtime, get_episode_airtime
from content_checkers.mdb_list import get_wanted_from_mdblists
from database import add_collected_items, add_wanted_items
from not_wanted_magnets import purge_not_wanted_magnets_file
import traceback
from datetime import datetime, timedelta
from database import get_db_connection
import asyncio
from utilities.plex_functions import run_get_collected_from_plex, run_get_recent_from_plex
from notifications import send_notifications
import requests
from pathlib import Path
import pickle
from utilities.zurg_utilities import run_get_collected_from_zurg, run_get_recent_from_zurg
import ntplib
from content_checkers.trakt import check_trakt_early_releases
from debrid.base import TooManyDownloadsError, RateLimitError
import tempfile
from api_tracker import api  # Add this import for the api module
from plexapi.server import PlexServer
from database.core import get_db_connection

queue_logger = logging.getLogger('queue_logger')
program_runner = None

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
        
        from queue_manager import QueueManager
        
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
            'Wanted': 5,
            'Scraping': 5,
            'Adding': 5,
            'Checking': 300,
            'Sleeping': 900,
            'Unreleased': 3600,
            'Blacklisted': 3600,
            'Pending Uncached': 3600,
            'Upgrading': 3600,
            'task_plex_full_scan': 3600,
            #'task_debug_log': 60,
            'task_refresh_release_dates': 3600,
            #'task_purge_not_wanted_magnets_file': 604800,
            'task_generate_airtime_report': 3600,
            'task_check_service_connectivity': 60,
            'task_send_notifications': 15,  # Run every 0.25 minutes (15 seconds)
            'task_sync_time': 3600,  # Run every hour
            'task_check_trakt_early_releases': 3600,  # Run every hour
            'task_reconcile_queues': 300,  # Run every 5 minutes
            'task_heartbeat': 120,  # Run every 2 minutes
            #'task_local_library_scan': 900,  # Run every 5 minutes
            'task_refresh_download_stats': 300,  # Run every 5 minutes
            'task_check_plex_files': 60,  # Run every 60 seconds
            #'task_update_show_ids': 3600,  # Run every hour
            'task_get_plex_watch_history': 24 * 60 * 60,  # Run every 24 hours
            'task_refresh_plex_tokens': 24 * 60 * 60,  # Run every 24 hours
        }
        self.start_time = time.time()
        self.last_run_times = {task: self.start_time for task in self.task_intervals}
        
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
            #'task_update_show_ids'
            'task_refresh_plex_tokens'
        }

        if get_setting('File Management', 'file_collection_management') == 'Plex':
            self.enabled_tasks.add('task_plex_full_scan')
        else:
            self.enabled_tasks.add('task_local_library_scan')

        if get_setting('File Management', 'file_collection_management') == 'Plex' and get_setting('Plex', 'update_plex_on_file_discovery'):
            self.enabled_tasks.add('task_check_plex_files')

        if get_setting('Debug', 'not_add_plex_watch_history_items_to_queue', False):
            self.enabled_tasks.add('task_get_plex_watch_history')
        
        # Add this line to store content sources
        self.content_sources = None

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
                'My Friends Plex RSS Watchlist': 900
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
        current_time = time.time()
        time_since_last_run = current_time - self.last_run_times[task_name]
        should_run = time_since_last_run >= self.task_intervals[task_name]
        if should_run:
            self.last_run_times[task_name] = current_time
            self.currently_running_tasks.add(task_name)  # Mark task as running
        return should_run

    def task_check_service_connectivity(self):
        # logging.debug("Checking service connectivity")
        from routes.program_operation_routes import check_service_connectivity
        if check_service_connectivity():
            logging.debug("Service connectivity check passed")
        else:
            logging.error("Service connectivity check failed")
            self.handle_connectivity_failure()

    def handle_connectivity_failure(self):
        from routes.program_operation_routes import stop_program, check_service_connectivity
        from extensions import app  # Import the Flask app

        logging.warning("Pausing program queue due to connectivity failure")
        self.pause_reason = "Connectivity failure - waiting for services to be available"
        self.pause_queue()
        
        # Set the initial failure time if not already set
        if not self.connectivity_failure_time:
            self.connectivity_failure_time = time.time()
            self.connectivity_retry_count = 0

    def check_connectivity_status(self):
        """Check connectivity status during normal program cycle"""
        from routes.program_operation_routes import stop_program, check_service_connectivity
        from extensions import app

        if not self.connectivity_failure_time:
            return

        current_time = time.time()
        time_since_failure = current_time - self.connectivity_failure_time
        
        # Check every minute
        if time_since_failure >= 60 * (self.connectivity_retry_count + 1):
            self.connectivity_retry_count += 1
            
            try:
                if check_service_connectivity():
                    logging.info("Service connectivity restored")
                    self.connectivity_failure_time = None
                    self.connectivity_retry_count = 0
                    self.resume_queue()
                    return
            except Exception as e:
                logging.error(f"Error checking connectivity: {str(e)}")

            logging.warning(f"Service connectivity check failed. Retry {self.connectivity_retry_count}/5")
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
        from queue_manager import QueueManager
        
        QueueManager().pause_queue()
        self.queue_paused = True
        logging.info("Queue paused")

    def resume_queue(self):
        from queue_manager import QueueManager

        QueueManager().resume_queue()
        self.queue_paused = False
        self.pause_reason = None  # Clear pause reason on resume
        logging.info("Queue resumed")

    # Update this method to use the cached content sources
    def process_queues(self):
        try:
            # Check connectivity status if we're in a failure state
            self.check_connectivity_status()
            
            # Remove excessive debug logging at start of cycle
            self.update_heartbeat()
            self.check_heartbeat()
            self.check_task_health()
            current_time = time.time()
            
            # Update all queues from database
            self.queue_manager.update_all_queues()
            
            # Process queue tasks
            for queue_name in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading']:
                should_run = self.should_run_task(queue_name)
                time_since_last = current_time - self.last_run_times[queue_name]
                # Remove per-queue debug logging unless it's going to run
                if should_run:
                    # logging.debug(f"Queue {queue_name}: Time since last run: {time_since_last:.2f}s")
                    # logging.info(f"Processing {queue_name} queue")
                    self.safe_process_queue(queue_name)

            # Process content source tasks
            for source, data in self.get_content_sources().items():
                task_name = f'task_{source}_wanted'
                should_run = self.should_run_task(task_name)
                time_since_last = current_time - self.last_run_times[task_name]
                # Remove content source debug logging unless it's going to run
                if should_run:
                    # logging.debug(f"Content source {source}: Time since last run: {time_since_last:.2f}s")
                    try:
                        self.process_content_source(source, data)
                    except Exception as e:
                        logging.error(f"Error processing content source {source}: {str(e)}")
                        logging.error(traceback.format_exc())
                    finally:
                        self.currently_running_tasks.discard(task_name)
            
            # Process other enabled tasks
            for task_name in self.enabled_tasks:
                if (task_name not in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading'] 
                    and not task_name.endswith('_wanted')):
                    if self.should_run_task(task_name):
                        # Only log when task will actually run
                        # logging.debug(f"Running task: {task_name}")
                        try:
                            task_method = getattr(self, task_name)
                            task_method()
                        except Exception as e:
                            logging.error(f"Error running task {task_name}: {str(e)}")
                            logging.error(traceback.format_exc())
                        finally:
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
        
    def process_content_source(self, source, data):
        source_type = source.split('_')[0]
        versions = data.get('versions', {})

        logging.debug(f"Processing content source: {source} (type: {source_type})")

        try:
            wanted_content = []
            if source_type == 'Overseerr':
                wanted_content = get_wanted_from_overseerr()
            elif source_type == 'MDBList':
                mdblist_urls = data.get('urls', '').split(',')
                for mdblist_url in mdblist_urls:
                    mdblist_url = mdblist_url.strip()
                    wanted_content.extend(get_wanted_from_mdblists(mdblist_url, versions))
            elif source_type == 'Trakt Watchlist':
                try:
                    wanted_content = get_wanted_from_trakt_watchlist()
                except (ValueError, api.exceptions.RequestException) as e:
                    logging.error(f"Failed to fetch Trakt watchlist: {str(e)}")
                    # Don't raise here - allow other content sources to be processed
                    return
            elif source_type == 'Trakt Lists':
                trakt_lists = data.get('trakt_lists', '').split(',')
                for trakt_list in trakt_lists:
                    trakt_list = trakt_list.strip()
                    try:
                        wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions))
                    except (ValueError, api.exceptions.RequestException) as e:
                        logging.error(f"Failed to fetch Trakt list {trakt_list}: {str(e)}")
                        # Continue to next list instead of failing completely
                        continue
            elif source_type == 'Trakt Collection':
                wanted_content = get_wanted_from_trakt_collection()
            elif source_type == 'Collected':
                wanted_content = get_wanted_from_collected()
            elif source_type == 'My Plex Watchlist':
                wanted_content = get_wanted_from_plex_watchlist(versions)
            elif source_type == 'My Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions)
            elif source_type == 'My Friends Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_friends_plex_rss(plex_rss_url, versions)
            elif source_type == 'Other Plex Watchlist':
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

            if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                # For tuple-based content sources (like Plex), count actual items in tuples
                total_items_found = sum(len(items) for items, _ in wanted_content)
                logging.debug(f"Retrieved wanted content from {source}: {total_items_found} items")
            else:
                logging.debug(f"Retrieved wanted content from {source}: {len(wanted_content)} items")

            if wanted_content:
                total_items = 0
                if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                    # Handle list of tuples
                    for items, item_versions in wanted_content:
                        try:
                            processed_items = process_metadata(items)
                            if processed_items:
                                all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                                for item in all_items:
                                    item['content_source'] = source
                                add_wanted_items(all_items, item_versions or versions)
                                total_items += len(all_items)
                        except Exception as e:
                            logging.error(f"Error processing items from {source}: {str(e)}")
                            logging.error(traceback.format_exc())
                else:
                    # Handle single list of items
                    try:
                        processed_items = process_metadata(wanted_content)
                        if processed_items:
                            all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                            for item in all_items:
                                item['content_source'] = source
                            add_wanted_items(all_items, versions)
                            total_items += len(all_items)
                    except Exception as e:
                        logging.error(f"Error processing items from {source}: {str(e)}")
                        logging.error(traceback.format_exc())
                
                logging.info(f"Added {total_items} wanted items from {source}")
            else:
                logging.warning(f"No wanted content retrieved from {source}")

        except Exception as e:
            logging.error(f"Error processing content source {source}: {str(e)}")
            logging.error(traceback.format_exc())
            # Don't re-raise - allow other content sources to continue processing

    def task_refresh_release_dates(self):
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
            
            while self.running:
                try:
                    cycle_start = time.time()
                    #logging.debug("Starting main program cycle")
                    
                    # Log program state
                    #logging.debug(f"Program state - Running: {self.running}, Initializing: {self.initializing}")
                    
                    self.process_queues()
                    
                    cycle_duration = time.time() - cycle_start
                    #logging.debug(f"Completed main program cycle in {cycle_duration:.2f} seconds")
                    
                    # Check queue manager state
                    if hasattr(self, 'queue_manager'):
                        paused = self.queue_manager.is_paused()
                        current_time = time.time()
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
                finally:
                    time.sleep(1)  # Main loop runs every second

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

    def check_task_health(self):
        current_time = time.time()
        for task, last_run_time in self.last_run_times.items():
            time_since_last_run = current_time - last_run_time
            # Only log task health at debug level if there's an issue
            if time_since_last_run > self.task_intervals[task] * 2:
                # logging.warning(f"Task {task} hasn't run in {time_since_last_run:.2f} seconds (should run every {self.task_intervals[task]} seconds)")
                self.last_run_times[task] = current_time

    def task_check_trakt_early_releases(self):
        check_trakt_early_releases()

    def update_heartbeat(self):
        """Update the heartbeat file directly."""
        import os

        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        heartbeat_file = os.path.join(db_content_dir, 'program_heartbeat')
        
        try:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
            
            # Write directly to the heartbeat file
            with open(heartbeat_file, 'w') as f:
                f.write(str(int(time.time())))
                f.flush()
                os.fsync(f.fileno())
        except (IOError, OSError) as e:
            logging.error(f"Failed to update heartbeat file: {e}")

    def check_heartbeat(self):
        """Check heartbeat file with proper error handling."""
        import os

        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        heartbeat_file = os.path.join(db_content_dir, 'program_heartbeat')
        
        if not os.path.exists(heartbeat_file):
            logging.warning("Heartbeat file does not exist - creating new one")
            self.update_heartbeat()
            return True

        try:
            with open(heartbeat_file, 'r') as f:
                last_heartbeat = int(f.read().strip())
                current_time = int(time.time())
                time_diff = current_time - last_heartbeat

                #logging.debug(f"Time since last heartbeat: {time_diff} seconds")
                
                # If more than 5 minutes have passed since last heartbeat
                if time_diff > 300:
                    logging.warning(f"Stale heartbeat detected - {time_diff} seconds since last update")
                    return False
                
                return True
        except (IOError, OSError, ValueError) as e:
            logging.error(f"Error checking heartbeat: {e}")
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

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            # Get all items in Checking state
            cursor.execute("""
                SELECT * FROM media_items 
                WHERE state = 'Checking' 
                AND filled_by_file IS NOT NULL
            """)
            checking_items = cursor.fetchall()
            
            for checking_item in checking_items:
                if not checking_item['filled_by_file']:
                    continue

                # Find matching items with the same filled_by_file
                cursor.execute("""
                    SELECT * FROM media_items 
                    WHERE filled_by_file = ? 
                    AND id != ? 
                    AND state != 'Checking'
                """, (checking_item['filled_by_file'], checking_item['id']))
                matching_items = cursor.fetchall()

                for matching_item in matching_items:
                    # Log the reconciliation
                    reconciliation_logger.info(
                        f"Reconciling items:\n"
                        f"  Checking Item: ID={checking_item['id']}, Title={checking_item['title']}, "
                        f"Type={checking_item['type']}, File={checking_item['filled_by_file']}\n"
                        f"  Matching Item: ID={matching_item['id']}, Title={matching_item['title']}, "
                        f"State={matching_item['state']}, Type={matching_item['type']}"
                    )

                    # Update the checking item to Collected state with timestamp
                    cursor.execute("""
                        UPDATE media_items 
                        SET state = 'Collected', 
                            collected_at = ? 
                        WHERE id = ?
                    """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), checking_item['id']))

                    # Delete the matching item
                    cursor.execute('DELETE FROM media_items WHERE id = ?', (matching_item['id'],))
                    
                    reconciliation_logger.info(
                        f"Updated checking item (ID={checking_item['id']}) to Collected state and "
                        f"deleted matching item (ID={matching_item['id']})"
                    )

            conn.commit()
            logging.info("Queue reconciliation completed successfully")
            
        except Exception as e:
            logging.error(f"Error during queue reconciliation: {str(e)}")
            conn.rollback()
        finally:
            conn.close()

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

    def task_refresh_plex_tokens():
        logging.info("Performing periodic Plex token validation")
        token_status = validate_plex_tokens()
        for username, status in token_status.items():
            if not status['valid']:
                logging.error(f"Invalid Plex token detected during periodic check for user {username}")

    def task_check_plex_files(self):
        """Check for new files in Plex location and update libraries"""
        if not get_setting('Plex', 'update_plex_on_file_discovery') and not get_setting('Plex', 'disable_plex_library_checks'):
            logging.debug("Skipping Plex file check")
            return

        plex_file_location = get_setting('Plex', 'mounted_file_location', default='/mnt/zurg/__all__')
        if not os.path.exists(plex_file_location):
            logging.warning(f"Plex file location does not exist: {plex_file_location}")
            return

        # Get all media items from database that are in Checking state
        conn = get_db_connection()
        cursor = conn.cursor()
        items = cursor.execute('SELECT id, filled_by_title, filled_by_file FROM media_items WHERE state = "Checking"').fetchall()
        conn.close()
        logging.info(f"Found {len(items)} media items in Checking state to verify")

        # Check if Plex library checks are disabled
        if get_setting('Plex', 'disable_plex_library_checks', default=False):
            logging.info("Plex library checks disabled - marking found files as Collected")
            updated_items = 0
            not_found_items = 0

            for item in items:
                filled_by_title = item['filled_by_title']
                filled_by_file = item['filled_by_file']
                
                if not filled_by_title or not filled_by_file:
                    continue

                # Check if the file exists in the expected location
                file_path = os.path.join(plex_file_location, filled_by_title, filled_by_file)
                if not os.path.exists(file_path):
                    not_found_items += 1
                    logging.debug(f"File not found on disk: {file_path}")
                    continue

                logging.info(f"Found file on disk, marking as Collected: {file_path}")
                # Update item state to Collected
                conn = get_db_connection()
                cursor = conn.cursor()
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ?', (now, item['id']))
                conn.commit()
                conn.close()
                updated_items += 1

                # Send notification for collected item
                try:
                    from notifications import send_notifications
                    from routes.settings_routes import get_enabled_notifications_for_category
                    from extensions import app

                    with app.app_context():
                        response = get_enabled_notifications_for_category('collected')
                        if response.json['success']:
                            enabled_notifications = response.json['enabled_notifications']
                            if enabled_notifications:
                                # Get full item details for notification
                                from database.database_reading import get_media_item_by_id
                                item_details = get_media_item_by_id(item['id'])
                                notification_data = {
                                    'id': item['id'],
                                    'title': item_details.get('title', 'Unknown Title'),
                                    'type': item_details.get('type', 'unknown'),
                                    'year': item_details.get('year', ''),
                                    'version': item_details.get('version', ''),
                                    'season_number': item_details.get('season_number'),
                                    'episode_number': item_details.get('episode_number'),
                                    'new_state': 'Collected'
                                }
                                send_notifications([notification_data], enabled_notifications, notification_category='collected')
                except Exception as e:
                    logging.error(f"Failed to send collected notification: {str(e)}")

            logging.info(f"Plex check disabled summary: {updated_items} items marked as Collected, {not_found_items} items not found")
            if get_setting('Plex', 'disable_plex_library_checks'):
                logging.info(f"Plex library checks disabled, skipping Plex scans")
                return


        if not get_setting('Plex', 'url', default=False):
            return

        try:
            plex_url = get_setting('Plex', 'url', default='')
            plex_token = get_setting('Plex', 'token', default='')
            
            if not plex_url or not plex_token:
                logging.warning("Plex URL or token not configured")
                return

            # Connect to Plex server
            plex = PlexServer(plex_url, plex_token)
            sections = plex.library.sections()
            logging.info(f"Connected to Plex server, found {len(sections)} library sections")

            updated_sections = set()  # Track which sections we've updated
            updated_items = 0
            skipped_items = 0
            not_found_items = 0

            for item in items:
                filled_by_title = item['filled_by_title']
                filled_by_file = item['filled_by_file']
                
                if not filled_by_title or not filled_by_file:
                    continue

                # Check if the file exists in the expected location
                file_path = os.path.join(plex_file_location, filled_by_title, filled_by_file)
                if not os.path.exists(file_path):
                    not_found_items += 1
                    logging.debug(f"File not found on disk: {file_path}")
                    continue

                logging.info(f"Found file on disk: {file_path}")
                updated_items += 1

                # Update all Plex sections using their root directories
                for section in sections:
                    if section in updated_sections:
                        continue
                        
                    try:
                        for location in section.locations:
                            specific_path = os.path.join(location, filled_by_title)
                            if os.path.exists(specific_path):
                                logging.info(f"Updating Plex section '{section.title}' for path: {specific_path}")
                                section.update(path=specific_path)
                                updated_sections.add(section)
                                break
                    except Exception as e:
                        logging.error(f"Failed to update Plex section '{section.title}': {str(e)}")

            logging.info(f"Plex update summary: {updated_items} items updated, {skipped_items} items skipped (cached), {not_found_items} items not found")
            if len(updated_sections) > 0:
                logging.info("Updated Plex sections:")
                for section in updated_sections:
                    logging.info(f"  - {section.title}")

                # Send notification for Plex library update
                try:
                    from notifications import send_notifications
                    from routes.settings_routes import get_enabled_notifications_for_category
                    from extensions import app

                    with app.app_context():
                        response = get_enabled_notifications_for_category('plex')
                        if response.json['success']:
                            enabled_notifications = response.json['enabled_notifications']
                            if enabled_notifications:
                                notification_data = {
                                    'updated_items': updated_items,
                                    'skipped_items': skipped_items,
                                    'not_found_items': not_found_items,
                                    'updated_sections': [section.title for section in updated_sections],
                                    'new_state': 'PlexUpdated'
                                }
                                send_notifications([notification_data], enabled_notifications, notification_category='plex')
                except Exception as e:
                    logging.error(f"Failed to send Plex update notification: {str(e)}")

        except Exception as e:
            logging.error(f"Error in task_check_plex_files: {str(e)}")

    def task_update_show_ids(self):
        """Update show IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
        import sqlite3
        from cli_battery.app.direct_api import DirectAPI
        import os

        logging.info("Starting show ID update task (DRY RUN)")
        api = DirectAPI()

        # Connect to media_items.db
        db_path = os.path.join('/user/db_content', 'media_items.db')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            # Get all unique shows by grouping episodes
            cursor.execute("""
                SELECT 
                    title,
                    imdb_id,
                    GROUP_CONCAT(id) as episode_ids,
                    COUNT(*) as episode_count
                FROM media_items 
                WHERE type='episode'
                GROUP BY title, imdb_id
            """)
            shows = cursor.fetchall()
            
            logging.info(f"Found {len(shows)} shows to check")

            for show in shows:
                try:
                    show_title = show['title']
                    show_imdb_id = show['imdb_id']
                    episode_ids = show['episode_ids'].split(',')

                    # Get show metadata from direct API
                    metadata, source = api.get_show_metadata(show_imdb_id)
                    if not metadata:
                        logging.warning(f"No metadata found in API for show {show_title} (imdb_id: {show_imdb_id})")
                        continue

                    api_imdb_id = metadata.get('imdb_id')
                    api_tmdb_id = metadata.get('tmdb_id')

                    if api_imdb_id and api_imdb_id != show_imdb_id:
                        logging.info(f"[DRY RUN] Would update show {show_title}:")
                        logging.info(f"  IMDB ID: {show_imdb_id} -> {api_imdb_id}")
                        if api_tmdb_id:
                            logging.info(f"  TMDB ID: (updating to {api_tmdb_id})")
                        # When ready to make actual changes, uncomment these lines:
                        # cursor.execute("""
                        #     UPDATE media_items
                        #     SET imdb_id = ?, tmdb_id = ?
                        #     WHERE id IN ({})
                        # """.format(','.join('?' * len(episode_ids))), 
                        # [api_imdb_id, api_tmdb_id] + episode_ids)
                        # conn.commit()

                except Exception as e:
                    logging.error(f"Error processing show {show_title}: {str(e)}")
                    continue

        except Exception as e:
            logging.error(f"Error in task_update_show_ids: {str(e)}")
        finally:
            cursor.close()
            conn.close()

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
        add_wanted_items(all_items, versions)
        logging.info(f"Processed and added wanted item from webhook: {wanted_item}")

def generate_airtime_report():
    logging.info("Generating airtime report for wanted and unreleased items...")

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
    
def get_and_add_all_collected_from_plex():
    if get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex':
        logging.info("Getting all collected content from Plex")
        collected_content = asyncio.run(run_get_collected_from_plex())
    elif get_setting('File Management', 'file_collection_management', 'Plex') == 'Zurg':
        logging.info("Getting all collected content from Zurg")
        collected_content = asyncio.run(run_get_collected_from_zurg())

    if collected_content:
        movies = collected_content['movies']
        episodes = collected_content['episodes']
        
        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes")
        
        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            add_collected_items(movies + episodes)
            return collected_content  # Return the original content even if some items were skipped
        
    logging.error("Failed to retrieve content")
    return None

def get_and_add_recent_collected_from_plex():
    if get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex':
        logging.info("Getting recently added content from Plex")
        collected_content = asyncio.run(run_get_recent_from_plex())
    elif get_setting('File Management', 'file_collection_management', 'Plex') == 'Zurg':
        logging.info("Getting recently added content from Zurg")
        collected_content = asyncio.run(run_get_recent_from_zurg())
    
    if collected_content:
        movies = collected_content['movies']
        episodes = collected_content['episodes']
        
        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes")
        
        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
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