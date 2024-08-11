import logging
import time
from queue_manager import QueueManager
from initialization import initialize
from settings import get_setting, get_all_settings
from web_server import start_server, update_stats, app
from utilities.plex_functions import get_collected_from_plex
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist
from metadata.metadata import process_metadata, refresh_release_dates
from content_checkers.mdb_list import get_wanted_from_mdblists
from database import add_collected_items, add_wanted_items
from flask import request, jsonify
import traceback

queue_logger = logging.getLogger('queue_logger')

class ProgramRunner:
    def __init__(self):
        self.queue_manager = QueueManager()
        self.tick_counter = 0
        self.task_intervals = {
            'wanted': 5,
            'scraping': 5,
            'adding': 5,
            'checking': 300,
            'sleeping': 900,
            'unreleased': 3600,
            'blacklisted': 3600,
            'task_plex_full_scan': 3600,
            'task_debug_log': 60,
            'task_refresh_release_dates': 3600,
        }
        self.start_time = time.time()
        self.last_run_times = {task: self.start_time for task in self.task_intervals}
        
        self.enabled_tasks = {
            'wanted', 'scraping', 'adding', 'checking', 'sleeping', 'unreleased', 'blacklisted',
            'task_plex_full_scan', 'task_debug_log', 'task_refresh_release_dates',
        }
        
        self.content_sources = self.load_content_sources()

    def load_content_sources(self):
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})
        
        # Add default intervals for each content source type
        default_intervals = {
            'Overseerr': 900,
            'MDBList': 900,
            'Collected': 86400,
            'Trakt Watchlist': 900,
            'Trakt Lists': 900
        }
        
        for source, data in content_sources.items():
            source_type = source.split('_')[0]
            data['interval'] = default_intervals.get(source_type, 3600)
            task_name = f'task_{source}_wanted'
            self.task_intervals[task_name] = data['interval']
            if data.get('enabled', False):
                self.enabled_tasks.add(task_name)

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
        collected_content = get_collected_from_plex('all')
        if collected_content:
            add_collected_items(collected_content['movies'] + collected_content['episodes'])

    def process_content_source(self, source, data):
        source_type = source.split('_')[0]
        versions = data.get('versions', {})

        if not versions:
            logging.warning(f"No versions specified for {source}. Skipping.")
            return

        wanted_content = None
        if source_type == 'Overseerr':
            wanted_content = get_wanted_from_overseerr()
        elif source_type == 'MDBList':
            wanted_content = get_wanted_from_mdblists()
        elif source_type == 'Collected':
            wanted_content = get_wanted_from_collected()
        elif source_type == 'Trakt Watchlist':
            wanted_content = get_wanted_from_trakt_watchlist()
        elif source_type == 'Trakt Lists':
            wanted_content = get_wanted_from_trakt_lists()

        if wanted_content:
            wanted_content_processed = process_metadata(wanted_content)
            if wanted_content_processed:
                add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'], versions)

    def task_refresh_release_dates(self):
        refresh_release_dates()

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
        if disable_initialization == "False":
            initialize(skip_initial_plex_update)
        logging.info("Initialization complete")

    def run(self):
        start_server()  # Start the web server

        self.run_initialization()

        while True:
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
        
        add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'], versions)
        logging.info(f"Processed and added wanted item: {wanted_item}")

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    logging.debug(f"Received webhook: {data}")
    try:
        process_overseerr_webhook(data)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

def run_program():
    logging.info("Program started")
    runner = ProgramRunner()
    runner.run()

if __name__ == "__main__":
    run_program()