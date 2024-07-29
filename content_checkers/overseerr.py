import logging
import requests
from settings import get_setting
from typing import List, Dict, Any

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
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    try:
        wanted_content_raw = fetch_overseerr_wanted_content(overseerr_url, overseerr_api_key)
        wanted_items = []

        for item in wanted_content_raw:
            media = item.get('media', {})
            logging.debug(f"Processing wanted item: {media}")

            if media.get('mediaType') in ['movie', 'tv']:
                wanted_item = {
                    #'imdb_id': media.get('imdbId'),
                    'tmdb_id': media.get('tmdbId'),
                    #'title': media.get('title'),
                    #'year': media.get('releaseDate', '')[:4] if media.get('releaseDate') else None,
                    #'release_date': media.get('releaseDate'),
                    'media_type': media.get('mediaType')
                }

                wanted_items.append(wanted_item)
                logging.debug(f"Added wanted item: {wanted_item}")

        logging.info(f"Retrieved {len(wanted_items)} wanted items from Overseerr.")
        logging.debug(f"Full list of wanted items: {wanted_items}")
        return wanted_items
    except Exception as e:
        logging.error(f"Unexpected error while processing Overseerr response: {e}")
        return []