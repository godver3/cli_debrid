import logging
from typing import List, Dict, Any, Tuple
from utilities.settings import get_all_settings, get_setting
import os
import pickle
from datetime import datetime, timedelta
from database.database_reading import get_distinct_imdb_ids  # NEW: efficient helper

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
    collected_sources = [data for source, data in content_sources.items()
                         if source.startswith('Collected') and data.get('enabled', False)]

    if not collected_sources:
        logging.info("No enabled Collected sources found in settings.")
        return []

    disable_caching = True  # Hard-coded for now; retained existing behaviour
    cache = {} if disable_caching else load_collected_cache()
    current_time = datetime.now()

    # --- NEW: get unique imdb_ids in a single lightweight query ---
    imdb_ids = get_distinct_imdb_ids(states=["Wanted", "Collected"], media_type="episode")
    logging.info(f"Found {len(imdb_ids)} unique TV shows from local database")

    # Prepare base list of items (may be filtered further if caching re-enabled)
    base_items: List[Dict[str, Any]] = []
    cache_skipped = 0  # Keep metric for potential future use

    for imdb_id in imdb_ids:
        if not disable_caching:
            cache_key = f"{imdb_id}_tv"
            cache_item = cache.get(cache_key)
            if cache_item:
                last_processed = cache_item['timestamp']
                if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                    cache_skipped += 1
                    continue
            # Update cache entry timestamp
            cache[cache_key] = {
                'timestamp': current_time,
                'data': {'imdb_id': imdb_id, 'media_type': 'tv'}
            }

        base_items.append({'imdb_id': imdb_id, 'media_type': 'tv'})

    # Build final structure per collected source
    all_wanted_items: List[Tuple[List[Dict[str, Any]], Dict[str, bool]]] = []
    for source in collected_sources:
        versions = source.get('versions', {})
        # Provide a shallow copy so that downstream mutation per-source (if any)
        # does not affect the shared list
        all_wanted_items.append((list(base_items), versions))

    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_collected_cache(cache)

    return all_wanted_items