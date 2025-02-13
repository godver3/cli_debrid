import os
import pickle
import time
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Tuple
from settings import get_setting

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
    If caching is disabled via settings, immediately execute the removal.
    
    Args:
        item_title: Title of the item to remove
        item_path: Path of the item to remove
        episode_title: Optional episode title for TV shows
    """
    # Check if caching is enabled
    if not get_setting('Debug', 'enable_plex_removal_caching', default=True):
        # If caching is disabled, execute removal immediately
        from utilities.plex_functions import remove_file_from_plex
        remove_file_from_plex(item_title, item_path, episode_title)
        return

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
    
    Args:
        min_age_hours: Minimum age in hours before processing a cached removal
    """
    from utilities.plex_functions import remove_file_from_plex
    
    cache = _load_cache()
    if not cache:
        return
        
    current_time = time.time()
    min_age_seconds = min_age_hours * 3600
    processed_keys = []
    
    for key, entries in cache.items():
        for entry in entries:
            item_title, item_path, episode_title, timestamp = entry
            
            # Check if entry is old enough
            if current_time - timestamp >= min_age_seconds:
                try:
                    # Actually remove from Plex
                    remove_file_from_plex(item_title, item_path, episode_title)
                    logging.info(f"Processed cached Plex removal for {item_title} ({item_path})")
                    processed_keys.append(key)
                except Exception as e:
                    logging.error(f"Error processing cached Plex removal for {item_title}: {str(e)}")
    
    # Remove processed entries
    for key in processed_keys:
        cache.pop(key, None)
    
    # Save updated cache
    _save_cache(cache) 