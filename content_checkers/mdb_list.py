import logging
from api_tracker import api
from typing import List, Dict, Any, Tuple
from settings import get_all_settings, get_setting
from database import get_media_item_presence
import os
import pickle
from datetime import datetime, timedelta

REQUEST_TIMEOUT = 10  # seconds

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
MDBLIST_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'mdblist_cache.pkl')
CACHE_EXPIRY_DAYS = 7

def load_mdblist_cache():
    try:
        if os.path.exists(MDBLIST_CACHE_FILE):
            with open(MDBLIST_CACHE_FILE, 'rb') as f:
                return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading MDB List cache: {e}. Creating a new cache.")
    return {}

def save_mdblist_cache(cache):
    try:
        os.makedirs(os.path.dirname(MDBLIST_CACHE_FILE), exist_ok=True)
        with open(MDBLIST_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving MDB List cache: {e}")

def get_mdblist_sources() -> List[Dict[str, Any]]:
    content_sources = get_all_settings().get('Content Sources', {})
    mdblist_sources = [data for source, data in content_sources.items() if source.startswith('MDBList')]
    
    if not mdblist_sources:
        logging.error("No MDBList sources configured. Please add MDBList sources in settings.")
        return []
    
    return mdblist_sources

def fetch_items_from_mdblist(url: str) -> List[Dict[str, Any]]:
    headers = {
        'Accept': 'application/json'
    }
    # Ensure the URL starts with 'https://'
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url
    if not url.endswith('/json'):
        url += '/json'
    
    try:
        logging.info(f"Fetching items from MDBList URL: {url}")
        response = api.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching items from MDBList: {e}")
        return []

def assign_media_type(item: Dict[str, Any]) -> str:
    media_type = item.get('mediatype', '').lower()
    if media_type == 'movie':
        return 'movie'
    elif media_type in ['show', 'tv']:
        return 'tv'
    else:
        logging.warning(f"Unknown media type: {media_type}. Defaulting to 'movie'.")
        return 'movie'

def get_wanted_from_mdblists(mdblist_url: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    disable_caching = True  # Hardcoded to True
    cache = {} if disable_caching else load_mdblist_cache()
    current_time = datetime.now()
    
    items = fetch_items_from_mdblist(mdblist_url)
    processed_items = []
    
    skipped_count = 0
    cache_skipped = 0
    for item in items:
        imdb_id = item.get('imdb_id')
        if not imdb_id:
            skipped_count += 1
            continue
        
        media_type = assign_media_type(item)
        wanted_item = {
            'imdb_id': imdb_id,
            'media_type': media_type,
        }
        
        if not disable_caching:
            # Check cache for this item
            cache_key = f"{imdb_id}_{media_type}"
            cache_item = cache.get(cache_key)
            
            if cache_item:
                last_processed = cache_item['timestamp']
                if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                    cache_skipped += 1
                    continue
            
            # Add or update cache entry
            cache[cache_key] = {
                'timestamp': current_time,
                'data': wanted_item
            }
        
        processed_items.append(wanted_item)

    if skipped_count > 0:
        logging.info(f"Skipped {skipped_count} items due to missing IMDB IDs")
    
    logging.info(f"Found {len(processed_items)} items from MDBList")
    all_wanted_items.append((processed_items, versions))
    
    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_mdblist_cache(cache)
    return all_wanted_items