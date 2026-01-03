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
from datetime import datetime, date, timedelta, timezone
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
TRAKT_IMDB_ID_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'trakt_imdb_id_cache.pkl')
CACHE_EXPIRY_DAYS = 7

# Get config directory from environment variable with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
TRAKT_CONFIG_FILE = os.path.join(CONFIG_DIR, '.pytrakt.json')
TRAKT_FRIENDS_DIR = os.path.join(CONFIG_DIR, 'trakt_friends')

# IMDB→Trakt ID Cache Functions
def load_imdb_trakt_cache():
    """Load IMDB→Trakt ID cache with smart expiration"""
    if os.path.exists(TRAKT_IMDB_ID_CACHE_FILE):
        try:
            with open(TRAKT_IMDB_ID_CACHE_FILE, 'rb') as f:
                return pickle.load(f)
        except Exception as e:
            logging.warning(f"Failed to load IMDB→Trakt ID cache: {e}")
            return {}
    return {}

def save_imdb_trakt_cache(cache):
    """Save IMDB→Trakt ID cache"""
    try:
        with open(TRAKT_IMDB_ID_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Failed to save IMDB→Trakt ID cache: {e}")

def is_cache_entry_valid(cache_entry, imdb_id, current_state=None):
    """
    Determine if a cache entry is still valid based on smart expiration rules.

    Args:
        cache_entry: Dict with 'trakt_id', 'cached_at', 'release_date'
        imdb_id: IMDB ID for logging
        current_state: Current state of the item ('Unreleased', 'Wanted', etc.)

    Returns:
        bool: True if cache is valid, False if expired
    """
    if not cache_entry or 'cached_at' not in cache_entry:
        return False

    try:
        cached_date = datetime.fromisoformat(cache_entry['cached_at'])
        now = datetime.now()
        age_days = (now - cached_date).days

        # Rule 1: Trakt IDs never expire (but check release date for freshness)
        trakt_id = cache_entry.get('trakt_id')
        if not trakt_id:
            return False

        # Rule 2: If state is "Unreleased", cache for 1 day only
        if current_state == 'Unreleased':
            if age_days > 1:
                logging.debug(f"Cache expired for {imdb_id}: Unreleased item older than 1 day")
                return False
            return True

        # Rule 3: Check release date age
        release_date = cache_entry.get('release_date')
        if release_date and release_date != 'Unknown':
            try:
                release_year = int(release_date[:4])
                current_year = datetime.now().year
                years_since_release = current_year - release_year

                # Recent movies (<2 years): Cache 7 days
                if years_since_release < 2:
                    if age_days > 7:
                        logging.debug(f"Cache expired for {imdb_id}: Recent movie cache older than 7 days")
                        return False
                # Old movies (>2 years): Cache 90 days
                else:
                    if age_days > 90:
                        logging.debug(f"Cache expired for {imdb_id}: Old movie cache older than 90 days")
                        return False
            except (ValueError, TypeError):
                # Can't parse release date, default to 30 day expiry
                if age_days > 30:
                    logging.debug(f"Cache expired for {imdb_id}: Unknown age, default 30 day expiry")
                    return False
        else:
            # No release date info, default to 30 day expiry
            if age_days > 30:
                logging.debug(f"Cache expired for {imdb_id}: No release date, default 30 day expiry")
                return False

        return True

    except Exception as e:
        logging.warning(f"Error validating cache entry for {imdb_id}: {e}")
        return False

# Global rate limiter for Trakt API
class TraktRateLimiter:
    """
    Global rate limiter to prevent hitting Trakt API limits.

    Per Trakt API documentation:
    - AUTHED_API_GET_LIMIT: 1000 calls every 5 minutes (same for all authenticated users)
    - VIP vs Free does NOT affect API rate limits (only affects account limits like list counts)
    """
    def __init__(self):
        from threading import Lock
        self.lock = Lock()
        # Authenticated users get 1000/5min regardless of VIP status
        # Use 90% to be conservative
        self.requests_per_window = 900  # 90% of 1000 req/5min for authenticated users
        self.window_seconds = 300  # 5 minutes
        self.request_times = []
        self.enabled = get_setting('Scraping', 'trakt_rate_limit_enabled', True)
        # Per-request delay to prevent bursts (1.0s = 1 req/sec = 300 req/5min)
        # Increased from 0.2s to avoid Cloudflare rate limiting when multiple tasks overlap
        self.per_request_delay = 1.0
        self.last_request_time = 0

        # Log the rate limiting strategy
        logging.info(f"Trakt rate limiter initialized: {self.requests_per_window} requests/5min limit, 1.0s per-request delay")

    def wait_if_needed(self):
        """Wait if we're approaching the rate limit"""
        if not self.enabled:
            return

        with self.lock:
            now = time.time()

            # Add per-request delay to prevent bursts
            time_since_last = now - self.last_request_time
            if time_since_last < self.per_request_delay:
                delay = self.per_request_delay - time_since_last
                time.sleep(delay)
                now = time.time()

            # Remove requests older than the window
            self.request_times = [t for t in self.request_times if now - t < self.window_seconds]

            if len(self.request_times) >= self.requests_per_window:
                # Calculate how long to wait
                oldest_in_window = self.request_times[0]
                wait_time = self.window_seconds - (now - oldest_in_window) + 1
                logging.warning(f"Trakt rate limit approaching ({len(self.request_times)}/{self.requests_per_window}). Waiting {wait_time:.1f}s")
                time.sleep(wait_time)
                # Clear old requests after waiting
                now = time.time()
                self.request_times = [t for t in self.request_times if now - t < self.window_seconds]

            # Record this request
            self.request_times.append(now)
            self.last_request_time = now

# Global rate limiter instance
_trakt_rate_limiter = TraktRateLimiter()

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
        logging.debug(f"Getting Trakt headers for friend's account: {auth_id}")
        
        # Load the friend's auth state
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            logging.error(f"Friend's Trakt auth file not found: {state_file}")
            return {}
        
        with open(state_file, 'r') as file:
            state = json.load(file)
        
        logging.debug(f"Loaded friend's auth state: {state.get('status', 'unknown')}")
        
        # Check if the token is expired or nearing expiration (within 1 hour)
        if state.get('expires_at'):
            expires_at_ts = _to_timestamp(state.get('expires_at'))
            
            # Check if token should be refreshed (expired or nearing expiration)
            if _should_refresh_token(expires_at_ts):
                current_time = time.time()
                if current_time > expires_at_ts:
                    logging.info(f"Friend's Trakt token for {auth_id} is expired. Attempting refresh.")
                else:
                    logging.info(f"Friend's Trakt token for {auth_id} is nearing expiration (within 1 hour). Attempting refresh.")
                
                logging.debug(f"Attempting to refresh friend's Trakt token for {auth_id}")
                if refresh_friend_token(auth_id):
                    logging.debug(f"Successfully refreshed friend's Trakt token for {auth_id}")
                    # Reload the state after successful refresh
                    with open(state_file, 'r') as file:
                        state = json.load(file)
                else:
                    logging.error(f"Failed to refresh friend's Trakt token for {auth_id}")
        
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
        logging.debug(f"Refreshing token for friend's Trakt account: {auth_id}")
        
        # Load the state
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            logging.error(f"Friend's Trakt auth file not found: {state_file}")
            return False
        
        with open(state_file, 'r') as file:
            state = json.load(file)
        
        logging.debug(f"Current token expires at: {state.get('expires_at')}")
        
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
        logging.debug(f"Making token refresh request to Trakt API for auth_id: {auth_id}")
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
        logging.debug(f"Token refresh response status: {response.status_code}")
        
        if response.status_code == 200:
            token_data = response.json()
            
            # Update state with new token information
            now = datetime.now(timezone.utc)
            state.update({
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'expires_at': int((now + timedelta(seconds=token_data['expires_in'])).timestamp()),
                'last_refresh': now.isoformat()
            })
            
            # Save the updated state
            with open(state_file, 'w') as file:
                json.dump(state, file)
            
            logging.info(f"Successfully refreshed token for friend's Trakt account: {auth_id}")
            return True
        else:
            try:
                error_data = response.json()
                error_message = error_data.get('error_description', 'Unknown error')
                logging.error(f"Error refreshing friend's Trakt token: {error_message}")
                logging.error(f"Full error response: {error_data}")
            except json.JSONDecodeError:
                logging.error(f"Error refreshing friend's Trakt token: Non-JSON response")
                logging.error(f"Response status: {response.status_code}")
                logging.error(f"Response text: {response.text[:500]}...")
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
            # Wait if rate limit is approaching (global rate limiter)
            _trakt_rate_limiter.wait_if_needed()

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

                    logging.warning(f"Rate limit hit (429). Waiting {delay:.2f} seconds before retry {attempt + 1}/{max_retries}")
                    sleep(delay)
                    continue
                elif status_code == 420:  # VIP Enhanced - Account limit exceeded
                    # Account limit exceeded (list count, item count, etc)
                    is_vip = e.response.headers.get('X-VIP-User', 'false') == 'true'
                    account_limit = e.response.headers.get('X-Account-Limit', 'unknown')
                    upgrade_url = e.response.headers.get('X-Upgrade-URL', 'https://trakt.tv/vip')

                    retry_after = int(e.response.headers.get('Retry-After', 0))
                    delay = retry_after if retry_after > 0 else initial_delay * (2 ** attempt) + random.uniform(0, 1)

                    if is_vip:
                        logging.warning(f"Account limit exceeded (420). Limit: {account_limit}. Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}")
                    else:
                        logging.warning(f"Account limit exceeded (420). Upgrade to VIP at {upgrade_url} for higher limits. Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}")
                    sleep(delay)
                    continue
                elif status_code == 423:  # Locked User Account
                    logging.error("Trakt account is locked or deactivated. Please contact Trakt support.")
                    raise ValueError("Trakt account is locked or deactivated")
                elif status_code == 426:  # VIP Only
                    upgrade_url = e.response.headers.get('X-Upgrade-URL', 'https://trakt.tv/vip')
                    logging.error(f"This API method requires Trakt VIP. Upgrade at {upgrade_url}")
                    raise ValueError(f"VIP required: {upgrade_url}")
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

def fetch_items_from_trakt(
    endpoint: str,
    headers: Dict[str, str] | None = None,
    max_retries: int = 5,
    initial_delay: int = DEFAULT_INITIAL_RETRY_DELAY,
) -> List[Dict[str, Any]]:
    """Fetch items from Trakt API with retry and exponential back-off.

    Args:
        endpoint: The Trakt API endpoint (e.g. "/search/imdb/tt1234567").
        headers: Optional custom headers. Falls back to :func:`get_trakt_headers`.
        max_retries: Maximum number of retry attempts before giving up.
        initial_delay: Initial delay (seconds) for the exponential back-off.

    Returns:
        A list of dictionaries returned from the Trakt API, or an empty list
        if all retry attempts fail.
    """

    if headers is None:
        headers = get_trakt_headers()

    # If header retrieval failed, exit early.
    if not headers:
        return []

    url = f"{TRAKT_API_URL}{endpoint}"
    logging.debug(f"Fetching items from Trakt API: {url}")

    for attempt in range(max_retries):
        try:
            # Wait if rate limit is approaching (global rate limiter)
            _trakt_rate_limiter.wait_if_needed()

            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

            # Detect HTML responses that sometimes appear instead of JSON
            content_type = response.headers.get("content-type", "")
            if "html" in content_type.lower():
                logging.error(f"Received HTML response instead of JSON from Trakt API. Status: {response.status_code}, Content-Type: {content_type}")
                logging.error(f"Response headers: {dict(response.headers)}")
                logging.error(f"Response body preview: {response.text[:500]}...")
                raise ValueError("Received HTML response instead of JSON from Trakt API")

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as http_err:
            status_code = http_err.response.status_code if http_err.response else "N/A"

            if status_code == 429:  # Too Many Requests – rate-limited
                retry_after = int(http_err.response.headers.get("Retry-After", 0)) if http_err.response else 0
                if retry_after > 0:
                    # Cloudflare/Trakt explicitly told us how long to wait - respect it exactly
                    delay = retry_after
                    logging.warning(
                        f"Rate limit hit (429). Cloudflare requires {delay}s wait. Retrying {attempt + 1}/{max_retries}"
                    )
                else:
                    # No Retry-After header, use exponential backoff
                    delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                    logging.warning(
                        f"Rate limit hit (429). Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}"
                    )

            elif status_code == 420:  # VIP Enhanced - Account limit exceeded
                is_vip = http_err.response.headers.get('X-VIP-User', 'false') == 'true' if http_err.response else False
                account_limit = http_err.response.headers.get('X-Account-Limit', 'unknown') if http_err.response else 'unknown'
                upgrade_url = http_err.response.headers.get('X-Upgrade-URL', 'https://trakt.tv/vip') if http_err.response else 'https://trakt.tv/vip'

                retry_after = int(http_err.response.headers.get("Retry-After", 0)) if http_err.response else 0
                delay = retry_after if retry_after > 0 else initial_delay * (2 ** attempt) + random.uniform(0, 1)

                if is_vip:
                    logging.warning(f"Account limit exceeded (420). Limit: {account_limit}. Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}")
                else:
                    logging.warning(f"Account limit exceeded (420). Upgrade to VIP at {upgrade_url} for higher limits. Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}")

            elif status_code == 423:  # Locked User Account
                logging.error("Trakt account is locked or deactivated. Please contact Trakt support.")
                return []

            elif status_code == 426:  # VIP Only
                upgrade_url = http_err.response.headers.get('X-Upgrade-URL', 'https://trakt.tv/vip') if http_err.response else 'https://trakt.tv/vip'
                logging.error(f"This API method requires Trakt VIP. Upgrade at {upgrade_url}")
                return []

            elif status_code in (502, 504):  # Temporary gateway issues
                delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                logging.warning(
                    f"Temporary Trakt API error {status_code}. Waiting {delay:.2f} seconds before retry {attempt + 1}/{max_retries}"
                )

            else:
                logging.error(
                    f"Unrecoverable HTTP error {status_code} when fetching items from Trakt API: {http_err}"
                )
                return []

        except (requests.exceptions.RequestException, ValueError) as req_err:
            # Network problem or unexpected content-type
            delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
            logging.warning(
                f"Request failed ({req_err}). Retrying in {delay:.2f} seconds. Attempt {attempt + 1}/{max_retries}"
            )

        # If this was the last attempt, break out of the loop; otherwise sleep and retry.
        if attempt < max_retries - 1:
            sleep(delay)

    logging.error(f"Failed to fetch items from Trakt API after {max_retries} attempts: {url}")
    return []

def assign_media_type(item: Dict[str, Any]) -> str:
    if 'movie' in item:  # Item is a wrapper e.g. {"movie": {...}}
        return 'movie'
    elif 'show' in item:  # Item is a wrapper e.g. {"show": {...}}
        return 'tv'
    elif 'episode' in item:  # Item is a wrapper e.g. {"episode": {...}, "show": {...}}
        return 'tv'
    # If not a wrapper, item might be the direct movie/show object
    # Check for show-specific keys first
    elif 'first_aired' in item or \
         'aired_episodes' in item or \
         ('status' in item and item['status'] in ['returning series', 'ended', 'in production', 'canceled', 'planned', 'pilot']):
        return 'tv'
    # Check for movie-specific keys if not identified as a show
    elif 'released' in item: # Heuristic for direct movie object
        return 'movie'
    else:
        item_title = item.get('title', 'N/A')
        item_year = item.get('year', 'N/A')
        id_keys = list(item.get('ids', {}).keys())
        logging.warning(f"Unknown media type for item: Title='{item_title}', Year='{item_year}', ID keys={id_keys}. Skipping. Full item keys: {list(item.keys())}")
        return ''

def get_imdb_id(item: Dict[str, Any], media_type: str) -> str:
    ids_container = None

    if 'episode' in item:
        if 'show' not in item:
            logging.error(f"Episode item missing show data: {json.dumps(item, indent=2)}")
            return ''
        ids_container = item['show']
    else:  # Item is a movie or a show (not an episode)
        # Based on media_type, determine the expected key if it's a wrapped item
        expected_wrapper_key = 'show' if media_type == 'tv' else media_type  # 'movie' or 'show'

        if expected_wrapper_key in item:
            # It's a wrapped item like item['movie'] = {...} or item['show'] = {...}
            ids_container = item[expected_wrapper_key]
        else:
            # It's a direct item (e.g., from /movies/trending or /shows/trending)
            # The item itself is the container of 'ids'
            ids_container = item
            
    if not ids_container:
        logging.error(f"Could not determine ids_container for item: {json.dumps(item, indent=2)} with media_type: {media_type}")
        return ''
        
    ids = ids_container.get('ids', {})
    if not ids:
        logging.warning(f"No 'ids' dictionary found in determined container for item: {json.dumps(item, indent=2)}. Container was part of item.")
        return ''
    
    # Prioritize IMDb, then TMDB, then TVDB, ensuring the ID is a string.
    imdb = ids.get('imdb')
    if imdb:
        return str(imdb)
    
    tmdb = ids.get('tmdb')
    if tmdb:
        return str(tmdb)
        
    tvdb = ids.get('tvdb')
    if tvdb:
        return str(tvdb)

    logging.warning(f"No IMDb, TMDB, or TVDB ID found in 'ids' for item: {json.dumps(item, indent=2)}")
    return ''

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

def _to_timestamp(value: Any) -> float | None:
    """Converts an ISO 8601 string or a Unix timestamp to a float timestamp."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            # Handle ISO format, replacing 'Z' with timezone info
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, TypeError):
            # Handle stringified timestamp
            try:
                return float(value)
            except (ValueError, TypeError):
                return None
    return None

def _should_refresh_token(expires_at_ts: float, refresh_threshold_hours: int = None) -> bool:
    """
    Check if a token should be refreshed based on its expiration time.
    
    Args:
        expires_at_ts: Token expiration timestamp
        refresh_threshold_hours: Hours before expiration to start refreshing (default: from settings or 1)
    
    Returns:
        True if token should be refreshed, False otherwise
    """
    if not expires_at_ts:
        return False
    
    # Get refresh threshold from settings, default to 1 hour
    if refresh_threshold_hours is None:
        refresh_threshold_hours = get_setting('Trakt', 'refresh_threshold_hours', 1)
    
    current_time = time.time()
    refresh_threshold = expires_at_ts - (refresh_threshold_hours * 3600)
    return current_time >= refresh_threshold

def is_refresh_token_expired() -> bool:
    """
    Check if the refresh token has expired by examining the current config.
    
    Returns:
        True if refresh token is expired or missing, False otherwise
    """
    try:
        trakt_config = get_trakt_config()
        # Only check for refresh token, not access token
        # Access token can be expired while refresh token is still valid
        return not trakt_config.get('OAUTH_REFRESH')
    except Exception as e:
        logging.error(f"Error checking refresh token status: {e}")
        return True

def ensure_trakt_auth():
    logging.debug("Checking Trakt authentication")
    
    # Read config directly from file like the battery does
    trakt_config = get_trakt_config()
    
    # Extract tokens from config
    access_token = trakt_config.get('OAUTH_TOKEN')
    refresh_token = trakt_config.get('OAUTH_REFRESH')
    expires_at = trakt_config.get('OAUTH_EXPIRES_AT')
    
    if not access_token or not expires_at:
        # Check if we have a refresh token available
        if refresh_token:
            logging.info("Access token missing but refresh token available. Attempting to refresh...")
            # Try to refresh the token
            try:
                # Use the trakt library for refresh but read result directly
                trakt.core.CONFIG_PATH = TRAKT_CONFIG_FILE
                trakt.core.load_config()
                trakt.core._validate_token(trakt.core.CORE)
                trakt.core.load_config()
                logging.info("Token refreshed successfully")
                
                # Re-read config after refresh
                trakt_config = get_trakt_config()
                access_token = trakt_config.get('OAUTH_TOKEN')
                expires_at = trakt_config.get('OAUTH_EXPIRES_AT')
                
                if access_token and expires_at:
                    return access_token
            except Exception as e:
                logging.warning(f"Failed to refresh token: {e}")
                # Continue to the error case below
        
        logging.error("Trakt authentication not properly configured")
        return None
    
    expires_at_ts = _to_timestamp(expires_at)
    if not expires_at_ts:
        logging.error("Trakt 'OAUTH_EXPIRES_AT' is missing or invalid.")
        return None

    # Check if token should be refreshed (expired or nearing expiration)
    if _should_refresh_token(expires_at_ts):
        current_time = int(time.time())
        if current_time > expires_at_ts:
            logging.info("Token expired, refreshing")
        else:
            logging.info("Token nearing expiration (within 1 hour), refreshing")
        
        try:
            # Use the trakt library for refresh but read result directly
            trakt.core.CONFIG_PATH = TRAKT_CONFIG_FILE
            trakt.core.load_config()
            trakt.core._validate_token(trakt.core.CORE)
            trakt.core.load_config()
            logging.debug("Token refreshed successfully and config reloaded.")
            
            # Re-read config after refresh
            trakt_config = get_trakt_config()
            access_token = trakt_config.get('OAUTH_TOKEN')
            return access_token
        except Exception as e:
            # Check if this is a refresh token expiration error
            error_str = str(e).lower()
            # Only clear refresh token for specific refresh token errors, not general invalid_grant
            if "refresh_token" in error_str and ("invalid" in error_str or "expired" in error_str or "revoked" in error_str):
                logging.error(f"Refresh token has expired or is invalid. Manual re-authentication required: {e}")
                # Clear the expired tokens to force re-authentication
                try:
                    trakt_config.pop('OAUTH_TOKEN', None)
                    trakt_config.pop('OAUTH_REFRESH', None)
                    trakt_config.pop('OAUTH_EXPIRES_AT', None)
                    save_trakt_config(trakt_config)
                    logging.info("Cleared expired tokens from config file")
                except Exception as clear_error:
                    logging.error(f"Failed to clear expired tokens: {clear_error}")
                return None
            elif "invalid_grant" in error_str:
                # This might be an access token issue, not necessarily refresh token
                logging.warning(f"Invalid grant error - this might be an access token issue: {e}")
                logging.info("Attempting to refresh access token using refresh token...")
                # Don't clear the refresh token, just return None to indicate we need to retry
                return None
            else:
                logging.error(f"Failed to refresh Trakt token: {e}", exc_info=True)
                return None
    else:
        logging.debug("Token is valid")
    
    return access_token

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
    
    # Clean username for API use (handle email-based usernames)
    clean_username = clean_username_for_api(username)
    if clean_username != username:
        logging.info(f"Cleaned username for list access: '{username}' -> '{clean_username}'")
    
    # Get list items
    endpoint = f"/users/{clean_username}/lists/{list_id}/items"
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

def clean_username_for_api(username: str, auth_id: str = None) -> str:
    """Clean username for use in Trakt API endpoints"""
    if not username:
        return username
    
    # If we have an auth_id, try to get the slug from the auth state first
    if auth_id:
        try:
            state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
            if os.path.exists(state_file):
                with open(state_file, 'r') as file:
                    state = json.load(file)
                
                # Check if we have a slug stored in the auth state
                slug = state.get('slug')
                if slug:
                    logging.info(f"Using slug '{slug}' from auth state instead of username '{username}'")
                    return slug
        except Exception as e:
            logging.debug(f"Could not read auth state for slug lookup: {str(e)}")
    
    # If username contains @, it's likely an email address
    if '@' in username:
        # Convert email to Trakt slug format: replace dots and @ with hyphens
        # e.g., liam.d.hughes@gmail.com becomes liam-d-hughes-gmail-com
        slug_username = username.replace('.', '-').replace('@', '-')
        logging.info(f"Converting email-like username '{username}' to slug format '{slug_username}'")
        return slug_username
    
    return username

def get_wanted_from_friend_trakt_watchlist(source_config: Dict[str, Any], versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """Get wanted items from a friend's Trakt watchlist"""
    auth_id = source_config.get('auth_id')
    if not auth_id:
        logging.error("No auth_id provided for friend's Trakt watchlist")
        return []
    
    logging.debug(f"Getting wanted items from friend's Trakt watchlist for auth_id: {auth_id}")
    
    # Get headers for the friend's account
    headers = get_trakt_friend_headers(auth_id)
    if not headers:
        logging.error(f"Could not get headers for friend's Trakt account: {auth_id}")
        return []
    
    logging.debug(f"Successfully got headers for friend's Trakt account: {auth_id}")
    logging.debug(f"Headers: {headers}")
    
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
    
    # Clean the username for API use
    clean_username = clean_username_for_api(username, auth_id)
    logging.info(f"Fetching watchlist for friend's Trakt account: {username} (cleaned: {clean_username})")
    
    try:
        # Get the watchlist from Trakt
        endpoint = f"/users/{clean_username}/watchlist"
        logging.debug(f"Making watchlist request to endpoint: {endpoint}")
        logging.debug(f"Using headers: {headers}")
        items = fetch_items_from_trakt(endpoint, headers)
        
        # Process the items
        processed_items = process_trakt_items(items)
        logging.info(f"Found {len(processed_items)} wanted items from friend's Trakt watchlist: {username}")
        
        # Return in the same format as other content source functions
        return [(processed_items, versions)]
        
    except Exception as e:
        logging.error(f"Error fetching friend's Trakt watchlist using cleaned username '{clean_username}' (original: '{username}'): {str(e)}")
        
        # Try fallback: use user ID instead of username
        try:
            # Get user ID from auth state
            state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
            with open(state_file, 'r') as file:
                state = json.load(file)
            
            # Try to get user ID from the user data we fetched during authorization
            user_id = state.get('user_id')
            if user_id:
                logging.info(f"Trying fallback with user ID: {user_id}")
                endpoint = f"/users/{user_id}/watchlist"
                items = fetch_items_from_trakt(endpoint, headers)
                
                processed_items = process_trakt_items(items)
                logging.info(f"Found {len(processed_items)} wanted items from friend's Trakt watchlist using user ID: {user_id}")
                return [(processed_items, versions)]
            else:
                logging.error("No user ID available for fallback")
        except Exception as fallback_error:
            logging.error(f"Fallback attempt also failed: {str(fallback_error)}")
        
        return []

def get_wanted_from_special_trakt_lists(source_config: Dict[str, Any], versions_profile: Dict[str, Any]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """
    Fetches and processes items from configured Special Trakt Lists.
    """
    logging.debug(f"Fetching from Special Trakt Lists: {source_config.get('display_name', 'N/A')}")
    access_token = ensure_trakt_auth()
    if access_token is None:
        logging.error("Failed to obtain a valid Trakt access token for Special Trakt Lists.")
        return []

    selected_list_types = source_config.get('special_list_type', [])
    media_type_filter = source_config.get('media_type', 'All').lower()  # movies, shows, all

    if not selected_list_types:
        logging.warning(f"No special list types selected for source: {source_config.get('display_name')}")
        return []

    all_items_for_this_source = []
    seen_imdb_ids_for_this_source = set()

    special_list_api_details = {
        "Trending": {"movies": "/movies/trending", "shows": "/shows/trending"},
        "Popular": {"movies": "/movies/popular", "shows": "/shows/popular"},
        "Anticipated": {"movies": "/movies/anticipated", "shows": "/shows/anticipated"},
        "Box Office": {"movies": "/movies/boxoffice", "shows": None}, # Movies only
        "Played": {"movies": "/movies/played/weekly", "shows": "/shows/played/weekly"},
        "Watched": {"movies": "/movies/watched/weekly", "shows": "/shows/watched/weekly"},
        "Collected": {"movies": "/movies/collected/weekly", "shows": "/shows/collected/weekly"},
        "Favorited": {"movies": "/movies/favorited/weekly", "shows": "/shows/favorited/weekly"}
    }
    fetch_params_str = "limit=100&extended=full" # Common parameters

    for list_type in selected_list_types:
        if list_type not in special_list_api_details:
            logging.warning(f"Unknown special list type '{list_type}' in source config. Skipping.")
            continue

        api_paths_for_type = special_list_api_details[list_type]
        endpoints_to_call = []

        if media_type_filter == 'movies' or media_type_filter == 'all':
            if api_paths_for_type.get("movies"):
                endpoints_to_call.append(api_paths_for_type["movies"])
        if media_type_filter == 'shows' or media_type_filter == 'all':
            if api_paths_for_type.get("shows"):
                endpoints_to_call.append(api_paths_for_type["shows"])
        
        if list_type == "Box Office" and media_type_filter == 'shows':
            logging.info("Box Office list type is only for movies. Skipping for 'shows' filter.")
            continue # Box office is movies only

        for endpoint_path in endpoints_to_call:
            if not endpoint_path: continue # Skip if None (e.g. Box Office for shows)

            full_api_path = f"{endpoint_path}?{fetch_params_str}"
            logging.info(f"Fetching from Special Trakt List '{list_type}', endpoint: {full_api_path}")
            
            response = make_trakt_request('get', full_api_path)
            if response:
                try:
                    raw_items = response.json()
                    if not isinstance(raw_items, list):
                        # Some endpoints might return a dict with items inside, e.g. list items
                        # For simplicity, this example assumes endpoints return a direct list of media items.
                        # If structure varies (e.g. item['movie'] or item['show']), process_trakt_items handles it.
                        logging.warning(f"Expected a list from {full_api_path}, got {type(raw_items)}. Skipping this response.")
                        continue
                    
                    processed_batch = process_trakt_items(raw_items)
                    for item_detail in processed_batch:
                        if item_detail['imdb_id'] and item_detail['imdb_id'] not in seen_imdb_ids_for_this_source:
                            all_items_for_this_source.append(item_detail)
                            seen_imdb_ids_for_this_source.add(item_detail['imdb_id'])
                except json.JSONDecodeError:
                    logging.error(f"Failed to decode JSON from {full_api_path}")
                except Exception as e:
                    logging.error(f"Error processing items from {full_api_path}: {e}")
            else:
                logging.error(f"Failed to fetch data from {full_api_path} for list type {list_type}")

    if not all_items_for_this_source:
        logging.info(f"No items found for Special Trakt List source: {source_config.get('display_name')}")
        return []
    
    logging.info(f"Found {len(all_items_for_this_source)} unique items from Special Trakt List source: {source_config.get('display_name')}")
    return [(all_items_for_this_source, versions_profile)]

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
            
            # Clean username for URL construction (handle email-based usernames)
            clean_username = clean_username_for_api(username)
            if clean_username != username:
                logging.debug(f"Cleaned username for URL construction: '{username}' -> '{clean_username}'")
            
            # Construct the standard Trakt list URL
            list_url = f"https://trakt.tv/users/{clean_username}/lists/{list_id}"

            liked_lists_details.append({
                'name': list_name,
                'url': list_url,
                'username': username, # Include for potential display name generation
                'list_id': str(list_id) # Include for potential display name generation
            })

    except Exception as e:
        logging.error(f"Error fetching liked lists details: {str(e)}", exc_info=True)

    return liked_lists_details

def get_trakt_config():
    """Get the current Trakt configuration from .pytrakt.json"""
    if os.path.exists(TRAKT_CONFIG_FILE):
        try:
            with open(TRAKT_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.warning(f"Could not decode .pytrakt.json file at {TRAKT_CONFIG_FILE}. It might be empty or malformed.")
            return {}
    return {}

def save_trakt_config(config):
    """Save the Trakt configuration to .pytrakt.json"""
    os.makedirs(os.path.dirname(TRAKT_CONFIG_FILE), exist_ok=True)
    with open(TRAKT_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

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
