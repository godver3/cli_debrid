import re
import logging
import requests
import json
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse
from settings import get_all_settings
import trakt.core
import time
from database import get_media_item_presence

REQUEST_TIMEOUT = 10  # seconds
TRAKT_API_URL = "https://api.trakt.tv"

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
        response = requests.get(full_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
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
    trakt.core.load_config()
    
    current_time = int(time.time())
    
    if trakt.core.OAUTH_EXPIRES_AT is None:
        logging.warning("OAUTH_EXPIRES_AT is None, token may have never been set")
    elif current_time > trakt.core.OAUTH_EXPIRES_AT:
        logging.info("Token has expired, attempting to refresh")
    else:
        logging.info("Token is still valid. Expires in %s seconds", trakt.core.OAUTH_EXPIRES_AT - current_time)
        return trakt.core.OAUTH_TOKEN
    
    try:
        logging.info("Validating/refreshing token")
        trakt.core._validate_token(trakt.core.CORE)
        logging.info("Token successfully refreshed. New expiration: %s", trakt.core.OAUTH_EXPIRES_AT)
        return trakt.core.OAUTH_TOKEN
    except Exception as e:
        logging.error("Failed to refresh Trakt token: %s", str(e), exc_info=True)
        return None

def get_wanted_from_trakt_watchlist() -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
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
    # Check if the URL exclusively contains 'asc'
    if trakt_list_url.strip().lower() == 'asc':
        logging.info("Ignoring 'asc' URL")
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