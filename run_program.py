import logging
import time
from initialization import initialize
from settings import get_setting, get_all_settings
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
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
        
        from queue_manager import QueueManager

        self.queue_manager = QueueManager()
        self.tick_counter = 0
        self.task_intervals = {
            'wanted': 5,
            'scraping': 5,
            'adding': 5,
            'checking': 300, #300
            'sleeping': 900,
            'unreleased': 3600,
            'blacklisted': 3600,
            'pending_uncached': 3600,
            'task_plex_full_scan': 3600,
            'task_debug_log': 60,
            'task_refresh_release_dates': 3600,
            'task_purge_not_wanted_magnets_file': 604800,
            'task_generate_airtime_report': 3600,
            'task_check_service_connectivity': 60,
        }
        self.start_time = time.time()
        self.last_run_times = {task: self.start_time for task in self.task_intervals}
        
        self.enabled_tasks = {
            'wanted', 
            'scraping', 
            'adding', 
            'checking', 
            'sleeping', 
            'unreleased', 
            'blacklisted',
            'pending_uncached',
            'task_plex_full_scan', 
            'task_debug_log', 
            'task_refresh_release_dates',
            'task_generate_airtime_report',
            'task_check_service_connectivity'
        }
        
        # Add this line to store content sources
        self.content_sources = None

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
                'Trakt Lists': 900
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
        self.queue_manager.update_all_queues()

        for queue_name in ['wanted', 'scraping', 'adding', 'checking', 'sleeping', 'unreleased', 'blacklisted', 'pending_uncached']:
            if self.should_run_task(queue_name):
                self.safe_process_queue(queue_name)

        if self.should_run_task('task_plex_full_scan'):
            self.task_plex_full_scan()
        if self.should_run_task('task_refresh_release_dates'):
            self.task_refresh_release_dates()
        if self.should_run_task('task_debug_log'):
            self.task_debug_log()
        if self.should_run_task('task_check_service_connectivity'):
            self.task_check_service_connectivity()
        if self.should_run_task('task_purge_not_wanted_magnets_file'):
            self.task_purge_not_wanted_magnets_file()
        if self.should_run_task('task_generate_airtime_report'):
            self.task_generate_airtime_report()
            
        # Process content source tasks
        for source, data in self.get_content_sources().items():
            task_name = f'task_{source}_wanted'
            if self.should_run_task(task_name):
                self.process_content_source(source, data)

    def safe_process_queue(self, queue_name: str):
        try:
            logging.debug(f"Starting to process {queue_name} queue")
            
            # Get the appropriate process method
            process_method = getattr(self.queue_manager, f'process_{queue_name}')
            
            # Call the process method and capture any return value
            result = process_method()
                     
            # Return the result if any
            return result
        
        except AttributeError as e:
            logging.error(f"Error: No process method found for {queue_name} queue. Error: {str(e)}")
        except Exception as e:
            logging.error(f"Error processing {queue_name} queue: {str(e)}")
            logging.error(f"Traceback: {traceback.format_exc()}")
        
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

    def get_wanted_content(self, source_type, data):
        versions = data.get('versions', {})
        logging.debug(f"Getting wanted content for {source_type} with versions: {versions}")
        
        if source_type == 'Overseerr':
            content = get_wanted_from_overseerr()
            return [(content, versions)] if content else []
        elif source_type == 'MDBList':
            mdblist_urls = data.get('urls', '').split(',')
            result = []
            for url in mdblist_urls:
                content = get_wanted_from_mdblists(url.strip(), versions)
                if isinstance(content, list) and len(content) > 0 and isinstance(content[0], tuple):
                    result.extend(content)
                else:
                    result.append((content, versions))
            return result
        elif source_type == 'Collected':
            content = get_wanted_from_collected()
            return [(content, versions)] if content else []
        elif source_type == 'Trakt Watchlist':
            content = get_wanted_from_trakt_watchlist()
            return [(content, versions)] if content else []
        elif source_type == 'Trakt Lists':
            trakt_lists = data.get('trakt_lists', '').split(',')
            result = []
            for url in trakt_lists:
                content = get_wanted_from_trakt_lists(url.strip(), versions)
                if isinstance(content, list) and len(content) > 0 and isinstance(content[0], tuple):
                    result.extend(content)
                else:
                    result.append((content, versions))
            return result
        else:
            logging.warning(f"Unknown source type: {source_type}")
            return []

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
            if interval > 60:  # Only log tasks that run less frequently than every minute
                time_until_next_run = interval - (current_time - self.last_run_times[task])
                minutes, seconds = divmod(int(time_until_next_run), 60)
                hours, minutes = divmod(minutes, 60)
                debug_info.append(f"{task}: {hours:02d}:{minutes:02d}:{seconds:02d}")

        logging.info("Time until next task run:\n" + "\n".join(debug_info))

    def run_initialization(self):
        logging.info("Running initialization...")
        skip_initial_plex_update = get_setting('Debug', 'skip_initial_plex_update', False)
        
        disable_initialization = get_setting('Debug', 'disable_initialization', '')
        if not disable_initialization:
            initialize(skip_initial_plex_update)
        logging.info("Initialization complete")

    def start(self):
        if not self.running:
            self.running = True
            self.run()

    def stop(self):
        self.running = False

    def is_running(self):
        return self.running

    def run(self):
        self.run_initialization()
            
        while self.running:
            self.process_queues()
            time.sleep(1)  # Main loop runs every second

    def invalidate_content_sources_cache(self):
        self.content_sources = None

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

    wanted_content = [wanted_item]
    wanted_content_processed = process_metadata(wanted_content)
    if wanted_content_processed:
        # Get the versions for Overseerr from settings
        content_sources = ProgramRunner().get_content_sources(force_refresh=True)
        overseerr_settings = next((data for source, data in content_sources.items() if source.startswith('Overseerr')), {})
        versions = overseerr_settings.get('versions', {})
        
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
    logging.info("Getting all collected content from Plex")
    collected_content = asyncio.run(run_get_collected_from_plex())
    
    if collected_content:
        movies = collected_content['movies']
        episodes = collected_content['episodes']
        
        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from Plex")
        
        #append_runtime_airtime(movies)
        #append_runtime_airtime(episodes)
        
        add_collected_items(movies + episodes)
    else:
        logging.error("Failed to retrieve content from Plex")

def get_and_add_recent_collected_from_plex():
    logging.info("Getting recently added content from Plex")
    collected_content = asyncio.run(run_get_recent_from_plex())
    
    if collected_content:
        movies = collected_content['movies']
        episodes = collected_content['episodes']
        
        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from Plex")
        
        #append_runtime_airtime(movies)
        #append_runtime_airtime(episodes)

        add_collected_items(movies + episodes, recent=True)
    else:
        logging.error("Failed to retrieve content from Plex")

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