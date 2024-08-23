import logging
import time
from queue_manager import QueueManager
from initialization import initialize
from settings import get_setting, get_all_settings
from utilities.debug_commands import get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist
from metadata.metadata import process_metadata, refresh_release_dates
from content_checkers.mdb_list import get_wanted_from_mdblists
from database import add_collected_items, add_wanted_items
from flask import request, jsonify
from not_wanted_magnets import task_purge_not_wanted_magnets_file
import traceback
from shared import update_stats
from shared import app
import threading
from queue_utils import safe_process_queue
import signal

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
            'task_plex_full_scan': 3600,
            'task_debug_log': 60,
            'task_refresh_release_dates': 3600,
            'task_purge_not_wanted_magnets_file': 604800,
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
            'task_plex_full_scan', 
            'task_debug_log', 
            'task_refresh_release_dates',
        }
        
        self.content_sources = self.load_content_sources()

    def load_content_sources(self):
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})
        
        default_intervals = {
            'Overseerr': 900,
            'MDBList': 900,
            'Collected': 86400,
            'Trakt Watchlist': 900,
            'Trakt Lists': 900
        }
        
        for source, data in content_sources.items():
            if isinstance(data, str):
                data = {'enabled': data.lower() == 'true'}
            
            if not isinstance(data, dict):
                logging.error(f"Unexpected data type for content source {source}: {type(data)}")
                continue
            
            source_type = source.split('_')[0]

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
        
        return content_sources
        
    def should_run_task(self, task_name):
        if task_name not in self.enabled_tasks:
            return False
        current_time = time.time()
        if current_time - self.last_run_times[task_name] >= self.task_intervals[task_name]:
            self.last_run_times[task_name] = current_time
            return True
        return False

    def process_queues(self):
        self.queue_manager.update_all_queues()

        for queue_name in ['wanted', 'scraping', 'adding', 'checking', 'sleeping', 'unreleased', 'blacklisted']:
            if self.should_run_task(queue_name):
                self.safe_process_queue(queue_name)

        if self.should_run_task('task_plex_full_scan'):
            self.task_plex_full_scan()
        if self.should_run_task('task_refresh_release_dates'):
            self.task_refresh_release_dates()
        if self.should_run_task('task_debug_log'):
            self.task_debug_log()

        # Process content source tasks
        for source, data in self.content_sources.items():
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
            
            # Log successful processing
            logging.info(f"Successfully processed {queue_name} queue")
            update_stats(processed=1)
            
            # Return the result if any
            return result
        
        except AttributeError as e:
            logging.error(f"Error: No process method found for {queue_name} queue. Error: {str(e)}")
        except Exception as e:
            logging.error(f"Error processing {queue_name} queue: {str(e)}")
            logging.error(f"Traceback: {traceback.format_exc()}")
            update_stats(failed=1)
        
        return None

    def task_plex_full_scan(self):
        get_and_add_all_collected_from_plex()

    def process_content_source(self, source, data):
        source_type = source.split('_')[0]
        versions = data.get('versions', {})

        logging.debug(f"Processing content source: {source} (type: {source_type})")

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
                    processed_items = process_metadata(items)
                    if processed_items:
                        all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
                        add_wanted_items(all_items, item_versions or versions)
                        total_items += len(all_items)
            else:
                # Handle single list of items
                processed_items = process_metadata(wanted_content)
                if processed_items:
                    all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
                    add_wanted_items(all_items, versions)
                    total_items += len(all_items)
            
            logging.info(f"Added {total_items} wanted items from {source}")
        else:
            logging.warning(f"No wanted content retrieved from {source}")

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
        
    def task_refresh_release_dates(self):
        task_purge_not_wanted_magnets_file()

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
        overseerr_settings = next((data for source, data in ProgramRunner().content_sources.items() if source.startswith('Overseerr')), {})
        versions = overseerr_settings.get('versions', {})
        
        all_items = wanted_content_processed.get('movies', []) + wanted_content_processed.get('episodes', [])
        add_wanted_items(all_items, versions)
        logging.info(f"Processed and added wanted item from webhook: {wanted_item}")

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