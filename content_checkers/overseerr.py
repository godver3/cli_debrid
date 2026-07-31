import logging
from routes.api_tracker import api
from utilities.settings import get_setting, get_all_settings
from typing import List, Dict, Any, Tuple
import os
import pickle
from datetime import datetime, timedelta

DEFAULT_TAKE = 100
REQUEST_TIMEOUT = 15  # seconds


class OverseerrAuthError(Exception):
    """Raised when Overseerr rejects the configured API key (401/403)."""
    pass

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
OVERSEERR_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'overseerr_cache.pkl')
CACHE_EXPIRY_DAYS = 7
OVERSEERR_AUTH_STATE_FILE = os.path.join(DB_CONTENT_DIR, 'overseerr_auth_state.pkl')


def _load_overseerr_auth_state() -> Dict[str, bool]:
    """Per-source_id -> True if currently in a notified auth-failure state. Persisted
    to disk so a program restart (e.g. after a settings save) doesn't cause a
    duplicate notification for a failure the user already saw."""
    try:
        if os.path.exists(OVERSEERR_AUTH_STATE_FILE):
            with open(OVERSEERR_AUTH_STATE_FILE, 'rb') as f:
                return pickle.load(f)
    except Exception as e:
        logging.debug(f"Error loading Overseerr auth state: {e}. Starting fresh.")
    return {}


def _save_overseerr_auth_state(state: Dict[str, bool]):
    try:
        os.makedirs(os.path.dirname(OVERSEERR_AUTH_STATE_FILE), exist_ok=True)
        with open(OVERSEERR_AUTH_STATE_FILE, 'wb') as f:
            pickle.dump(state, f)
    except Exception as e:
        logging.debug(f"Error saving Overseerr auth state: {e}")


def _notify_overseerr_auth_failure(source_id: str, display_name: str):
    """Fire a notification only on the transition into an auth-failure state, so a
    ~15-minute polling cycle doesn't spam a notification every run while the key
    stays broken."""
    state = _load_overseerr_auth_state()
    if state.get(source_id):
        return  # Already notified for this ongoing failure
    state[source_id] = True
    _save_overseerr_auth_state(state)
    try:
        from routes.notifications import store_notification
        store_notification(
            title="Overseerr Connection Failed",
            message=(
                f"'{display_name}' rejected the configured API key (401/403). "
                f"Wanted content from this source will not be fetched until the API key is corrected in Settings."
            ),
            notification_type='error',
            link="/settings"
        )
    except Exception as e:
        logging.warning(f"Could not store Overseerr auth-failure notification: {e}")


def _clear_overseerr_auth_failure(source_id: str, display_name: str):
    """Called on a successful fetch — clears the notified state and, if we were
    previously in a failure state, lets the user know it recovered."""
    state = _load_overseerr_auth_state()
    if not state.get(source_id):
        return
    del state[source_id]
    _save_overseerr_auth_state(state)
    try:
        from routes.notifications import store_notification
        store_notification(
            title="Overseerr Connection Restored",
            message=f"'{display_name}' is authenticating successfully again.",
            notification_type='info',
            link="/settings"
        )
    except Exception as e:
        logging.warning(f"Could not store Overseerr auth-recovery notification: {e}")


def parse_bool(value: Any) -> bool:
    """Safely parse various truthy/falsey representations into a boolean."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on", "y", "t"}

def normalize_tag_to_string(tag: Any) -> str:
    """Normalize a tag value that may be a dict, int, or str into a lowercase string."""
    if isinstance(tag, dict):
        for key in ("tag", "name", "label", "id"):
            if key in tag and tag[key] is not None:
                return str(tag[key]).strip().lower()
        return ""
    if isinstance(tag, int):
        return str(tag)
    if isinstance(tag, str):
        return tag.strip().lower()
    return ""

def extract_requested_seasons(raw_seasons: Any) -> List[int]:
    """Extract season numbers from a list that may contain dicts or ints."""
    seasons: List[int] = []
    if not isinstance(raw_seasons, list):
        return seasons
    for season_entry in raw_seasons:
        season_number: Any = None
        if isinstance(season_entry, dict):
            season_number = season_entry.get("seasonNumber")
            if season_number is None:
                season_number = season_entry.get("season")
            if season_number is None:
                season_number = season_entry.get("number")
        elif isinstance(season_entry, int):
            season_number = season_entry
        elif isinstance(season_entry, str):
            if season_entry.isdigit():
                season_number = int(season_entry)
        # Coerce to int if possible
        if isinstance(season_number, str) and season_number.isdigit():
            season_number = int(season_number)
        if isinstance(season_number, int):
            seasons.append(season_number)
    return seasons

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

def get_overseerr_requester_by_tmdb(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, media_type: str) -> str:
    """
    Get the requester name for a media item from Overseerr by TMDB ID.

    Args:
        overseerr_url: Overseerr base URL
        overseerr_api_key: API key
        tmdb_id: TMDB ID of the media
        media_type: 'movie' or 'episode'/'show'

    Returns:
        str: Requester display name, email, or 'Unknown' if not found
    """
    try:
        details = get_overseerr_details(overseerr_url, overseerr_api_key, tmdb_id, media_type)

        if not details:
            logging.debug(f"No Overseerr details found for TMDB ID {tmdb_id}")
            return 'Unknown'

        # Navigate to mediaInfo.requests
        media_info = details.get('mediaInfo', {})
        requests = media_info.get('requests', [])

        if not requests:
            logging.debug(f"No requests found for TMDB ID {tmdb_id}")
            return 'Unknown'

        # Get the most recent request (last in the list)
        latest_request = requests[-1]
        requested_by = latest_request.get('requestedBy', {})

        # Extract display name or email
        requester = requested_by.get('displayName') or requested_by.get('email') or 'Unknown'

        logging.info(f"Found Overseerr requester '{requester}' for TMDB {tmdb_id}")
        return requester

    except Exception as e:
        logging.error(f"Error getting Overseerr requester for TMDB {tmdb_id}: {e}")
        return 'Unknown'

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
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code in (401, 403):
                logging.error(f"Error fetching wanted content from Overseerr: {e}")
                raise OverseerrAuthError(f"Overseerr rejected the API key (HTTP {status_code})") from e
            logging.error(f"Error fetching wanted content from Overseerr: {e}")
            break
        except Exception as e:
            logging.error(f"Unexpected error while processing Overseerr response: {e}")
            break

    logging.info(f"Found {len(wanted_content)} wanted items from Overseerr")
    return wanted_content

def get_wanted_from_overseerr(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    overseerr_sources = [(source_id, data) for source_id, data in content_sources.items() if source_id.startswith('Overseerr') and data.get('enabled', False)]
    allow_partial = parse_bool(get_setting('Debug', 'allow_partial_overseerr_requests', 'False'))
    disable_caching = True  # Hardcoded to True
    logging.info(f"allow_partial: {allow_partial}")

    all_wanted_items = []
    cache = {} if disable_caching else load_overseerr_cache()
    current_time = datetime.now()

    for source_id, source in overseerr_sources:
        overseerr_url = source.get('url')
        overseerr_api_key = source.get('api_key')
        display_name = source.get('display_name') or source_id
        ignore_tags_str = source.get('ignore_tags', '')
        ignore_tags = {tag.strip().lower() for tag in ignore_tags_str.split(',') if tag.strip()} if ignore_tags_str else set()

        if not overseerr_url or not overseerr_api_key:
            logging.error(f"Overseerr URL or API key not set for source: {source}. Please configure in settings.")
            continue

        try:
            wanted_content_raw = fetch_overseerr_wanted_content(overseerr_url, overseerr_api_key)
            _clear_overseerr_auth_failure(source_id, display_name)
            wanted_items = []
            cache_skipped = 0
            ignored_by_tag = 0

            for item in wanted_content_raw:
                raw_tags = item.get('tags') or []
                item_tags: set[str] = set()
                for raw_tag in raw_tags:
                    normalized = normalize_tag_to_string(raw_tag)
                    if normalized:
                        item_tags.add(normalized)
                if ignore_tags and not item_tags.isdisjoint(ignore_tags):
                    ignored_by_tag += 1
                    continue

                media = item.get('media', {})

                if media.get('mediaType') in ['movie', 'tv']:
                    # Extract requester information
                    requested_by = item.get('requestedBy', {})
                    requester_display_name = requested_by.get('displayName') or requested_by.get('email') or 'Unknown'
                    request_id = item.get('id')  # Overseerr's request ID
                    tmdb_id = media.get('tmdbId')

                    # Debug logging for ALL requests to see what we're getting
                    logging.info(f"DEBUG Overseerr: Request {request_id} (TMDB: {tmdb_id}) - requestedBy: {requested_by}")
                    logging.info(f"DEBUG Overseerr: Extracted requester_display_name: {repr(requester_display_name)}")

                    wanted_item = {
                        'tmdb_id': media.get('tmdbId'),
                        'media_type': media.get('mediaType'),
                        'content_source': item.get('content_source'),
                        'content_source_detail': requester_display_name,  # Store requester name
                        'overseerr_request_id': request_id  # Store request ID for tracking
                    }

                    # Handle season information for TV shows when partial requests are allowed
                    if allow_partial and media.get('mediaType') == 'tv' and 'seasons' in item:
                        requested_seasons_os = extract_requested_seasons(item.get('seasons'))
                        if requested_seasons_os:
                            wanted_item['requested_seasons'] = requested_seasons_os

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

            if ignored_by_tag > 0:
                logging.info(f"Ignored {ignored_by_tag} items from Overseerr source based on tags.")
            all_wanted_items.append((wanted_items, versions))
            logging.info(f"Retrieved {len(wanted_items)} wanted items from Overseerr source")
        except OverseerrAuthError as e:
            logging.error(f"Overseerr auth failure for source '{display_name}': {e}")
            _notify_overseerr_auth_failure(source_id, display_name)
        except Exception as e:
            logging.error(f"Unexpected error while processing Overseerr source: {e}")

    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_overseerr_cache(cache)
    logging.info(f"Retrieved items from {len(all_wanted_items)} Overseerr sources.")
    return all_wanted_items

def get_overseerr_request_id(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, media_type: str) -> int | None:
    """
    Get Overseerr request ID for a media item by TMDB ID

    Args:
        overseerr_url: Overseerr base URL
        overseerr_api_key: API key
        tmdb_id: TMDB ID of the media
        media_type: 'movie' or 'episode'/'show'

    Returns:
        int: Request ID if found, None otherwise
    """
    try:
        details = get_overseerr_details(overseerr_url, overseerr_api_key, tmdb_id, media_type)

        if not details:
            logging.debug(f"No Overseerr details found for TMDB ID {tmdb_id}")
            return None

        # Navigate to mediaInfo.requests
        media_info = details.get('mediaInfo', {})
        requests = media_info.get('requests', [])

        if not requests:
            logging.debug(f"No requests found for TMDB ID {tmdb_id}")
            return None

        # Find approved/available request (Status 2 = APPROVED, Status 3 = AVAILABLE)
        for request in reversed(requests):
            status = request.get('status')
            if status in [2, 3]:
                request_id = request.get('id')
                logging.info(f"Found Overseerr request ID {request_id} for TMDB {tmdb_id} (status {status})")
                return request_id

        # Fallback: return most recent request ID
        if requests:
            request_id = requests[-1].get('id')
            logging.info(f"Found Overseerr request ID {request_id} for TMDB {tmdb_id} (fallback)")
            return request_id

        return None

    except Exception as e:
        logging.error(f"Error getting Overseerr request ID for TMDB {tmdb_id}: {e}")
        return None

def get_tmdb_from_imdb(imdb_id: str) -> tuple[int | None, str | None]:
    """
    Convert IMDB ID to TMDB ID using Overseerr's search API

    Args:
        imdb_id: IMDB ID (e.g., "tt0137523")

    Returns:
        tuple: (tmdb_id, media_type) or (None, None) if not found
    """
    try:
        overseerr_url = get_setting('Overseerr', 'overseerr_url')
        api_key = get_setting('Overseerr', 'overseerr_api_key')

        if not overseerr_url or not api_key:
            return None, None

        # Search by IMDB ID
        url = f"{overseerr_url}/api/v1/search?query={imdb_id}"
        headers = get_overseerr_headers(api_key)

        response = api.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 200:
            data = response.json()
            results = data.get('results', [])

            # Find exact IMDB ID match
            for result in results:
                if result.get('externalIds', {}).get('imdbId') == imdb_id:
                    tmdb_id = result.get('id')
                    media_type = result.get('mediaType')  # 'movie' or 'tv'
                    logging.info(f"Converted IMDB {imdb_id} to TMDB {tmdb_id} ({media_type})")
                    return tmdb_id, media_type

        logging.debug(f"Could not find TMDB ID for IMDB {imdb_id}")
        return None, None

    except Exception as e:
        logging.error(f"Error converting IMDB to TMDB: {e}")
        return None, None

def remove_from_overseerr_by_tmdb_id(tmdb_id: int, media_type: str, imdb_id: str = None, overseerr_url: str = None, api_key: str = None) -> dict:
    """
    Remove an Overseerr request by looking up TMDB ID (or converting from IMDB ID)

    Args:
        tmdb_id: TMDB ID of the media (can be None if imdb_id provided)
        media_type: 'movie' or 'episode'/'show'
        imdb_id: Optional IMDB ID to use if tmdb_id is not available
        overseerr_url: Optional Overseerr URL (will read from settings if not provided)
        api_key: Optional API key (will read from settings if not provided)

    Returns:
        dict: {'success': bool, 'message': str, 'request_id': int | None}
    """
    try:
        # Use provided URL/key or fall back to settings
        if not overseerr_url:
            overseerr_url = get_setting('Overseerr', 'overseerr_url')
        if not api_key:
            api_key = get_setting('Overseerr', 'overseerr_api_key')

        if not overseerr_url or not api_key:
            return {
                'success': False,
                'message': 'Overseerr not configured',
                'request_id': None
            }

        # If no TMDB ID but have IMDB ID, try to convert
        if not tmdb_id and imdb_id:
            logging.info(f"No TMDB ID available, attempting to convert IMDB {imdb_id}")
            tmdb_id, converted_media_type = get_tmdb_from_imdb(imdb_id)
            if tmdb_id:
                media_type = converted_media_type or media_type
                logging.info(f"Successfully converted IMDB {imdb_id} to TMDB {tmdb_id}")
            else:
                return {
                    'success': False,
                    'message': f'Could not find TMDB ID for IMDB {imdb_id}',
                    'request_id': None
                }

        if not tmdb_id:
            return {
                'success': False,
                'message': 'No TMDB ID or IMDB ID available',
                'request_id': None
            }

        # Step 1: Lookup request ID
        request_id = get_overseerr_request_id(overseerr_url, api_key, tmdb_id, media_type)

        if not request_id:
            return {
                'success': False,
                'message': f'No request found for TMDB ID {tmdb_id}',
                'request_id': None,
                'not_found': True  # Flag to indicate item wasn't in Overseerr
            }

        # Step 2: Delete the request
        url = f"{overseerr_url}/api/v1/request/{request_id}"
        headers = get_overseerr_headers(api_key)

        response = api.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 204:
            logging.info(f"Successfully deleted Overseerr request {request_id} for TMDB {tmdb_id}")
            return {
                'success': True,
                'message': f'Removed request {request_id}',
                'request_id': request_id
            }
        else:
            error_text = response.text if hasattr(response, 'text') else 'Unknown error'
            logging.error(f"Failed to delete Overseerr request {request_id}: HTTP {response.status_code} - {error_text}")
            return {
                'success': False,
                'message': f'HTTP {response.status_code}: {error_text}',
                'request_id': request_id
            }

    except Exception as e:
        logging.error(f"Error removing from Overseerr: {e}")
        return {
            'success': False,
            'message': str(e),
            'request_id': None
        }


def _source_allows_requester(allowed_requesters, requester_name):
    """Return True if the source should process requests from this requester.

    allowed_requesters: list of usernames, or ['__all__'] for all users.
    requester_name: the display name of the requester from the webhook.
    """
    if not allowed_requesters:
        return True
    if '__all__' in allowed_requesters:
        return True
    return requester_name in allowed_requesters
