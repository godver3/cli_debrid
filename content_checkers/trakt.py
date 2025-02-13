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
from database.database_reading import get_all_media_items, get_media_item_presence
from database.database_writing import update_media_item
from datetime import datetime, date, timedelta
from settings import get_setting
import random
from time import sleep

REQUEST_TIMEOUT = 10  # seconds
TRAKT_API_URL = "https://api.trakt.tv"
# Add default delays for rate limiting
DEFAULT_REMOVAL_DELAY = 2  # seconds between watchlist removals
DEFAULT_INITIAL_RETRY_DELAY = 3  # seconds for rate limit retry

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
LAST_ACTIVITY_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'trakt_last_activity.pkl')
TRAKT_WATCHLIST_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'trakt_watchlist_cache.pkl')
TRAKT_LISTS_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'trakt_lists_cache.pkl')
TRAKT_COLLECTION_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'trakt_collection_cache.pkl')
CACHE_EXPIRY_DAYS = 7

# Get config directory from environment variable with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
TRAKT_CONFIG_FILE = os.path.join(CONFIG_DIR, '.pytrakt.json')

def load_trakt_credentials() -> Dict[str, str]:
    try:
        with open(TRAKT_CONFIG_FILE, 'r') as file:
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

def make_trakt_request(method, endpoint, data=None, max_retries=5, initial_delay=DEFAULT_INITIAL_RETRY_DELAY):
    """
    Make a request to Trakt API with rate limiting and exponential backoff.
    
    Args:
        method: HTTP method ('get' or 'post')
        endpoint: API endpoint
        data: JSON data for POST requests
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay between retries in seconds
    """
    url = f"{TRAKT_API_URL}{endpoint}"
    headers = get_trakt_headers()
    if not headers:
        return None

    for attempt in range(max_retries):
        try:
            if method.lower() == 'get':
                response = api.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:  # post
                response = api.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
            
            # Check if response is HTML instead of JSON
            content_type = response.headers.get('content-type', '')
            if 'html' in content_type.lower():
                logging.error(f"Received HTML response instead of JSON from Trakt API (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                    logging.info(f"Waiting {delay:.2f} seconds before retry")
                    sleep(delay)
                    continue
                else:
                    raise ValueError("Received HTML response from Trakt API after all retries")
            
            response.raise_for_status()
            return response
            
        except api.exceptions.RequestException as e:
            if hasattr(e, 'response'):
                status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
                if status_code == 429:  # Too Many Requests
                    # Get retry-after header or use exponential backoff
                    retry_after = int(e.response.headers.get('Retry-After', 0))
                    delay = retry_after if retry_after > 0 else initial_delay * (2 ** attempt) + random.uniform(0, 1)
                    
                    logging.warning(f"Rate limit hit. Waiting {delay:.2f} seconds before retry {attempt + 1}/{max_retries}")
                    sleep(delay)
                    continue
                elif status_code == 502:  # Bad Gateway
                    logging.warning(f"Trakt API Bad Gateway error (attempt {attempt + 1}/{max_retries})")
                elif status_code == 504:  # Gateway Timeout
                    logging.warning(f"Trakt API Gateway Timeout error (attempt {attempt + 1}/{max_retries})")
                else:
                    logging.error(f"Trakt API error: {status_code} - {str(e)}")
            
            if attempt == max_retries - 1:
                logging.error(f"Failed to make Trakt API request after {max_retries} attempts: {str(e)}")
                raise
            
            delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
            logging.warning(f"Request failed. Retrying in {delay:.2f} seconds. Attempt {attempt + 1}/{max_retries}")
            sleep(delay)
            
    return None

def fetch_items_from_trakt(endpoint: str) -> List[Dict[str, Any]]:
    logging.debug(f"Fetching items from Trakt URL: {endpoint}")
    
    try:
        response = make_trakt_request('get', endpoint)
        if response:
            return response.json()
    except Exception as e:
        logging.error(f"Failed to fetch items from Trakt: {str(e)}")
    
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
    logging.debug("Checking Trakt authentication")
    
    trakt.core.CONFIG_PATH = TRAKT_CONFIG_FILE
    trakt.core.load_config()
    
    # Manually load the config file if OAUTH_EXPIRES_AT is None
    if trakt.core.OAUTH_EXPIRES_AT is None:
        try:
            with open(trakt.core.CONFIG_PATH, 'r') as config_file:
                config_data = json.load(config_file)
                trakt.core.OAUTH_EXPIRES_AT = config_data.get('OAUTH_EXPIRES_AT')
        except Exception as e:
            logging.error(f"Error manually loading config: {e}")
    
    if trakt.core.OAUTH_TOKEN is None or trakt.core.OAUTH_EXPIRES_AT is None:
        logging.error("Trakt authentication not properly configured")
        return None
    
    current_time = int(time.time())
    
    if current_time > (trakt.core.OAUTH_EXPIRES_AT - 86400):
        logging.info("Token expired, refreshing")
        try:
            trakt.core._validate_token(trakt.core.CORE)
            logging.debug("Token refreshed successfully")
        except Exception as e:
            logging.error(f"Failed to refresh Trakt token: {e}", exc_info=True)
            return None
    else:
        logging.debug("Token is valid")
    
    return trakt.core.OAUTH_TOKEN

def load_trakt_cache(cache_file):
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as f:
                return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading Trakt cache: {e}. Creating a new cache.")
    return {}

def save_trakt_cache(cache, cache_file):
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving Trakt cache: {e}")

def get_last_activity() -> Dict[str, Any]:
    endpoint = "/sync/last_activities"
    return fetch_items_from_trakt(endpoint)

def check_for_updates(list_url: str = None) -> bool:
    cached_activity = load_trakt_cache(LAST_ACTIVITY_CACHE_FILE)
    current_activity = get_last_activity()
    current_time = int(time.time())
    cache_age = current_time - cached_activity.get('last_updated', 0)

    # If cache is 24 hours old or older, recreate it
    if cache_age >= 86400:  # 86400 seconds = 24 hours
        logging.info("Cache is 24 hours old or older. Recreating cache.")
        cached_activity = {'lists': {}, 'watchlist': None, 'last_updated': current_time}
        save_trakt_cache(cached_activity, LAST_ACTIVITY_CACHE_FILE)
        return True

    if list_url:
        list_id = list_url.split('/')[-1].split('?')[0]
        if list_id not in cached_activity['lists'] or current_activity['lists']['updated_at'] != cached_activity['lists'].get(list_id):
            logging.info(f"Update detected for list {list_id}")
            cached_activity['lists'][list_id] = current_activity['lists']['updated_at']
            cached_activity['last_updated'] = current_time
            save_trakt_cache(cached_activity, LAST_ACTIVITY_CACHE_FILE)
            return True
        else:
            logging.info(f"No update detected for list {list_id}")
    else:  # Checking watchlist
        if 'watchlist' not in cached_activity or current_activity['watchlist']['updated_at'] != cached_activity['watchlist']:
            logging.info("Update detected for watchlist")
            cached_activity['watchlist'] = current_activity['watchlist']['updated_at']
            cached_activity['last_updated'] = current_time
            save_trakt_cache(cached_activity, LAST_ACTIVITY_CACHE_FILE)
            return True
        else:
            logging.info("No update detected for watchlist")

    return False

def get_wanted_from_trakt_watchlist() -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    logging.debug("Fetching Trakt watchlist")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token")
        raise Exception("Failed to obtain a valid Trakt access token")

    all_wanted_items = []
    trakt_sources = get_trakt_sources()
    disable_caching = True  # Hardcoded to True
    cache = {} if disable_caching else load_trakt_cache(TRAKT_WATCHLIST_CACHE_FILE)
    current_time = datetime.now()

    # Check if watchlist removal is enabled
    should_remove = get_setting('Debug', 'trakt_watchlist_removal', False)
    keep_series = get_setting('Debug', 'trakt_watchlist_keep_series', False)

    if should_remove:
        logging.debug("Trakt watchlist removal enabled" + (" (keeping series)" if keep_series else ""))

    # Process Trakt Watchlist
    for watchlist_source in trakt_sources['watchlist']:
        if watchlist_source.get('enabled', False):
            versions = watchlist_source.get('versions', {})
            watchlist_items = fetch_items_from_trakt("/sync/watchlist")
            
            processed_items = []
            movies_to_remove = []
            shows_to_remove = []
            skipped_count = 0
            cache_skipped = 0
            
            for item in watchlist_items:
                media_type = assign_media_type(item)
                if not media_type:
                    skipped_count += 1
                    continue
                
                imdb_id = get_imdb_id(item, media_type)
                if not imdb_id:
                    skipped_count += 1
                    continue

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
                        'data': {
                            'imdb_id': imdb_id,
                            'media_type': media_type
                        }
                    }

                # Check if the item is already collected
                item_state = get_media_item_presence(imdb_id=imdb_id)
                if item_state == "Collected" and should_remove:
                    # If it's a TV show and we want to keep series, skip removal
                    if media_type == 'tv' and keep_series:
                        logging.debug(f"Keeping TV series: {imdb_id}")
                        processed_items.append({
                            'imdb_id': imdb_id,
                            'media_type': media_type
                        })
                    else:
                        # Add to removal list
                        item_type = 'show' if media_type == 'tv' else media_type
                        removal_item = {"ids": item[item_type]['ids']}
                        if media_type == 'tv':
                            shows_to_remove.append(removal_item)
                        else:
                            movies_to_remove.append(removal_item)
                else:
                    if not disable_caching:
                        # Add or update cache entry
                        cache[cache_key] = {
                            'timestamp': current_time,
                            'data': {
                                'imdb_id': imdb_id,
                                'media_type': media_type
                            }
                        }
                    processed_items.append({
                        'imdb_id': imdb_id,
                        'media_type': media_type
                    })

            # Perform bulk removals if there are items to remove
            if movies_to_remove or shows_to_remove:
                removal_data = {}
                if movies_to_remove:
                    removal_data['movies'] = movies_to_remove
                if shows_to_remove:
                    removal_data['shows'] = shows_to_remove

                try:
                    response = make_trakt_request(
                        'post',
                        "/sync/watchlist/remove",
                        data=removal_data
                    )
                    
                    if response and response.status_code == 200:
                        result = response.json()
                        removed_movies = result.get('deleted', {}).get('movies', 0)
                        removed_shows = result.get('deleted', {}).get('shows', 0)
                        if removed_movies > 0 or removed_shows > 0:
                            logging.info(f"Removed {removed_movies} movies and {removed_shows} shows from watchlist")
                    else:
                        logging.error("Bulk removal from watchlist failed")
                        # Add back all items that failed to be removed
                        for item in movies_to_remove + shows_to_remove:
                            imdb_id = item['ids'].get('imdb') or item['ids'].get('tmdb') or item['ids'].get('tvdb')
                            if imdb_id:
                                processed_items.append({
                                    'imdb_id': imdb_id,
                                    'media_type': 'movie' if item in movies_to_remove else 'tv'
                                })
                except Exception as e:
                    logging.error(f"Failed to perform bulk removal from watchlist: {e}")
                    # Add back all items that failed to be removed
                    for item in movies_to_remove + shows_to_remove:
                        imdb_id = item['ids'].get('imdb') or item['ids'].get('tmdb') or item['ids'].get('tvdb')
                        if imdb_id:
                            processed_items.append({
                                'imdb_id': imdb_id,
                                'media_type': 'movie' if item in movies_to_remove else 'tv'
                            })

            if skipped_count > 0:
                logging.info(f"Skipped {skipped_count} items due to missing IDs")
            logging.info(f"Found {len(processed_items)} items from Trakt watchlist")
            all_wanted_items.append((processed_items, versions))

    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_trakt_cache(cache, TRAKT_WATCHLIST_CACHE_FILE)
    return all_wanted_items

def get_wanted_from_trakt_lists(trakt_list_url: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    logging.debug("Fetching Trakt lists")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token")
        raise Exception("Failed to obtain a valid Trakt access token")
    
    all_wanted_items = []
    disable_caching = True  # Hardcoded to True
    cache = {} if disable_caching else load_trakt_cache(TRAKT_LISTS_CACHE_FILE)
    current_time = datetime.now()
    cache_skipped = 0
    
    list_info = parse_trakt_list_url(trakt_list_url)
    if not list_info:
        logging.error(f"Failed to parse Trakt list URL: {trakt_list_url}")
        return [([], versions)]
    
    username = list_info['username']
    list_id = list_info['list_id']
    
    # Get list items
    endpoint = f"/users/{username}/lists/{list_id}/items"
    list_items = fetch_items_from_trakt(endpoint)
    
    processed_items = []
    skipped_count = 0
    
    for item in list_items:
        media_type = assign_media_type(item)
        if not media_type:
            skipped_count += 1
            continue
        
        imdb_id = get_imdb_id(item, media_type)
        if not imdb_id:
            skipped_count += 1
            continue

        if not disable_caching:
            # Check cache for this item
            cache_key = f"{list_id}_{imdb_id}_{media_type}"
            cache_item = cache.get(cache_key)
            
            if cache_item:
                last_processed = cache_item['timestamp']
                if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                    cache_skipped += 1
                    continue
            
            # Add or update cache entry
            cache[cache_key] = {
                'timestamp': current_time,
                'data': {
                    'imdb_id': imdb_id,
                    'media_type': media_type
                }
            }
        
        processed_items.append({
            'imdb_id': imdb_id,
            'media_type': media_type
        })

    if skipped_count > 0:
        logging.info(f"Skipped {skipped_count} items due to missing IDs")
    
    logging.info(f"Found {len(processed_items)} items from Trakt list")
    all_wanted_items.append((processed_items, versions))
    
    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_trakt_cache(cache, TRAKT_LISTS_CACHE_FILE)
    return all_wanted_items

def get_wanted_from_trakt_collection() -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    logging.debug("Fetching Trakt collection")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token")
        raise Exception("Failed to obtain a valid Trakt access token")

    all_wanted_items = []
    disable_caching = True  # Hardcoded to True
    cache = {} if disable_caching else load_trakt_cache(TRAKT_COLLECTION_CACHE_FILE)
    current_time = datetime.now()
    cache_skipped = 0

    # Get collection items
    response = make_trakt_request('get', "/sync/collection/movies")
    movie_items = response.json() if response else []
    
    response = make_trakt_request('get', "/sync/collection/shows")
    show_items = response.json() if response else []
    
    collection_items = movie_items + show_items
    processed_items = []
    skipped_count = 0
    
    for item in collection_items:
        media_type = assign_media_type(item)
        if not media_type:
            skipped_count += 1
            continue
        
        imdb_id = get_imdb_id(item, media_type)
        if not imdb_id:
            skipped_count += 1
            continue

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
                'data': {
                    'imdb_id': imdb_id,
                    'media_type': media_type
                }
            }
        
        processed_items.append({
            'imdb_id': imdb_id,
            'media_type': media_type
        })

    if skipped_count > 0:
        logging.info(f"Skipped {skipped_count} items due to missing IDs")
    
    logging.info(f"Found {len(processed_items)} items from Trakt collection")
    all_wanted_items.append((processed_items, {}))
    
    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_trakt_cache(cache, TRAKT_COLLECTION_CACHE_FILE)
    return all_wanted_items

def check_trakt_early_releases():
    logging.debug("Checking Trakt for early releases")
    
    trakt_early_releases = get_setting('Scraping', 'trakt_early_releases', False)
    if not trakt_early_releases:
        logging.debug("Trakt early releases check is disabled")
        return

    # Get all items with state sleeping, wanted, or unreleased
    states_to_check = ('Sleeping', 'Wanted', 'Unreleased')
    items_to_check = get_all_media_items(state=states_to_check)
    
    skipped_count = 0
    updated_count = 0
    
    for item in items_to_check:
        if item['type'] == 'episode':
            skipped_count += 1
            continue
        
        imdb_id = item['imdb_id']
        trakt_id = fetch_items_from_trakt(f"/search/imdb/{imdb_id}")
        
        if trakt_id and isinstance(trakt_id, list) and len(trakt_id) > 0:
            # Check if 'movie' key exists in the first item
            if 'movie' in trakt_id[0]:
                trakt_id = str(trakt_id[0]['movie']['ids']['trakt'])
            elif 'show' in trakt_id[0]:
                trakt_id = str(trakt_id[0]['show']['ids']['trakt'])
            else:
                logging.warning(f"Unexpected Trakt API response structure for IMDB ID: {imdb_id}")
                continue

            endpoint = f"/movies/{trakt_id}/lists/personal/popular" if item['type'] == 'movie' else f"/shows/{trakt_id}/lists/personal/popular"
            trakt_lists = fetch_items_from_trakt(endpoint)
            
            for trakt_list in trakt_lists:
                if re.search(r'(latest|new).*?(releases)', trakt_list['name'], re.IGNORECASE):
                    update_media_item(item['id'], early_release=True)
                    updated_count += 1
                    break
    
    if updated_count > 0:
        logging.info(f"Set early release flag for {updated_count} items")
    if skipped_count > 0:
        logging.debug(f"Skipped {skipped_count} episodes")
    
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    wanted_items = get_wanted_from_trakt()
    print(f"Total wanted items: {len(wanted_items)}")
    print(json.dumps(wanted_items[:10], indent=2))  # Print first 10 items for brevity