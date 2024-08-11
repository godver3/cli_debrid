import logging
import requests
from settings import get_setting, get_all_settings
from typing import List, Dict, Any
from database import get_media_item_presence

DEFAULT_TAKE = 100
REQUEST_TIMEOUT = 3  # seconds

def get_overseerr_headers(api_key: str) -> Dict[str, str]:
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url: str, endpoint: str) -> str:
    return f"{base_url}{endpoint}"

def get_overseerr_details(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, media_type: str) -> Dict[str, Any]:
    headers = get_overseerr_headers(overseerr_api_key)
    endpoint = f"/api/v1/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}"
    url = get_url(overseerr_url, endpoint)
    
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Error fetching details for TMDB ID {tmdb_id}: {str(e)}")
        return {}

def fetch_overseerr_wanted_content(overseerr_url: str, overseerr_api_key: str, take: int = DEFAULT_TAKE) -> List[Dict[str, Any]]:
    headers = get_overseerr_headers(overseerr_api_key)
    wanted_content = []
    skip = 0
    page = 1

    while True:
        try:
            logging.debug(f"Fetching page {page} (skip={skip}, take={take})")
            response = requests.get(
                get_url(overseerr_url, f"/api/v1/request?take={take}&skip={skip}"),
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            results = data.get('results', [])
            
            if not results:
                logging.debug("No more results returned. Stopping pagination.")
                break

            wanted_content.extend(results)
            skip += take
            page += 1

            if len(results) < take:
                logging.debug("Received fewer results than requested. This is likely the last page.")
                break

        except requests.RequestException as e:
            logging.error(f"Error fetching wanted content from Overseerr: {e}")
            break
        except Exception as e:
            logging.error(f"Unexpected error while processing Overseerr response: {e}")
            break

    logging.info(f"Fetched a total of {len(wanted_content)} wanted content items from Overseerr")
    return wanted_content

def get_wanted_from_overseerr() -> List[Dict[str, Any]]:
    content_sources = get_all_settings().get('Content Sources', {})
    overseerr_sources = [data for source, data in content_sources.items() if source.startswith('Overseerr') and data.get('enabled', False)]
    
    all_wanted_items = []
    
    for source in overseerr_sources:
        overseerr_url = source.get('url')
        overseerr_api_key = source.get('api_key')
        versions = source.get('versions', {})
        
        if not overseerr_url or not overseerr_api_key:
            logging.error(f"Overseerr URL or API key not set for source: {source}. Please configure in settings.")
            continue

        try:
            wanted_content_raw = fetch_overseerr_wanted_content(overseerr_url, overseerr_api_key)
            wanted_items = []

            for item in wanted_content_raw:
                media = item.get('media', {})
                logging.debug(f"Processing wanted item: {media}")

                if media.get('mediaType') in ['movie', 'tv']:
                    wanted_item = {
                        'tmdb_id': media.get('tmdbId'),
                        'media_type': media.get('mediaType'),
                        'versions': versions  # Add versions to each wanted item
                    }

                    wanted_items.append(wanted_item)
                    logging.debug(f"Added wanted item: {wanted_item}")

            all_wanted_items.extend(wanted_items)
            logging.info(f"Retrieved {len(wanted_items)} wanted items from Overseerr source.")
        except Exception as e:
            logging.error(f"Unexpected error while processing Overseerr source: {e}")

    logging.info(f"Retrieved a total of {len(all_wanted_items)} wanted items from all Overseerr sources.")
    logging.debug(f"Full list of wanted items: {all_wanted_items}")
    
    # Final filtering step
    new_wanted_items = []
    for item in all_wanted_items:
        tmdb_id = item.get('tmdb_id')
        if tmdb_id:
            status = get_media_item_presence(tmdb_id=tmdb_id)
            if status == "Missing":
                new_wanted_items.append(item)
            else:
                logging.debug(f"Skipping existing item with TMDB ID {tmdb_id}")
        else:
            logging.warning(f"Skipping item without TMDB ID: {item}")

    logging.info(f"After filtering, {len(new_wanted_items)} new wanted items remain.")
    logging.debug(f"Full list of new wanted items: {new_wanted_items}")
    return new_wanted_items