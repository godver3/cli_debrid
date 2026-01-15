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

def remove_from_mdblist(api_key: str, list_id: str, items: List[dict]) -> dict:
    """
    Remove items from an MDBList personal list

    Args:
        api_key: MDBList API key
        list_id: MDBList list ID (numeric, extracted from URL)
        items: List of items to remove, each with 'imdb_id', 'tmdb_id', and 'type'

    Returns:
        dict: {'success': bool, 'removed': int, 'message': str}
    """
    if not api_key:
        return {
            'success': False,
            'removed': 0,
            'message': 'MDBList API key not configured'
        }

    if not list_id:
        return {
            'success': False,
            'removed': 0,
            'message': 'MDBList list ID not provided'
        }

    if not items:
        return {
            'success': False,
            'removed': 0,
            'message': 'No items provided for removal'
        }

    # Build removal payload
    movies = []
    shows = []

    for item in items:
        imdb_id = item.get('imdb_id')
        tmdb_id = item.get('tmdb_id')
        item_type = item.get('type', '').lower()

        if not imdb_id and not tmdb_id:
            logging.warning(f"Skipping item without IMDB or TMDB ID: {item}")
            continue

        # Build item identifier
        item_ids = {}
        if imdb_id:
            item_ids['imdb'] = imdb_id
        if tmdb_id:
            item_ids['tmdb'] = int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id

        # Categorize by type
        if item_type == 'movie':
            movies.append(item_ids)
        elif item_type in ['show', 'episode', 'tv']:
            shows.append(item_ids)
        else:
            # Default to movie if type unknown
            movies.append(item_ids)

    if not movies and not shows:
        return {
            'success': False,
            'removed': 0,
            'message': 'No valid items to remove'
        }

    # Build request payload
    payload = {}
    if movies:
        payload['movies'] = movies
    if shows:
        payload['shows'] = shows

    # MDBList API endpoint for removing items from a list
    # API: POST https://api.mdblist.com/lists/{list_id}/items/remove?apikey={api_key}
    url = f"https://api.mdblist.com/lists/{list_id}/items/remove"

    params = {
        'apikey': api_key
    }

    headers = {
        'Content-Type': 'application/json'
    }

    try:
        logging.info(f"Removing {len(movies)} movie(s) and {len(shows)} show(s) from MDBList {list_id}")
        response = api.post(url, params=params, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 200:
            result = response.json()
            removed_count = result.get('removed', 0)
            not_found_count = result.get('not_found', 0)

            logging.info(f"Successfully removed {removed_count} item(s) from MDBList {list_id} ({not_found_count} not found)")
            return {
                'success': True,
                'removed': removed_count,
                'message': f'Removed {removed_count} item(s) from MDBList' + (f' ({not_found_count} not found)' if not_found_count > 0 else '')
            }
        elif response.status_code == 401 or response.status_code == 403:
            logging.error(f"MDBList authentication failed for list {list_id}: HTTP {response.status_code}")
            return {
                'success': False,
                'removed': 0,
                'message': 'MDBList authentication failed - check API key'
            }
        else:
            logging.error(f"Failed to remove from MDBList {list_id}: HTTP {response.status_code} - {response.text}")
            return {
                'success': False,
                'removed': 0,
                'message': f'HTTP {response.status_code}: {response.text}'
            }

    except api.exceptions.RequestException as e:
        logging.error(f"Error removing from MDBList {list_id}: {e}")
        return {
            'success': False,
            'removed': 0,
            'message': str(e)
        }

def extract_list_id_from_url(url: str) -> str | None:
    """
    Extract MDBList list ID from URL

    Examples:
        https://mdblist.com/lists/linaspurinis/top-watched-movies-of-the-week-from-trakt -> None (public list)
        https://mdblist.com/lists/user/12345 -> '12345' (personal list ID)
        https://mdblist.com/lists/12345 -> '12345'

    Args:
        url: MDBList URL

    Returns:
        str | None: List ID if it's a personal list, None otherwise
    """
    import re

    # Pattern to match numeric list IDs (personal lists)
    # Personal lists use format: /lists/{user}/{id} or /lists/{id}
    match = re.search(r'/lists/(?:\w+/)?(\d+)', url)
    if match:
        return match.group(1)

    # If no numeric ID found, it's likely a public list (slug-based)
    return None
