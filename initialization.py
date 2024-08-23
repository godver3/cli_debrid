import logging
from metadata.metadata import refresh_release_dates, process_metadata
from content_checkers.overseerr import get_wanted_from_overseerr
from content_checkers.mdb_list import get_wanted_from_mdblists
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist
from utilities.debug_commands import get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex
from utilities.debug_commands import get_all_wanted_from_enabled_sources
from database import add_wanted_items, add_collected_items, update_media_item_state, get_all_media_items
from settings import get_setting

def reset_queued_item_status():
    logging.info("Resetting queued item status...")
    states_to_reset = ['Scraping', 'Adding', 'Checking', 'Sleeping']
    for state in states_to_reset:
        items = get_all_media_items(state=state)
        for item in items:
            update_media_item_state(item['id'], 'Wanted')
            logging.info(f"Reset item {format_item_log(item)} (ID: {item['id']}) from {state} to Wanted")

def plex_collection_update(skip_initial_plex_update):
    logging.info("Updating Plex collection...")

    if skip_initial_plex_update:
        get_and_add_recent_collected_from_plex()
        return
    get_and_add_all_collected_from_plex()

def format_item_log(item):
    if item['type'] == 'movie':
        return f"{item['title']} ({item['year']})"
    elif item['type'] == 'episode':
        return f"{item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}"
    else:
        return item['title']

def initialize(skip_initial_plex_update=False):
    #logging.debug("Running initial setup...")
    #reset_queued_item_status()
    plex_collection_update(skip_initial_plex_update)
    
    get_all_wanted_from_enabled_sources()
    
    refresh_release_dates()

    
