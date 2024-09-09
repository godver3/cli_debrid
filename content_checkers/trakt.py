import re
import logging
from api_tracker import api
import json
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse
from settings import get_all_settings
import trakt.core
import time
import pickle
import os

REQUEST_TIMEOUT = 10  # seconds
TRAKT_API_URL = "https://api.trakt.tv"

CACHE_FILE = 'db_content/trakt_last_activity.pkl'

def load_trakt_credentials() -> Dict[str, str]:
    try:
        with open('config/.pytrakt.json', 'r') as file:
            credentials = json.load(file)
        return credentials
    except FileNotFoundError:
        logging.error("Trakt credentials file not found.")
        return {}
    except json.JSONDecodeError:
        logging.error("Error decoding Trakt credentials file.")
        return {}

def get_trakt_headers() -> Dict[str, str]:
    credentials = load_trakt_credentials()
    client_id = credentials.get('CLIENT_ID')
    access_token = credentials.get('OAUTH_TOKEN')
    if not client_id or not access_token:
        logging.error("Trakt API credentials not set. Please configure in settings.")
        return {}
    return {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': client_id,
        'Authorization': f'Bearer {access_token}'
    }

def get_trakt_sources() -> Dict[str, List[Dict[str, Any]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    watchlist_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Watchlist')]
    list_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Lists')]
    
    return {
        'watchlist': watchlist_sources,
        'lists': list_sources
    }

def clean_trakt_urls(urls: str) -> List[str]:
    # Split the URLs and clean each one
    url_list = [url.strip() for url in urls.split(',')]
    cleaned_urls = []
    for url in url_list:
        # Remove everything from '?' to the end
        cleaned = re.sub(r'\?.*$', '', url)
        # Ensure the URL starts with 'https://'
        if not cleaned.startswith('http://') and not cleaned.startswith('https://'):
            cleaned = 'https://' + cleaned
        # Only add the URL if it doesn't contain 'asc'
        if 'asc' not in cleaned:
            cleaned_urls.append(cleaned)
    return cleaned_urls

def parse_trakt_list_url(url: str) -> Dict[str, str]:
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.strip('/').split('/')

    if len(path_parts) < 3 or path_parts[0] != 'users':
        logging.error(f"Invalid Trakt list URL: {url}")
        return {}

    return {
        'username': path_parts[1],
        'list_id': path_parts[3] if len(path_parts) > 3 else 'watchlist'
    }

def fetch_items_from_trakt(endpoint: str) -> List[Dict[str, Any]]:
    headers = get_trakt_headers()
    if not headers:
        return []

    full_url = f"{TRAKT_API_URL}{endpoint}"
    logging.debug(f"Fetching items from Trakt URL: {full_url}")

    try:
        response = api.get(full_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching items from Trakt: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response text: {e.response.text}")
        return []

def assign_media_type(item: Dict[str, Any]) -> str:
    if 'movie' in item:
        return 'movie'
    elif 'show' in item:
        return 'tv'
    else:
        logging.warning(f"Unknown media type: {item}. Skipping.")
        return ''

def get_imdb_id(item: Dict[str, Any], media_type: str) -> str:
    media_type_key = 'show' if media_type == 'tv' else media_type

    if media_type_key not in item:
        logging.error(f"Media type '{media_type_key}' not found in item: {json.dumps(item, indent=2)}")
        return ''
    ids = item[media_type_key].get('ids', {})
    if not ids:
        logging.error(f"No IDs found for media type '{media_type_key}' in item: {json.dumps(item, indent=2)}")
        return ''
    return ids.get('imdb') or ids.get('tmdb') or ids.get('tvdb') or ''

def process_trakt_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    processed_items = []
    for item in items:
        media_type = assign_media_type(item)
        if not media_type:
            continue
        
        imdb_id = get_imdb_id(item, media_type)
        if not imdb_id:
            logging.warning(f"Skipping item due to missing ID: {item.get(media_type, {}).get('title', 'Unknown Title')}")
            continue
        processed_items.append({
            'imdb_id': imdb_id,
            'media_type': media_type
        })
    return processed_items

def ensure_trakt_auth():
    logging.info("Starting Trakt authentication check")
    
    trakt.core.CONFIG_PATH = './config/.pytrakt.json'
    
    logging.info("Loading Trakt configuration")
    trakt.core.load_config()
    
    logging.info(f"OAUTH_TOKEN: {trakt.core.OAUTH_TOKEN}")
    logging.info(f"OAUTH_EXPIRES_AT: {trakt.core.OAUTH_EXPIRES_AT}")
    
    # Manually load the config file if OAUTH_EXPIRES_AT is None
    if trakt.core.OAUTH_EXPIRES_AT is None:
        try:
            with open(trakt.core.CONFIG_PATH, 'r') as config_file:
                config_data = json.load(config_file)
                trakt.core.OAUTH_EXPIRES_AT = config_data.get('OAUTH_EXPIRES_AT')
            logging.info(f"Manually loaded OAUTH_EXPIRES_AT: {trakt.core.OAUTH_EXPIRES_AT}")
        except Exception as e:
            logging.error(f"Error manually loading config: {str(e)}")
    
    if trakt.core.OAUTH_TOKEN is None or trakt.core.OAUTH_EXPIRES_AT is None:
        logging.error("Trakt authentication not properly configured")
        return None
    
    current_time = int(time.time())
    
    if current_time > (trakt.core.OAUTH_EXPIRES_AT - 86400):
        logging.info("Token has expired, attempting to refresh")
        try:
            trakt.core._validate_token(trakt.core.CORE)
            logging.info(f"Token successfully refreshed. New expiration: {trakt.core.OAUTH_EXPIRES_AT}")
        except Exception as e:
            logging.error(f"Failed to refresh Trakt token: {str(e)}", exc_info=True)
            return None
    else:
        logging.info(f"Token is still valid. Expires in {trakt.core.OAUTH_EXPIRES_AT - current_time} seconds")
    
    return trakt.core.OAUTH_TOKEN

def load_last_activity_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    return {'lists': {}, 'watchlist': None}

def save_last_activity_cache(data: Dict[str, Any]):
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(data, f)

def get_last_activity() -> Dict[str, Any]:
    endpoint = "/sync/last_activities"
    return fetch_items_from_trakt(endpoint)

def check_for_updates(list_url: str = None) -> bool:
    cached_activity = load_last_activity_cache()
    current_activity = get_last_activity()

    if list_url:
        list_id = list_url.split('/')[-1].split('?')[0]
        if current_activity['lists']['updated_at'] != cached_activity['lists'].get(list_id):
            cached_activity['lists'][list_id] = current_activity['lists']['updated_at']
            save_last_activity_cache(cached_activity)
            return True
    else:  # Checking watchlist
        if current_activity['watchlist']['updated_at'] != cached_activity['watchlist']:
            cached_activity['watchlist'] = current_activity['watchlist']['updated_at']
            save_last_activity_cache(cached_activity)
            return True

    return False

def get_wanted_from_trakt_watchlist() -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    if not check_for_updates():
        logging.info("Watchlist is up to date, skipping fetch")
        return []

    logging.info("Preparing to make Trakt API call for watchlist")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token")
        raise Exception("Failed to obtain a valid Trakt access token")
    logging.info("Successfully obtained valid access token")

    all_wanted_items = []
    trakt_sources = get_trakt_sources()

    # Process Trakt Watchlist
    for watchlist_source in trakt_sources['watchlist']:
        if watchlist_source.get('enabled', False):
            versions = watchlist_source.get('versions', {})
            logging.info("Fetching user's watchlist")
            watchlist_items = fetch_items_from_trakt("/sync/watchlist")
            logging.debug(f"Watchlist items fetched: {len(watchlist_items)}")
            processed_items = process_trakt_items(watchlist_items)
            all_wanted_items.append((processed_items, versions))

    logging.info(f"Retrieved watchlist items from Trakt")
    
    return all_wanted_items

def get_wanted_from_trakt_lists(trakt_list_url: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    if not check_for_updates(trakt_list_url):
        logging.info(f"List {trakt_list_url} is up to date, skipping fetch")
        return []

    logging.info("Preparing to make Trakt API call for lists")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token")
        raise Exception("Failed to obtain a valid Trakt access token")
    logging.info("Successfully obtained valid access token")
    
    all_wanted_items = []
    logging.info(f"Processing Trakt list: {trakt_list_url}")
    
    list_info = parse_trakt_list_url(trakt_list_url)
    if list_info:
        endpoint = f"/users/{list_info['username']}/lists/{list_info['list_id']}/items"
        logging.info(f"Fetching items from list: {trakt_list_url}")
        items = fetch_items_from_trakt(endpoint)
        logging.debug(f"List items fetched: {len(items)}")
        processed_items = process_trakt_items(items)
        all_wanted_items.append((processed_items, versions))
    
    logging.info(f"Retrieved items from Trakt list")
    
    return all_wanted_items
    
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    wanted_items = get_wanted_from_trakt()
    print(f"Total wanted items: {len(wanted_items)}")
    print(json.dumps(wanted_items[:10], indent=2))  # Print first 10 items for brevity