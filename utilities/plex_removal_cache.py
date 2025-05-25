import os
import pickle
import time
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Tuple
from utilities.settings import get_setting
from database.database_reading import get_media_item_by_filename

# Cache file path
CACHE_FILE = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'plex_removal_cache.pkl')

def _load_cache() -> Dict[str, List[Tuple[str, str, Optional[str], float]]]:
    """Load the removal cache from disk."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'rb') as f:
                return pickle.load(f)
    except Exception as e:
        logging.error(f"Error loading Plex removal cache: {str(e)}")
    return {}

def _save_cache(cache: Dict[str, List[Tuple[str, str, Optional[str], float]]]) -> None:
    """Save the removal cache to disk."""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving Plex removal cache: {str(e)}")

def cache_plex_removal(item_title: str, item_path: str, episode_title: Optional[str] = None) -> None:
    """
    Cache a Plex removal operation for later execution.
    If caching is disabled via settings, immediately execute the removal,
    including attempting to remove symlinks from disk first.
    
    Args:
        item_title: Title of the item to remove
        item_path: Path of the item to remove
        episode_title: Optional episode title for TV shows
    """
    if item_path is None:
        logging.error(f"Attempted to cache Plex removal for title '{item_title}' with a None item_path. Skipping.")
        return

    # Check if caching is enabled
    if not get_setting('Debug', 'enable_plex_removal_caching', default=True):
        logging.info(f"Plex removal caching is disabled. Processing immediate removal for {item_title} ({item_path}).")
        from utilities.plex_functions import remove_file_from_plex
        
        can_attempt_plex_removal = True

        # Step 1: If item_path is a symlink and exists on disk, try to remove it from disk.
        if os.path.islink(item_path):
            if os.path.exists(item_path): # Symlink exists on disk
                logging.info(f"Immediate removal: Item {item_path} for '{item_title}' is an existing symlink. Attempting to remove from disk.")
                try:
                    os.unlink(item_path)
                    logging.info(f"Immediate removal: Successfully removed symlink {item_path} from disk.")
                except OSError as e:
                    logging.error(f"Immediate removal: Failed to remove symlink {item_path} from disk: {e}. Plex removal will not be attempted.")
                    can_attempt_plex_removal = False
            else: # Symlink was defined, but it's already gone from disk.
                logging.info(f"Immediate removal: Symlink {item_path} for '{item_title}' does not exist on disk (already removed). Proceeding with Plex removal.")
        
        # Step 2: If conditions allow, attempt to remove the item from Plex.
        if can_attempt_plex_removal:
            try:
                logging.info(f"Immediate removal: Attempting Plex removal for {item_title} ({item_path}).")
                remove_file_from_plex(item_title, item_path, episode_title)
                logging.info(f"Immediate removal: Successfully processed Plex removal for {item_title} ({item_path}).")
            except Exception as e:
                logging.error(f"Immediate removal: Error during Plex removal for {item_title} ({item_path}): {str(e)}.")
        else:
            logging.warning(f"Immediate removal: Plex removal skipped for {item_title} ({item_path}) due to issues with symlink handling.")
        return # End of immediate removal path

    # Caching is enabled, proceed as before
    cache = _load_cache()
    timestamp = time.time()
    
    # Create a unique key based on the path to avoid duplicates
    key = item_path
    
    if key not in cache:
        cache[key] = []
    
    # Add the removal request to the cache
    cache[key].append((item_title, item_path, episode_title, timestamp))
    
    _save_cache(cache)
    logging.info(f"Cached Plex removal request for {item_title} ({item_path})")

def process_removal_cache(min_age_hours: int = 6) -> None:
    """
    Process cached removal operations that are older than the specified age.
    Checks if the item is back in the local database before attempting removal.
    If an item_path is a symlink and exists on disk, it will be deleted from disk first.
    Then, the item will be removed from Plex.
    
    Args:
        min_age_hours: Minimum age in hours before processing a cached removal
    """
    from utilities.plex_functions import remove_file_from_plex
    
    cache = _load_cache()
    if not cache:
        logging.debug("Plex removal cache is empty. Nothing to process.")
        return
        
    current_time = time.time()
    min_age_seconds = min_age_hours * 3600

    # min_age_seconds = 0 # For testing, set to 0 to process all items immediately
    
    # Build a new cache dictionary with only the entries that need to be kept for the next run.
    next_run_cache = {} 
    # Define states that indicate an item is actively managed and should not be removed by cache
    active_db_states = ['Collected', 'Upgrading', 'Checking']

    for key, entries in cache.items():
        remaining_entries_for_key = []
        for entry in entries:
            item_title, item_path, episode_title, timestamp = entry
            
            # Default assumption: keep the entry in the cache unless successfully processed or cancelled.
            entry_should_be_kept_in_cache = True 

            if current_time - timestamp >= min_age_seconds:
                logging.debug(f"Processing entry for {item_title} ({item_path}), age {current_time - timestamp:.0f}s / {min_age_seconds}s required")
                
                # --- New check: Is the item back in our local database? ---
                try:
                    db_item_filename = os.path.basename(item_path)
                    db_item = get_media_item_by_filename(db_item_filename)
                    if db_item and db_item.get('state') in active_db_states:
                        logging.info(f"Item '{item_title}' ({item_path}) found back in the database with state '{db_item.get('state')}'. Cancelling Plex removal from cache.")
                        entry_should_be_kept_in_cache = False # Remove from cache as it's "handled" (cancelled)
                        # Continue to the next entry in this key's list
                        if entry_should_be_kept_in_cache: # Should be False here due to above line
                             remaining_entries_for_key.append(entry)
                        continue 
                except Exception as e_db_check:
                    logging.error(f"Error checking database for item {item_path}: {str(e_db_check)}. Proceeding with removal logic.")
                # --- End of new check ---

                can_attempt_plex_removal = True # Assume true, may be set to false if symlink disk removal fails

                # Step 1: If item_path is a symlink and exists on disk, try to remove it from disk.
                if os.path.islink(item_path):
                    if os.path.exists(item_path): # Symlink exists on disk
                        logging.info(f"Item {item_path} for '{item_title}' is an existing symlink. Attempting to remove from disk.")
                        try:
                            os.unlink(item_path)
                            logging.info(f"Successfully removed symlink {item_path} from disk.")
                        except OSError as e:
                            logging.error(f"Failed to remove symlink {item_path} from disk: {e}. Plex removal will not be attempted for this item in this run. Keeping in cache.")
                            can_attempt_plex_removal = False # Prevent Plex removal attempt
                    else: # Symlink was defined in cache, but it's already gone from disk.
                        logging.info(f"Symlink {item_path} for '{item_title}' does not exist on disk (already removed). Proceeding with Plex removal.")
                
                # Step 2: If conditions allow, attempt to remove the item from Plex.
                if can_attempt_plex_removal:
                    try:
                        logging.info(f"Attempting Plex removal for {item_title} ({item_path}).")
                        remove_file_from_plex(item_title, item_path, episode_title)
                        logging.info(f"Successfully processed Plex removal for {item_title} ({item_path}).")
                        entry_should_be_kept_in_cache = False # Successfully processed, so don't keep this entry.
                    except Exception as e:
                        logging.error(f"Error during Plex removal for {item_title} ({item_path}): {str(e)}. Keeping in cache.")
                        # entry_should_be_kept_in_cache remains True if Plex removal fails.
                # else (can_attempt_plex_removal is False due to symlink unlink failure):
                # entry_should_be_kept_in_cache remains True, as processing could not complete.
            # else (entry is not old enough yet):
            # entry_should_be_kept_in_cache remains True.
            
            if entry_should_be_kept_in_cache:
                remaining_entries_for_key.append(entry)
        
        if remaining_entries_for_key:
            next_run_cache[key] = remaining_entries_for_key
            
    _save_cache(next_run_cache)
    if not next_run_cache and cache: # Cache was not empty before, but is now
        logging.info("Finished processing Plex removal cache. All items processed.")
    elif not next_run_cache and not cache: # Cache was already empty
        pass # No need to log "finished processing" if there was nothing to process
    else:
        logging.info(f"Finished processing Plex removal cache. {sum(len(v) for v in next_run_cache.values())} item(s) remain.") 