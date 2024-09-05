import logging
from metadata.metadata import refresh_release_dates

from database import update_media_item_state, get_all_media_items
from settings import get_all_settings

def reset_queued_item_status():
    logging.info("Resetting queued item status...")
    states_to_reset = ['Scraping', 'Adding', 'Checking', 'Sleeping']
    for state in states_to_reset:
        items = get_all_media_items(state=state)
        for item in items:
            update_media_item_state(item['id'], 'Wanted')
            logging.info(f"Reset item {format_item_log(item)} (ID: {item['id']}) from {state} to Wanted")

def plex_collection_update(skip_initial_plex_update):
    from run_program import get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex

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

def get_all_wanted_from_enabled_sources():
    from routes.debug_routes import get_and_add_wanted_content

    content_sources = get_all_settings().get('Content Sources', {})
    
    for source_id, source_data in content_sources.items():
        if not source_data.get('enabled', False):
            logging.info(f"Skipping disabled source: {source_id}")
            continue

        get_and_add_wanted_content(source_id)

    logging.info("Finished processing all enabled content sources")

def initialize(skip_initial_plex_update=False):
    #logging.debug("Running initial setup...")
    #reset_queued_item_status()
    plex_collection_update(skip_initial_plex_update)
    
    get_all_wanted_from_enabled_sources()
    
    refresh_release_dates()

    
