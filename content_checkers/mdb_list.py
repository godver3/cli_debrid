import logging
from routes.api_tracker import api
from typing import List, Dict, Any, Tuple
from utilities.settings import get_all_settings, get_setting
import os
import pickle
from datetime import datetime, timedelta
# import requests # No longer needed for direct test here

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
    # Ensure the URL starts with 'http://' or 'https://'
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url
    
    # Append /json only if the URL does not already end with .json
    if not url.endswith('.json'):
        if not url.endswith('/'):
            url += '/'
        url += 'json'
    
    try:
        logging.info(f"Fetching items from MDBList URL: {url}")
        response = api.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching items from MDBList: {e}")
        return []

def assign_media_type(item: Dict[str, Any]) -> str:
    # Try new format first (e.g., from Trakt)
    media_type_new = item.get('type', '').lower()
    if media_type_new == 'movie':
        return 'movie'
    elif media_type_new == 'show': # handles 'show' for Trakt TV shows
        return 'tv'

    # Fallback to original MDBList format
    media_type_orig = item.get('mediatype', '').lower()
    if media_type_orig == 'movie':
        return 'movie'
    elif media_type_orig in ['show', 'tv']:
        return 'tv'
    
    # If neither format provides a clear type, log warning and default
    if media_type_new: # Log the value from the new format if it was present but not 'movie' or 'show'
        logging.warning(f"Unknown media type from 'type' key: {item.get('type')}. Defaulting to 'movie'.")
    elif media_type_orig: # Log the value from the original format if it was present but not recognized
        logging.warning(f"Unknown media type from 'mediatype' key: {item.get('mediatype')}. Defaulting to 'movie'.")
    else: # Log if neither key was found
        logging.warning(f"Media type key ('type' or 'mediatype') not found in item. Defaulting to 'movie'. Item keys: {list(item.keys())}")
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
    for item_index, item in enumerate(items):
        imdb_id = None
        # Try to get imdb_id from original MDBList format
        if 'imdb_id' in item:
            imdb_id = item.get('imdb_id')
        # Else, try to get imdb_id from new Trakt-like format
        elif 'movie' in item and isinstance(item['movie'], dict) and 'ids' in item['movie'] and isinstance(item['movie']['ids'], dict):
            imdb_id = item['movie']['ids'].get('imdb')
        elif 'show' in item and isinstance(item['show'], dict) and 'ids' in item['show'] and isinstance(item['show']['ids'], dict):
            imdb_id = item['show']['ids'].get('imdb')
        
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