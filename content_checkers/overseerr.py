import logging
from api_tracker import api
from settings import get_setting, get_all_settings
from typing import List, Dict, Any, Tuple
from database import get_media_item_presence
import os
import pickle
from datetime import datetime, timedelta

DEFAULT_TAKE = 100
REQUEST_TIMEOUT = 15  # seconds

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
OVERSEERR_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'overseerr_cache.pkl')
CACHE_EXPIRY_DAYS = 7

def load_overseerr_cache():
    try:
        if os.path.exists(OVERSEERR_CACHE_FILE):
            with open(OVERSEERR_CACHE_FILE, 'rb') as f:
                return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading Overseerr cache: {e}. Creating a new cache.")
    return {}

def save_overseerr_cache(cache):
    try:
        os.makedirs(os.path.dirname(OVERSEERR_CACHE_FILE), exist_ok=True)
        with open(OVERSEERR_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving Overseerr cache: {e}")

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
        response = api.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching details for TMDB ID {tmdb_id}: {str(e)}")
        return {}

def fetch_overseerr_wanted_content(overseerr_url: str, overseerr_api_key: str, take: int = DEFAULT_TAKE) -> List[Dict[str, Any]]:
    headers = get_overseerr_headers(overseerr_api_key)
    wanted_content = []
    skip = 0
    page = 1

    while True:
        try:
            request_url = get_url(overseerr_url, f"/api/v1/request?take={take}&skip={skip}&filter=approved")
            logging.debug(f"Fetching Overseerr requests with URL: {request_url}")
            response = api.get(
                request_url,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            results = data.get('results', [])
            
            if not results:
                break

            wanted_content.extend(results)
            skip += take
            page += 1

            if len(results) < take:
                break

        except api.exceptions.RequestException as e:
            logging.error(f"Error fetching wanted content from Overseerr: {e}")
            break
        except Exception as e:
            logging.error(f"Unexpected error while processing Overseerr response: {e}")
            break

    logging.info(f"Found {len(wanted_content)} wanted items from Overseerr")
    return wanted_content

def get_wanted_from_overseerr(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    overseerr_sources = [data for source, data in content_sources.items() if source.startswith('Overseerr') and data.get('enabled', False)]
    allow_partial = get_setting('Debug', 'allow_partial_overseerr_requests', 'False')
    disable_caching = True  # Hardcoded to True
    logging.info(f"allow_partial: {allow_partial}")
    
    all_wanted_items = []
    cache = {} if disable_caching else load_overseerr_cache()
    current_time = datetime.now()
    
    for source in overseerr_sources:
        overseerr_url = source.get('url')
        overseerr_api_key = source.get('api_key')
        
        if not overseerr_url or not overseerr_api_key:
            logging.error(f"Overseerr URL or API key not set for source: {source}. Please configure in settings.")
            continue

        try:
            wanted_content_raw = fetch_overseerr_wanted_content(overseerr_url, overseerr_api_key)
            wanted_items = []
            cache_skipped = 0

            for item in wanted_content_raw:
                media = item.get('media', {})

                if media.get('mediaType') in ['movie', 'tv']:
                    wanted_item = {
                        'tmdb_id': media.get('tmdbId'),
                        'media_type': media.get('mediaType'),
                    }

                    # Handle season information for TV shows when partial requests are allowed
                    if allow_partial and media.get('mediaType') == 'tv' and 'seasons' in item:
                        requested_seasons = []
                        for season in item.get('seasons', []):
                            if season.get('seasonNumber') is not None:
                                requested_seasons.append(season.get('seasonNumber'))
                        if requested_seasons:
                            wanted_item['requested_seasons'] = requested_seasons

                    if not disable_caching:
                        # Check cache for this item
                        cache_key = f"{wanted_item['tmdb_id']}_{wanted_item['media_type']}"
                        if 'requested_seasons' in wanted_item:
                            cache_key += f"_s{'_'.join(map(str, wanted_item['requested_seasons']))}"
                        
                        cache_item = cache.get(cache_key)
                        
                        if cache_item:
                            last_processed = cache_item['timestamp']
                            # For TV shows, only use cache if it's the same seasons
                            if (current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS) and
                                (wanted_item['media_type'] != 'tv' or
                                 wanted_item.get('requested_seasons') == cache_item['data'].get('requested_seasons'))):
                                cache_skipped += 1
                                continue
                        
                        # Add or update cache entry
                        cache[cache_key] = {
                            'timestamp': current_time,
                            'data': wanted_item
                        }
                    
                    wanted_items.append(wanted_item)

            all_wanted_items.append((wanted_items, versions))
            logging.info(f"Retrieved {len(wanted_items)} wanted items from Overseerr source")
        except Exception as e:
            logging.error(f"Unexpected error while processing Overseerr source: {e}")

    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_overseerr_cache(cache)
    logging.info(f"Retrieved items from {len(all_wanted_items)} Overseerr sources.")
    return all_wanted_items