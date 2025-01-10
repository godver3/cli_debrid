import logging
import random
import time
import os
from initialization import initialize
from settings import get_setting, get_all_settings
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist
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
            'task_debug_log': 60,
            'task_refresh_release_dates': 3600,
            'task_purge_not_wanted_magnets_file': 604800,
            'task_generate_airtime_report': 3600,
            'task_check_service_connectivity': 60,
            'task_send_notifications': 15,  # Run every 0.25 minutes (15 seconds)
            'task_sync_time': 3600,  # Run every hour
            'task_check_trakt_early_releases': 3600,  # Run every hour
            'task_reconcile_queues': 300,  # Run every 5 minutes
            'task_heartbeat': 120,  # Run every 2 minutes
            'task_local_library_scan': 900,  # Run every 5 minutes
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
            'task_heartbeat'
        }

        if get_setting('File Management', 'file_collection_management') == 'Plex':
            self.enabled_tasks.add('task_plex_full_scan')
        else:
            self.enabled_tasks.add('task_local_library_scan')
        
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
                'Plex Watchlist': 900
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
                    data['interval'] = int(custom_interval) * 60  # Convert minutes to seconds
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
        if task_name not in self.enabled_tasks:
            return False
        current_time = time.time()
        time_since_last_run = current_time - self.last_run_times[task_name]
        should_run = time_since_last_run >= self.task_intervals[task_name]
        if should_run:
            self.last_run_times[task_name] = current_time
        return should_run

    def task_check_service_connectivity(self):
        logging.debug("Checking service connectivity")
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
        self.pause_queue()

        retry_count = 0
        max_retries = 5  # 5 minutes (5 * 60 seconds)

        while retry_count < max_retries:
            time.sleep(60)  # Wait for 1 minute
            retry_count += 1

            if check_service_connectivity():
                logging.info("Service connectivity restored")
                self.resume_queue()
                return

            logging.warning(f"Service connectivity check failed. Retry {retry_count}/{max_retries}")

        logging.error("Service connectivity not restored after 5 minutes. Stopping the program.")
        with app.app_context():
            stop_result = stop_program()
            logging.info(f"Program stop result: {stop_result}")

    def pause_queue(self):
        from queue_manager import QueueManager
        
        QueueManager().pause_queue()
        self.queue_paused = True
        logging.info("Queue paused")

    def resume_queue(self):
        from queue_manager import QueueManager

        QueueManager().resume_queue()
        self.queue_paused = False
        logging.info("Queue resumed")

    # Update this method to use the cached content sources
    def process_queues(self):
        try:
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
            #except TooManyDownloadsError:
            #    logging.warning("Pausing queue due to too many active downloads on Debrid")
            #    self.queue_manager.pause_queue()
            #    return None
            except RateLimitError:
                logging.warning("Rate limit exceeded on Debrid API")
                self.handle_rate_limit()
                return None
            
            queue_contents = self.queue_manager.queues[queue_name].get_contents()
            
            duration = time.time() - start_time
            
            return result
        
        except Exception as e:
            logging.error(f"Error processing {queue_name} queue: {str(e)}")
            logging.error(traceback.format_exc())
            return None

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
                wanted_content = get_wanted_from_trakt_watchlist()
            elif source_type == 'Trakt Lists':
                trakt_lists = data.get('trakt_lists', '').split(',')
                for trakt_list in trakt_lists:
                    trakt_list = trakt_list.strip()
                    wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions))
            elif source_type == 'Collected':
                wanted_content = get_wanted_from_collected()
            elif source_type == 'Plex Watchlist':
                wanted_content = get_wanted_from_plex_watchlist(versions)
            else:
                logging.warning(f"Unknown source type: {source_type}")
                return

            logging.debug(f"Retrieved wanted content from {source}: {len(wanted_content)} items")

            if wanted_content:
                total_items = 0
                if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                    # Handle list of tuples
                    for items, item_versions in wanted_content:
                        try:
                            processed_items = process_metadata(items)
                            if processed_items:
                                all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
                                add_wanted_items(all_items, item_versions or versions)
                                total_items += len(all_items)
                        except Exception as e:
                            logging.error(f"Error processing items from {source}: {str(e)}")
                else:
                    # Handle single list of items
                    try:
                        processed_items = process_metadata(wanted_content)
                        if processed_items:
                            all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
                            add_wanted_items(all_items, versions)
                            total_items += len(all_items)
                    except Exception as e:
                        logging.error(f"Error processing items from {source}: {str(e)}")
                
                logging.info(f"Added {total_items} wanted items from {source}")
            else:
                logging.warning(f"No wanted content retrieved from {source}")

        except Exception as e:
            logging.error(f"Error processing content source {source}: {str(e)}")
            logging.error(traceback.format_exc())

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
        heartbeat_file = os.path.join(tempfile.gettempdir(), 'program_heartbeat')
        with open(heartbeat_file, 'w') as f:
            f.write(str(int(time.time())))

    def check_heartbeat(self):
        heartbeat_file = os.path.join(tempfile.gettempdir(), 'program_heartbeat')
        if os.path.exists(heartbeat_file):
            with open(heartbeat_file, 'r') as f:
                last_heartbeat = int(f.read())
            if time.time() - last_heartbeat > 300:  # 5 minutes
                logging.error("Program heartbeat is stale. Restarting.")
                self.stop()
                self.start()

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
        from database.core import get_db_connection
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
        try:
            # Get all items in Checking state
            cursor = conn.execute('''
                SELECT * FROM media_items 
                WHERE state = 'Checking' 
                AND filled_by_file IS NOT NULL
            ''')
            checking_items = cursor.fetchall()
            
            for checking_item in checking_items:
                if not checking_item['filled_by_file']:
                    continue

                # Find matching items with the same filled_by_file
                cursor = conn.execute('''
                    SELECT * FROM media_items 
                    WHERE filled_by_file = ? 
                    AND id != ? 
                    AND state != 'Checking'
                ''', (checking_item['filled_by_file'], checking_item['id']))
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
                    conn.execute('''
                        UPDATE media_items 
                        SET state = 'Collected', 
                            collected_at = ? 
                        WHERE id = ?
                    ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), checking_item['id']))

                    # Delete the matching item
                    conn.execute('DELETE FROM media_items WHERE id = ?', (matching_item['id'],))
                    
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
        #program_runner.start()  # This will now run the main loop directly
    else:
        logging.info("Program is already running")
    return program_runner

if __name__ == "__main__":
    run_program()