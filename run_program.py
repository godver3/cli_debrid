import urwid
import asyncio
import logging
from datetime import datetime
from database import (
    get_working_movies_by_state,
    get_working_episodes_by_state,
    clone_wanted_to_working,
    update_working_movie,
    update_working_episode,
    fetch_item_status,
    purge_wanted_database,
    purge_working_database,
    verify_database,
)
from plex_integration import populate_db_from_plex 
from content_checkers.overseer_checker import get_unavailable_content
from content_checkers.mdb_list import sync_mdblist_with_overseerr
from logging_config import get_logger, get_log_messages
from typing import Dict, Any, List, Tuple
import sqlite3
from scraper.scraper import scrape, detect_season_pack
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet
import aiofiles
from os import path
import pickle
from aiohttp import web
from settings import get_setting

logger = get_logger()

sync_requested = False

async def handle_webhook(request):
    global sync_requested
    sync_requested = True
    return web.Response(text="Sync request received")

app = web.Application()
app.add_routes([web.post('/webhook', handle_webhook)])

async def start_webhook_server():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 6699)
    await site.start()
    logger.info("Webhook server started at http://localhost:6699")

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
        self.adding_column = ContentColumn("Adding to Debrid")
        self.checking_column = ContentColumn("Checking")
        self.filled_column = ContentColumn("Filled")

        columns = urwid.Columns([
            self.wanted_column,
            self.scraping_column,
            self.adding_column,
            self.checking_column,
            self.filled_column
        ])

        content_panel = urwid.LineBox(columns, title="Content Queues")
        self.log_box = LogBox()
        log_panel = urwid.LineBox(self.log_box, title="Log Messages")

        self.layout = urwid.Pile([
            (20, content_panel),  # Fixed height for the content panel
            (25, log_panel)      # Fixed height for the log panel
        ])

    def create_loop(self):
        evl = urwid.AsyncioEventLoop(loop=asyncio.get_running_loop())
        return urwid.MainLoop(self.layout, event_loop=evl, unhandled_input=self.handle_input)

    def handle_input(self, key):
        if key in ('q', 'Q'):
            raise urwid.ExitMainLoop()

    def update_display(self):
        self.update_content_columns()

    def update_content_columns(self):

        def fetch_and_convert(fetch_func, state):
            items = fetch_func(state)
            return [dict(item) for item in items]  # Explicitly convert each item to a dictionary

        try:
            wanted_content = [format_item(item) for item in fetch_and_convert(get_working_movies_by_state, 'Wanted') + fetch_and_convert(get_working_episodes_by_state, 'Wanted')]
            scraping_content = [format_item(item) for item in fetch_and_convert(get_working_movies_by_state, 'Scraping') + fetch_and_convert(get_working_episodes_by_state, 'Scraping')]
            adding_content = [format_item(item) for item in fetch_and_convert(get_working_movies_by_state, 'Adding') + fetch_and_convert(get_working_episodes_by_state, 'Adding')]
            checking_content = [format_item(item) for item in fetch_and_convert(get_working_movies_by_state, 'Checking') + fetch_and_convert(get_working_episodes_by_state, 'Checking')]
            filled_content = [format_item(item) for item in fetch_and_convert(get_working_movies_by_state, 'Filled') + fetch_and_convert(get_working_episodes_by_state, 'Filled')]

            self.wanted_column.update_column(["Wanted"] + wanted_content)
            self.scraping_column.update_column(["Scraping"] + scraping_content)
            self.adding_column.update_column(["Adding to Debrid"] + adding_content)
            self.checking_column.update_column(["Checking"] + checking_content)
            self.filled_column.update_column(["Filled"] + filled_content)

        except Exception as e:
            logger.error(f"Error updating content columns: {e}")

async def initial_setup():
    logger.info("Running initial setup...")
    await asyncio.to_thread(purge_wanted_database)
    await asyncio.to_thread(purge_working_database)
    await asyncio.to_thread(verify_database)
    #await asyncio.to_thread(populate_db_from_plex, get_setting('Plex', 'url'), get_setting('Plex', 'token'))
    await asyncio.to_thread(sync_mdblist_with_overseerr)
    await asyncio.to_thread(get_unavailable_content)
    await asyncio.to_thread(clone_wanted_to_working)

async def update_ui_periodically(ui, loop):
    while True:
        ui.update_display()
        loop.draw_screen()
        await asyncio.sleep(0.1)  # Refresh UI every 0.1 seconds

def dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

async def fetch_and_convert(fetch_func, state):
    items = await asyncio.to_thread(fetch_func, state)
    return [dict(item) for item in items]  # Explicitly convert each item to a dictionary

async def process_queue(state: str, process_func: callable) -> None:
    logger.info(f"Starting to process queue for state: {state}")

    movies = await fetch_and_convert(get_working_movies_by_state, state)
    episodes = await fetch_and_convert(get_working_episodes_by_state, state)

    all_items = movies + episodes
    logger.info(f"Total items to process for state {state}: {len(all_items)}")

    for item in all_items:
        try:
            if 'episode_title' in item:
                item_title = f"{item.get('show_title', 'Unknown Show')} - {item.get('episode_title', 'Unknown Episode')}"
            else:
                item_title = item.get('title', 'Unknown Title')

            item_id = item.get('id', 'Unknown ID')

            logger.debug(f"Processing item: {item_title} (ID: {item_id})")

            if state == 'Adding':
                # For 'Adding' state, we need to fetch the scraping results
                scraping_results = await get_scraping_results(item_id)
                result = await process_func(item, scraping_results)
            else:
                result = await process_func(item)

            logger.debug(f"Result from process_func: {result}")

            if isinstance(result, tuple) and len(result) == 2:
                new_state, additional_info = result
                if state == 'Scraping' and new_state == 'Adding':
                    await update_scraping_results(item_id, additional_info)
            elif isinstance(result, str):
                new_state = result
                additional_info = None
            else:
                logger.error(f"Unexpected result format: {result}")
                continue

            logger.debug(f"New state: {new_state}, Additional info: {additional_info is not None}")

            if additional_info:
                best_result = additional_info[0] if isinstance(additional_info, list) else additional_info
                if 'episode_number' in item:
                    await asyncio.to_thread(update_working_episode, item_id, new_state, best_result.get('title'))
                else:
                    await asyncio.to_thread(update_working_movie, item_id, new_state, best_result.get('title'))
            else:
                if 'episode_number' in item:
                    await asyncio.to_thread(update_working_episode, item_id, new_state)
                else:
                    await asyncio.to_thread(update_working_movie, item_id, new_state)

        except Exception as e:
            logger.error(f"Error processing item in state {state}. Item details: {item}. Error: {str(e)}", exc_info=True)

    logger.info(f"Finished processing queue for state: {state}")

async def update_scraping_results(item_id: int, results: List[Dict[str, Any]]) -> None:
    try:
        all_results = {}

        # Update results for this item
        all_results[item_id] = results

        # Save updated results
        with open(SCRAPING_RESULTS_FILE, 'wb') as f:
            pickle.dump(all_results, f)

        logger.debug(f"Updated scraping results for item {item_id}")
    except Exception as e:
        logger.error(f"Error updating scraping results for item {item_id}: {str(e)}")

async def get_scraping_results(item_id: int) -> List[Dict[str, Any]]:
    try:
        if path.exists(SCRAPING_RESULTS_FILE):
            with open(SCRAPING_RESULTS_FILE, 'rb') as f:
                all_results = pickle.load(f)
            results = all_results.get(item_id, [])
            logger.debug(f"Retrieved scraping results for item {item_id}: {len(results)} results")
            return results
        else:
            logger.warning(f"No scraping results file found for item {item_id}")
            return []
    except Exception as e:
        logger.error(f"Error retrieving scraping results for item {item_id}: {str(e)}")
        return []

async def process_wanted(item: Dict[str, Any]) -> str:
    if 'show_title' in item:
        title = f"{item['show_title']} - {item.get('episode_title', 'Unknown Episode')}"
    else:
        title = item.get('title', 'Unknown Title')

    logger.info(f"Adding wanted item to scraper: {title}")

    await asyncio.sleep(0.2)  # Simulate work
    return 'Scraping'

async def process_scraping(item: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    if 'show_title' in item:
        title = f"{item['show_title']} - {item.get('episode_title', 'Unknown Episode')}"
    else:
        title = item.get('title', 'Unknown Title')

    logger.debug(f"Scraping for item: {title}")

    try:
        # Verify the status of the item
        current_status = await fetch_item_status(item['id'])
        if current_status != 'Scraping':
            logger.warning(f"Item {title} is no longer in Scraping state. Current state: {current_status}")
            return current_status, []

        multi = False
        if 'show_title' in item:
            # Check if there are multiple episodes in the season
            relevant_states = ['Wanted', 'Scraping', 'Adding', 'Checking']
            episodes_in_season = []

            for state in relevant_states:
                episodes_in_state = await fetch_and_convert(get_working_episodes_by_state, state)
                episodes_in_season.extend([ep for ep in episodes_in_state if ep['show_imdb_id'] == item['show_imdb_id'] and ep['season_number'] == item['season_number']])

            logger.debug(f"Number of episodes found in the same season across all states: {len(episodes_in_season)}")

            if len(episodes_in_season) > 1:
                multi = True

        logger.debug(f"Show appears to be MULTI: {multi}")

        if 'imdb_id' in item:  # Assuming movies have 'imdb_id'
            logger.debug(f"Scrape query: IMDb ID={item['imdb_id']}, type=movie, multi={multi}")
            results = await scrape(item['imdb_id'], 'movie')
        else:  # Assuming episodes have 'show_imdb_id', 'season_number', and 'episode_number'
            logger.debug(f"Scrape query: IMDb ID={item['show_imdb_id']}, type=episode, season={item['season_number']}, episode={item['episode_number']}, multi={multi}")
            results = await scrape(item['show_imdb_id'], 'episode', item['season_number'], item['episode_number'], multi)

        logger.debug(f"Scrape results: {results}")

        if results:
            logger.debug(f"Found results for item: {title}")
            # Store the scraping results in the database
            await update_scraping_results(item['id'], results)
            return 'Adding', results
        else:
            logger.info(f"No results found for item: {title}")
            return 'Wanted', []
    except Exception as e:
        logger.error(f"Error scraping item {title}: {str(e)}")
        return 'Wanted', []

async def process_adding(item: Dict[str, Any], scraping_results: List[Dict[str, Any]]) -> str:
    if 'show_title' in item:
        title = f"{item['show_title']} - {item.get('episode_title', 'Unknown Episode')}"
    else:
        title = item.get('title', 'Unknown Title')

    logger.info(f"Processing Adding item: {title}")

    for result in scraping_results:
        magnet_link = result.get('magnet')
        torrent_hash = extract_hash_from_magnet(magnet_link)
        if torrent_hash:
            cached_status = await is_cached_on_rd([torrent_hash])
            if cached_status[torrent_hash]:
                logger.debug(f"Selected cached torrent: {result['title']}")
                await add_to_real_debrid(magnet_link)

                # Check if the result is a season pack
                season_pack_detected = detect_season_pack(result.get('title', '')) != 'N/A'
                if season_pack_detected and 'show_imdb_id' in item:
                    logger.debug(f"Season pack detected for item: {title}")
                    relevant_states = ['Wanted', 'Scraping', 'Adding', 'Checking']
                    episodes_in_season = []

                    for state in relevant_states:
                        episodes_in_state = await fetch_and_convert(get_working_episodes_by_state, state)
                        episodes_in_season.extend([ep for ep in episodes_in_state if ep['show_imdb_id'] == item['show_imdb_id'] and ep['season_number'] == item['season_number']])

                    for episode in episodes_in_season:
                        if episode['id'] != item['id']:  # Skip the current item
                            await asyncio.to_thread(update_working_episode, episode['id'], 'Checking', result['title'], result['magnet'])

                    return 'Checking'

                return 'Checking'
            else:
                logger.debug(f"Torrent not cached: {result['title']}")

    logger.debug(f"No cached torrents found for item: {title}")
    return 'Wanted'

async def process_checking(item: Dict[str, Any]) -> str:
    if 'show_title' in item:
        title = f"{item['show_title']} - {item.get('episode_title', 'Unknown Episode')}"
    else:
        title = item.get('title', 'Unknown Title')

    logger.debug(f"Checking item: {title}")
    await asyncio.sleep(0.5)  # Simulate work

    # Simulate getting the torrent title used to fill the request
    filled_by_title = "Example Torrent Title"  # Replace with actual logic
    filled_by_magnet = "magnet:?xt=urn:btih:example"  # Replace with actual logic

    if 'episode_number' in item:
        await asyncio.to_thread(update_working_episode, item['id'], 'Filled', filled_by_title, filled_by_magnet)
    else:
        await asyncio.to_thread(update_working_movie, item['id'], 'Filled', filled_by_title, filled_by_magnet)

    return 'Filled'

async def process_filled(item: Dict[str, Any]) -> str:
    if 'show_title' in item:
        title = f"{item['show_title']} - {item.get('episode_title', 'Unknown Episode')}"
    else:
        title = item.get('title', 'Unknown Title')

    logger.info(f"Processing filled item: {title}")
    await asyncio.sleep(5)  # Simulate work
    return 'Completed'

async def main_loop(ui: MainUI, loop: urwid.MainLoop) -> None:
    global sync_requested
    await initial_setup()
    logger.info("Starting main loop...")

    # Schedule immediate processing tasks
    async def schedule_immediate_tasks():
        global sync_requested
        while True:
            tasks = [
                asyncio.create_task(process_queue('Wanted', process_wanted)),
                asyncio.create_task(process_queue('Scraping', process_scraping)),
                asyncio.create_task(process_queue('Adding', process_adding)),
                asyncio.create_task(process_queue('Checking', process_checking)),
                asyncio.create_task(process_queue('Filled', process_filled)),
            ]
            await asyncio.gather(*tasks)

            ui.update_display()
            loop.draw_screen()

            if sync_requested:
                await asyncio.to_thread(sync_mdblist_with_overseerr)
                sync_requested = False  # Reset the flag

            await asyncio.sleep(5)  # Short delay between iterations

    # Schedule periodic tasks
    async def periodic_task(interval, coro):
        while True:
            await asyncio.to_thread(coro)
            await asyncio.sleep(interval)

    asyncio.create_task(schedule_immediate_tasks())
    asyncio.create_task(periodic_task(5 * 60, sync_mdblist_with_overseerr))  # Every 5 minutes
    asyncio.create_task(periodic_task(5 * 60, get_unavailable_content))  # Every 5 minutes

    # Delay for 60 minutes before scheduling the periodic populate_db_from_plex task
    await asyncio.sleep(60 * 60)
    asyncio.create_task(periodic_task(60 * 60, lambda: populate_db_from_plex(get_setting('Plex', 'url'), get_setting('Plex', 'token'))))  # Every 60 minutes

async def run_program():
    ui = MainUI()
    logger.info("Program started")

    # Configure logging
    handler = UrwidLogHandler(ui.log_box)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    loop = ui.create_loop()

    ui_task = asyncio.create_task(update_ui_periodically(ui, loop))
    main_task = asyncio.create_task(main_loop(ui, loop))
    webhook_server_task = asyncio.create_task(start_webhook_server())

    def input_filter(keys, raw):
        if 'q' in keys or 'Q' in keys:
            raise urwid.ExitMainLoop()
        return keys

    loop.input_filter = input_filter

    try:
        loop.start()
        await asyncio.gather(main_task, ui_task, webhook_server_task)
    except urwid.ExitMainLoop:
        main_task.cancel()
        ui_task.cancel()
        webhook_server_task.cancel()
        try:
            await asyncio.gather(main_task, ui_task, webhook_server_task)
        except asyncio.CancelledError:
            pass
    finally:
        loop.stop()

# This function is used to integrate with your main menu
async def run_program_from_menu():
    await run_program()

if __name__ == "__main__":
    asyncio.run(run_program())
