import logging
import time
from queue_manager import QueueManager
from initialization import initialize
from settings import get_setting
from web_server import start_server, update_stats, app
from tabulate import tabulate
from utilities.plex_functions import get_collected_from_plex
from content_checkers.overseerr import get_wanted_from_overseerr, map_collected_media_to_wanted, get_overseerr_show_details, get_overseerr_movie_details, get_release_date, refresh_release_dates
from content_checkers.mdb_list import get_wanted_from_mdblists
from database import add_collected_items, add_wanted_items
from flask import request, jsonify

queue_logger = logging.getLogger('queue_logger')

class ProgramRunner:
    def __init__(self):
        self.queue_manager = QueueManager()
        self.tick_counter = 0
        self.task_intervals = {
            'wanted': 5,  # 5 seconds
            'scraping': 5,  # 5 seconds
            'adding': 5,  # 5 seconds
            'checking': 300,  # 5 minutes
            'sleeping': 900,  # 15 minutes
            'task_plex_full_scan': 43200,  # 12 hours
            'task_overseerr_wanted': 60,  # 1 minute
            'task_mdb_list_wanted': 900,  # 15 minutes
            'task_debug_log': 60,  # 1 minute
            'task_refresh_release_dates': 3600,  # 1 hour
        }
        self.start_time = time.time()
        self.last_run_times = {task: self.start_time for task in self.task_intervals}
        
        # List of enabled tasks
        self.enabled_tasks = {
            'wanted',
            'scraping',
            'adding',
            'checking',
            'sleeping',
            'task_plex_full_scan',
            'task_overseerr_wanted',
            'task_mdb_list_wanted',
            'task_debug_log',
            'task_refresh_release_dates'
        }

    def run_initialization(self):
        logging.info("Running initialization...")
        skip_initial_plex_update = get_setting('Debug', 'skip_initial_plex_update', False)
        initialize(skip_initial_plex_update)
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

        if self.should_run_task('wanted'):
            self.queue_manager.process_wanted()
            update_stats(processed=1)  # Update processed count

        if self.should_run_task('scraping'):
            self.queue_manager.process_scraping()
            update_stats(processed=1)  # Update processed count

        if self.should_run_task('adding'):
            self.queue_manager.process_adding()
            update_stats(processed=1, successful=1)  # Update processed and successful counts

        if self.should_run_task('checking'):
            self.queue_manager.process_checking()
            update_stats(processed=1)  # Update processed count

        if self.should_run_task('sleeping'):
            self.queue_manager.process_sleeping()

        if self.should_run_task('task_plex_full_scan'):
            task_plex_full_scan()

        if self.should_run_task('task_overseerr_wanted'):
            task_overseerr_wanted()

        if self.should_run_task('task_mdb_list_wanted'):
            task_mdb_list_wanted()

        if self.should_run_task('task_refresh_release_dates'):
            task_refresh_release_dates()

        if self.should_run_task('task_debug_log'):
            self.task_debug_log()

    def task_debug_log(self):
        current_time = time.time()
        debug_info = []
        for task, interval in self.task_intervals.items():
            if interval > 60:  # Only log tasks that run less frequently than every minute
                time_until_next_run = interval - (current_time - self.last_run_times[task])
                minutes, seconds = divmod(int(time_until_next_run), 60)
                hours, minutes = divmod(minutes, 60)
                debug_info.append(f"{task}: {hours:02d}:{minutes:02d}:{seconds:02d}")

        logging.debug("Time until next task run:\n" + "\n".join(debug_info))

    def run(self):
        self.run_initialization()
        #start_server()

        while True:
            self.process_queues()
            queue_contents = self.queue_manager.get_queue_contents()
            self.log_queue_contents(queue_contents)
            time.sleep(1)  # Main loop runs every second

    def log_queue_contents(self, queue_contents):
        headers = ["Wanted", "Scraping", "Adding", "Checking", "Sleeping"]
        max_items = 20
        table_data = []
        for i in range(max_items):
            row = []
            for queue_name in headers:
                if i < len(queue_contents[queue_name]):
                    item = queue_contents[queue_name][i]
                    if item['type'] == 'movie':
                        row.append(f"{item['title']} ({item['year']})")
                    elif item['type'] == 'episode':
                        row.append(f"{item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}")
                else:
                    row.append("")
            table_data.append(row)
        log_lines = tabulate(table_data, headers=headers, tablefmt="grid")
        queue_logger.info(log_lines)

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


        if media_type == 'movie':
            movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, None)
            if movie_details:
                release_date = get_release_date(movie_details, 'movie')
                movie_item = {
                    'imdb_id': movie_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID'),
                    'tmdb_id': tmdb_id,
                    'title': movie_details.get('title', 'Unknown Title'),
                    'year': release_date[:4] if release_date != 'Unknown' else 'Unknown Year',
                    'release_date': release_date
                }
                add_wanted_items([movie_item])
                logging.info(f"Added movie to wanted items: {movie_item['title']}")
        elif media_type == 'tv':
            show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, None)
            if show_details:
                imdb_id = show_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID')
                show_title = show_details.get('name', 'Unknown Show Title')
                for season in show_details.get('seasons', []):
                    season_number = season.get('seasonNumber')
                    if season_number == 0:
                        continue  # Skip specials
                    for episode in season.get('episodes', []):
                        release_date = get_release_date(episode, 'tv')
                        episode_item = {
                            'imdb_id': imdb_id,
                            'tmdb_id': tmdb_id,
                            'title': show_title,
                            'episode_title': episode.get('name', 'Unknown Episode Title'),
                            'year': release_date[:4] if release_date != 'Unknown' else 'Unknown Year',
                            'season_number': season_number,
                            'episode_number': episode.get('episodeNumber', 'Unknown Episode Number'),
                            'release_date': release_date
                        }
                        add_wanted_items([episode_item])
                logging.info(f"Added TV show episodes to wanted items: {show_title}")
        else:
            logging.error(f"Unknown media type: {media_type}")

# Webhook route
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    logging.debug(f"Received webhook: {data}")
    try:
        runner.process_overseerr_webhook(data)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

def run_program():
    logging.info("Program started")

    global runner
    runner = ProgramRunner()

    # Start the web server
    start_server()

    runner.run()

def task_plex_full_scan():
    collected_content = get_collected_from_plex('all')
    if collected_content:
        add_collected_items(collected_content['movies'] + collected_content['episodes'])
    return

def task_overseerr_wanted():
    wanted_content = get_wanted_from_overseerr()
    if wanted_content:
        add_wanted_items(wanted_content['movies'] + wanted_content['episodes'])
    return

def task_mdb_list_wanted():
    wanted_content = get_wanted_from_mdblists()
    if wanted_content:
        add_wanted_items(wanted_content['movies'] + wanted_content['episodes'])
    return

def task_refresh_release_dates():
    refresh_release_dates()

if __name__ == "__main__":
    run_program()
