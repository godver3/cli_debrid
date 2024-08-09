import logging
import time
from queue_manager import QueueManager
from initialization import initialize
from settings import get_setting
from web_server import start_server, update_stats, app
from utilities.plex_functions import get_collected_from_plex
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from content_checkers.trakt import get_wanted_from_trakt
from metadata.metadata import process_metadata, refresh_release_dates
from content_checkers.mdb_list import get_wanted_from_mdblists
from database import add_collected_items, add_wanted_items
from flask import request, jsonify

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
            'task_overseerr_wanted': 900,
            'task_mdb_list_wanted': 900,
            'task_debug_log': 60,
            'task_refresh_release_dates': 3600,
            'task_collected_wanted': 86400,
            'task_trakt_wanted': 900,
        }
        self.start_time = time.time()
        self.last_run_times = {task: self.start_time for task in self.task_intervals}
        
        self.enabled_tasks = {
            'wanted', 'scraping', 'adding', 'checking', 'sleeping', 'unreleased', 'blacklisted',
            'task_plex_full_scan', 'task_overseerr_wanted', 'task_debug_log', 'task_refresh_release_dates',
        }
        
        if get_setting('MDBList', 'urls', ''):
            self.enabled_tasks.add('task_mdb_list_wanted')
        if get_setting('Collected Content Source', 'enabled', ''):
            self.enabled_tasks.add('task_collected_wanted')
        if get_setting('Trakt', 'user_watchlist_enabled', '') or get_setting('Trakt', 'trakt_lists', ''):
            self.enabled_tasks.add('task_trakt_wanted')

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
        if self.should_run_task('task_overseerr_wanted'):
            self.task_overseerr_wanted()
        if self.should_run_task('task_mdb_list_wanted'):
            self.task_mdb_list_wanted()
        if self.should_run_task('task_refresh_release_dates'):
            self.task_refresh_release_dates()
        if self.should_run_task('task_collected_wanted'):
            self.task_collected_wanted()
        if self.should_run_task('task_trakt_wanted'):
            self.task_trakt_wanted()
        if self.should_run_task('task_debug_log'):
            self.task_debug_log()

    def safe_process_queue(self, queue_name):
        try:
            getattr(self.queue_manager, f'process_{queue_name}')()
            update_stats(processed=1)
        except Exception as e:
            logging.error(f"Error processing {queue_name} queue: {str(e)}")
            update_stats(failed=1)

    def task_plex_full_scan(self):
        collected_content = get_collected_from_plex('all')
        if collected_content:
            add_collected_items(collected_content['movies'] + collected_content['episodes'])

    def task_overseerr_wanted(self):
        wanted_content = get_wanted_from_overseerr()
        if wanted_content:
            wanted_content_processed = process_metadata(wanted_content)
            if wanted_content_processed:
                add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])

    def task_mdb_list_wanted(self):
        wanted_content = get_wanted_from_mdblists()
        if wanted_content:
            wanted_content_processed = process_metadata(wanted_content)
            if wanted_content_processed:
                add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])

    def task_collected_wanted(self):
        wanted_content = get_wanted_from_collected()
        if wanted_content:
            wanted_content_processed = process_metadata(wanted_content)
            if wanted_content_processed:
                add_wanted_items(wanted_content_processed['episodes'])

    def task_trakt_wanted(self):
        wanted_content = get_wanted_from_trakt()
        if wanted_content:
            wanted_content_processed = process_metadata(wanted_content)
            if wanted_content_processed:
                add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])

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

    def run(self):
        start_server()  # Start the web server

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
        add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])
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

# Other functions (task_plex_full_scan, task_overseerr_wanted, etc.) remain the same

if __name__ == "__main__":
    run_program()
