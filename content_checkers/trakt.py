import re
import logging
import requests
import json
from typing import List, Dict, Any
from urllib.parse import urlparse, parse_qs
from settings import get_setting

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

def get_trakt_lists() -> List[str]:
    trakt_lists = get_setting('Trakt', 'trakt_lists')
    if not trakt_lists:
        logging.info("No additional Trakt lists configured.")
        return []

    # Clean the entire URL string first
    cleaned_lists = clean_trakt_urls(trakt_lists)

    # Split by comma and strip whitespace
    urls = [url.strip() for url in cleaned_lists.split(',') if url.strip()]

    logging.debug(f"Parsed Trakt lists: {urls}")
    return urls

def clean_trakt_urls(urls: str) -> str:
    # Remove everything from '?' to the end of 'asc' or 'desc' for each URL
    cleaned = re.sub(r'\?.*?(asc|desc)(?=[,\s]|$)', '', urls)

    # Ensure each URL starts with 'https://'
    cleaned = re.sub(r'(?<!https:\/\/)(?=trakt\.tv)', 'https://', cleaned)

    return cleaned

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
    logging.debug(f"Headers: {json.dumps(headers, indent=2)}")

    try:
        response = requests.get(full_url, headers=headers, timeout=REQUEST_TIMEOUT)
        logging.debug(f"Response status code: {response.status_code}")
        logging.debug(f"Response headers: {json.dumps(dict(response.headers), indent=2)}")

        response.raise_for_status()

        json_response = response.json()
        logging.debug(f"Response JSON: {json.dumps(json_response[:2], indent=2)}...")  # Log first 2 items

        return json_response
    except requests.RequestException as e:
        logging.error(f"Error fetching items from Trakt: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response text: {e.response.text}")
        return []

def assign_media_type(item: Dict[str, Any]) -> str:
    if 'movie' in item:
        return 'movie'
    elif 'show' in item:
        return 'show'
    else:
        logging.warning(f"Unknown media type: {item}. Skipping.")
        return ''

def get_imdb_id(item: Dict[str, Any], media_type: str) -> str:
    if media_type == 'tv':
        media_type_key = 'show'
    else:
        media_type_key = media_type

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
        
        # Map 'show' to 'tv' for consistency
        if media_type == 'show':
            media_type = 'tv'

        imdb_id = get_imdb_id(item, media_type)
        if not imdb_id:
            logging.warning(f"Skipping item due to missing ID: {item.get(media_type, {}).get('title', 'Unknown Title')}")
            logging.debug(f"Full item details: {json.dumps(item, indent=2)}")
            continue
        processed_items.append({
            'imdb_id': imdb_id,
            'media_type': media_type  # This will now be either 'movie' or 'tv'
        })
    return processed_items

def get_wanted_from_trakt() -> List[Dict[str, Any]]:
    all_wanted_items = []

    # Fetch watchlist if enabled
    if get_setting('Trakt', 'user_watchlist_enabled'):
        logging.info("Fetching user's watchlist")
        watchlist_items = fetch_items_from_trakt("/sync/watchlist")
        logging.debug(f"Watchlist items fetched: {len(watchlist_items)}")
        all_wanted_items.extend(process_trakt_items(watchlist_items))
    else:
        logging.info("User watchlist is not enabled")

    # Fetch items from additional lists
    trakt_lists = get_trakt_lists()
    logging.info(f"Found {len(trakt_lists)} Trakt lists to process")
    for list_url in trakt_lists:
        list_info = parse_trakt_list_url(list_url)
        if list_info:
            endpoint = f"/users/{list_info['username']}/lists/{list_info['list_id']}/items"
            logging.info(f"Fetching items from list: {list_url}")
            list_items = fetch_items_from_trakt(endpoint)
            logging.debug(f"List items fetched: {len(list_items)}")
            all_wanted_items.extend(process_trakt_items(list_items))
        else:
            logging.warning(f"Skipping invalid Trakt list URL: {list_url}")

    logging.info(f"Retrieved {len(all_wanted_items)} wanted items from Trakt")
    return all_wanted_items

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    wanted_items = get_wanted_from_trakt()
    print(f"Total wanted items: {len(wanted_items)}")
    print(json.dumps(wanted_items[:10], indent=2))  # Print first 5 items for brevity
