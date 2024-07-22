import urwid
import logging
from datetime import datetime, timedelta
from logging_config import get_logger, get_log_messages
from typing import Dict, Any, List
import sqlite3
from scraper.scraper import scrape, detect_season_pack
import os
import sys
import json
import time

from database import (
    get_all_media_items,
    purge_database, verify_database,
    update_media_item_state, get_item_state
)

from utilities.plex_functions import get_collected_from_plex  # Ensure this import exists
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from settings import get_setting
from content_checkers.overseerr import get_wanted_from_overseerr
from content_checkers.mdb_list import get_wanted_from_mdblists

logger = get_logger()

sync_requested = False
LAST_PLEX_COLLECTION_TIME_FILE = 'last_plex_collection_time.json'

def handle_webhook(request):
    global sync_requested
    sync_requested = True
    return web.Response(text="Sync request received")

def start_webhook_server():
    pass  # Placeholder for starting the webhook server

class UrwidLogHandler(logging.Handler):
    def __init__(self, log_box):
        super().__init__()
        self.log_box = log_box

    def emit(self, record):
        log_entry = self.format(record)
        self.log_box.add_log(log_entry)
        self.flush()

    def flush(self):
        pass

def format_item(item):
    if 'show_title' in item:
        title = item['show_title']
        season = item.get('season_number', 'Unknown')
        episode = item.get('episode_number', 'Unknown')
        year = item.get('year', 'Unknown')
        return f"{title} - S{season:02d}E{episode:02d} ({year})"
    else:
        title = item.get('title', 'Unknown')
        year = item.get('year', 'Unknown')
        return f"{title} ({year})"

class ContentColumn(urwid.ListBox):
    def __init__(self, title):
        self.body = urwid.SimpleFocusListWalker([urwid.Text(title, align='center'), urwid.Divider('-')])
        super().__init__(self.body)

    def update_column(self, items):
        self.body.clear()
        self.body.append(urwid.Text(items[0], align='center'))
        self.body.append(urwid.Divider('-'))
        for item in items[1:]:
            self.body.append(urwid.Text(item))

class LogBox(urwid.ListBox):
    def __init__(self, max_logs=22):
        self.body = urwid.SimpleFocusListWalker([])
        self.max_logs = max_logs
        super().__init__(self.body)

    def add_log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.body.append(urwid.Text(f"{timestamp} - {message}"))
        if len(self.body) > self.max_logs:
            self.body.pop(0)
        self.body.set_focus(len(self.body) - 1)

class MainUI:
    def __init__(self):
        self.wanted_column = ContentColumn("Wanted")
        self.scraping_column = ContentColumn("Scraping")

        columns = urwid.Columns([
            self.wanted_column,
            self.scraping_column
        ])

        content_panel = urwid.LineBox(columns, title="Content Queues")
        self.log_box = LogBox()
        log_panel = urwid.LineBox(self.log_box, title="Log Messages")

        self.layout = urwid.Pile([
            (20, content_panel),  # Fixed height for the content panel
            (25, log_panel)      # Fixed height for the log panel
        ])

    def create_loop(self):
        return urwid.MainLoop(self.layout, unhandled_input=self.handle_input)

    def handle_input(self, key):
        if key in ('q', 'Q'):
            raise urwid.ExitMainLoop()

    def update_display(self):
        self.update_content_columns()

    def update_content_columns(self):
        try:
            wanted_content = [format_item(item) for item in get_all_media_items('Wanted')]
            scraping_content = [format_item(item) for item in get_all_media_items('Scraping')]

            self.wanted_column.update_column(["Wanted"] + wanted_content)
            self.scraping_column.update_column(["Scraping"] + scraping_content)

        except Exception as e:
            logger.error(f"Error updating content columns: {e}")

def load_last_plex_collection_time():
    if os.path.exists(LAST_PLEX_COLLECTION_TIME_FILE):
        with open(LAST_PLEX_COLLECTION_TIME_FILE, 'r') as f:
            return datetime.fromisoformat(json.load(f)['last_collection_time'])
    return None

def save_last_plex_collection_time(timestamp):
    with open(LAST_PLEX_COLLECTION_TIME_FILE, 'w') as f:
        json.dump({'last_collection_time': timestamp.isoformat()}, f)

def initial_setup():
    logger.info("Running initial setup...")
    purge_database("all", "Wanted")
    verify_database()

    last_plex_collection_time = load_last_plex_collection_time()
    current_time = datetime.now()
    if last_plex_collection_time and (current_time - last_plex_collection_time) < timedelta(hours=1):
        logger.info("Using cached Plex content")
        add_recent_plex_content_to_collected()
    else:
        logger.info("Fetching new Plex content")
        add_plex_content_to_collected()
        save_last_plex_collection_time(current_time)

    add_overseerr_content_to_wanted()
    add_mdb_list_content_to_wanted()

def add_plex_content_to_collected():
    collected_content = get_collected_from_plex('all')
    if collected_content:
        add_or_update_media_items_batch(collected_content['movies'] + collected_content['episodes'], status='Collected', full_scan=True)
        
def add_recent_plex_content_to_collected():
    collected_content = get_collected_from_plex('recent')
    if collected_content:
        add_or_update_media_items_batch(collected_content['movies'] + collected_content['episodes'], status='Collected', full_scan=True)
        
def add_overseerr_content_to_wanted():
    wanted_content = get_wanted_from_overseerr()
    if wanted_content:
        add_or_update_media_items_batch(wanted_content['movies'] + wanted_content['episodes'], status='Wanted')
        
def add_mdb_list_content_to_wanted():
    wanted_content = get_wanted_from_mdb_list()
    if wanted_content:
        add_or_update_media_items_batch(wanted_content['movies'] + wanted_content['episodes'], status='Wanted')

def update_ui(loop, ui):
    ui.update_display()
    loop.draw_screen()

def fetch_and_convert(fetch_func, state):
    items = fetch_func(state)
    return [dict(item) for item in items]  # Explicitly convert each item to a dictionary

def process_queue(state: str, process_func: callable) -> None:
    logger.info(f"Starting to process queue for state: {state}")

    items = fetch_and_convert(get_all_media_items, state)

    logger.info(f"Total items to process for state {state}: {len(items)}")

    for item in items:
        try:
            if 'episode_title' in item:
                item_title = f"{item.get('show_title', 'Unknown Show')} - {item.get('episode_title', 'Unknown Episode')}"
            else:
                item_title = item.get('title', 'Unknown Title')

            item_id = item.get('id', 'Unknown ID')

            logger.debug(f"Processing item: {item_title} (ID: {item_id})")

            if state == 'Scraping':
                scraping_results = get_scraping_results(item_id)
                result = process_func(item, scraping_results)
            else:
                result = process_func(item)

            logger.debug(f"Result from process_func: {result}")

            new_state = result if isinstance(result, str) else result[0]
            additional_info = result[1] if isinstance(result, tuple) and len(result) == 2 else None

            logger.debug(f"New state: {new_state}, Additional info: {additional_info is not None}")

            if additional_info:
                best_result = additional_info[0] if isinstance(additional_info, list) else additional_info
                update_media_item_state(item_id, new_state, best_result.get('title'))
            else:
                update_media_item_state(item_id, new_state)

        except Exception as e:
            logger.error(f"Error processing item in state {state}. Item details: {item}. Error: {str(e)}", exc_info=True)

    logger.info(f"Finished processing queue for state: {state}")

def update_scraping_results(item_id: int, results: List[Dict[str, Any]]) -> None:
    try:
        all_results = {}

        all_results[item_id] = results

        with open(SCRAPING_RESULTS_FILE, 'wb') as f:
            f.write(pickle.dumps(all_results))

        logger.debug(f"Updated scraping results for item {item_id}")
    except Exception as e:
        logger.error(f"Error updating scraping results for item {item_id}: {str(e)}")

def get_scraping_results(item_id: int) -> List[Dict[str, Any]]:
    try:
        if os.path.exists(SCRAPING_RESULTS_FILE):
            with open(SCRAPING_RESULTS_FILE, 'rb') as f:
                all_results = pickle.loads(f.read())
            results = all_results.get(item_id, [])
            logger.debug(f"Retrieved scraping results for item {item_id}: {len(results)} results")
            return results
        else:
            logger.warning(f"No scraping results file found for item {item_id}")
            return []
    except Exception as e:
        logger.error(f"Error retrieving scraping results for item {item_id}: {str(e)}")
        return []

def process_wanted(item: Dict[str, Any]) -> str:
    if 'show_title' in item:
        title = f"{item['show_title']} - {item.get('episode_title', 'Unknown Episode')}"
    else:
        title = item.get('title', 'Unknown Title')

    logger.info(f"Adding wanted item to scraper: {title}")

    time.sleep(0.2)  # Simulate work
    return 'Scraping'

def process_scraping(item: Dict[str, Any], scraping_results: List[Dict[str, Any]]) -> str:
    if 'show_title' in item:
        title = f"{item['show_title']} - {item.get('episode_title', 'Unknown Episode')}"
    else:
        title = item.get('title', 'Unknown Title')

    logger.debug(f"Scraping for item: {title}")

    try:
        current_status = get_item_state(item['id'])
        if current_status != 'Scraping':
            logger.warning(f"Item {title} is no longer in Scraping state. Current state: {current_status}")
            return current_status

        time.sleep(1)  # Simulate work

        logger.info(f"Scraping results for {title}: {scraping_results}")
        return 'Completed'

    except Exception as e:
        logger.error(f"Error scraping item {title}: {str(e)}")
        return 'Wanted'

def main_loop(ui: MainUI, loop: urwid.MainLoop) -> None:
    global sync_requested
    initial_setup()
    logger.info("Starting main loop...")

    while True:
        process_queue('Wanted', process_wanted)
        process_queue('Scraping', process_scraping)

        update_ui(loop, ui)

        if sync_requested:
            sync_requested = False

        time.sleep(5)  # Short delay between iterations

def run_program():
    ui = MainUI()
    logger.info("Program started")

    handler = UrwidLogHandler(ui.log_box)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    loop = ui.create_loop()

    try:
        main_loop(ui, loop)
    except urwid.ExitMainLoop:
        pass
    finally:
        if hasattr(loop, 'idle_handle'):
            loop.remove_enter_idle(loop.idle_handle)
        loop.screen.stop()

# This function is used to integrate with your main menu
def run_program_from_menu():
    run_program()

if __name__ == "__main__":
    run_program()
