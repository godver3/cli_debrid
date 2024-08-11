import logging
import requests
from typing import List, Dict, Any, Tuple
from settings import get_all_settings
from database import get_media_item_presence

REQUEST_TIMEOUT = 10  # seconds

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

def get_wanted_from_mdblists(mdblist_url: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    
    items = fetch_items_from_mdblist(mdblist_url)
    processed_items = []
    for item in items:
        imdb_id = item.get('imdb_id')
        if not imdb_id:
            logging.warning(f"Skipping item due to missing IMDB ID: {item.get('title', 'Unknown Title')}")
            continue
        
        media_type = assign_media_type(item)
        wanted_item = {
            'imdb_id': imdb_id,
            'media_type': media_type,
        }
        processed_items.append(wanted_item)
    
    all_wanted_items.append((processed_items, versions))
    
    logging.info(f"Retrieved {len(processed_items)} wanted items from MDB List: {mdblist_url}")
    
    # Final filtering step
    filtered_items = []
    for items, item_versions in all_wanted_items:
        new_items = []
        for item in items:
            imdb_id = item.get('imdb_id')
            if imdb_id:
                status = get_media_item_presence(imdb_id=imdb_id)
                if status == "Missing":
                    new_items.append(item)
                else:
                    logging.debug(f"Skipping existing item with IMDB ID {imdb_id}")
            else:
                logging.warning(f"Skipping item without IMDB ID: {item}")
        if new_items:
            filtered_items.append((new_items, item_versions))
    
    logging.info(f"After filtering, {sum(len(items) for items, _ in filtered_items)} new wanted items remain.")
    logging.debug(f"Full list of new wanted items: {filtered_items}")
    return filtered_items