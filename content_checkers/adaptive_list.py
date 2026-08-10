"""
Adaptive List Content Checker
Fetches content from TMDB discover API using saved filter configurations.
Adaptive lists are time-sensitive - results change based on when they're checked
due to filters like "released_within" and "upcoming_days".
"""

import logging
import pickle
import os
import re
import requests
from typing import List, Dict, Any, Tuple
from datetime import datetime, timedelta
from utilities.settings import get_setting
from utilities.flixpatrol_api import fetch_top10 as fetch_flixpatrol_top10
from utilities.mdblist_api import fetch_list_items as fetch_mdblist_items

REQUEST_TIMEOUT = 15  # seconds

# Cache configuration
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
ADAPTIVE_LIST_TMDB_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'adaptive_list_tmdb_cache.pkl')
ADAPTIVE_LIST_IMDB_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'adaptive_list_imdb_cache.pkl')

# Cache control flag
disable_caching = False

logger = logging.getLogger(__name__)

# TMDB Metadata Cache Functions
def load_tmdb_cache():
    """Load TMDB metadata cache"""
    if disable_caching or not os.path.exists(ADAPTIVE_LIST_TMDB_CACHE_FILE):
        return {}
    try:
        with open(ADAPTIVE_LIST_TMDB_CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
            logger.debug(f"Loaded TMDB cache with {len(cache)} entries")
            return cache
    except Exception as e:
        logger.warning(f"Failed to load TMDB cache: {e}")
        return {}

def save_tmdb_cache(cache):
    """Save TMDB metadata cache"""
    if disable_caching:
        return
    try:
        with open(ADAPTIVE_LIST_TMDB_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
            logger.debug(f"Saved TMDB cache with {len(cache)} entries")
    except Exception as e:
        logger.error(f"Failed to save TMDB cache: {e}")

# TMDB→IMDB ID Cache Functions
def load_imdb_cache():
    """Load TMDB→IMDB ID mapping cache with smart expiration"""
    if disable_caching or not os.path.exists(ADAPTIVE_LIST_IMDB_CACHE_FILE):
        return {}
    try:
        with open(ADAPTIVE_LIST_IMDB_CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
            logger.debug(f"Loaded IMDB cache with {len(cache)} entries")
            return cache
    except Exception as e:
        logger.warning(f"Failed to load IMDB cache: {e}")
        return {}

def save_imdb_cache(cache):
    """Save TMDB→IMDB ID mapping cache"""
    if disable_caching:
        return
    try:
        with open(ADAPTIVE_LIST_IMDB_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
            logger.debug(f"Saved IMDB cache with {len(cache)} entries")
    except Exception as e:
        logger.error(f"Failed to save IMDB cache: {e}")

def is_cache_entry_valid(cache_entry, cache_key, current_time=None):
    """
    Determine if a cache entry is still valid based on smart expiration rules.
    
    Args:
        cache_entry: Dict with data and 'cached_at', 'release_date' (optional)
        cache_key: Cache key for logging
        current_time: Current datetime (defaults to now)
    
    Returns:
        bool: True if cache is valid, False if expired
    """
    if not cache_entry or 'cached_at' not in cache_entry:
        return False
    
    try:
        if current_time is None:
            current_time = datetime.now()
        
        cached_date = cache_entry['cached_at']
        if isinstance(cached_date, str):
            cached_date = datetime.fromisoformat(cached_date)
        
        age_days = (current_time - cached_date).days
        
        # Check release date age for smart TTL
        release_date = cache_entry.get('release_date')
        if release_date and release_date != 'Unknown':
            try:
                release_year = int(str(release_date)[:4])
                current_year = current_time.year
                years_since_release = current_year - release_year
                
                # Unreleased content: Cache 1 day
                if years_since_release < 0:
                    if age_days > 1:
                        logger.debug(f"Cache expired for {cache_key}: Unreleased content older than 1 day")
                        return False
                    return True
                
                # Recent content (<2 years): Cache 7 days
                if years_since_release < 2:
                    if age_days > 7:
                        logger.debug(f"Cache expired for {cache_key}: Recent content cache older than 7 days")
                        return False
                    return True
                
                # Old content (≥2 years): Cache 90 days
                if age_days > 90:
                    logger.debug(f"Cache expired for {cache_key}: Old content cache older than 90 days")
                    return False
                return True
                
            except (ValueError, TypeError):
                # Can't parse release date, default to 30 day expiry
                if age_days > 30:
                    logger.debug(f"Cache expired for {cache_key}: Unknown age, default 30 day expiry")
                    return False
        else:
            # No release date info, default to 30 day expiry
            if age_days > 30:
                logger.debug(f"Cache expired for {cache_key}: No release date, default 30 day expiry")
                return False
        
        return True
        
    except Exception as e:
        logger.warning(f"Error validating cache entry for {cache_key}: {e}")
        return False


def get_wanted_from_adaptive_list(list_configs: Dict[str, Any], versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """
    Fetch wanted items from TMDB discover API using saved filter configurations.

    Args:
        list_configs: List of adaptive list configurations, each containing:
            - name: Display name for the list
            - media_type: 'movie' or 'tv'
            - filters: Dict of TMDB discover filter parameters
        versions: Dict of version flags to assign to items

    Returns:
        List of tuples: [(items_list, versions_dict), ...]
    """
    all_wanted_items = []

    # Get TMDB API key
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    if not tmdb_api_key:
        logging.error("[Adaptive List] TMDB API key not configured")
        return []

    
    list_name = list_configs.get('display_name', 'Unnamed List')
    media_type = list_configs.get('media_type', 'movie')
    filters = list_configs.get('filters', {})

    if not filters:
        logging.warning(f"[Adaptive List] No filters configured for list '{list_name}', skipping")
        return []

    logging.info(f"[Adaptive List] Processing list '{list_name}' (type: {media_type})")
    logging.info(f"[Adaptive List] Filters received: {filters}")

    try:
        has_lists = 'lists' in filters and filters['lists']
        if has_lists and filters.get('merge_with_adaptive'):
            list_items = fetch_from_lists(tmdb_api_key, filters, list_name, source_media_type=media_type)
            discover_items = fetch_from_tmdb_discover(tmdb_api_key, media_type, filters, list_name)
            seen_tmdb_ids = set()
            items = []
            for item in list_items + discover_items:
                key = item['tmdb_id']
                if key in seen_tmdb_ids:
                    continue
                seen_tmdb_ids.add(key)
                items.append(item)
            logging.info(f"[Adaptive List] Merge mode: {len(list_items)} list item(s) + {len(discover_items)} discover item(s) -> {len(items)} after dedup")
        elif has_lists:
            items = fetch_from_lists(tmdb_api_key, filters, list_name, source_media_type=media_type)
        else:
            items = fetch_from_tmdb_discover(tmdb_api_key, media_type, filters, list_name)

        if items:
            logging.info(f"[Adaptive List] Found {len(items)} items from list '{list_name}'")
            all_wanted_items.append((items, versions))
        else:
            logging.info(f"[Adaptive List] No items found from list '{list_name}'")

    except Exception as e:
        logging.error(f"[Adaptive List] Error processing list '{list_name}': {e}")
        return []

    return all_wanted_items


def fetch_from_tmdb_discover(api_key: str, media_type: str, filters: Dict, list_name: str) -> List[Dict[str, Any]]:
    """
    Fetch items from TMDB discover API with given filters.
    Fetches multiple pages to get comprehensive results.

    Args:
        api_key: TMDB API key
        media_type: 'movie' or 'tv'
        filters: Dict of filter parameters
        list_name: Name of the list (for logging)

    Returns:
        List of wanted item dicts with imdb_id and media_type
    """
    items = []

    # Build base URL
    if media_type == 'tv':
        base_url = f"https://api.themoviedb.org/3/discover/tv?api_key={api_key}&language=en-US"
        date_field = 'first_air_date'
    else:
        base_url = f"https://api.themoviedb.org/3/discover/movie?api_key={api_key}&language=en-US"
        date_field = 'primary_release_date'

    # Build parameters from filters
    params = build_discover_params(filters, date_field, media_type)

    # Fetch up to 30 pages (600 items max) to balance coverage with API limits
    max_pages = 30
    total_fetched = 0

    for page in range(1, max_pages + 1):
        url = f"{base_url}&page={page}&{'&'.join(params)}"

        logging.info(f"[Adaptive List] Fetching page {page} for '{list_name}'")

        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            results = data.get('results', [])
            total_pages = data.get('total_pages', 1)
            total_results = data.get('total_results', 0)
            logging.info(f"[Adaptive List] TMDB response: {len(results)} results on page {page}, total_results={total_results}, total_pages={total_pages}")

            if not results:
                break

            # Apply title filter BEFORE IMDB lookup (more efficient - reduces API calls)
            title_filter_str = filters.get('title_filter', '')
            if title_filter_str:
                try:
                    # Parse JavaScript-style regex: /pattern/flags
                    js_regex_match = re.match(r'^/(.+)/([gimsuvy]*)$', title_filter_str)
                    if js_regex_match:
                        pattern_str = js_regex_match.group(1)
                        flags_str = js_regex_match.group(2)
                        # Convert JS flags to Python flags (i = IGNORECASE, m = MULTILINE, s = DOTALL)
                        py_flags = 0
                        if 'i' in flags_str:
                            py_flags |= re.IGNORECASE
                        if 'm' in flags_str:
                            py_flags |= re.MULTILINE
                        if 's' in flags_str:
                            py_flags |= re.DOTALL
                        title_pattern = re.compile(pattern_str, py_flags)
                        logging.info(f"[Adaptive List] Parsed JS regex '{title_filter_str}' -> pattern='{pattern_str}', flags={py_flags}")
                    else:
                        # Plain regex or text pattern
                        title_pattern = re.compile(title_filter_str, re.IGNORECASE)
                except re.error as e:
                    logging.warning(f"[Adaptive List] Invalid regex '{title_filter_str}': {e}, treating as plain text")
                    title_pattern = re.compile(re.escape(title_filter_str), re.IGNORECASE)

                original_count = len(results)
                results = [r for r in results if title_pattern.search(r.get('title', '') or r.get('name', ''))]
                logging.info(f"[Adaptive List] Title filter '{title_filter_str}' reduced page results from {original_count} to {len(results)}")

            # Process each result to get IMDB ID
            for item in results:
                tmdb_id = item.get('id')
                if not tmdb_id:
                    continue

                # Get release date for smart caching
                release_date = item.get('release_date') or item.get('first_air_date')

                # Fetch external IDs to get IMDB ID
                item_title = item.get('title') or item.get('name', '')
                imdb_id = get_imdb_id(api_key, tmdb_id, media_type, release_date)

                # Add item with IMDB ID if available, otherwise use TMDB ID fallback
                wanted_item = {
                    'imdb_id': imdb_id,  # Can be None - will use TMDB ID fallback
                    'tmdb_id': tmdb_id,
                    'media_type': 'movie' if media_type == 'movie' else 'tv',
                    'title': item_title,
                }
                items.append(wanted_item)
                total_fetched += 1

                if not imdb_id:
                    logging.info(f"[Adaptive List] No IMDB ID for '{item_title}' (TMDB ID: {tmdb_id}) - using TMDB ID fallback")

            # Stop if we've reached the last page
            if page >= total_pages:
                break

        except requests.exceptions.RequestException as e:
            logging.error(f"[Adaptive List] TMDB API error on page {page}: {e}")
            break
        except Exception as e:
            logging.error(f"[Adaptive List] Error processing page {page}: {e}")
            break

    # Note: Title filter is now applied BEFORE IMDB lookup (inside the page loop above)
    # This is more efficient as it reduces the number of IMDB API calls needed

    logging.info(f"[Adaptive List] Fetched {len(items)} items from '{list_name}' (IMDB IDs + TMDB ID fallbacks)")
    return items


def fetch_from_lists(api_key: str, filters: Dict, list_name: str, source_media_type: str = 'all') -> List[Dict[str, Any]]:
    """
    Fetch items from FlixPatrol/MDBList sources and apply filters.

    Args:
        api_key: TMDB API key
        filters: Dict of filter parameters including 'lists' parameter
        list_name: Name of the adaptive list (for logging)
        source_media_type: 'movie', 'tv', or 'all' — restricts which media types are fetched

    Returns:
        List of wanted item dicts with imdb_id and media_type
    """
    items = []
    all_list_items = []

    # Parse lists parameter: "flixpatrol:netflix,flixpatrol:disney,mdblist:top-imdb"
    lists_param = filters.get('lists', '')
    if not lists_param:
        return []
    
    list_sources = [pair.strip() for pair in lists_param.split(',') if pair.strip()]
    
    logging.info(f"[Adaptive List] Fetching from {len(list_sources)} list source(s) for '{list_name}'")
    
    # Fetch all lists and enrich with TMDB data
    for list_pair in list_sources:
        try:
            parts = list_pair.split(':', 1)
            if len(parts) != 2:
                continue
            
            source, list_id = parts
            
            if source == 'flixpatrol':
                # FlixPatrol returns {'items': [...], 'platform': '...', 'date': '...'}
                result = fetch_flixpatrol_top10(list_id)
                if result and 'items' in result:
                    # Enrich items with TMDB data
                    enriched = enrich_list_items_with_tmdb(api_key, result['items'])
                    all_list_items.extend(enriched)
                    logging.debug(f"[Adaptive List] Fetched {len(enriched)} enriched items from FlixPatrol:{list_id}")
            elif source == 'mdblist':
                # MDBList returns {'items': [...], 'list_name': '...'}
                result = fetch_mdblist_items(list_id, limit=50)
                if result and 'items' in result:
                    # MDBList items already have tmdb_id, just need to enrich with full details
                    enriched = enrich_mdblist_items_with_tmdb(api_key, result['items'])
                    all_list_items.extend(enriched)
                    logging.debug(f"[Adaptive List] Fetched {len(enriched)} enriched items from MDBList:{list_id}")
            elif source == 'trakt-special':
                enriched = fetch_trakt_special_items(api_key, list_id, source_media_type=source_media_type)
                all_list_items.extend(enriched)
                logging.debug(f"[Adaptive List] Fetched {len(enriched)} items from Trakt special:{list_id}")
            elif source == 'trakt-mylist':
                enriched = fetch_trakt_mylist_items(list_id)
                all_list_items.extend(enriched)
                logging.debug(f"[Adaptive List] Fetched {len(enriched)} items from Trakt mylist:{list_id}")
            elif source == 'tmdb_shows':
                enriched = fetch_tmdb_shows_items(api_key, list_id)
                all_list_items.extend(enriched)
                logging.debug(f"[Adaptive List] Fetched {len(enriched)} items from TMDB shows:{list_id}")
            elif source == 'tmdb_movies':
                enriched = fetch_tmdb_movies_items(api_key, list_id)
                all_list_items.extend(enriched)
                logging.debug(f"[Adaptive List] Fetched {len(enriched)} items from TMDB movies:{list_id}")
            elif source == 'mdblist-personal':
                enriched = fetch_mdblist_personal_items(api_key, list_id)
                all_list_items.extend(enriched)
                logging.debug(f"[Adaptive List] Fetched {len(enriched)} items from MDBList personal:{list_id}")
            elif source == 'scrob-special':
                enriched = fetch_scrob_special_items(api_key, list_id, source_media_type=source_media_type)
                all_list_items.extend(enriched)
                logging.debug(f"[Adaptive List] Fetched {len(enriched)} items from Scrob special:{list_id}")
            elif source == 'scrob-mylist':
                enriched = fetch_scrob_mylist_items(api_key, list_id)
                all_list_items.extend(enriched)
                logging.debug(f"[Adaptive List] Fetched {len(enriched)} items from Scrob mylist:{list_id}")
        except Exception as e:
            logging.error(f"[Adaptive List] Error fetching list {list_pair}: {e}")
            continue
    
    logging.info(f"[Adaptive List] Got {len(all_list_items)} total items from lists before filtering")
    
    # Apply client-side filters
    filtered_items = apply_list_filters(all_list_items, filters, source_media_type=source_media_type)
    
    logging.info(f"[Adaptive List] {len(filtered_items)} items after filtering")
    
    # Convert to wanted items format with IMDB IDs
    for item in filtered_items:
        tmdb_id = item.get('id')
        media_type = item.get('media_type', 'movie')
        
        if not tmdb_id:
            continue
        
        # Get release date for smart caching
        release_date = item.get('release_date') or item.get('first_air_date')
        
        # Fetch IMDB ID
        imdb_id = get_imdb_id(api_key, tmdb_id, media_type, release_date)
        
        # Add item with IMDB ID if available, otherwise use TMDB ID fallback
        item_title = item.get('title') or item.get('name', '')
        wanted_item = {
            'imdb_id': imdb_id,  # Can be None - will use TMDB ID fallback
            'tmdb_id': tmdb_id,
            'media_type': 'movie' if media_type == 'movie' else 'tv',
            'title': item_title,
        }
        items.append(wanted_item)

        if not imdb_id:
            logging.info(f"[Adaptive List] No IMDB ID for '{item_title}' (TMDB ID: {tmdb_id}) - using TMDB ID fallback")
    
    logging.info(f"[Adaptive List] Fetched {len(items)} items after filtering (IMDB IDs + TMDB ID fallbacks)")
    return items


def enrich_list_items_with_tmdb(api_key: str, items: List[Dict]) -> List[Dict]:
    """
    Enrich FlixPatrol items with TMDB data by searching for titles.
    
    Args:
        api_key: TMDB API key
        items: Raw FlixPatrol items with title and media_type
    
    Returns:
        List of enriched items with TMDB data
    """
    enriched = []
    
    for item in items:
        title = item.get('title', '')
        media_type = item.get('media_type', 'unknown')
        
        if not title:
            continue
        
        try:
            # Search TMDB by title
            search_types = ['movie', 'tv'] if media_type == 'unknown' else [media_type if media_type == 'tv' else 'movie']
            
            tmdb_id = None
            for search_type in search_types:
                search_url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={api_key}&query={requests.utils.quote(title)}&page=1"
                search_response = requests.get(search_url, timeout=5)
                if search_response.status_code == 200:
                    search_data = search_response.json()
                    if search_data.get('results'):
                        tmdb_id = search_data['results'][0]['id']
                        media_type = search_type
                        break
            
            if not tmdb_id:
                continue
            
            # Fetch full TMDB details
            if media_type == 'tv':
                tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&language=en-US"
            else:
                tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}&language=en-US"
            
            tmdb_response = requests.get(tmdb_url, timeout=5)
            if tmdb_response.status_code == 200:
                tmdb_data = tmdb_response.json()
                
                # Extract genre IDs and other data
                genres = tmdb_data.get('genres', [])
                genre_ids = [g['id'] for g in genres if isinstance(g, dict) and 'id' in g]
                
                enriched.append({
                    'id': tmdb_id,
                    'title': tmdb_data.get('title') or tmdb_data.get('name'),
                    'media_type': media_type,
                    'genre_ids': genre_ids,
                    'original_language': tmdb_data.get('original_language', ''),
                    'origin_country': tmdb_data.get('origin_country', []),
                    'vote_average': tmdb_data.get('vote_average', 0),
                    'vote_count': tmdb_data.get('vote_count', 0),
                    'release_date': tmdb_data.get('release_date', ''),
                    'first_air_date': tmdb_data.get('first_air_date', ''),
                    'runtime': tmdb_data.get('runtime', 0) if media_type == 'movie' else tmdb_data.get('episode_run_time', [0])[0] if tmdb_data.get('episode_run_time') else 0,
                })
        except Exception as e:
            logging.debug(f"[Adaptive List] Error enriching {title}: {e}")
            continue
    
    return enriched


def enrich_mdblist_items_with_tmdb(api_key: str, items: List[Dict]) -> List[Dict]:
    """
    Enrich MDBList items with TMDB data.
    
    Args:
        api_key: TMDB API key
        items: MDBList items with tmdb_id
    
    Returns:
        List of enriched items with full TMDB data
    """
    enriched = []
    
    for item in items:
        tmdb_id = item.get('tmdb_id')
        media_type = item.get('media_type', 'movie')
        
        if not tmdb_id:
            continue
        
        try:
            # Fetch full TMDB details
            if media_type == 'tv':
                tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&language=en-US"
            else:
                tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}&language=en-US"
            
            tmdb_response = requests.get(tmdb_url, timeout=5)
            if tmdb_response.status_code == 200:
                tmdb_data = tmdb_response.json()
                
                # Extract genre IDs and other data
                genres = tmdb_data.get('genres', [])
                genre_ids = [g['id'] for g in genres if isinstance(g, dict) and 'id' in g]
                
                enriched.append({
                    'id': tmdb_id,
                    'title': tmdb_data.get('title') or tmdb_data.get('name'),
                    'media_type': media_type,
                    'genre_ids': genre_ids,
                    'original_language': tmdb_data.get('original_language', ''),
                    'origin_country': tmdb_data.get('origin_country', []),
                    'vote_average': tmdb_data.get('vote_average', 0),
                    'vote_count': tmdb_data.get('vote_count', 0),
                    'release_date': tmdb_data.get('release_date', ''),
                    'first_air_date': tmdb_data.get('first_air_date', ''),
                    'runtime': tmdb_data.get('runtime', 0) if media_type == 'movie' else tmdb_data.get('episode_run_time', [0])[0] if tmdb_data.get('episode_run_time') else 0,
                })
        except Exception as e:
            logging.debug(f"[Adaptive List] Error enriching TMDB ID {tmdb_id}: {e}")
            continue
    
    return enriched



def fetch_trakt_special_items(tmdb_api_key, list_type, source_media_type='all'):
    """Fetch Trakt special list using extended=full, no per-item TMDB calls needed."""
    from content_checkers.trakt import get_trakt_config
    SPECIAL_ENDPOINTS = {
        'trending':    {'movie': '/movies/trending',         'show': '/shows/trending'},
        'popular':     {'movie': '/movies/popular',          'show': '/shows/popular'},
        'favorited':   {'movie': '/movies/favorited/weekly', 'show': '/shows/favorited/weekly'},
        'played':      {'movie': '/movies/played/weekly',    'show': '/shows/played/weekly'},
        'watched':     {'movie': '/movies/watched/weekly',   'show': '/shows/watched/weekly'},
        'collected':   {'movie': '/movies/collected/weekly', 'show': '/shows/collected/weekly'},
        'anticipated': {'movie': '/movies/anticipated',      'show': '/shows/anticipated'},
        'boxoffice':   {'movie': '/movies/boxoffice',        'show': None},
        'recommendations': {'movie': '/recommendations/movies', 'show': '/recommendations/shows'},
    }
    endpoints = SPECIAL_ENDPOINTS.get(list_type)
    if not endpoints:
        logging.warning(f"[Adaptive List] Unknown Trakt special list type: {list_type}")
        return []
    try:
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '')
        if not access_token or not client_id:
            logging.warning("[Adaptive List] Trakt not authenticated")
            return []
    except Exception as e:
        logging.warning(f"[Adaptive List] Could not get Trakt config: {e}")
        return []
    headers = {'Content-Type': 'application/json', 'trakt-api-version': '2',
               'trakt-api-key': client_id, 'Authorization': f'Bearer {access_token}'}
    GENRE_MAP = {
        'action': 28, 'adventure': 12, 'animation': 16, 'comedy': 35, 'crime': 80,
        'documentary': 99, 'drama': 18, 'family': 10751, 'fantasy': 14, 'history': 36,
        'horror': 27, 'music': 10402, 'mystery': 9648, 'romance': 10749,
        'science-fiction': 878, 'thriller': 53, 'war': 10752, 'western': 37,
        'action & adventure': 10759, 'kids': 10762, 'news': 10763,
        'reality': 10764, 'soap': 10766, 'talk': 10767, 'war & politics': 10768,
    }
    enriched = []
    for mt, ep in [('movie', endpoints.get('movie')), ('tv', endpoints.get('show'))]:
        if not ep:
            continue
        # Skip endpoint if source is restricted to one media type
        if source_media_type in ('movie',) and mt != 'movie':
            continue
        if source_media_type in ('tv',) and mt != 'tv':
            continue
        try:
            r = requests.get(f'https://api.trakt.tv{ep}?limit=40&extended=full',
                             headers=headers, timeout=15)
            r.raise_for_status()
            for item in r.json():
                media_obj = item.get('movie') or item.get('show') or item
                ids = media_obj.get('ids', {})
                tmdb_id = ids.get('tmdb')
                if not tmdb_id:
                    continue
                trakt_genres = media_obj.get('genres', []) or []
                genre_ids = [GENRE_MAP[g] for g in trakt_genres if g in GENRE_MAP]
                runtime = media_obj.get('runtime', 0) or 0
                year = media_obj.get('year')
                released = media_obj.get('released') or media_obj.get('first_aired', '')
                released = released[:10] if released and len(released) >= 10 else (f'{year}-01-01' if year else '')
                enriched.append({
                    'id': tmdb_id, 'title': media_obj.get('title', ''), 'media_type': mt,
                    'genre_ids': genre_ids, 'original_language': media_obj.get('language', ''),
                    'origin_country': [], 'vote_average': media_obj.get('rating', 0) or 0,
                    'vote_count': media_obj.get('votes', 0) or 0,
                    'release_date': released if mt == 'movie' else '',
                    'first_air_date': released if mt == 'tv' else '',
                    'runtime': runtime,
                })
        except Exception as e:
            logging.warning(f"[Adaptive List] Trakt special {list_type}/{mt} error: {e}")
    logging.info(f"[Adaptive List] Fetched {len(enriched)} items from Trakt special:{list_type}")
    return enriched


def fetch_trakt_mylist_items(slug: str) -> list:
    """Fetch items from a user's own Trakt list by slug using extended=full."""
    from content_checkers.trakt import get_trakt_config
    try:
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '')
        if not access_token or not client_id:
            logging.warning("[Adaptive List] Trakt not authenticated for mylist fetch")
            return []
    except Exception as e:
        logging.warning(f"[Adaptive List] Could not get Trakt config: {e}")
        return []

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': client_id,
        'Authorization': f'Bearer {access_token}',
    }
    GENRE_MAP = {
        'action': 28, 'adventure': 12, 'animation': 16, 'comedy': 35, 'crime': 80,
        'documentary': 99, 'drama': 18, 'family': 10751, 'fantasy': 14, 'history': 36,
        'horror': 27, 'music': 10402, 'mystery': 9648, 'romance': 10749,
        'science-fiction': 878, 'thriller': 53, 'war': 10752, 'western': 37,
        'action & adventure': 10759, 'kids': 10762, 'news': 10763,
        'reality': 10764, 'soap': 10766, 'talk': 10767, 'war & politics': 10768,
    }
    enriched = []
    page = 1
    while True:
        try:
            r = requests.get(
                f'https://api.trakt.tv/users/me/lists/{slug}/items?extended=full&limit=100&page={page}',
                headers=headers, timeout=15
            )
            r.raise_for_status()
            page_items = r.json()
            if not page_items:
                break
            for item in page_items:
                raw_type = item.get('type', '')
                if raw_type == 'episode':
                    raw_type = 'show'
                mt = 'tv' if raw_type == 'show' else 'movie'
                media_obj = item.get('movie') or item.get('show') or {}
                ids = media_obj.get('ids', {})
                tmdb_id = ids.get('tmdb')
                if not tmdb_id:
                    continue
                trakt_genres = media_obj.get('genres', []) or []
                genre_ids = [GENRE_MAP[g] for g in trakt_genres if g in GENRE_MAP]
                runtime = media_obj.get('runtime', 0) or 0
                year = media_obj.get('year')
                released = media_obj.get('released') or media_obj.get('first_aired', '')
                released = released[:10] if released and len(released) >= 10 else (f'{year}-01-01' if year else '')
                enriched.append({
                    'id': tmdb_id,
                    'title': media_obj.get('title', ''),
                    'media_type': mt,
                    'genre_ids': genre_ids,
                    'original_language': media_obj.get('language', ''),
                    'origin_country': [],
                    'vote_average': media_obj.get('rating', 0) or 0,
                    'vote_count': media_obj.get('votes', 0) or 0,
                    'release_date': released if mt == 'movie' else '',
                    'first_air_date': released if mt == 'tv' else '',
                    'runtime': runtime,
                })
            total_pages = int(r.headers.get('X-Pagination-Page-Count', 1))
            if page >= total_pages:
                break
            page += 1
        except Exception as e:
            logging.warning(f"[Adaptive List] Trakt mylist {slug} page {page} error: {e}")
            break
    logging.info(f"[Adaptive List] Fetched {len(enriched)} items from Trakt mylist:{slug}")
    return enriched


def _scrob_media_to_enriched_item(api_key: str, media: Dict) -> Dict[str, Any]:
    """Convert one raw Scrob media dict into this module's enriched-item shape.

    Mirrors routes.discover_routes._scrob_items_to_discover's per-item ID
    resolution (episode -> parent show's tmdb_id), but fetches full TMDB
    details itself since adaptive_list's fetch_* functions are self-contained
    (no shared per-request cache like the Discover route layer has).
    Returns {} if the item has no usable tmdb_id or the TMDB lookup fails.
    """
    raw_type = (media.get('type') or '').lower()
    if raw_type == 'movie':
        mt = 'movie'
        tmdb_id = media.get('tmdb_id')
    elif raw_type == 'episode':
        mt = 'tv'
        tmdb_id = media.get('show_tmdb_id')
    elif raw_type in ('series', 'show', 'tv'):
        mt = 'tv'
        tmdb_id = media.get('tmdb_id')
    else:
        return {}

    if not tmdb_id:
        return {}

    endpoint = 'tv' if mt == 'tv' else 'movie'
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={api_key}&language=en-US',
            timeout=REQUEST_TIMEOUT
        )
        if not r.ok:
            return {}
        d = r.json()
    except Exception:
        return {}

    return {
        'id': tmdb_id,
        'title': d.get('title') or d.get('name', ''),
        'media_type': mt,
        'genre_ids': [g['id'] for g in d.get('genres', [])],
        'original_language': d.get('original_language', ''),
        'origin_country': [],
        'vote_average': d.get('vote_average', 0),
        'vote_count': d.get('vote_count', 0),
        'release_date': d.get('release_date', '') if mt == 'movie' else '',
        'first_air_date': d.get('first_air_date', '') if mt == 'tv' else '',
        'runtime': d.get('runtime', 0) or 0,
    }


def fetch_scrob_special_items(api_key: str, list_type: str, source_media_type: str = 'all') -> List[Dict]:
    """Fetch a Scrob special list (Trending, Popular, etc.)."""
    from content_checkers.scrob import get_scrob_config, _scrob_get, SPECIAL_LIST_ENDPOINTS

    if list_type not in SPECIAL_LIST_ENDPOINTS:
        logging.warning(f"[Adaptive List] Unknown Scrob special list type: {list_type}")
        return []
    if not get_scrob_config():
        logging.warning("[Adaptive List] Scrob not configured")
        return []

    api_paths = SPECIAL_LIST_ENDPOINTS[list_type]
    endpoints_to_call = []
    if source_media_type in ('all', 'movie') and api_paths.get('movies'):
        endpoints_to_call.append(api_paths['movies'])
    if source_media_type in ('all', 'tv') and api_paths.get('shows'):
        endpoints_to_call.append(api_paths['shows'])

    raw_items = []
    for endpoint_path, endpoint_params in endpoints_to_call:
        data = _scrob_get(endpoint_path, params=endpoint_params)
        if data:
            raw_items.extend(data.get('results', []))

    enriched = []
    for item in raw_items:
        media = item.get('media', item)
        result = _scrob_media_to_enriched_item(api_key, media)
        if result:
            enriched.append(result)
    logging.info(f"[Adaptive List] Fetched {len(enriched)} items from Scrob special:{list_type}")
    return enriched


def fetch_scrob_mylist_items(api_key: str, list_id: str) -> List[Dict]:
    """Fetch items from a Scrob custom list by ID."""
    from content_checkers.scrob import get_scrob_config, _scrob_get

    if not get_scrob_config():
        logging.warning("[Adaptive List] Scrob not configured")
        return []

    data = _scrob_get(f'/lists/{list_id}')
    if data is None:
        logging.warning(f"[Adaptive List] Failed to fetch Scrob list {list_id}")
        return []

    enriched = []
    for item in data.get('items', []):
        media = item.get('media', item)
        result = _scrob_media_to_enriched_item(api_key, media)
        if result:
            enriched.append(result)
    logging.info(f"[Adaptive List] Fetched {len(enriched)} items from Scrob mylist:{list_id}")
    return enriched


def fetch_tmdb_shows_items(api_key: str, list_id: str) -> List[Dict]:
    """
    Fetch TMDB show lists (popular, top_rated, airing_today, trending).
    list_id format: 'tmdb_shows_popular', 'tmdb_shows_top_rated', etc.
    """
    VALID_TYPES = {
        'popular':      'tv/popular',
        'top_rated':    'tv/top_rated',
        'airing_today': 'tv/airing_today',
        'trending':     'trending/tv/week',
    }
    # Strip 'tmdb_shows_' prefix if present, otherwise use list_id directly
    list_type = list_id.replace('tmdb_shows_', '', 1) if list_id.startswith('tmdb_shows_') else list_id
    endpoint = VALID_TYPES.get(list_type)
    if not endpoint:
        logging.warning(f"[Adaptive List] Unknown TMDB shows list type: {list_id}")
        return []

    enriched = []
    try:
        for page in range(1, 3):  # 2 pages = up to 40 results
            url = f"https://api.themoviedb.org/3/{endpoint}?api_key={api_key}&language=en-US&page={page}"
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            if not r.ok:
                break
            for item in r.json().get('results', []):
                enriched.append({
                    'id': item['id'],
                    'title': item.get('name') or item.get('title', ''),
                    'media_type': 'tv',
                    'genre_ids': item.get('genre_ids', []),
                    'original_language': item.get('original_language', ''),
                    'origin_country': item.get('origin_country', []),
                    'vote_average': item.get('vote_average', 0),
                    'vote_count': item.get('vote_count', 0),
                    'release_date': '',
                    'first_air_date': item.get('first_air_date', ''),
                    'runtime': 0,
                })
    except Exception as e:
        logging.warning(f"[Adaptive List] TMDB shows {list_id} error: {e}")
    logging.info(f"[Adaptive List] Fetched {len(enriched)} items from TMDB shows:{list_id}")
    return enriched


def fetch_tmdb_movies_items(api_key: str, list_id: str) -> List[Dict]:
    """
    Fetch TMDB movie lists (popular, top_rated, now_playing, upcoming).
    list_id format: 'tmdb_movies_popular', 'tmdb_movies_top_rated', etc.
    """
    VALID_TYPES = {
        'popular':     'movie/popular',
        'top_rated':   'movie/top_rated',
        'now_playing': 'movie/now_playing',
        'upcoming':    'movie/upcoming',
        'trending':    'trending/movie/week',
    }
    list_type = list_id.replace('tmdb_movies_', '', 1) if list_id.startswith('tmdb_movies_') else list_id
    endpoint = VALID_TYPES.get(list_type)
    if not endpoint:
        logging.warning(f"[Adaptive List] Unknown TMDB movies list type: {list_id}")
        return []

    enriched = []
    try:
        for page in range(1, 3):  # 2 pages = up to 40 results
            url = f"https://api.themoviedb.org/3/{endpoint}?api_key={api_key}&language=en-US&page={page}"
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            if not r.ok:
                break
            for item in r.json().get('results', []):
                enriched.append({
                    'id': item['id'],
                    'title': item.get('title') or item.get('name', ''),
                    'media_type': 'movie',
                    'genre_ids': item.get('genre_ids', []),
                    'original_language': item.get('original_language', ''),
                    'origin_country': item.get('origin_country', []),
                    'vote_average': item.get('vote_average', 0),
                    'vote_count': item.get('vote_count', 0),
                    'release_date': item.get('release_date', ''),
                    'first_air_date': '',
                    'runtime': 0,
                })
    except Exception as e:
        logging.warning(f"[Adaptive List] TMDB movies {list_id} error: {e}")
    logging.info(f"[Adaptive List] Fetched {len(enriched)} items from TMDB movies:{list_id}")
    return enriched


def fetch_mdblist_personal_items(api_key: str, list_id: str) -> List[Dict]:
    """
    Fetch items from a user's personal MDBList list by numeric ID.
    Enriches with full TMDB data for filtering.
    """
    from utilities.mdblist_api import fetch_custom_list_items
    enriched = []
    try:
        result = fetch_custom_list_items(list_id, limit=100)
        if not result.get('items'):
            logging.warning(f"[Adaptive List] MDBList personal list {list_id} returned no items: {result.get('error', '')}")
            return []
        for item in result['items']:
            tmdb_id = item.get('tmdb_id')
            if not tmdb_id:
                continue
            media_type = item.get('media_type', 'movie')
            try:
                endpoint = 'tv' if media_type == 'tv' else 'movie'
                r = requests.get(
                    f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={api_key}&language=en-US",
                    timeout=8
                )
                if r.ok:
                    d = r.json()
                    genres = d.get('genres', [])
                    genre_ids = [g['id'] for g in genres if isinstance(g, dict)]
                    enriched.append({
                        'id': tmdb_id,
                        'title': d.get('title') or d.get('name', ''),
                        'media_type': media_type,
                        'genre_ids': genre_ids,
                        'original_language': d.get('original_language', ''),
                        'origin_country': d.get('origin_country', []),
                        'vote_average': d.get('vote_average', 0),
                        'vote_count': d.get('vote_count', 0),
                        'release_date': d.get('release_date', ''),
                        'first_air_date': d.get('first_air_date', ''),
                        'runtime': d.get('runtime', 0) if media_type == 'movie' else (d.get('episode_run_time') or [0])[0],
                    })
                else:
                    enriched.append({
                        'id': tmdb_id,
                        'title': item.get('title', ''),
                        'media_type': media_type,
                        'genre_ids': [],
                        'original_language': '',
                        'origin_country': [],
                        'vote_average': 0,
                        'vote_count': 0,
                        'release_date': '',
                        'first_air_date': '',
                        'runtime': 0,
                    })
            except Exception as e:
                logging.debug(f"[Adaptive List] Error enriching MDBList personal item {tmdb_id}: {e}")
                continue
    except Exception as e:
        logging.warning(f"[Adaptive List] MDBList personal list {list_id} error: {e}")
    logging.info(f"[Adaptive List] Fetched {len(enriched)} items from MDBList personal:{list_id}")
    return enriched


def apply_list_filters(items: List[Dict], filters: Dict, source_media_type: str = 'all') -> List[Dict]:
    """
    Apply filters to list items (client-side filtering like in discover.js).
    Only apply filters when data is present - matches frontend behavior.

    Args:
        items: List items to filter
        filters: Filter configuration
        source_media_type: 'movie', 'tv', or 'all' — enforces source-level media type restriction

    Returns:
        Filtered list of items
    """
    filtered = []

    logging.info(f"[Adaptive List] Filtering {len(items)} items with filters: {filters} (source_media_type={source_media_type})")
    
    # Count items by filter
    genre_filtered = 0
    lang_filtered = 0
    country_filtered = 0
    rating_filtered = 0
    votes_filtered = 0
    released_within_filtered = 0
    upcoming_filtered = 0
    runtime_filtered = 0
    seasons_filtered = 0
    media_type_filtered = 0
    title_filtered = 0
    keyword_filtered = 0

    # If keyword filters are set, pre-fetch keyword IDs for all items (list sources don't include keywords in item data)
    item_keywords_map: Dict[str, List[int]] = {}  # "kw_{tmdb_id}_{media_type}" -> list of keyword IDs
    if filters.get('keywords') or filters.get('keywords_exclude'):
        kw_api_key = get_setting('TMDB', 'api_key', '')
        if kw_api_key:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_keywords(tmdb_id, media_type_kw):
                cache_key_kw = f"kw_{tmdb_id}_{media_type_kw}"
                try:
                    kw_endpoint = 'tv' if media_type_kw == 'tv' else 'movie'
                    r = requests.get(
                        f"https://api.themoviedb.org/3/{kw_endpoint}/{tmdb_id}/keywords?api_key={kw_api_key}",
                        timeout=5
                    )
                    if r.ok:
                        kw_data = r.json()
                        kw_list = kw_data.get('keywords') or kw_data.get('results') or []
                        return cache_key_kw, [kw['id'] for kw in kw_list if isinstance(kw, dict)]
                except Exception:
                    pass
                return cache_key_kw, []

            unique_items = {(item['id'], item.get('media_type', 'movie')) for item in items if item.get('id')}
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(_fetch_keywords, tid, mt): (tid, mt) for tid, mt in unique_items}
                for future in as_completed(futures):
                    try:
                        cache_key_kw, kw_ids = future.result()
                        item_keywords_map[cache_key_kw] = kw_ids
                    except Exception:
                        pass

    # If seasons_max is set, pre-fetch number_of_seasons for TV items that don't have it
    seasons_max = int(filters['seasons_max']) if filters.get('seasons_max') else 0
    if seasons_max > 0:
        api_key = get_setting('TMDB', 'api_key', '')
        tv_missing = [item for item in items if item.get('media_type') == 'tv' and not item.get('number_of_seasons') and item.get('id')]
        for item in tv_missing:
            try:
                r = requests.get(f"https://api.themoviedb.org/3/tv/{item['id']}?api_key={api_key}&language=en-US", timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    seasons = [s for s in data.get('seasons', []) if s.get('season_number', 0) != 0]
                    item['number_of_seasons'] = len(seasons)
            except Exception:
                pass

    # Compile title filter regex if provided (supports JavaScript-style /pattern/flags)
    title_filter_pattern = None
    title_filter_str = filters.get('title_filter', '')
    if title_filter_str:
        try:
            # Parse JavaScript-style regex: /pattern/flags
            js_regex_match = re.match(r'^/(.+)/([gimsuvy]*)$', title_filter_str)
            if js_regex_match:
                pattern_str = js_regex_match.group(1)
                flags_str = js_regex_match.group(2)
                # Convert JS flags to Python flags (i = IGNORECASE, m = MULTILINE, s = DOTALL)
                py_flags = 0
                if 'i' in flags_str:
                    py_flags |= re.IGNORECASE
                if 'm' in flags_str:
                    py_flags |= re.MULTILINE
                if 's' in flags_str:
                    py_flags |= re.DOTALL
                title_filter_pattern = re.compile(pattern_str, py_flags)
                logging.info(f"[Adaptive List] Parsed JS regex '{title_filter_str}' -> pattern='{pattern_str}', flags={py_flags}")
            else:
                # Plain regex or text pattern
                title_filter_pattern = re.compile(title_filter_str, re.IGNORECASE)
        except re.error as e:
            logging.warning(f"[Adaptive List] Invalid regex '{title_filter_str}': {e}, treating as plain text")
            title_filter_pattern = re.compile(re.escape(title_filter_str), re.IGNORECASE)
    
    # Default upcoming_days to 0 if released_within is set but upcoming_days is not
    if filters.get('released_within') and 'upcoming_days' not in filters:
        filters['upcoming_days'] = 0
    
    for item in items:
        skip = False

        # Source-level media type restriction (from source config, not filters dict)
        if source_media_type not in ('all', '') and not skip:
            if item.get('media_type') != source_media_type:
                media_type_filtered += 1
                skip = True

        # Media type filter (from filters dict, e.g. for mixed sources)
        if filters.get('media_type') and filters['media_type'] != 'all' and not skip:
            if item.get('media_type') != filters['media_type']:
                media_type_filtered += 1
                skip = True

        # Year range filter using release_date / first_air_date
        if not skip and (filters.get('year_from') or filters.get('year_to')):
            release_date = item.get('release_date') or item.get('first_air_date') or ''
            if release_date:
                try:
                    item_year = int(release_date[:4])
                    if filters.get('year_from') and item_year < int(filters['year_from']):
                        skip = True
                    if filters.get('year_to') and item_year > int(filters['year_to']):
                        skip = True
                except (ValueError, TypeError):
                    pass
        
        # Released within filter (checks if item is too old)
        if filters.get('released_within') and not skip:
            release_date = item.get('release_date') or item.get('first_air_date')
            if release_date:
                try:
                    days_ago = int(filters['released_within'])
                    cutoff_date = datetime.now() - timedelta(days=days_ago)
                    item_date = datetime.strptime(release_date[:10], '%Y-%m-%d')
                    if item_date < cutoff_date:
                        released_within_filtered += 1
                        skip = True
                except (ValueError, TypeError):
                    pass
            else:
                released_within_filtered += 1
                skip = True
        
        # Upcoming days filter (checks if item is too far in the future)
        # When upcoming_days = 0, only allow items released up to today (no future items)
        # When upcoming_days > 0, allow items releasing within the next N days
        if 'upcoming_days' in filters and not skip:
            release_date = item.get('release_date') or item.get('first_air_date')
            if release_date:
                try:
                    days_ahead = int(filters['upcoming_days'])
                    now = datetime.now()
                    future_date = now + timedelta(days=days_ahead)
                    item_date = datetime.strptime(release_date[:10], '%Y-%m-%d')
                    if item_date > future_date:
                        upcoming_filtered += 1
                        skip = True
                except (ValueError, TypeError):
                    pass
            else:
                upcoming_filtered += 1
                skip = True
        
        # Runtime filter
        if filters.get('runtime_max') and not skip:
            runtime = item.get('runtime', 0)
            if runtime > 0:
                try:
                    max_runtime = int(filters['runtime_max'])
                    if runtime > max_runtime:
                        runtime_filtered += 1
                        skip = True
                except ValueError:
                    pass
        
        # Seasons max filter (TV only — movies always pass)
        if seasons_max > 0 and not skip and item.get('media_type') == 'tv':
            n = item.get('number_of_seasons')
            if n and n > seasons_max:
                seasons_filtered += 1
                skip = True

        # Genre filter (exclude) - only if item has genre data
        if filters.get('genres_exclude') and not skip:
            excluded_genres = [int(g) for g in filters['genres_exclude'].split(',') if g.strip().isdigit()]
            item_genres = item.get('genre_ids', [])
            if item_genres and any(g in item_genres for g in excluded_genres):
                genre_filtered += 1
                skip = True

        # Keyword filters - require/exclude specific TMDB keyword IDs
        if (filters.get('keywords') or filters.get('keywords_exclude')) and not skip and item_keywords_map:
            tmdb_id = item.get('id')
            media_type_kw = item.get('media_type', 'movie')
            cache_key_kw = f"kw_{tmdb_id}_{media_type_kw}"
            kw_ids = item_keywords_map.get(cache_key_kw, [])
            if filters.get('keywords_exclude') and not skip:
                excluded_kws = [int(k) for k in filters['keywords_exclude'].split(',') if k.strip().isdigit()]
                if any(k in kw_ids for k in excluded_kws):
                    keyword_filtered += 1
                    skip = True
            if filters.get('keywords') and not skip:
                required_kws = [int(k) for k in filters['keywords'].split(',') if k.strip().isdigit()]
                if not any(k in kw_ids for k in required_kws):
                    keyword_filtered += 1
                    skip = True

        # Language filter - only if item has language data
        if filters.get('language') and not skip:
            allowed_langs = [l.strip() for l in filters['language'].split(',') if l.strip()]
            item_lang = item.get('original_language', '')
            if item_lang and item_lang not in allowed_langs:
                lang_filtered += 1
                skip = True
        
        # Country filter - only if item has country data
        if filters.get('country') and not skip:
            allowed_countries = [c.strip() for c in filters['country'].split(',') if c.strip()]
            item_countries = item.get('origin_country', [])
            if item_countries and not any(c in allowed_countries for c in item_countries):
                country_filtered += 1
                skip = True
        
        # Rating filter - only if item has rating data
        if filters.get('tmdb_rating_min') and not skip:
            try:
                min_rating = float(filters['tmdb_rating_min'])
                item_rating = item.get('vote_average', 0)
                if item_rating > 0 and item_rating < min_rating:
                    rating_filtered += 1
                    skip = True
            except ValueError:
                pass
        
        # Votes filter - only if item has vote count data
        if filters.get('tmdb_votes_min') and not skip:
            try:
                min_votes = int(filters['tmdb_votes_min'])
                item_votes = item.get('vote_count', 0)
                if item_votes > 0 and item_votes < min_votes:
                    votes_filtered += 1
                    skip = True
            except ValueError:
                pass

        # Title filter (client-side regex/text filter)
        if title_filter_pattern and not skip:
            item_title = item.get('title') or item.get('name', '')
            if item_title and not title_filter_pattern.search(item_title):
                title_filtered += 1
                skip = True

        if not skip:
            filtered.append(item)
    
    total_dropped = len(items) - len(filtered)
    drop_reasons = []
    if media_type_filtered: drop_reasons.append(f"media_type={media_type_filtered}")
    if genre_filtered:      drop_reasons.append(f"genre={genre_filtered}")
    if keyword_filtered:    drop_reasons.append(f"keyword={keyword_filtered}")
    if rating_filtered:     drop_reasons.append(f"rating={rating_filtered}")
    if votes_filtered:      drop_reasons.append(f"votes={votes_filtered}")
    if runtime_filtered:    drop_reasons.append(f"runtime={runtime_filtered}")
    if seasons_filtered:    drop_reasons.append(f"seasons={seasons_filtered}")
    if released_within_filtered: drop_reasons.append(f"released_within={released_within_filtered}")
    if upcoming_filtered:   drop_reasons.append(f"upcoming={upcoming_filtered}")
    if lang_filtered:       drop_reasons.append(f"lang={lang_filtered}")
    if country_filtered:    drop_reasons.append(f"country={country_filtered}")
    if title_filtered:      drop_reasons.append(f"title={title_filtered}")
    drop_str = ', '.join(drop_reasons) if drop_reasons else 'none'
    logging.info(
        f"[Adaptive List] FILTER RESULT: {len(filtered)}/{len(items)} passed "
        f"(dropped {total_dropped}: {drop_str})"
    )
    
    return filtered


def build_discover_params(filters: Dict, date_field: str, media_type: str) -> List[str]:
    """
    Build TMDB discover API parameters from filter configuration.
    Handles time-sensitive filters like released_within and upcoming_days.

    Args:
        filters: Dict of filter parameters from saved configuration
        date_field: 'primary_release_date' for movies, 'first_air_date' for TV
        media_type: 'movie' or 'tv'

    Returns:
        List of URL parameter strings
    """
    params = []
    today = datetime.now()

    # Sort options
    sort_by = filters.get('sort_by', 'popularity.desc')
    # Fix sort_by for release date - TMDB uses different field names per media type
    if 'primary_release_date' in sort_by or 'release_date' in sort_by:
        order = 'desc' if '.desc' in sort_by else 'asc'
        sort_by = f"{date_field}.{order}"
    params.append(f"sort_by={sort_by}")

    # Genre filtering (include and exclude)
    if filters.get('genres'):
        genres_or = filters['genres'].replace(',', '|')
        params.append(f"with_genres={genres_or}")
    if filters.get('genres_exclude'):
        params.append(f"without_genres={filters['genres_exclude']}")

    # Keyword filtering
    if filters.get('keywords'):
        keywords_or = filters['keywords'].replace(',', '|')
        params.append(f"with_keywords={keywords_or}")
    if filters.get('keywords_exclude'):
        params.append(f"without_keywords={filters['keywords_exclude']}")

    # Language filtering
    if filters.get('language'):
        language_or = filters['language'].replace(',', '|')
        params.append(f"with_original_language={language_or}")

    # Country filtering
    if filters.get('country'):
        country_or = filters['country'].replace(',', '|')
        params.append(f"with_origin_country={country_or}")

    # Watch provider filtering
    if filters.get('watch_provider'):
        provider_or = filters['watch_provider'].replace(',', '|')
        params.append(f"with_watch_providers={provider_or}")
        watch_region = filters.get('watch_region', 'US')
        params.append(f"watch_region={watch_region}")
        # TMDB's with_watch_providers matches a title if it's available via
        # flatrate (subscription), buy, OR rent on that provider — without this,
        # picking e.g. Netflix also returns titles that are merely purchasable/
        # rentable on Netflix's storefront elsewhere, not actually streamable
        # with a subscription. Restrict to subscription availability.
        params.append("with_watch_monetization_types=flatrate")
    if filters.get('watch_provider_exclude'):
        params.append(f"without_watch_providers={filters['watch_provider_exclude']}")

    # TV Network filtering (TV only)
    if media_type == 'tv':
        if filters.get('network'):
            network_or = filters['network'].replace(',', '|')
            params.append(f"with_networks={network_or}")
        if filters.get('network_exclude'):
            params.append(f"without_networks={filters['network_exclude']}")

    # Release type filtering (Movies only)
    if media_type == 'movie' and filters.get('release_type'):
        release_type_or = filters['release_type'].replace(',', '|')
        params.append(f"with_release_type={release_type_or}")

    # TIME-SENSITIVE DATE FILTERING
    # These are what make the list "adaptive" - results change based on when checked
    date_start = None
    date_end = None
    date_filter_applied = False

    # Released Within: items released in the past X days
    released_within = filters.get('released_within')
    if released_within not in [None, '', '0', 0]:
        try:
            released_within_int = int(released_within)
            if released_within_int > 0:
                date_start = today - timedelta(days=released_within_int)
                date_filter_applied = True
                logging.info(f"[Adaptive List] Released within {released_within_int} days from: {date_start.strftime('%Y-%m-%d')}")
        except (ValueError, TypeError):
            logging.warning(f"[Adaptive List] Invalid released_within value: {released_within}")

    # Upcoming Releases: items releasing in the next X days
    upcoming_days = filters.get('upcoming_days')
    if upcoming_days not in [None, '', '0', 0]:
        try:
            upcoming_days_int = int(upcoming_days)
            if upcoming_days_int > 0:
                date_end = today + timedelta(days=upcoming_days_int)
                date_filter_applied = True
                logging.info(f"[Adaptive List] Upcoming {upcoming_days_int} days until: {date_end.strftime('%Y-%m-%d')}")
        except (ValueError, TypeError):
            logging.warning(f"[Adaptive List] Invalid upcoming_days value: {upcoming_days}")

    # Apply date range
    if date_start:
        params.append(f"{date_field}.gte={date_start.strftime('%Y-%m-%d')}")
        logging.info(f"[Adaptive List] Adding TMDB filter: {date_field}.gte={date_start.strftime('%Y-%m-%d')}")
    if date_end:
        params.append(f"{date_field}.lte={date_end.strftime('%Y-%m-%d')}")
        logging.info(f"[Adaptive List] Adding TMDB filter: {date_field}.lte={date_end.strftime('%Y-%m-%d')}")

    # Year range filtering (only if no specific date filter applied)
    if not date_filter_applied:
        if filters.get('year_from'):
            try:
                year_int = int(filters['year_from'])
                params.append(f"{date_field}.gte={year_int}-01-01")
            except ValueError:
                pass
        if filters.get('year_to'):
            try:
                year_int = int(filters['year_to'])
                params.append(f"{date_field}.lte={year_int}-12-31")
            except ValueError:
                pass

    # TMDB Rating filtering
    if filters.get('tmdb_rating_min'):
        try:
            rating = float(filters['tmdb_rating_min'])
            if rating > 0:
                params.append(f"vote_average.gte={rating}")
        except ValueError:
            pass

    if filters.get('tmdb_rating_max'):
        try:
            rating = float(filters['tmdb_rating_max'])
            if rating < 10:
                params.append(f"vote_average.lte={rating}")
        except ValueError:
            pass

    # Vote count filtering
    if filters.get('tmdb_votes_min'):
        try:
            votes = int(filters['tmdb_votes_min'])
            if votes > 0:
                params.append(f"vote_count.gte={votes}")
        except ValueError:
            pass

    # Runtime filtering (Movies only)
    if media_type == 'movie':
        if filters.get('runtime_min'):
            try:
                runtime = int(filters['runtime_min'])
                if runtime > 0:
                    params.append(f"with_runtime.gte={runtime}")
            except ValueError:
                pass
        if filters.get('runtime_max'):
            try:
                runtime = int(filters['runtime_max'])
                if runtime < 300:
                    params.append(f"with_runtime.lte={runtime}")
            except ValueError:
                pass

    # Budget filtering (Movies only)
    if media_type == 'movie':
        if filters.get('budget_min'):
            try:
                budget = int(filters['budget_min'])
                if budget > 0:
                    params.append(f"budget.gte={budget}")
            except ValueError:
                pass
        if filters.get('budget_max'):
            try:
                budget = int(filters['budget_max'])
                params.append(f"budget.lte={budget}")
            except ValueError:
                pass

    # Revenue filtering (Movies only)
    if media_type == 'movie':
        if filters.get('revenue_min'):
            try:
                revenue = int(filters['revenue_min'])
                if revenue > 0:
                    params.append(f"revenue.gte={revenue}")
            except ValueError:
                pass
        if filters.get('revenue_max'):
            try:
                revenue = int(filters['revenue_max'])
                params.append(f"revenue.lte={revenue}")
            except ValueError:
                pass

    # Production company filtering
    if filters.get('production_company'):
        company_ids = [c.strip() for c in filters['production_company'].split(',') if c.strip().isdigit()]
        if company_ids:
            params.append(f"with_companies={'|'.join(company_ids)}")
    if filters.get('production_company_exclude'):
        exclude_ids = [c.strip() for c in filters['production_company_exclude'].split(',') if c.strip().isdigit()]
        if exclude_ids:
            params.append(f"without_companies={','.join(exclude_ids)}")

    # Apply minimum vote count when filtering by rating to avoid misleading results
    # But not for upcoming content which won't have votes yet
    if filters.get('tmdb_rating_min') and not filters.get('tmdb_votes_min') and not filters.get('upcoming_days'):
        try:
            rating = float(filters['tmdb_rating_min'])
            if rating > 0:
                params.append("vote_count.gte=10")
        except ValueError:
            pass

    # Include video filter - allows non-standard video content
    if filters.get('include_video'):
        params.append("include_video=true")

    return params


def get_imdb_id(api_key: str, tmdb_id: int, media_type: str, release_date: str | None = None) -> str | None:
    """
    Fetch IMDB ID for a TMDB item with smart caching.

    Args:
        api_key: TMDB API key
        tmdb_id: TMDB ID of the item
        media_type: 'movie' or 'tv'
        release_date: Release date string (YYYY-MM-DD) for smart TTL

    Returns:
        IMDB ID string or None if not found
    """
    # Load cache
    imdb_cache = load_imdb_cache()
    cache_key = f"{tmdb_id}_{media_type}"
    current_time = datetime.now()
    
    # Check cache first
    if cache_key in imdb_cache:
        cache_entry = imdb_cache[cache_key]
        if is_cache_entry_valid(cache_entry, cache_key, current_time):
            logger.debug(f"[Adaptive List] Cache hit for TMDB {tmdb_id} ({media_type})")
            return cache_entry.get('imdb_id')
        else:
            logger.debug(f"[Adaptive List] Cache expired for TMDB {tmdb_id} ({media_type})")
    
    # Cache miss or expired - fetch from API
    try:
        if media_type == 'tv':
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={api_key}"
        else:
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={api_key}"

        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        imdb_id = data.get('imdb_id')
        
        # Cache the result (including None results to avoid repeated failed lookups)
        cache_entry = {
            'imdb_id': imdb_id,
            'cached_at': current_time,
            'release_date': release_date or 'Unknown'
        }
        imdb_cache[cache_key] = cache_entry
        save_imdb_cache(imdb_cache)
        
        logger.debug(f"[Adaptive List] Cached IMDB ID for TMDB {tmdb_id} ({media_type}): {imdb_id}")
        
        return imdb_id

    except Exception as e:
        logging.debug(f"[Adaptive List] Could not get IMDB ID for TMDB {tmdb_id}: {e}")
        return None


def remove_from_adaptive_list(items: List[dict]) -> dict:
    """
    Adaptive lists don't support removal as they're dynamic TMDB queries.
    Items are filtered by the ghostlist/already collected check instead.

    Returns:
        dict: Always returns success with 0 removed
    """
    return {
        'success': True,
        'removed': 0,
        'message': 'Adaptive lists do not support item removal - items are managed via ghostlist'
    }
