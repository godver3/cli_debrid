import logging
from metadata.metadata import refresh_release_dates, process_metadata
from content_checkers.overseerr import get_wanted_from_overseerr
from content_checkers.mdb_list import get_wanted_from_mdblists
from utilities.plex_functions import get_collected_from_plex
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
    if skip_initial_plex_update:
        logging.info("Skipping initial Plex update due to debug flag.")
        return
    logging.info("Updating Plex collection...")
    collected_content = get_collected_from_plex('all')
    if collected_content:
        add_collected_items(collected_content['movies'] + collected_content['episodes'])

def overseerr_wanted_update():
    logging.info("Updating Overseerr wanted items...")
    
    wanted_content = get_wanted_from_overseerr()
    if wanted_content:
        wanted_content_processed = process_metadata(wanted_content)
        if wanted_content_processed:
            add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])

def mdblist_wanted_update():
    logging.info("Updating MDBList wanted items...")
    
    wanted_content = get_wanted_from_mdblists()
    if wanted_content:
        wanted_content_processed = process_metadata(wanted_content)
        if wanted_content_processed:
            add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])

def format_item_log(item):
    if item['type'] == 'movie':
        return f"{item['title']} ({item['year']})"
    elif item['type'] == 'episode':
        return f"{item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}"
    else:
        return item['title']

def initialize(skip_initial_plex_update=False):
    logging.debug("Running initial setup...")
    reset_queued_item_status()
    plex_collection_update(skip_initial_plex_update)
    overseerr_wanted_update()
            
    # Conditionally run mdblist_wanted_update
    mdb_list_api_key = get_setting('MDBList', 'api_key', '')
    mdb_list_urls = get_setting('MDBList', 'urls', '')
    if mdb_list_api_key and mdb_list_urls:
        mdblist_wanted_update()
        
    refresh_release_dates()

    
