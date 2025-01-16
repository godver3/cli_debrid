import logging
from typing import List, Dict, Any, Tuple
from database import get_all_media_items
from settings import get_all_settings
import os
import pickle
from datetime import datetime, timedelta

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
COLLECTED_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'collected_cache.pkl')
CACHE_EXPIRY_DAYS = 7

def load_collected_cache():
    try:
        if os.path.exists(COLLECTED_CACHE_FILE):
            with open(COLLECTED_CACHE_FILE, 'rb') as f:
                return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading Collected cache: {e}. Creating a new cache.")
    return {}

def save_collected_cache(cache):
    try:
        os.makedirs(os.path.dirname(COLLECTED_CACHE_FILE), exist_ok=True)
        with open(COLLECTED_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving Collected cache: {e}")

def get_wanted_from_collected() -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    collected_sources = [data for source, data in content_sources.items() if source.startswith('Collected') and data.get('enabled', False)]
    
    if not collected_sources:
        logging.warning("No enabled Collected sources found in settings.")
        return []

    all_wanted_items = []
    cache = load_collected_cache()
    current_time = datetime.now()

    for source in collected_sources:
        versions = source.get('versions', {})

        wanted_items = get_all_media_items(state="Wanted", media_type="episode")
        collected_items = get_all_media_items(state="Collected", media_type="episode")
        
        all_items = wanted_items + collected_items
        consolidated_items = {}

        for item in all_items:
            imdb_id = item['imdb_id']
            if imdb_id not in consolidated_items:
                # Check cache for this item
                cache_key = f"{imdb_id}_tv"  # All collected items are TV shows
                cache_item = cache.get(cache_key)
                
                if cache_item:
                    last_processed = cache_item['timestamp']
                    if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                        logging.debug(f"Skipping recently processed item: {cache_key}")
                        continue
                
                # Add or update cache entry
                cache[cache_key] = {
                    'timestamp': current_time,
                    'data': {
                        'imdb_id': imdb_id,
                        'media_type': 'tv'
                    }
                }
                
                consolidated_items[imdb_id] = {
                    'imdb_id': imdb_id,
                    'media_type': 'tv'
                }

        result = list(consolidated_items.values())

        # Debug printing
        logging.info(f"Retrieved {len(result)} unique TV shows from local database")
        for item in result:
            logging.debug(f"IMDB ID: {item['imdb_id']}, Media Type: {item['media_type']}")

        all_wanted_items.append((result, versions))

    # Save updated cache
    save_collected_cache(cache)
    return all_wanted_items