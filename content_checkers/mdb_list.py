import logging
import requests
from typing import List, Dict, Any
from settings import get_setting

REQUEST_TIMEOUT = 10  # seconds

def get_mdblist_urls() -> List[str]:
    mdblist_urls = get_setting('MDBList', 'urls')
    if not mdblist_urls:
        logging.error("MDBList URLs not set. Please configure in settings.")
        return []
    return [url.strip() for url in mdblist_urls.split(',')]

def fetch_items_from_mdblist(url: str) -> List[Dict[str, Any]]:
    headers = {
        #'Authorization': f'Bearer {api_key}',
        'Accept': 'application/json'
    }

    # Ensure the URL starts with 'https://'
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url

    if not url.endswith('/json'):
        url += '/json'

    try:
        logging.info(f"Fetching items from MDBList URL: {url}")
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
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

def get_wanted_from_mdblists() -> List[Dict[str, Any]]:
    #mdblist_api_key = get_setting('MDBList', 'api_key')

    #if not mdblist_api_key:
        #logging.error("MDBList API key not set. Please configure in settings.")
        #return []

    url_list = get_mdblist_urls()
    all_wanted_items = []

    for url in url_list:
        items = fetch_items_from_mdblist(url) #, mdblist_api_key)
        for item in items:
            imdb_id = item.get('imdb_id')
            if not imdb_id:
                logging.warning(f"Skipping item due to missing IMDB ID: {item.get('title', 'Unknown Title')}")
                continue

            media_type = assign_media_type(item)
            wanted_item = {
                'imdb_id': imdb_id,
                'media_type': media_type
            }
            all_wanted_items.append(wanted_item)

    logging.info(f"Retrieved {len(all_wanted_items)} wanted items from all MDB Lists")
    return all_wanted_items
