import logging
import os
import pickle
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import random

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
CACHE_EXPIRY_HOURS = 6

def get_cache_file_path(source_id: str) -> str:
    """Get the cache file path for a specific content source."""
    safe_source_id = source_id.replace('/', '_').replace('\\', '_')
    return os.path.join(DB_CONTENT_DIR, f'content_source_{safe_source_id}_cache.pkl')

def load_source_cache(source_id: str) -> Dict[str, Any]:
    """Load cache for a specific content source."""
    cache_file = get_cache_file_path(source_id)
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as f:
                cache = pickle.load(f)
                logging.debug(f"Loaded cache for source {source_id} with {len(cache)} entries")
                return cache
        else:
            logging.debug(f"No existing cache file found for source {source_id}")
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading cache for source {source_id}: {e}. Creating a new cache.")
    return {}

def save_source_cache(source_id: str, cache: Dict[str, Any]) -> None:
    """Save cache for a specific content source."""
    cache_file = get_cache_file_path(source_id)
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, 'wb') as f:
            pickle.dump(cache, f)
            logging.debug(f"Saved cache for source {source_id} with {len(cache)} entries")
    except Exception as e:
        logging.error(f"Error saving cache for source {source_id}: {e}")

def create_cache_key(item: Dict[str, Any], source_id: str) -> str:
    """Create a cache key for an item from a specific source."""
    # Base key components
    key_parts = []
    
    # Add ID (prefer IMDB, fallback to TMDB)
    if 'imdb_id' in item:
        key_parts.append(f"imdb_{item['imdb_id']}")
    elif 'tmdb_id' in item:
        key_parts.append(f"tmdb_{item['tmdb_id']}")
    
    # Add media type - only 'movie' or 'tv' at this stage
    media_type = item.get('media_type', item.get('type', 'unknown'))
    if media_type == 'episode':
        media_type = 'tv'  # Normalize episode to tv for caching
    key_parts.append(media_type)
    
    # For TV shows with specific season requests (Overseerr)
    if media_type == 'tv' and 'requested_seasons' in item:
        key_parts.append(f"s{'_'.join(map(str, sorted(item['requested_seasons'])))}")
    
    # Add source-specific info
    key_parts.append(source_id)
    
    return '_'.join(key_parts)

def should_process_item(item: Dict[str, Any], source_id: str, cache: Dict[str, Any]) -> bool:
    """
    Determine if an item should be processed based on cache status.
    Returns True if item should be processed, False if it should be skipped.
    """
    # Check if cache checking is disabled in debug settings
    from utilities.settings import get_setting
    
    # If cache checking is disabled, always process the item
    if get_setting('Debug', 'disable_content_source_caching', False):
        return True
    
    cache_key = create_cache_key(item, source_id)
    cache_item = cache.get(cache_key)
    
    if not cache_item:
        return True
    
    last_processed = cache_item.get('timestamp')
    if not last_processed:
         # Should ideally not happen if timestamp is always stored, but handle defensively
         logging.warning(f"Cache item {cache_key} for source {source_id} missing timestamp. Reprocessing.")
         return True

    if isinstance(last_processed, (int, float)):
        try:
            last_processed = datetime.fromtimestamp(last_processed)
        except Exception as e:
             logging.warning(f"Error converting timestamp for {cache_key}: {e}. Reprocessing.")
             return True
    elif not isinstance(last_processed, datetime):
        logging.warning(f"Invalid timestamp format for {cache_key}: {type(last_processed)}. Reprocessing.")
        return True

    # --- Retrieve or Calculate Expiry Duration ---
    # Get stored random expiry duration if available
    expiry_duration_hours = cache_item.get('expiry_duration_hours')

    if expiry_duration_hours is None:
        # Fallback for older cache entries or if calculation failed during update
        # Use the old logic as a default
        logging.debug(f"Cache item {cache_key} missing expiry duration. Using fallback logic.")
        if source_id.startswith('Collected_'): # Check if it's a Collected source
            expiry_duration_hours = 48
        else:
            # Use the global constant or a default like 12 if CACHE_EXPIRY_HOURS is also removed
            expiry_duration_hours = CACHE_EXPIRY_HOURS # Or change to 12 if preferred default

    if not isinstance(expiry_duration_hours, (int, float)) or expiry_duration_hours <= 0:
        logging.warning(f"Invalid expiry duration {expiry_duration_hours} for {cache_key}. Using default 12 hours.")
        expiry_duration_hours = 12 # Default to 12 hours if invalid

    # --- Check if cache has expired using the determined duration ---
    if datetime.now() - last_processed >= timedelta(hours=expiry_duration_hours):
        logging.debug(f"Cache expired for {cache_key} (Expiry: {expiry_duration_hours} hours). Reprocessing.")
        return True # Cache expired, should process

    # --- Additional Checks (e.g., seasons) ---
    # For TV shows with season info, check if requested seasons match
    # This check remains important even if not expired, in case requested seasons changed
    if item.get('media_type') == 'tv' and 'requested_seasons' in item:
        cached_data = cache_item.get('data', {})
        cached_seasons = cached_data.get('requested_seasons', [])
        if set(item['requested_seasons']) != set(cached_seasons):
            logging.debug(f"Requested seasons changed for {cache_key}. Reprocessing.")
            return True # Requested seasons differ, should process

    return False # Cache hit and still valid, skip processing

def update_cache_for_item(item: Dict[str, Any], source_id: str, cache: Dict[str, Any]) -> None:
    """Update cache entry for a specific item with randomized expiry."""
    cache_key = create_cache_key(item, source_id)

    # Calculate randomized expiry duration: 12 hours +/- 6 hours (range 6 to 18)
    base_expiry_hours = 12
    random_offset_hours = random.uniform(-6, 6) # Offset between -6 and +6 hours
    expiry_duration_hours = base_expiry_hours + random_offset_hours
    # Ensure it's at least a minimum positive duration, e.g., 1 hour
    expiry_duration_hours = max(1.0, expiry_duration_hours)
    logging.debug(f"Calculated expiry for {cache_key}: {expiry_duration_hours:.2f} hours")

    cache[cache_key] = {
        'timestamp': datetime.now(),
        'expiry_duration_hours': expiry_duration_hours, # Store the calculated duration
        'data': item.copy()  # Store a copy of the full item data
    }
    logging.debug(f"Updated cache for {cache_key}") 