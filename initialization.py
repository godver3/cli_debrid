import logging
import time
from metadata.metadata import refresh_release_dates
from database import update_media_item_state, get_all_media_items
from settings import get_all_settings

# Progress ranges for each phase
PROGRESS_RANGES = {
    'reset': (0, 5),    # 5 seconds
    'plex': (5, 50),    # 2 minutes
    'sources': (50, 90), # 2 minutes
    'release': (90, 100) # 30 seconds
}

# Duration for each phase in seconds
PHASE_DURATIONS = {
    'reset': 5,
    'plex': 120,  # 2 minutes
    'sources': 120,  # 2 minutes
    'release': 30
}

# Global variable to track initialization progress
initialization_status = {
    'current_step': '',
    'total_steps': 4,
    'current_step_number': 0,
    'progress_value': 0,  # Current progress percentage
    'substep_details': '',
    'error_details': None,
    'is_substep': False,
    'phase_start_time': 0,  # Start time of current phase
    'current_phase': None   # Current phase identifier
}

def get_initialization_status():
    # Update progress based on elapsed time if in a timed phase
    if initialization_status['current_phase'] and initialization_status['phase_start_time'] > 0:
        phase = initialization_status['current_phase']
        start_time = initialization_status['phase_start_time']
        current_time = time.time()
        
        # Calculate elapsed percentage of the phase
        elapsed_time = current_time - start_time
        phase_duration = PHASE_DURATIONS.get(phase, 0)
        
        if phase_duration > 0:
            progress_range = PROGRESS_RANGES.get(phase, (0, 0))
            range_size = progress_range[1] - progress_range[0]
            
            # Calculate progress within the phase
            progress_pct = min(1.0, elapsed_time / phase_duration)
            new_progress = progress_range[0] + (range_size * progress_pct)
            
            # Update the progress value
            initialization_status['progress_value'] = new_progress
    
    return initialization_status

def start_phase(phase_name, step_name, details=''):
    """Start a new timed phase of initialization."""
    initialization_status['current_phase'] = phase_name
    initialization_status['phase_start_time'] = time.time()
    initialization_status['current_step'] = step_name
    initialization_status['substep_details'] = details
    initialization_status['progress_value'] = PROGRESS_RANGES.get(phase_name, (0, 0))[0]
    initialization_status['current_step_number'] += 1

def complete_phase(phase_name):
    """Mark a phase as complete, setting progress to the end of its range."""
    if phase_name in PROGRESS_RANGES:
        initialization_status['progress_value'] = PROGRESS_RANGES[phase_name][1]
    initialization_status['phase_start_time'] = 0  # Stop the time-based progress

def update_initialization_step(step_name, substep_details='', error=None, is_substep=False):
    """Update initialization status without changing the progress timing."""
    initialization_status['current_step'] = step_name
    initialization_status['substep_details'] = substep_details
    initialization_status['error_details'] = error
    initialization_status['is_substep'] = is_substep
    
    if not is_substep:
        initialization_status['current_step_number'] += 1
    
    logging.info(f"Initialization {'substep' if is_substep else 'step'} {initialization_status['current_step_number']}/{initialization_status['total_steps']}: {step_name}")
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
    update_initialization_step("Reset Items", "Identifying items in processing states", is_substep=True)
    logging.info("Resetting queued item status...")
    states_to_reset = ['Scraping', 'Adding', 'Checking', 'Sleeping']
    total_reset = 0
    
    for state in states_to_reset:
        items = get_all_media_items(state=state)
        if items:
            update_initialization_step("Reset Items", f"Processing {len(items)} items in {state} state", is_substep=True)
            for item in items:
                update_media_item_state(item['id'], 'Wanted')
                total_reset += 1
                logging.info(f"Reset item {format_item_log(item)} (ID: {item['id']}) from {state} to Wanted")
    
    update_initialization_step("Reset Items", f"Reset {total_reset} items to Wanted state", is_substep=True)

def plex_collection_update(skip_initial_plex_update):
    from run_program import get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex

    update_initialization_step("Plex Update", "Starting Plex scan")
    logging.info("Updating Plex collection...")

    try:
        update_initialization_step("Plex Update", 
                                 "Performing quick scan" if skip_initial_plex_update else "Performing full library scan",
                                 is_substep=True)
        
        if skip_initial_plex_update:
            result = get_and_add_recent_collected_from_plex()
        else:
            result = get_and_add_all_collected_from_plex()
        
        # Check if we got any content from Plex, even if some items were skipped
        if result and isinstance(result, dict):
            movies = result.get('movies', [])
            episodes = result.get('episodes', [])
            if len(movies) > 0 or len(episodes) > 0:
                update_initialization_step("Plex Update", 
                                        f"Found {len(movies)} movies and {len(episodes)} episodes",
                                        is_substep=True)
                return True
            
        error_msg = "Plex scan returned no content - skipping collection update to prevent data loss"
        update_initialization_step("Plex Update", error_msg, error=error_msg, is_substep=True)
        logging.error(error_msg)
        return False
        
    except Exception as e:
        error_msg = f"Error during Plex collection update: {str(e)}"
        update_initialization_step("Plex Update", error_msg, error=error_msg, is_substep=True)
        logging.error(error_msg)
        logging.error("Skipping collection update to prevent data loss")
        return False

def get_all_wanted_from_enabled_sources():
    from routes.debug_routes import get_and_add_wanted_content

    content_sources = get_all_settings().get('Content Sources', {})
    enabled_sources = [source_id for source_id, data in content_sources.items() 
                      if data.get('enabled', False)]
    
    update_initialization_step("Content Sources", 
                             f"Found {len(enabled_sources)} enabled sources",
                             is_substep=True)
    
    for source_id in enabled_sources:
        update_initialization_step("Content Sources", 
                                 f"Retrieving wanted content from {source_id}",
                                 is_substep=True)
        get_and_add_wanted_content(source_id)

    update_initialization_step("Content Sources", 
                             f"Processed {len(enabled_sources)} sources",
                             is_substep=True)

def initialize(skip_initial_plex_update=False):
    """Initialize the application state."""
    # Reset initialization status
    initialization_status['current_step'] = ''
    initialization_status['current_step_number'] = 0
    initialization_status['progress_value'] = 0
    initialization_status['substep_details'] = ''
    initialization_status['error_details'] = None
    initialization_status['is_substep'] = False
    initialization_status['phase_start_time'] = 0
    initialization_status['current_phase'] = None

    # Start initialization
    update_initialization_step('Starting initialization')
    
    # Reset Items Phase (5 seconds)
    start_phase('reset', 'Reset Items', 'Starting item reset')
    reset_queued_item_status()
    complete_phase('reset')
    
    # Plex Update Phase (2 minutes)
    start_phase('plex', 'Plex Update', 'Starting Plex scan')
    plex_result = plex_collection_update(skip_initial_plex_update)
    complete_phase('plex')
    
    # Content Sources Phase (2 minutes)
    if plex_result:
        start_phase('sources', 'Content Sources', 'Processing content sources')
        get_all_wanted_from_enabled_sources()
        complete_phase('sources')
    
    # Release Dates Phase (30 seconds)
    start_phase('release', 'Release Dates', 'Updating metadata for all items')
    refresh_release_dates()
    complete_phase('release')
    
    # Complete
    final_status = "completed successfully" if plex_result else "completed with Plex update issues"
    update_initialization_step("Complete", final_status)
    
    return plex_result
