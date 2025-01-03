import logging
from plexapi.myplex import MyPlexAccount
from typing import List, Dict, Any, Tuple
from settings import get_setting
import requests
import uuid
import json

def get_plex_client():
    plex_token = get_setting('Plex', 'token')
    
    if not plex_token:
        logging.error("Plex token not configured. Please add Plex token in settings.")
        return None
    
    try:
        account = MyPlexAccount(token=plex_token)
        return account
    except Exception as e:
        logging.error(f"Error connecting to Plex: {e}")
        return None

def get_metadata_for_item(rating_key: str, headers: Dict[str, str]) -> Dict[str, Any]:
    try:
        response = requests.get(
            f'https://metadata.provider.plex.tv/library/metadata/{rating_key}',
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Error fetching metadata for item {rating_key}: {e}")
        return {}

def get_watchlist_page(headers: Dict[str, str], start: int = 0) -> Dict[str, Any]:
    try:
        response = requests.get(
            f'https://metadata.provider.plex.tv/library/sections/watchlist/all',
            params={'X-Plex-Container-Start': start, 'X-Plex-Container-Size': 100},
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Error fetching watchlist page at offset {start}: {e}")
        return {}

def get_wanted_from_plex_watchlist(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    processed_items = []
    
    account = get_plex_client()
    if not account:
        return [([], versions)]
    
    try:
        # Required headers for Plex API
        headers = {
            'X-Plex-Token': account.authenticationToken,
            'X-Plex-Client-Identifier': str(uuid.uuid4()),
            'X-Plex-Product': 'Plex Media Server',
            'X-Plex-Version': '1.0',
            'X-Plex-Device': 'Debrid CLI',
            'X-Plex-Platform': 'Linux',
            'Accept': 'application/json'
        }
        
        # Get all pages
        start = 0
        total_items = 0
        while True:
            data = get_watchlist_page(headers, start)
            if not data:
                break
                
            container = data.get('MediaContainer', {})
            items = container.get('Metadata', [])
            
            if not items:
                break
                
            total_size = int(container.get('totalSize', 0))
            
            # Process items in this page
            for metadata in items:
                rating_key = metadata.get('ratingKey')
                if not rating_key:
                    logging.warning(f"Skipping item due to missing rating key: {metadata.get('title')}")
                    continue
                    
                # Get full metadata for the item
                full_metadata = get_metadata_for_item(rating_key, headers)
                if not full_metadata:
                    continue
                    
                # Extract IMDB ID from the full metadata
                imdb_id = None
                metadata_container = full_metadata.get('MediaContainer', {})
                metadata_item = metadata_container.get('Metadata', [{}])[0]
                
                # Check for Guid array
                for guid in metadata_item.get('Guid', []):
                    if isinstance(guid, dict) and 'id' in guid and 'imdb://' in guid['id']:
                        imdb_id = guid['id'].split('//')[1]
                        break
                
                if not imdb_id:
                    logging.warning(f"Skipping item due to missing IMDB ID: {metadata.get('title')} (type: {metadata.get('type')})")
                    continue
                
                media_type = 'movie' if metadata.get('type') == 'movie' else 'tv'
                
                wanted_item = {
                    'imdb_id': imdb_id,
                    'media_type': media_type
                }
                
                processed_items.append(wanted_item)
            
            # Update progress
            total_items += len(items)
            logging.info(f"Processed {total_items} of {total_size} items from Plex watchlist")
            
            # Check if we've processed all items
            if total_items >= total_size:
                break
                
            # Move to next page
            start += len(items)
            
    except Exception as e:
        logging.error(f"Error fetching Plex watchlist: {str(e)}")
        if isinstance(e, requests.exceptions.RequestException):
            logging.error(f"Response content: {getattr(e.response, 'content', 'No response content')}")
        return [([], versions)]
    
    logging.info(f"Retrieved {len(processed_items)} total items from Plex watchlist")
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items
