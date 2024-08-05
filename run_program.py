import os
import logging
import time
from queue_manager import QueueManager
from initialization import initialize
from settings import get_setting
from web_server import start_server, update_stats, app, queue_manager
from tabulate import tabulate
from utilities.plex_functions import get_collected_from_plex
from content_checkers.overseerr import get_wanted_from_overseerr 
from content_checkers.collected import get_wanted_from_collected
from metadata.metadata import get_overseerr_show_details, get_overseerr_movie_details, get_release_date, refresh_release_dates, process_metadata
from content_checkers.mdb_list import get_wanted_from_mdblists
from database import add_collected_items, add_wanted_items
from flask import request, jsonify

queue_logger = logging.getLogger('queue_logger')

class ProgramRunner:
    def __init__(self):
        self.queue_manager = queue_manager
        self.tick_counter = 0
        self.task_intervals = {
            'wanted': 5,  # 5 seconds
            'scraping': 5,  # 5 seconds
            'adding': 5,  # 5 seconds
            'checking': 30,  # 5 minutes
            'sleeping': 900,  # 15 minutes
            'upgrading': 300,  # 5 minutes
            'task_plex_full_scan': 3600,  # 1 hour
            'task_overseerr_wanted': 900,  # 15 minutes
            'task_mdb_list_wanted': 900,  # 15 minutes
            'task_debug_log': 30,  # 30 seconds
            'task_refresh_release_dates': 3600,  # 1 hour
            'task_collected_wanted': 86400, # 24 hours
            'task_process_folder': 60, #1 minutes
        }
        self.start_time = time.time()
        self.last_run_times = {task: self.start_time for task in self.task_intervals}
        self.folders_to_process = []
        self.rd_mount_location = get_setting('RealDebrid', 'mount_location', '')
        if not self.rd_mount_location:
            logging.warning("RealDebrid mount location not set in settings")
        else:
            logging.info(f"RealDebrid mount location: {self.rd_mount_location}")
        
        # List of enabled tasks
        self.enabled_tasks = {
            'wanted',
            'scraping',
            'adding',
            'checking',
            'sleeping',
            'upgrading',
            #'task_plex_full_scan',
            #'task_overseerr_wanted',
            'task_debug_log',
            #'task_refresh_release_dates',
            'task_process_folder'
        }
        
        # Conditionally enable task_mdb_list_wanted
        mdb_list_urls = get_setting('MDBList', 'urls', '')
        if mdb_list_urls:
            self.enabled_tasks.add('task_mdb_list_wanted')

        collected_content_source = get_setting('Collected Content Source', 'enabled', '')
        if collected_content_source:
            self.enabled_tasks.add('task_collected_wanted')


    def run_initialization(self):
        logging.info("Running initialization...")
        skip_initial_plex_update = get_setting('Debug', 'skip_initial_plex_update', False)
        #initialize(skip_initial_plex_update)
        logging.info("Initialization complete")

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

        for task in self.enabled_tasks:
            if self.should_run_task(task):
                if task == 'task_process_folder':
                    self.task_process_folder()
                elif task == 'task_debug_log':
                    self.task_debug_log()
                elif task == 'checking':
                    # Pass the folders_to_process to the checking queue
                    self.queue_manager.process_checking(self.folders_to_process)
                    self.folders_to_process = []  # Clear the list after processing
                elif task in ['wanted', 'scraping', 'adding', 'sleeping', 'upgrading']:
                    getattr(self.queue_manager, f"process_{task}")()
                elif hasattr(self, task):
                    getattr(self, task)()
                else:
                    logging.warning(f"Unknown task: {task}")
                update_stats(processed=1)

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
        self.run_initialization()
        start_server()  # Start the web server

        while True:
            self.process_queues()
            #queue_contents = self.queue_manager.get_queue_contents()
            #self.log_queue_contents(queue_contents)
            time.sleep(1)  # Main loop runs every second

    def process_upgrading(self):
        logging.debug("Processing upgrading queue")
        self.queue_manager.upgrading_queue.process_queue()
        update_stats(processed=1)  # Update processed count

    def process_overseerr_webhook(self, data):
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

        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')

        wanted_item = {
            'tmdb_id': tmdb_id,
            'media_type': media_type
        }

        wanted_content = [wanted_item]
        wanted_content_processed = process_metadata(wanted_content)
        if wanted_content_processed:
            add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])
            logging.info(f"Processed and added wanted item: {wanted_item}")

    def task_process_folder(self):
        if self.folders_to_process:
            logging.debug("Folders waiting to be processed:")
            for i, folder in enumerate(self.folders_to_process, 1):
                full_path = os.path.join(self.rd_mount_location, folder)
                logging.debug(f"{i}. {full_path}")
        else:
            logging.debug("No folders in the processing queue")

# Move the webhook route outside of the class
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

@app.route('/webhook/folder_ready', methods=['POST'])
def folder_ready_webhook():
    data = request.json
    logging.debug(f"Received folder ready webhook: {data}")
    try:
        folder_path = data.get('folder_path')
        if not folder_path:
            raise ValueError("Missing folder_path in webhook data")
        
        full_path = os.path.join(runner.rd_mount_location, folder_path)
        if not os.path.exists(full_path):
            raise ValueError(f"Folder does not exist: {full_path}")
        
        runner.folders_to_process.append(folder_path)
        logging.info(f"Folder added for processing: {full_path}")
        return jsonify({"status": "success", "message": f"Folder {folder_path} added to processing queue"}), 200
    except Exception as e:
        logging.error(f"Error processing folder ready webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Define process_overseerr_webhook as a standalone function
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

    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    wanted_item = {
        'tmdb_id': tmdb_id,
        'media_type': media_type
    }

    wanted_content = [wanted_item]
    wanted_content_processed = process_metadata(wanted_content)
    if wanted_content_processed:
        add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])
        logging.info(f"Processed and added wanted item: {wanted_item}")

def run_program():
    global runner
    logging.info("Program started")
    runner = ProgramRunner()
    #start_server()  # Start the web server
    runner.run()

def task_plex_full_scan():
    collected_content = get_collected_from_plex('all')
    if collected_content:
        add_collected_items(collected_content['movies'] + collected_content['episodes'])
    return

def task_overseerr_wanted():
    wanted_content = get_wanted_from_overseerr()
    if wanted_content:
        wanted_content_processed = process_metadata(wanted_content)
        if wanted_content_processed:
            add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])
    return

def task_mdb_list_wanted():
    #mdb_list_api_key = get_setting('MDBList', 'api_key', '')
    mdb_list_urls = get_setting('MDBList', 'urls', '')
    #if mdb_list_api_key and mdb_list_urls:
    if mdb_list_urls:
        wanted_content = get_wanted_from_mdblists()
        if wanted_content:
            wanted_content_processed = process_metadata(wanted_content)
            if wanted_content_processed:
                add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])

def task_collected_wanted():
    wanted_content = get_wanted_from_collected()
    if wanted_content:
        wanted_content_processed = process_metadata(wanted_content)
        if wanted_content_processed:
            add_wanted_items(wanted_content_processed['episodes'])

def task_refresh_release_dates():
    refresh_release_dates()

if __name__ == "__main__":
    run_program()
