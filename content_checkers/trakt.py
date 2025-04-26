import re
import logging
from routes.api_tracker import api
import json
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse
from utilities.settings import get_all_settings
import trakt.core
import time
import pickle
import os
from database.database_reading import get_all_media_items, get_media_item_presence
from database.database_writing import update_media_item
from datetime import datetime, date, timedelta
from utilities.settings import get_setting
import random
from time import sleep
import requests

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
TRAKT_FRIENDS_DIR = os.path.join(CONFIG_DIR, 'trakt_friends')

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

def get_trakt_friend_headers(auth_id: str) -> Dict[str, str]:
    """Get the Trakt API headers for a friend's account"""
    try:
        # Load the friend's auth state
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            logging.error(f"Friend's Trakt auth file not found: {state_file}")
            return {}
        
        with open(state_file, 'r') as file:
            state = json.load(file)
        
        # Check if the token is expired
        if state.get('expires_at'):
            # Convert from Unix timestamp to datetime
            expires_at = datetime.fromtimestamp(state['expires_at'])
            if datetime.now() > expires_at:
                # Token is expired, try to refresh it
                refresh_friend_token(auth_id)
                # Reload the state
                with open(state_file, 'r') as file:
                    state = json.load(file)
        
        # Get client ID from friend's state
        client_id = state.get('client_id')
        
        # Get access token from friend's state
        access_token = state.get('access_token')
        
        if not client_id or not access_token:
            logging.error("Trakt API credentials not set or friend's token not available.")
            return {}
        
        return {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}'
        }
    except Exception as e:
        logging.error(f"Error getting friend's Trakt headers: {str(e)}")
        return {}

def refresh_friend_token(auth_id: str) -> bool:
    """Refresh the access token for a friend's Trakt account"""
    try:
        # Load the state
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            logging.error(f"Friend's Trakt auth file not found: {state_file}")
            return False
        
        with open(state_file, 'r') as file:
            state = json.load(file)
        
        # Check if we have a refresh token
        if not state.get('refresh_token'):
            logging.error(f"No refresh token available for friend's Trakt account: {auth_id}")
            return False
        
        # Get client credentials from friend's state
        client_id = state.get('client_id')
        client_secret = state.get('client_secret')
        
        if not client_id or not client_secret:
            logging.error(f"No client credentials available for friend's Trakt account: {auth_id}")
            return False
        
        # Refresh the token
        response = requests.post(
            f"{TRAKT_API_URL}/oauth/token",
            json={
                'refresh_token': state['refresh_token'],
                'client_id': client_id,
                'client_secret': client_secret,
                'grant_type': 'refresh_token'
            },
            timeout=REQUEST_TIMEOUT
        )
        
        if response.status_code == 200:
            token_data = response.json()
            
            # Update state with new token information
            state.update({
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'expires_at': (datetime.now() + timedelta(seconds=token_data['expires_in'])).timestamp()
            })
            
            # Save the updated state
            with open(state_file, 'w') as file:
                json.dump(state, file)
            
            logging.info(f"Successfully refreshed token for friend's Trakt account: {auth_id}")
            return True
        else:
            error_message = response.json().get('error_description', 'Unknown error')
            logging.error(f"Error refreshing friend's Trakt token: {error_message}")
            return False
    
    except Exception as e:
        logging.error(f"Error refreshing friend's Trakt token: {str(e)}")
        return False

def get_trakt_sources() -> Dict[str, List[Dict[str, Any]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    watchlist_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Watchlist')]
    list_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Lists')]
    friend_watchlist_sources = [data for source, data in content_sources.items() if source.startswith('Friends Trakt Watchlist')]
    
    return {
        'watchlist': watchlist_sources,
        'lists': list_sources,
        'friend_watchlist': friend_watchlist_sources
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

def fetch_items_from_trakt(endpoint: str, headers=None) -> List[Dict[str, Any]]:
    """Fetch items from Trakt API"""
    if headers is None:
        headers = get_trakt_headers()
    
    url = f"{TRAKT_API_URL}{endpoint}"
    logging.debug(f"Fetching items from Trakt API: {url}")
    
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Error fetching items from Trakt API: {str(e)}")
        return []

def assign_media_type(item: Dict[str, Any]) -> str:
    if 'movie' in item:
        return 'movie'
    elif 'episode' in item:
        return 'tv'  # We still return 'tv' for episodes since we want to track the show
    elif 'show' in item:
        return 'tv'
    else:
        logging.warning(f"Unknown media type: {item}. Skipping.")
        return ''

def get_imdb_id(item: Dict[str, Any], media_type: str) -> str:
    # For episodes, we want the show's ID, not the episode ID
    if 'episode' in item:
        if 'show' not in item:
            logging.error(f"Episode item missing show data: {json.dumps(item, indent=2)}")
            return ''
        ids = item['show'].get('ids', {})
    else:
        media_type_key = 'show' if media_type == 'tv' else media_type
        if media_type_key not in item:
            logging.error(f"Media type '{media_type_key}' not found in item: {json.dumps(item, indent=2)}")
            return ''
        ids = item[media_type_key].get('ids', {})
    
    if not ids:
        logging.error(f"No IDs found for item: {json.dumps(item, indent=2)}")
        return ''
    
    return ids.get('imdb') or ids.get('tmdb') or ids.get('tvdb') or ''

def process_trakt_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    processed_items = []
    seen_imdb_ids = set()  # Track IMDb IDs we've already processed
    skipped_count = 0
    duplicate_count = 0
    
    for item in items:
        media_type = assign_media_type(item)
        if not media_type:
            skipped_count += 1
            continue
        
        imdb_id = get_imdb_id(item, media_type)
        if not imdb_id:
            logging.warning(f"Skipping item due to missing ID: {item.get(media_type, {}).get('title', 'Unknown Title')}")
            skipped_count += 1
            continue
            
        # Skip if we've already processed this IMDb ID
        if imdb_id in seen_imdb_ids:
            duplicate_count += 1
            continue
            
        seen_imdb_ids.add(imdb_id)
        processed_items.append({
            'imdb_id': imdb_id,
            'media_type': media_type
        })
    
    if skipped_count > 0:
        logging.info(f"Skipped {skipped_count} items due to missing media type or ID")
    if duplicate_count > 0:
        logging.info(f"Skipped {duplicate_count} duplicate items")
        
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

def get_wanted_from_trakt_watchlist(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
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
            watchlist_items = fetch_items_from_trakt("/sync/watchlist")
            processed_items = process_trakt_items(watchlist_items)
            
            # Handle removal of collected items if enabled
            if should_remove:
                movies_to_remove = []
                shows_to_remove = []
                for item in processed_items[:]:  # Create a copy to iterate while modifying
                    item_state = get_media_item_presence(imdb_id=item['imdb_id'])
                    if item_state == "Collected":
                        # If it's a TV show and we want to keep series, skip removal
                        if item['media_type'] == 'tv':
                            if keep_series:
                                logging.debug(f"Keeping TV series: {item['imdb_id']}")
                                continue
                            else:
                                # Check if the show has ended before removing
                                from content_checkers.plex_watchlist import get_show_status
                                show_status = get_show_status(item['imdb_id'])
                                if show_status != 'ended':
                                    logging.debug(f"Keeping ongoing TV series: {item['imdb_id']} - status: {show_status}")
                                    continue
                                logging.debug(f"Removing ended TV series: {item['imdb_id']} - status: {show_status}")
                        
                        # Find original item for removal
                        original_item = next(
                            (x for x in watchlist_items if get_imdb_id(x, item['media_type']) == item['imdb_id']), 
                            None
                        )
                        if original_item:
                            item_type = 'show' if item['media_type'] == 'tv' else item['media_type']
                            removal_item = {"ids": original_item[item_type]['ids']}
                            if item['media_type'] == 'tv':
                                shows_to_remove.append(removal_item)
                            else:
                                movies_to_remove.append(removal_item)
                            processed_items.remove(item)

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
                    except Exception as e:
                        logging.error(f"Failed to perform bulk removal from watchlist: {e}")

            all_wanted_items.append((processed_items, versions))

    return all_wanted_items

def get_wanted_from_trakt_lists(trakt_list_url: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    logging.debug("Fetching Trakt lists")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token")
        raise Exception("Failed to obtain a valid Trakt access token")
    
    # Skip processing if URL contains 'asc'
    if 'asc' in trakt_list_url:
        return [([], versions)]
    
    all_wanted_items = []
    
    list_info = parse_trakt_list_url(trakt_list_url)
    if not list_info:
        logging.error(f"Failed to parse Trakt list URL: {trakt_list_url}")
        return [([], versions)]
    
    username = list_info['username']
    list_id = list_info['list_id']
    
    # Get list items
    endpoint = f"/users/{username}/lists/{list_id}/items"
    list_items = fetch_items_from_trakt(endpoint)
    
    processed_items = process_trakt_items(list_items)
    logging.info(f"Found {len(processed_items)} items from Trakt list")
    all_wanted_items.append((processed_items, versions))
    
    return all_wanted_items

def get_wanted_from_trakt_collection(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    logging.debug("Fetching Trakt collection")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token")
        raise Exception("Failed to obtain a valid Trakt access token")

    all_wanted_items = []

    # Get collection items
    response = make_trakt_request('get', "/sync/collection/movies")
    movie_items = response.json() if response else []
    
    response = make_trakt_request('get', "/sync/collection/shows")
    show_items = response.json() if response else []
    
    collection_items = movie_items + show_items
    processed_items = process_trakt_items(collection_items)
    
    logging.info(f"Found {len(processed_items)} items from Trakt collection")
    all_wanted_items.append((processed_items, versions))
    
    return all_wanted_items

def get_wanted_from_friend_trakt_watchlist(source_config: Dict[str, Any], versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """Get wanted items from a friend's Trakt watchlist"""
    auth_id = source_config.get('auth_id')
    if not auth_id:
        logging.error("No auth_id provided for friend's Trakt watchlist")
        return []
    
    # Get headers for the friend's account
    headers = get_trakt_friend_headers(auth_id)
    if not headers:
        logging.error(f"Could not get headers for friend's Trakt account: {auth_id}")
        return []
    
    # Get the friend's username from the source config or from the auth state
    username = source_config.get('username')
    if not username:
        try:
            state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
            with open(state_file, 'r') as file:
                state = json.load(file)
            username = state.get('username')
        except Exception:
            logging.error(f"Could not get username for friend's Trakt account: {auth_id}")
            return []
    
    if not username:
        logging.error(f"No username available for friend's Trakt account: {auth_id}")
        return []
    
    logging.info(f"Fetching watchlist for friend's Trakt account: {username}")
    try:
        # Get the watchlist from Trakt
        endpoint = f"/users/{username}/watchlist"
        items = fetch_items_from_trakt(endpoint, headers)
        
        # Process the items
        processed_items = process_trakt_items(items)
        logging.info(f"Found {len(processed_items)} wanted items from friend's Trakt watchlist: {username}")
        
        # Return in the same format as other content source functions
        return [(processed_items, versions)]
        
    except Exception as e:
        logging.error(f"Error fetching friend's Trakt watchlist: {str(e)}")
        return []

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
    no_early_release_skipped = 0 # Counter for skipped items due to the flag

    for item in items_to_check:
        # Skip episodes immediately
        if item['type'] == 'episode':
            skipped_count += 1
            continue

        # Check if early release is explicitly disabled for this item
        if item.get('no_early_release', False): # Check the flag before doing API calls
            no_early_release_skipped += 1
            continue

        imdb_id = item['imdb_id']
        # Perform Trakt lookups only if no_early_release is False
        trakt_id_search = fetch_items_from_trakt(f"/search/imdb/{imdb_id}")

        if trakt_id_search and isinstance(trakt_id_search, list) and len(trakt_id_search) > 0:
            # Determine the correct trakt_id
            trakt_id = None
            if 'movie' in trakt_id_search[0] and 'ids' in trakt_id_search[0]['movie'] and 'trakt' in trakt_id_search[0]['movie']['ids']:
                trakt_id = str(trakt_id_search[0]['movie']['ids']['trakt'])
            elif 'show' in trakt_id_search[0] and 'ids' in trakt_id_search[0]['show'] and 'trakt' in trakt_id_search[0]['show']['ids']:
                 trakt_id = str(trakt_id_search[0]['show']['ids']['trakt'])
            else:
                logging.warning(f"Unexpected Trakt API response structure or missing Trakt ID for IMDB ID: {imdb_id}. Response: {trakt_id_search[0]}")
                continue # Skip if we can't get a valid trakt ID

            # If we couldn't extract a valid trakt_id, skip
            if not trakt_id:
                 logging.warning(f"Could not extract Trakt ID for IMDB ID: {imdb_id}")
                 continue

            endpoint = f"/movies/{trakt_id}/lists/personal/popular" if item['type'] == 'movie' else f"/shows/{trakt_id}/lists/personal/popular"
            try:
                trakt_lists = fetch_items_from_trakt(endpoint)
            except Exception as e:
                 logging.error(f"Error fetching Trakt lists for {item['type']} ID {trakt_id} (IMDB: {imdb_id}): {e}")
                 continue # Skip item if list fetching fails

            if trakt_lists: # Ensure trakt_lists is not None or empty
                for trakt_list in trakt_lists:
                    # Check if 'name' exists and is not None before applying regex
                    list_name = trakt_list.get('name')
                    if list_name and re.search(r'(latest|new).*?(releases)', list_name, re.IGNORECASE):
                        logging.info(f"Found {item['title']} in early release list '{list_name}'. Setting early_release=True for item ID {item['id']}.")
                        update_media_item(item['id'], early_release=True)
                        updated_count += 1
                        break # Found in a relevant list, no need to check other lists for this item
            else:
                 logging.debug(f"No popular personal lists found for Trakt ID {trakt_id} (IMDB: {imdb_id})")


    if updated_count > 0:
        logging.info(f"Set early release flag for {updated_count} items found in Trakt 'new release' lists.")
    if no_early_release_skipped > 0:
         logging.info(f"Skipped checking {no_early_release_skipped} items because their 'no_early_release' flag was set.")
    if skipped_count > 0:
        logging.debug(f"Skipped {skipped_count} episodes during Trakt early release check.")

def fetch_liked_trakt_lists_details() -> List[Dict[str, str]]:
    """Fetches details (name, URL) of lists the authenticated user has liked."""
    logging.debug("Fetching details of Trakt liked lists")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token for fetching liked lists")
        return []

    liked_lists_details = []
    try:
        liked_lists_endpoint = "/users/likes/lists?limit=1000" # Add limit just in case
        liked_lists_response = make_trakt_request('get', liked_lists_endpoint)

        if not liked_lists_response or liked_lists_response.status_code != 200:
            logging.error(f"Failed to fetch liked lists details. Status: {liked_lists_response.status_code if liked_lists_response else 'N/A'}")
            return []

        liked_lists_data = liked_lists_response.json()
        logging.info(f"Found {len(liked_lists_data)} liked lists to potentially import.")

        for entry in liked_lists_data:
            list_data = entry.get('list')
            if not list_data:
                logging.warning(f"Skipping liked list entry due to missing list data: {entry}")
                continue

            list_ids = list_data.get('ids')
            list_user = list_data.get('user')
            list_name = list_data.get('name', 'Unnamed List')

            if not list_ids or not list_user or 'trakt' not in list_ids or 'username' not in list_user:
                logging.warning(f"Skipping liked list '{list_name}' due to missing ID or user data.")
                continue

            username = list_user['username']
            list_id = list_ids['trakt']
            # Construct the standard Trakt list URL
            list_url = f"https://trakt.tv/users/{username}/lists/{list_id}"

            liked_lists_details.append({
                'name': list_name,
                'url': list_url,
                'username': username, # Include for potential display name generation
                'list_id': str(list_id) # Include for potential display name generation
            })

    except Exception as e:
        logging.error(f"Error fetching liked lists details: {str(e)}", exc_info=True)

    return liked_lists_details

def get_wanted_from_trakt():
    """Get wanted items from all Trakt sources"""
    config = get_all_settings()
    versions = config.get('Scraping', {}).get('versions', {})
    trakt_sources = get_trakt_sources()
    all_processed_items = {} 

    # Process main watchlist
    if trakt_sources['watchlist']:
        logging.info("Processing Trakt Watchlist sources...")
        watchlist_results = get_wanted_from_trakt_watchlist(versions)
        for item in watchlist_results:
            imdb_id = item[0][0]['imdb_id']
            if imdb_id:
                all_processed_items[imdb_id] = item

    # Process lists
    if trakt_sources['lists']:
        logging.info("Processing Trakt List sources...")
        for list_source in trakt_sources['lists']:
            if list_source.get('enabled', False):
                list_url = list_source.get('url', '')
                list_versions = {}
                for version in list_source.get('versions', []):
                    if version in versions:
                        list_versions[version] = True
                
                list_results = get_wanted_from_trakt_lists(list_url, list_versions if list_versions else versions)
                for item in list_results:
                    imdb_id = item[0][0]['imdb_id']
                    if imdb_id:
                        all_processed_items[imdb_id] = item

    # Process friend watchlists
    if trakt_sources['friend_watchlist']:
        logging.info("Processing Friends Trakt Watchlist sources...")
        for friend_source in trakt_sources['friend_watchlist']:
            if friend_source.get('enabled', False):
                friend_versions = {}
                for version in friend_source.get('versions', []):
                    if version in versions:
                        friend_versions[version] = True
                
                friend_results = get_wanted_from_friend_trakt_watchlist(friend_source, friend_versions if friend_versions else versions)
                for item in friend_results:
                    imdb_id = item[0][0]['imdb_id']
                    if imdb_id:
                        all_processed_items[imdb_id] = item

    final_wanted_list = list(all_processed_items.values())
    logging.info(f"Total unique wanted items from all Trakt sources: {len(final_wanted_list)}")
    return final_wanted_list

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    wanted_items = get_wanted_from_trakt()
    print(f"Total wanted items: {len(wanted_items)}")
    print(json.dumps(wanted_items[:10], indent=2))  # Print first 10 items for brevity