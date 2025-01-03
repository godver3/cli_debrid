import logging
from metadata.metadata import refresh_release_dates

from database import update_media_item_state, get_all_media_items
from settings import get_all_settings

# Global variable to track initialization progress
initialization_status = {
    'current_step': '',
    'total_steps': 8,  # Increased from 4 to 8 for more granular steps
    'current_step_number': 0,
    'substep_details': '',  # Added to track substeps
    'error_details': None   # Added to track any errors
}

def get_initialization_status():
    return initialization_status

def update_initialization_step(step_name, substep_details='', error=None):
    initialization_status['current_step'] = step_name
    initialization_status['current_step_number'] += 1
    initialization_status['substep_details'] = substep_details
    initialization_status['error_details'] = error
    logging.info(f"Initialization step {initialization_status['current_step_number']}/{initialization_status['total_steps']}: {step_name}")
    if substep_details:
        logging.info(f"  Details: {substep_details}")

def format_item_log(item):
    if item['type'] == 'movie':
        return f"{item['title']} ({item['year']})"
    elif item['type'] == 'episode':
        return f"{item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}"
    else:
        return item['title']

def reset_queued_item_status():
    update_initialization_step("Checking for items to reset", "Identifying items in processing states")
    logging.info("Resetting queued item status...")
    states_to_reset = ['Scraping', 'Adding', 'Checking', 'Sleeping']
    total_reset = 0
    
    for state in states_to_reset:
        items = get_all_media_items(state=state)
        if items:
            update_initialization_step("Resetting items", f"Processing {len(items)} items in {state} state")
            for item in items:
                update_media_item_state(item['id'], 'Wanted')
                total_reset += 1
                logging.info(f"Reset item {format_item_log(item)} (ID: {item['id']}) from {state} to Wanted")
    
    update_initialization_step("Reset complete", f"Reset {total_reset} items to Wanted state")

def plex_collection_update(skip_initial_plex_update):
    from run_program import get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex

    update_initialization_step("Preparing Plex update", "Initializing Plex connection")
    logging.info("Updating Plex collection...")

    try:
        update_initialization_step("Scanning Plex library", 
                                 "Performing quick scan" if skip_initial_plex_update else "Performing full library scan")
        
        if skip_initial_plex_update:
            result = get_and_add_recent_collected_from_plex()
        else:
            result = get_and_add_all_collected_from_plex()
        
        # Check if we got any content from Plex, even if some items were skipped
        if result and isinstance(result, dict):
            movies = result.get('movies', [])
            episodes = result.get('episodes', [])
            if len(movies) > 0 or len(episodes) > 0:
                update_initialization_step("Processing Plex results", 
                                        f"Found {len(movies)} movies and {len(episodes)} episodes")
                return True
            
        error_msg = "Plex scan returned no content - skipping collection update to prevent data loss"
        update_initialization_step("Plex scan failed", error_msg, error=error_msg)
        logging.error(error_msg)
        return False
        
    except Exception as e:
        error_msg = f"Error during Plex collection update: {str(e)}"
        update_initialization_step("Plex scan error", error_msg, error=error_msg)
        logging.error(error_msg)
        logging.error("Skipping collection update to prevent data loss")
        return False

def get_all_wanted_from_enabled_sources():
    from routes.debug_routes import get_and_add_wanted_content

    content_sources = get_all_settings().get('Content Sources', {})
    enabled_sources = [source_id for source_id, data in content_sources.items() 
                      if data.get('enabled', False)]
    
    update_initialization_step("Processing content sources", 
                             f"Found {len(enabled_sources)} enabled sources")
    
    for source_id in enabled_sources:
        update_initialization_step(f"Processing source: {source_id}", 
                                 f"Retrieving wanted content from {source_id}")
        get_and_add_wanted_content(source_id)

    update_initialization_step("Content source processing complete", 
                             f"Processed {len(enabled_sources)} sources")

def initialize(skip_initial_plex_update=False):
    # Reset initialization status
    initialization_status['current_step'] = ''
    initialization_status['current_step_number'] = 0
    initialization_status['substep_details'] = ''
    initialization_status['error_details'] = None
    
    logging.info("Starting initialization...")
    
    reset_queued_item_status()
    plex_result = plex_collection_update(skip_initial_plex_update)
    
    if plex_result:
        get_all_wanted_from_enabled_sources()
    
    update_initialization_step("Refreshing release dates", "Updating metadata for all items")
    refresh_release_dates()
    
    final_status = "completed successfully" if plex_result else "completed with Plex update issues"
    update_initialization_step("Initialization complete", final_status)
    
    return plex_result
