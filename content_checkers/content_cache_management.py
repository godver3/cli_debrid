import logging
import os
import pickle
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
CACHE_EXPIRY_DAYS = 7

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
    from settings import get_setting
    
    # If cache checking is disabled, always process the item
    if get_setting('Debug', 'disable_content_source_caching', False):
        return True
    
    cache_key = create_cache_key(item, source_id)
    cache_item = cache.get(cache_key)
    
    if not cache_item:
        return True
    
    last_processed = cache_item['timestamp']
    if isinstance(last_processed, (int, float)):
        last_processed = datetime.fromtimestamp(last_processed)
    
    # Check if cache has expired
    if datetime.now() - last_processed >= timedelta(days=CACHE_EXPIRY_DAYS):
        return True
    
    # For TV shows with season info, check if requested seasons match
    if item.get('media_type') == 'tv' and 'requested_seasons' in item:
        cached_seasons = cache_item.get('data', {}).get('requested_seasons', [])
        if set(item['requested_seasons']) != set(cached_seasons):
            return True
    
    return False

def update_cache_for_item(item: Dict[str, Any], source_id: str, cache: Dict[str, Any]) -> None:
    """Update cache entry for a specific item."""
    cache_key = create_cache_key(item, source_id)
    cache[cache_key] = {
        'timestamp': datetime.now(),
        'data': item.copy()  # Store a copy of the full item data
    } 