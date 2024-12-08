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

    try:
        if skip_initial_plex_update:
            result = get_and_add_recent_collected_from_plex()
        else:
            result = get_and_add_all_collected_from_plex()
        
        # Check if we got any content from Plex, even if some items were skipped
        if result and isinstance(result, dict):
            movies = result.get('movies', [])
            episodes = result.get('episodes', [])
            if len(movies) > 0 or len(episodes) > 0:
                logging.info(f"Successfully processed Plex content: {len(movies)} movies and {len(episodes)} episodes")
                return True
            
        logging.error("Plex scan returned no content - skipping collection update to prevent data loss")
        return False
        
    except Exception as e:
        logging.error(f"Error during Plex collection update: {str(e)}")
        logging.error("Skipping collection update to prevent data loss")
        return False

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
    logging.info("Starting initialization...")
    
    # Only update collection if Plex scan is successful
    plex_success = plex_collection_update(skip_initial_plex_update)
    
    if not plex_success:
        logging.warning("Skipping initial collection update due to Plex scan failure")
    else:
        get_all_wanted_from_enabled_sources()
    
    refresh_release_dates()

    
