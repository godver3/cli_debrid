"""
Discover Routes
Cinephage-style discover page with TMDB integration
"""

import hashlib
import logging
import requests
import json
import os
import gzip
import io
import threading
from datetime import datetime, timedelta
from flask import Blueprint, render_template, jsonify, request
from flask_login import current_user
from utilities.tmdb_cache import cache_response, get_cached_db_statuses, get_cached_episode_info
from utilities.settings import get_setting
from utilities.reverse_parser import get_default_version
from utilities.mdblist_api import (
    is_mdblist_configured,
    fetch_mdblist_top_lists,
    fetch_list_items,
    get_user_lists as get_mdblist_user_lists,
    fetch_custom_list_items,
    test_api_connection as test_mdblist_connection,
    CURATED_LISTS
)
from utilities.flixpatrol_api import (
    get_available_platforms as get_flixpatrol_platforms,
    fetch_top10 as fetch_flixpatrol_top10,
    fetch_top10_weekly as fetch_flixpatrol_top10_weekly,
    get_title_ids_from_flixpatrol
)
from routes.models import user_required

# ── Trakt personal list cache ──────────────────────────────────────────────────
# Keyed by slug+media_type for mylist results; separate entry for the list index.
_trakt_cache_lock = threading.Lock()
_trakt_lists_cache = {
    # 'index': {'data': [...], 'expires': datetime}
    # '<slug>:<media_type>': {'data': [...], 'updated_at': '<iso>', 'expires': datetime}
    # 'special:<list_type>:<media_type>': {'data': [...], 'content_hash': '<md5>', 'expires': datetime}
}
_TRAKT_LISTS_INDEX_TTL  = timedelta(minutes=10)   # how long to cache the list-of-lists
_TRAKT_MYLIST_TTL       = timedelta(hours=24)      # max age before force-refresh regardless
_TRAKT_SPECIAL_TTL      = timedelta(hours=24)      # hard TTL for special lists (trending etc.)

# ── MDBList personal list cache ───────────────────────────────────────────────
_mdblist_personal_cache_lock = threading.Lock()
_mdblist_personal_cache = {}   # 'index': {'data': [...], 'expires': datetime}
                               # '<list_id>': {'data': [...], 'expires': datetime}
_MDBLIST_PERSONAL_INDEX_TTL = timedelta(minutes=10)
_MDBLIST_PERSONAL_ITEMS_TTL = timedelta(hours=6)
_MDBLIST_PERSONAL_FILE_CACHE = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'mdblist_personal_cache.json')

def _load_mdblist_personal_file_cache():
    """Seed _mdblist_personal_cache from disk on startup."""
    try:
        if not os.path.exists(_MDBLIST_PERSONAL_FILE_CACHE):
            return
        with open(_MDBLIST_PERSONAL_FILE_CACHE, 'r', encoding='utf-8') as f:
            stored = json.load(f)
        loaded = 0
        with _mdblist_personal_cache_lock:
            for key, entry in stored.items():
                if key == 'index':
                    continue  # Don't restore index; re-fetch on first request
                try:
                    expires = datetime.fromisoformat(entry.get('expires', ''))
                except Exception:
                    continue
                _mdblist_personal_cache[key] = {
                    'data': entry['data'],
                    'expires': expires,
                }
                loaded += 1
        if loaded:
            logging.info(f"[MDBList Personal] Loaded {loaded} list entries from file cache")
    except Exception as e:
        logging.warning(f"[MDBList Personal] Could not load file cache: {e}")

def _save_mdblist_personal_file_cache():
    """Persist personal list item entries to disk."""
    try:
        with _mdblist_personal_cache_lock:
            to_store = {
                key: {'data': entry['data'], 'expires': entry['expires'].isoformat()}
                for key, entry in _mdblist_personal_cache.items()
                if key != 'index'
            }
        os.makedirs(os.path.dirname(_MDBLIST_PERSONAL_FILE_CACHE), exist_ok=True)
        tmp = _MDBLIST_PERSONAL_FILE_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(to_store, f)
        os.replace(tmp, _MDBLIST_PERSONAL_FILE_CACHE)
    except Exception as e:
        logging.warning(f"[MDBList Personal] Could not save file cache: {e}")

_load_mdblist_personal_file_cache()

# ── Trakt mylist file cache ────────────────────────────────────────────────────
# Persists mylist results across restarts so a large list doesn't need re-fetching.
# Only mylist entries (slug:media_type keys) are stored — not index or special lists.
_MYLIST_FILE_CACHE = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'trakt_mylist_cache.json')

def _load_mylist_file_cache():
    """Seed _trakt_lists_cache from disk on startup."""
    try:
        if not os.path.exists(_MYLIST_FILE_CACHE):
            return
        with open(_MYLIST_FILE_CACHE, 'r', encoding='utf-8') as f:
            stored = json.load(f)
        loaded = 0
        with _trakt_cache_lock:
            for key, entry in stored.items():
                if ':' not in key or key.startswith('special:'):
                    continue
                expires_str = entry.get('expires', '')
                try:
                    expires = datetime.fromisoformat(expires_str)
                except Exception:
                    continue
                _trakt_lists_cache[key] = {
                    'data': entry['data'],
                    'updated_at': entry.get('updated_at', ''),
                    'expires': expires,
                }
                loaded += 1
        if loaded:
            logging.info(f"[Personal] Loaded {loaded} mylist entries from file cache")
    except Exception as e:
        logging.warning(f"[Personal] Could not load mylist file cache: {e}")

def _save_mylist_file_cache():
    """Persist all mylist entries from _trakt_lists_cache to disk."""
    try:
        with _trakt_cache_lock:
            to_store = {
                key: {
                    'data': entry['data'],
                    'updated_at': entry.get('updated_at', ''),
                    'expires': entry['expires'].isoformat(),
                }
                for key, entry in _trakt_lists_cache.items()
                if ':' in key and not key.startswith('special:') and key != 'index'
            }
        os.makedirs(os.path.dirname(_MYLIST_FILE_CACHE), exist_ok=True)
        tmp = _MYLIST_FILE_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(to_store, f)
        os.replace(tmp, _MYLIST_FILE_CACHE)
    except Exception as e:
        logging.warning(f"[Personal] Could not save mylist file cache: {e}")

# Load file cache at import time so it's available before the first request
_load_mylist_file_cache()

# ── Scrob personal/special list cache ─────────────────────────────────────────
# In-memory only (no disk persistence) — Scrob is self-hosted and local, unlike
# MDBList's external rate-limited API, so re-fetching on restart is cheap.
_scrob_cache_lock = threading.Lock()
_scrob_cache = {
    # 'lists-index': {'data': [...], 'expires': datetime}
    # 'list:<list_id>': {'data': [...], 'expires': datetime}
    # 'special:<list_type>:<media_type>': {'data': [...], 'expires': datetime}
}
_SCROB_LISTS_INDEX_TTL = timedelta(minutes=10)
_SCROB_LIST_ITEMS_TTL = timedelta(hours=6)
_SCROB_SPECIAL_TTL = timedelta(hours=1)

# ── Trending cache ─────────────────────────────────────────────────────────────
# Keyed by '<media_type>:<anime_filter>'
_trending_cache_lock = threading.Lock()
_trending_cache = {}
_TRENDING_TTL = timedelta(hours=1)

# ── FlixPatrol top10 cache ─────────────────────────────────────────────────────
# Today keyed by '<platform>:<media_type>', weekly by 'weekly:<platform>:<media_type>'
_flixpatrol_cache_lock = threading.Lock()
_flixpatrol_cache = {}
_FLIXPATROL_TTL = timedelta(hours=6)
_FLIXPATROL_WEEKLY_TTL = timedelta(hours=24)

# Path to store filter presets - uses USER_CONFIG environment variable
PRESETS_FILE = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'discover_presets.json')
NETWORK_ENRICHMENT_FILE = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'tmdb_network_enrichment.json')

discover_bp = Blueprint('discover', __name__)

# --- Network cache (TMDB daily export) ---
_network_cache = None
_network_cache_date = None
_network_cache_lock = threading.Lock()

def _get_network_cache():
    """Load TMDB network list from daily export file, cached in memory per day."""
    global _network_cache, _network_cache_date
    today = datetime.utcnow().date()
    # Check cache without holding lock during HTTP I/O
    with _network_cache_lock:
        if _network_cache is not None and _network_cache_date == today:
            return _network_cache
    # Try today then fall back up to 2 days (export may not be ready yet)
    for delta in range(3):
        date = today - timedelta(days=delta)
        date_str = date.strftime('%m_%d_%Y')
        url = f"http://files.tmdb.org/p/exports/tv_network_ids_{date_str}.json.gz"
        try:
            resp = requests.get(url, timeout=30)
            if not resp.ok:
                continue
            networks = []
            with gzip.open(io.BytesIO(resp.content), 'rt', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        networks.append({'id': obj['id'], 'name': obj['name']})
                    except (json.JSONDecodeError, KeyError):
                        continue
            networks.sort(key=lambda x: x['name'].lower())
            with _network_cache_lock:
                _network_cache = networks
                _network_cache_date = today
            logging.info(f"[Discover] Loaded {len(networks)} TMDB networks from export ({date_str})")
            return networks
        except Exception as e:
            logging.warning(f"[Discover] Failed to load TMDB network export for {date_str}: {e}")
            continue
    logging.error("[Discover] Could not load TMDB network export after 3 attempts")
    return []

# --- Network enrichment cache (origin_country per network ID) ---
_network_enrichment = None  # {str(id): origin_country}
_enrichment_lock = threading.Lock()

def _load_enrichment_cache():
    global _network_enrichment
    with _enrichment_lock:
        if _network_enrichment is not None:
            return
        try:
            if os.path.exists(NETWORK_ENRICHMENT_FILE):
                with open(NETWORK_ENRICHMENT_FILE, 'r') as f:
                    _network_enrichment = json.load(f)
                logging.info(f"[Discover] Loaded {len(_network_enrichment)} enriched networks from cache")
            else:
                _network_enrichment = {}
        except Exception as e:
            logging.warning(f"[Discover] Failed to load network enrichment cache: {e}")
            _network_enrichment = {}

def _save_enrichment_cache():
    try:
        with open(NETWORK_ENRICHMENT_FILE, 'w') as f:
            json.dump(_network_enrichment, f)
    except Exception as e:
        logging.warning(f"[Discover] Failed to save network enrichment cache: {e}")

def _enrich_networks(networks, api_key):
    """Add origin_country and logo_path to network results, fetching missing ones from TMDB API."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _load_enrichment_cache()

    to_fetch = [n for n in networks if str(n['id']) not in _network_enrichment]

    if to_fetch:
        def fetch_one(network_id):
            try:
                url = f"https://api.themoviedb.org/3/network/{network_id}?api_key={api_key}"
                resp = requests.get(url, timeout=5)
                if resp.ok:
                    data = resp.json()
                    return str(network_id), {
                        'origin_country': data.get('origin_country', ''),
                        'logo_path': data.get('logo_path', '')
                    }
            except Exception:
                pass
            return str(network_id), {'origin_country': '', 'logo_path': ''}

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_one, n['id']): n for n in to_fetch}
            for future in as_completed(futures):
                nid, info = future.result()
                _network_enrichment[nid] = info

        with _enrichment_lock:
            _save_enrichment_cache()

    enriched = []
    for n in networks:
        info = _network_enrichment.get(str(n['id']), {})
        country = info.get('origin_country', '') if isinstance(info, dict) else ''
        logo_path = info.get('logo_path', '') if isinstance(info, dict) else ''
        display_name = f"{n['name']} ({country})" if country else n['name']
        enriched.append({'id': n['id'], 'name': display_name, 'logo_path': logo_path})
    return enriched


def add_db_status_and_episode_info(results, use_battery=False):
    """
    Add database status and episode info to results.
    For TV shows with episodes in the database, adds episode_info dict.

    Args:
        results: List of result items with 'id' and 'media_type' fields
        use_battery: If True, try Battery for season counts (slower but more accurate).
                     If False, skip Battery and only use TMDB (faster for discovery browsing).
    """
    if not results:
        return

    # Get database status for all items
    tmdb_ids = [str(item['id']) for item in results]
    db_statuses = get_cached_db_statuses(tmdb_ids) if tmdb_ids else {}

    # Get episode info for TV shows only
    tv_show_ids = [str(item['id']) for item in results if item.get('media_type') == 'tv']
    episode_info = get_cached_episode_info(tv_show_ids) if tv_show_ids else {}

    # For all TV shows, fetch season/episode counts if not already present
    tv_shows_needing_counts = []
    for item in results:
        if item.get('media_type') == 'tv':
            if not item.get('number_of_seasons') or not item.get('number_of_episodes'):
                tv_shows_needing_counts.append(item)

    # Try Battery first for shows in library (faster, cached data) - only if requested
    if use_battery and tv_shows_needing_counts:
        try:
            from cli_battery.app.direct_api import DirectAPI
            for item in tv_shows_needing_counts[:]:  # Use slice to allow removal during iteration
                try:
                    # Convert TMDB ID to IMDb ID
                    imdb_id, _ = DirectAPI.tmdb_to_imdb(str(item['id']), media_type='show')
                    if imdb_id:
                        # Try to get metadata from Battery
                        battery_metadata, source = DirectAPI.get_show_metadata(imdb_id)
                        if battery_metadata and battery_metadata.get('seasons'):
                            seasons_data = battery_metadata['seasons']
                            if isinstance(seasons_data, dict):
                                # Exclude Season 0 (specials) from both season and episode counts
                                item['number_of_seasons'] = len([s for s in seasons_data.keys() if s != '0' and s != 0])
                                total_eps = sum(
                                    s.get('episode_count', 0)
                                    for k, s in seasons_data.items()
                                    if isinstance(s, dict) and k != '0' and k != 0
                                )
                                item['number_of_episodes'] = total_eps
                                tv_shows_needing_counts.remove(item)
                                logging.debug(f"Battery provided season counts for TMDB {item['id']} from {source}")
                except Exception as e:
                    logging.debug(f"Battery lookup failed for TMDB {item['id']}: {e}")
        except ImportError:
            logging.debug("Battery not available, falling back to TMDB")

    # Fetch counts from TMDB for remaining shows
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    if tv_shows_needing_counts and tmdb_api_key:
        import requests
        for item in tv_shows_needing_counts:
            try:
                url = f"https://api.themoviedb.org/3/tv/{item['id']}?api_key={tmdb_api_key}"
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    # Exclude Season 0 (specials) from counts
                    seasons = data.get('seasons', [])
                    non_special_seasons = [s for s in seasons if s.get('season_number', 0) != 0]
                    item['number_of_seasons'] = len(non_special_seasons)
                    item['number_of_episodes'] = sum(s.get('episode_count', 0) for s in non_special_seasons)
                    logging.debug(f"TMDB API provided season counts for {item['id']} (excluding specials)")
            except Exception as e:
                logging.debug(f"Failed to fetch TMDB counts for {item['id']}: {e}")

    # Apply to results
    for item in results:
        item['db_status'] = db_statuses.get(str(item['id']), 'missing')
        # Add episode info for TV shows
        if item.get('media_type') == 'tv':
            item_episode_info = episode_info.get(str(item['id']))
            item['episode_info'] = item_episode_info

            # Override status based on wanted episodes and TMDB comparison
            # Partial = some episodes collected/blacklisted AND (some wanted OR missing from DB entirely)
            # This ensures correct navigation: Partial → discover details, Collected → library
            if item_episode_info and item['db_status'] != 'missing':
                # Get episode state counts
                collected = item_episode_info.get('collected_episodes', 0)
                blacklisted = item_episode_info.get('blacklisted_episodes', 0)
                wanted = item_episode_info.get('wanted_episodes', 0)
                db_total = item_episode_info.get('total_episodes', 0)  # Episodes that exist in DB

                # Get TMDB total (excluding Season 0)
                tmdb_total = item.get('number_of_episodes', 0)

                # Check if we're missing episodes entirely from the DB
                missing_from_db = (tmdb_total > 0 and db_total < tmdb_total)

                # Determine status
                if (collected > 0 or blacklisted > 0):
                    # We have some collected/blacklisted episodes
                    if wanted > 0 or missing_from_db:
                        # Either has wanted episodes OR missing episodes entirely from DB
                        item['db_status'] = 'partial'
                    else:
                        # All episodes accounted for and collected/blacklisted
                        item['db_status'] = 'collected'
                # If only wanted/unreleased episodes (no collected/blacklisted), keep current status

def get_digital_release_date(tmdb_id, media_type, tmdb_api_key):
    """
    Fetch digital release date for a movie/TV show from TMDB release_dates endpoint.
    Falls back to theatrical release if digital release is not available.
    Returns a dict with 'date' and 'type' ('digital' or 'theatrical'), or empty dict if not found.
    """
    try:
        if media_type == 'movie':
            # For movies, use the release_dates endpoint
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={tmdb_api_key}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Priority: US digital release (type 4), then any digital release, then US theatrical (type 3)
            us_digital = None
            any_digital = None
            us_theatrical = None
            
            for country in data.get('results', []):
                for release in country.get('release_dates', []):
                    if release.get('type') == 4:  # Digital release
                        release_date = release.get('release_date', '').split('T')[0]  # Get YYYY-MM-DD only
                        if country['iso_3166_1'] == 'US':
                            us_digital = release_date
                        elif not any_digital:
                            any_digital = release_date
                    elif release.get('type') == 3 and country['iso_3166_1'] == 'US':  # US Theatrical
                        us_theatrical = release.get('release_date', '').split('T')[0]
            
            # Return in priority order with type indicator
            if us_digital or any_digital:
                return {'date': us_digital or any_digital, 'type': 'digital'}
            elif us_theatrical:
                return {'date': us_theatrical, 'type': 'theatrical'}
            else:
                return {}
        else:
            # For TV shows, just return first_air_date (no digital release concept for TV)
            return {}
    except Exception as e:
        logging.debug(f"Error fetching digital release date for {media_type} {tmdb_id}: {e}")
        return {}

def get_certification(tmdb_id, media_type, tmdb_api_key, region='US'):
    """
    Fetch content certification/rating for a specific region from TMDB.
    For movies: uses release_dates endpoint (e.g., US: G, PG, PG-13, R, NC-17)
    For TV shows: uses content_ratings endpoint (e.g., US: TV-Y, TV-PG, TV-14, TV-MA)
    Returns certification string or empty string if not found.
    """
    try:
        if media_type == 'movie':
            # For movies, use the release_dates endpoint
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={tmdb_api_key}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Find certification for the specified region
            for country in data.get('results', []):
                if country.get('iso_3166_1') == region:
                    for release in country.get('release_dates', []):
                        cert = release.get('certification', '').strip()
                        if cert:
                            return cert
        else:
            # For TV shows, use the content_ratings endpoint
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/content_ratings?api_key={tmdb_api_key}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Find content rating for the specified region
            for rating in data.get('results', []):
                if rating.get('iso_3166_1') == region:
                    cert = rating.get('rating', '').strip()
                    if cert:
                        return cert

        # Log when no certification is found to help with debugging
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug(f"No certification found for {media_type} {tmdb_id} in region {region}")
        return ''
    except Exception as e:
        logging.error(f"Error fetching certification for {media_type} {tmdb_id} ({region}): {e}")
        return ''

def enrich_with_digital_dates(results, media_type, tmdb_api_key):
    """
    Batch enrich results with digital release dates.
    Updates the 'release_date' and 'release_type' fields for each result.
    """
    for item in results:
        if item.get('media_type', media_type) == 'movie':
            release_info = get_digital_release_date(item['id'], 'movie', tmdb_api_key)
            if release_info and release_info.get('date'):
                item['release_date'] = release_info['date']
                item['release_type'] = release_info.get('type', 'theatrical')
            else:
                # Clean existing release_date to ensure no timestamps
                if 'release_date' in item and item['release_date']:
                    item['release_date'] = str(item['release_date']).split('T')[0].split(' ')[0]
                # Always set release_type for movies, default to theatrical
                item['release_type'] = 'theatrical'
        else:
            # For TV shows, clean the date and don't set release_type
            if 'release_date' in item and item['release_date']:
                item['release_date'] = str(item['release_date']).split('T')[0].split(' ')[0]
    return results

@discover_bp.route('/')
@user_required
def index():
    """Main discover page"""
    from utilities.web_scraper import get_available_versions
    from .utils import is_user_system_enabled

    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    versions = get_available_versions()

    # Check permissions
    if not is_user_system_enabled():
        has_admin_permissions = True
    else:
        has_admin_permissions = current_user.is_authenticated and current_user.role == 'admin'

    return render_template('discover.html',
                         tmdb_configured=bool(tmdb_api_key),
                         versions=versions,
                         has_admin_permissions=has_admin_permissions)

@discover_bp.route('/api/search')
@user_required
def search():
    """
    Search discover content using TMDB API
    Supports text search, IMDb IDs (tt1234567), and TMDB IDs (tmdb:12345 or just 12345)
    No caching to ensure fresh results for each search
    """
    import re
    try:
        # Get parameters
        query = request.args.get('query', '').strip()
        media_type = request.args.get('type', 'all')  # all, movie, tv
        page = int(request.args.get('page', 1))
        sort_by = request.args.get('sort_by', 'relevance')  # relevance, popularity, rating

        # Get TMDB API key
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        results = []
        movie_data = {'total_pages': 1}
        tv_data = {'total_pages': 1}

        # Check for IMDb ID (tt followed by digits)
        imdb_match = re.match(r'^(tt\d+)$', query, re.IGNORECASE)
        if imdb_match:
            imdb_id = imdb_match.group(1).lower()
            logging.info(f"[Discover] IMDb ID search: {imdb_id}")

            # Use TMDB's find endpoint to look up by IMDb ID
            find_url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={tmdb_api_key}&external_source=imdb_id"
            find_response = requests.get(find_url, timeout=10)
            find_response.raise_for_status()
            find_data = find_response.json()

            # Process movie results
            if media_type in ['all', 'movie']:
                for idx, movie in enumerate(find_data.get('movie_results', [])):
                    results.append({
                        'id': movie['id'],
                        'title': movie.get('title', ''),
                        'overview': movie.get('overview', ''),
                        'poster_path': movie.get('poster_path'),
                        'backdrop_path': movie.get('backdrop_path'),
                        'release_date': movie.get('release_date', ''),
                        'vote_average': movie.get('vote_average', 0),
                        'vote_count': movie.get('vote_count', 0),
                        'popularity': movie.get('popularity', 0),
                        'relevance_order': idx,
                        'media_type': 'movie',
                        'genre_ids': movie.get('genre_ids', []),
                        'imdb_id': imdb_id
                    })

            # Process TV results
            if media_type in ['all', 'tv']:
                for idx, tv in enumerate(find_data.get('tv_results', [])):
                    results.append({
                        'id': tv['id'],
                        'name': tv.get('name', ''),
                        'title': tv.get('name', ''),
                        'overview': tv.get('overview', ''),
                        'poster_path': tv.get('poster_path'),
                        'backdrop_path': tv.get('backdrop_path'),
                        'first_air_date': tv.get('first_air_date', ''),
                        'release_date': tv.get('first_air_date', ''),
                        'vote_average': tv.get('vote_average', 0),
                        'vote_count': tv.get('vote_count', 0),
                        'popularity': tv.get('popularity', 0),
                        'relevance_order': len(results),
                        'media_type': 'tv',
                        'genre_ids': tv.get('genre_ids', []),
                        'imdb_id': imdb_id
                    })

            # Add database status and episode info
            add_db_status_and_episode_info(results, use_battery=False)

            return jsonify({
                'results': results,
                'page': 1,
                'total_pages': 1,
                'total_results': len(results),
                'search_type': 'imdb_id'
            })

        # Check for TMDB ID (tmdb: prefix or just digits)
        tmdb_match = re.match(r'^tmdb:?(\d+)$', query, re.IGNORECASE)
        if not tmdb_match:
            # Check for plain digits (treat as TMDB ID)
            digits_match = re.match(r'^(\d+)$', query)
            if digits_match and int(digits_match.group(1)) > 0:
                tmdb_match = digits_match

        if tmdb_match:
            tmdb_id = tmdb_match.group(1)
            logging.info(f"[Discover] TMDB ID search: {tmdb_id}")

            # Try to fetch both movie and TV with this ID
            if media_type in ['all', 'movie']:
                try:
                    movie_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    movie_response = requests.get(movie_url, timeout=10)
                    if movie_response.status_code == 200:
                        movie = movie_response.json()
                        results.append({
                            'id': movie['id'],
                            'title': movie.get('title', ''),
                            'overview': movie.get('overview', ''),
                            'poster_path': movie.get('poster_path'),
                            'backdrop_path': movie.get('backdrop_path'),
                            'release_date': movie.get('release_date', ''),
                            'vote_average': movie.get('vote_average', 0),
                            'vote_count': movie.get('vote_count', 0),
                            'popularity': movie.get('popularity', 0),
                            'relevance_order': 0,
                            'media_type': 'movie',
                            'genre_ids': [g['id'] for g in movie.get('genres', [])],
                            'imdb_id': movie.get('imdb_id')
                        })
                except Exception as e:
                    logging.debug(f"TMDB movie lookup failed for {tmdb_id}: {e}")

            if media_type in ['all', 'tv']:
                try:
                    tv_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    tv_response = requests.get(tv_url, timeout=10)
                    if tv_response.status_code == 200:
                        tv = tv_response.json()
                        results.append({
                            'id': tv['id'],
                            'name': tv.get('name', ''),
                            'title': tv.get('name', ''),
                            'overview': tv.get('overview', ''),
                            'poster_path': tv.get('poster_path'),
                            'backdrop_path': tv.get('backdrop_path'),
                            'first_air_date': tv.get('first_air_date', ''),
                            'release_date': tv.get('first_air_date', ''),
                            'vote_average': tv.get('vote_average', 0),
                            'vote_count': tv.get('vote_count', 0),
                            'popularity': tv.get('popularity', 0),
                            'relevance_order': len(results),
                            'media_type': 'tv',
                            'genre_ids': [g['id'] for g in tv.get('genres', [])]
                        })
                except Exception as e:
                    logging.debug(f"TMDB TV lookup failed for {tmdb_id}: {e}")

            # Add database status and episode info
            add_db_status_and_episode_info(results, use_battery=False)

            return jsonify({
                'results': results,
                'page': 1,
                'total_pages': 1,
                'total_results': len(results),
                'search_type': 'tmdb_id'
            })

        # Standard text search
        # Search movies if requested
        if media_type in ['all', 'movie']:
            movie_url = f"https://api.themoviedb.org/3/search/movie?api_key={tmdb_api_key}&query={query}&page={page}&language=en-US"
            movie_response = requests.get(movie_url, timeout=10)
            movie_response.raise_for_status()
            movie_data = movie_response.json()

            for idx, movie in enumerate(movie_data.get('results', [])):
                results.append({
                    'id': movie['id'],
                    'title': movie['title'],
                    'overview': movie.get('overview', ''),
                    'poster_path': movie.get('poster_path'),
                    'backdrop_path': movie.get('backdrop_path'),
                    'release_date': movie.get('release_date', ''),
                    'vote_average': movie.get('vote_average', 0),
                    'vote_count': movie.get('vote_count', 0),
                    'popularity': movie.get('popularity', 0),
                    'relevance_order': idx,  # Track original TMDB relevance order
                    'media_type': 'movie',
                    'genre_ids': movie.get('genre_ids', [])
                })

        # Search TV shows if requested
        if media_type in ['all', 'tv']:
            tv_url = f"https://api.themoviedb.org/3/search/tv?api_key={tmdb_api_key}&query={query}&page={page}&language=en-US"
            tv_response = requests.get(tv_url, timeout=10)
            tv_response.raise_for_status()
            tv_data = tv_response.json()

            for idx, tv in enumerate(tv_data.get('results', [])):
                results.append({
                    'id': tv['id'],
                    'name': tv['name'],
                    'title': tv['name'],  # Normalize for frontend
                    'overview': tv.get('overview', ''),
                    'poster_path': tv.get('poster_path'),
                    'backdrop_path': tv.get('backdrop_path'),
                    'first_air_date': tv.get('first_air_date', ''),
                    'release_date': tv.get('first_air_date', ''),  # Normalize
                    'vote_average': tv.get('vote_average', 0),
                    'vote_count': tv.get('vote_count', 0),
                    'popularity': tv.get('popularity', 0),
                    'relevance_order': idx,  # Track original TMDB relevance order
                    'media_type': 'tv',
                    'genre_ids': tv.get('genre_ids', [])
                })

        # Enrich with digital release dates
        results = enrich_with_digital_dates(results, media_type, tmdb_api_key)

        # Sort results based on user preference
        # Parse sort_by format: "field.order" (e.g., "popularity.desc", "vote_average.asc")
        sort_field = 'relevance'
        sort_order = 'desc'
        if '.' in sort_by:
            parts = sort_by.split('.')
            sort_field = parts[0]
            sort_order = parts[1] if len(parts) > 1 else 'desc'
        else:
            sort_field = sort_by

        reverse_sort = (sort_order == 'desc')

        if sort_field == 'popularity':
            results.sort(key=lambda x: x.get('popularity', 0), reverse=reverse_sort)
        elif sort_field in ['vote_average', 'rating']:
            results.sort(key=lambda x: x.get('vote_average', 0), reverse=reverse_sort)
        elif sort_field == 'release_date':
            results.sort(key=lambda x: x.get('release_date', '') or x.get('first_air_date', ''), reverse=reverse_sort)
        elif sort_field == 'vote_count':
            results.sort(key=lambda x: x.get('vote_count', 0), reverse=reverse_sort)
        else:  # relevance (default) - keep TMDB's relevance order
            results.sort(key=lambda x: x.get('relevance_order', 999))

        # Add database status and episode info
        add_db_status_and_episode_info(results)

        return jsonify({
            'results': results,
            'page': page,
            'total_pages': max(movie_data.get('total_pages', 1), tv_data.get('total_pages', 1)) if media_type == 'all' else (movie_data.get('total_pages', 1) if media_type == 'movie' else tv_data.get('total_pages', 1)),
            'total_results': len(results)
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB API error: {e}")
        return jsonify({'error': 'Failed to fetch from TMDB API'}), 500
    except Exception as e:
        logging.error(f"Discover search error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/trending')
@user_required
def trending():
    """
    Get trending content from TMDB
    Hybrid approach:
    - For anime=only: Uses discover endpoint with keyword filtering (210024)
    - For movies/shows: Uses trending/week endpoint for accurate weekly trending
    Frontend filters out anime from shows section
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Get type filter parameter
        media_type = request.args.get('type', 'movie')  # movie or tv

        # Get anime filter parameter
        anime_filter = request.args.get('anime', 'include')  # include, exclude, only

        # Serve from cache if fresh
        cache_key = f'{media_type}:{anime_filter}'
        with _trending_cache_lock:
            entry = _trending_cache.get(cache_key)
        if entry and datetime.now() < entry['expires']:
            logging.debug(f"[Trending] Serving from cache ({cache_key})")
            return jsonify({'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})
        stale_entry = entry  # keep reference to serve as fallback on TMDB failure

        results = []

        # For anime-only requests, use discover endpoint with keyword filtering
        if anime_filter == 'only':
            # TMDB keyword ID for anime
            ANIME_KEYWORD_ID = '210024'

            # Use discover endpoint for anime (only supports TV)
            url = f"https://api.themoviedb.org/3/discover/tv?api_key={tmdb_api_key}&sort_by=popularity.desc&page=1&with_keywords={ANIME_KEYWORD_ID}"

            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Process TV results
            for tv in data.get('results', [])[:20]:
                results.append({
                    'id': tv['id'],
                    'name': tv['name'],
                    'overview': tv.get('overview', ''),
                    'poster_path': tv.get('poster_path'),
                    'backdrop_path': tv.get('backdrop_path'),
                    'first_air_date': tv.get('first_air_date', ''),
                    'vote_average': tv.get('vote_average', 0),
                    'vote_count': tv.get('vote_count', 0),
                    'media_type': 'tv',
                    'genre_ids': tv.get('genre_ids', [])
                })
        else:
            # For movies and shows, use original trending/week endpoint
            if media_type == 'movie':
                base_url = f"https://api.themoviedb.org/3/trending/movie/week?api_key={tmdb_api_key}"
            else:  # tv
                base_url = f"https://api.themoviedb.org/3/trending/tv/week?api_key={tmdb_api_key}"

            # For TV shows, fetch 2 pages to get 35+ results (20 per page)
            # For movies, fetch 1 page to get 20 results
            all_items = []
            pages_to_fetch = 2 if media_type == 'tv' else 1

            for page in range(1, pages_to_fetch + 1):
                url = f"{base_url}&page={page}"
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()
                all_items.extend(data.get('results', []))

            # Process results
            # For TV shows, limit to 35 results; for movies, limit to 20
            limit = 35 if media_type == 'tv' else 20
            for item in all_items[:limit]:
                if media_type == 'movie':
                    results.append({
                        'id': item['id'],
                        'title': item['title'],
                        'overview': item.get('overview', ''),
                        'poster_path': item.get('poster_path'),
                        'backdrop_path': item.get('backdrop_path'),
                        'release_date': item.get('release_date', ''),
                        'vote_average': item.get('vote_average', 0),
                        'vote_count': item.get('vote_count', 0),
                        'media_type': 'movie',
                        'genre_ids': item.get('genre_ids', [])
                    })
                else:  # tv
                    results.append({
                        'id': item['id'],
                        'name': item['name'],
                        'overview': item.get('overview', ''),
                        'poster_path': item.get('poster_path'),
                        'backdrop_path': item.get('backdrop_path'),
                        'first_air_date': item.get('first_air_date', ''),
                        'vote_average': item.get('vote_average', 0),
                        'vote_count': item.get('vote_count', 0),
                        'media_type': 'tv',
                        'genre_ids': item.get('genre_ids', [])
                    })

        # Enrich with digital release dates
        results = enrich_with_digital_dates(results, 'all', tmdb_api_key)

        # Add database status and episode info (skip Battery for faster discovery browsing)
        add_db_status_and_episode_info(results, use_battery=False)

        logging.info(f"Trending: {media_type} with anime={anime_filter}, returned {len(results)} results")

        # Store in cache
        with _trending_cache_lock:
            _trending_cache[cache_key] = {
                'data': results,
                'expires': datetime.now() + _TRENDING_TTL,
            }

        return jsonify({
            'results': results,
            'total_results': len(results)
        })

    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"TMDB trending API error: {e}")
        if stale_entry:
            logging.warning(f"[Trending] Serving stale cache for {cache_key} due to TMDB error")
            return jsonify({'results': stale_entry['data'], 'total_results': len(stale_entry['data']), 'cached': True, 'stale': True})
        return jsonify({'error': 'Failed to fetch trending content'}), 503
    except Exception as e:
        logging.error(f"Discover trending error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

_tmdb_shows_cache_lock = threading.Lock()
_tmdb_shows_cache = {}
_TMDB_SHOWS_TTL = timedelta(hours=6)


@discover_bp.route('/api/tmdb/shows/<list_type>')
@user_required
def tmdb_shows(list_type):
    """
    Fetch TMDB show lists: popular, top_rated, airing_today, trending.
    Returns enriched results matching the discover card format.
    """
    VALID_TYPES = {
        'popular':      'tv/popular',
        'top_rated':    'tv/top_rated',
        'airing_today': 'tv/airing_today',
        'trending':     'trending/tv/week',
    }
    if list_type not in VALID_TYPES:
        return jsonify({'error': f'Unknown list type: {list_type}'}), 400

    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    if not tmdb_api_key:
        return jsonify({'error': 'TMDB API key not configured'}), 400

    with _tmdb_shows_cache_lock:
        entry = _tmdb_shows_cache.get(list_type)
    if entry and datetime.now() < entry['expires']:
        return jsonify({**entry['data'], 'cached': True})

    try:
        endpoint = VALID_TYPES[list_type]
        results = []
        for page in range(1, 3):  # 2 pages = up to 40 results
            url = f"https://api.themoviedb.org/3/{endpoint}?api_key={tmdb_api_key}&language=en-US&page={page}"
            r = requests.get(url, timeout=10)
            if not r.ok:
                break
            for item in r.json().get('results', []):
                results.append({
                    'id': item['id'],
                    'title': item.get('name') or item.get('title', ''),
                    'name': item.get('name') or item.get('title', ''),
                    'overview': item.get('overview', ''),
                    'poster_path': item.get('poster_path'),
                    'backdrop_path': item.get('backdrop_path'),
                    'release_date': item.get('first_air_date', ''),
                    'first_air_date': item.get('first_air_date', ''),
                    'vote_average': item.get('vote_average', 0),
                    'vote_count': item.get('vote_count', 0),
                    'media_type': 'tv',
                    'genre_ids': item.get('genre_ids', []),
                    'original_language': item.get('original_language', ''),
                    'origin_country': item.get('origin_country', []),
                    'popularity': item.get('popularity', 0),
                })

        add_db_status_and_episode_info(results, use_battery=False)

        response_data = {'success': True, 'results': results, 'total_results': len(results), 'list_type': list_type}
        with _tmdb_shows_cache_lock:
            _tmdb_shows_cache[list_type] = {'data': response_data, 'expires': datetime.now() + _TMDB_SHOWS_TTL}

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"TMDB shows {list_type} error: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/api/recommendations')
@user_required
def recommendations():
    """
    Personalised recommendations via Trakt. Gets TMDB IDs from Trakt then
    enriches with full TMDB data using the same pattern as Top 10/MDBList.
    """
    try:
        from content_checkers.trakt import get_trakt_config
        from concurrent.futures import ThreadPoolExecutor

        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')

        if not access_token or not client_id:
            return jsonify({'results': [], 'error': 'Trakt not authenticated', 'total_results': 0})

        trakt_headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}',
        }

        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        media_type = request.args.get('type', 'movie')
        endpoint_type = 'movies' if media_type == 'movie' else 'shows'

        resp = requests.get(
            f'https://api.trakt.tv/recommendations/{endpoint_type}?limit=40',
            headers=trakt_headers, timeout=15
        )
        resp.raise_for_status()
        trakt_items = resp.json()
        logging.info(f"Recommendations: {media_type} — Trakt returned {len(trakt_items)} items")

        # Build list of TMDB IDs, filtering watched items
        import os as _os
        _wh_path = _os.path.join(_os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'watch_history.db')
        exclude_imdb, exclude_tmdb = set(), set()
        if _os.path.exists(_wh_path):
            try:
                from database import get_db_connection as _gdc
                _wc = _gdc(_wh_path)
                exclude_imdb = {r[0] for r in _wc.execute("SELECT DISTINCT imdb_id FROM watch_history WHERE imdb_id IS NOT NULL").fetchall()}
                exclude_tmdb = {str(r[0]) for r in _wc.execute("SELECT DISTINCT tmdb_id FROM watch_history WHERE tmdb_id IS NOT NULL").fetchall()}
                _wc.close()
            except Exception:
                pass

        # Default Trakt response (no extended param): each item IS the media object
        # {ids: {tmdb, imdb, trakt, slug}, title, year}
        candidate_tmdb_ids = []
        for item in trakt_items:
            ids = item.get('ids', {})
            tmdb_id = ids.get('tmdb')
            imdb_id = ids.get('imdb', '')
            if not tmdb_id:
                continue
            if imdb_id in exclude_imdb or str(tmdb_id) in exclude_tmdb:
                continue
            candidate_tmdb_ids.append(tmdb_id)

        candidate_tmdb_ids = candidate_tmdb_ids[:20]
        logging.info(f"Recommendations: enriching {len(candidate_tmdb_ids)} TMDB IDs")

        if not tmdb_api_key or not candidate_tmdb_ids:
            return jsonify({'results': [], 'total_results': 0})

        tmdb_endpoint = 'movie' if media_type == 'movie' else 'tv'

        def fetch_tmdb_item(tmdb_id):
            try:
                r = requests.get(
                    f'https://api.themoviedb.org/3/{tmdb_endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                    timeout=5
                )
                if not r.ok:
                    return None
                d = r.json()
                genre_ids = [g['id'] for g in d.get('genres', [])]
                return {
                    'id': tmdb_id,
                    'title': d.get('title') or d.get('name', ''),
                    'name': d.get('name') or d.get('title', ''),
                    'overview': d.get('overview', ''),
                    'poster_path': d.get('poster_path'),
                    'backdrop_path': d.get('backdrop_path'),
                    'release_date': d.get('release_date', ''),
                    'first_air_date': d.get('first_air_date', ''),
                    'vote_average': d.get('vote_average', 0),
                    'vote_count': d.get('vote_count', 0),
                    'media_type': media_type,
                    'genre_ids': genre_ids,
                    'original_language': d.get('original_language', ''),
                }
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as ex:
            enriched = list(ex.map(fetch_tmdb_item, candidate_tmdb_ids))

        results = [r for r in enriched if r is not None]
        add_db_status_and_episode_info(results, use_battery=False)

        logging.info(f"Recommendations: {media_type}, returned {len(results)} results")
        return jsonify({'results': results, 'total_results': len(results)})

    except Exception as e:
        logging.error(f"Recommendations error: {e}", exc_info=True)
        return jsonify({'results': [], 'error': str(e), 'total_results': 0}), 500


@discover_bp.route('/api/genres')
@user_required
@cache_response('details')
def get_genres():
    """
    Get available genres from TMDB
    Cached for longer periods as genres don't change frequently
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400
        
        # Get movie genres
        movie_genres_url = f"https://api.themoviedb.org/3/genre/movie/list?api_key={tmdb_api_key}&language=en-US"
        movie_response = requests.get(movie_genres_url, timeout=10)
        movie_response.raise_for_status()
        movie_genres = movie_response.json()
        
        # Get TV genres
        tv_genres_url = f"https://api.themoviedb.org/3/genre/tv/list?api_key={tmdb_api_key}&language=en-US"
        tv_response = requests.get(tv_genres_url, timeout=10)
        tv_response.raise_for_status()
        tv_genres = tv_response.json()
        
        return jsonify({
            'movie_genres': movie_genres.get('genres', []),
            'tv_genres': tv_genres.get('genres', [])
        })
        
    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB genres API error: {e}")
        return jsonify({'error': 'Failed to fetch genres'}), 500
    except Exception as e:
        logging.error(f"Discover genres error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/keywords')
@user_required
def search_keywords():
    """
    Search for keywords using TMDB keyword search API
    Returns list of matching keywords with their IDs
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        query = request.args.get('query', '').strip()
        if not query or len(query) < 2:
            return jsonify({'keywords': []})

        # Search TMDB keywords
        url = f"https://api.themoviedb.org/3/search/keyword?api_key={tmdb_api_key}&query={query}&page=1"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Return keyword results (id and name)
        keywords = [{'id': kw['id'], 'name': kw['name']} for kw in data.get('results', [])[:20]]
        return jsonify({'keywords': keywords})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB keywords API error: {e}")
        return jsonify({'error': 'Failed to search keywords'}), 500
    except Exception as e:
        logging.error(f"Discover keywords error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/keyword/<int:keyword_id>')
@user_required
def get_keyword(keyword_id):
    """
    Get a single keyword by ID from TMDB
    Used to resolve keyword names when loading saved adaptive lists
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Get keyword details from TMDB
        url = f"https://api.themoviedb.org/3/keyword/{keyword_id}?api_key={tmdb_api_key}"
        response = requests.get(url, timeout=10)

        # A stale/deleted keyword ID (saved in an old adaptive list) is a normal,
        # expected 404 from TMDB — not a server error. Let the caller fall back to
        # displaying the raw ID instead of surfacing a 500 for a "not found" case.
        if response.status_code == 404:
            return jsonify({'id': keyword_id, 'name': '', 'error': 'Keyword not found'}), 404

        response.raise_for_status()
        data = response.json()

        return jsonify({'id': data.get('id'), 'name': data.get('name', '')})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB keyword API error: {e}")
        return jsonify({'error': 'Failed to fetch keyword'}), 502
    except Exception as e:
        logging.error(f"Get keyword error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/keywords/<string:media_type>/<int:tmdb_id>')
@user_required
def get_item_keywords(media_type, tmdb_id):
    """
    Fetch TMDB keyword IDs for a specific movie or TV show.
    Used by the discover page to support keyword filtering in list mode.
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'keywords': []}), 200

        endpoint = 'tv' if media_type == 'tv' else 'movie'
        url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/keywords?api_key={tmdb_api_key}"
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        data = response.json()
        keywords = data.get('keywords') or data.get('results') or []
        return jsonify({'keywords': [{'id': k['id'], 'name': k.get('name', '')} for k in keywords if isinstance(k, dict)]})

    except Exception as e:
        logging.debug(f"Item keywords fetch error for {media_type}/{tmdb_id}: {e}")
        return jsonify({'keywords': []}), 200


@discover_bp.route('/api/certifications')
@user_required
def get_certifications():
    """
    Get certifications for a specific region and media type from TMDB
    Used to populate certification filter dropdown based on selected watch region
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        region = request.args.get('region', 'US').upper()
        media_type = request.args.get('type', 'movie')  # 'movie' or 'tv'

        # Fetch certifications from TMDB
        if media_type == 'tv':
            url = f"https://api.themoviedb.org/3/certification/tv/list?api_key={tmdb_api_key}"
        else:
            url = f"https://api.themoviedb.org/3/certification/movie/list?api_key={tmdb_api_key}"

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Extract certifications for the specified region
        certifications_data = data.get('certifications', {})
        region_certs = certifications_data.get(region, [])

        # Format certifications for dropdown
        certifications = [
            {
                'certification': cert.get('certification', ''),
                'meaning': cert.get('meaning', ''),
                'order': cert.get('order', 999)
            }
            for cert in region_certs
        ]

        # Sort by order
        certifications.sort(key=lambda x: x['order'])

        return jsonify({'certifications': certifications, 'region': region})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB certifications API error: {e}")
        return jsonify({'error': 'Failed to fetch certifications'}), 500
    except Exception as e:
        logging.error(f"Get certifications error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/companies')
@user_required
def search_companies():
    """
    Search for production companies using TMDB company search API
    Returns list of matching companies with their IDs
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        query = request.args.get('query', '').strip()
        if not query or len(query) < 2:
            return jsonify({'companies': []})

        # Search TMDB companies
        url = f"https://api.themoviedb.org/3/search/company?api_key={tmdb_api_key}&query={query}&page=1"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Return company results (id and name)
        companies = [{'id': co['id'], 'name': co['name']} for co in data.get('results', [])[:20]]
        return jsonify({'companies': companies})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB companies API error: {e}")
        return jsonify({'error': 'Failed to search companies'}), 500
    except Exception as e:
        logging.error(f"Discover companies error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/providers')
@user_required
def get_providers():
    """
    Get available streaming providers from TMDB for a given region.
    Combines movie + TV providers, deduplicates by provider_id, sorts by display_priority.
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        region = request.args.get('region', 'US').upper()

        movie_url = f"https://api.themoviedb.org/3/watch/providers/movie?api_key={tmdb_api_key}&watch_region={region}&language=en-US"
        tv_url = f"https://api.themoviedb.org/3/watch/providers/tv?api_key={tmdb_api_key}&watch_region={region}&language=en-US"

        movie_resp = requests.get(movie_url, timeout=10)
        tv_resp = requests.get(tv_url, timeout=10)

        movie_providers = movie_resp.json().get('results', []) if movie_resp.ok else []
        tv_providers = tv_resp.json().get('results', []) if tv_resp.ok else []

        # Combine, deduplicate by provider_id, keep lowest display_priority
        seen = {}
        for p in movie_providers + tv_providers:
            pid = p['provider_id']
            priority = p.get('display_priority', 999)
            if pid not in seen or priority < seen[pid]['priority']:
                seen[pid] = {
                    'id': pid,
                    'name': p['provider_name'],
                    'logo_path': p.get('logo_path', ''),
                    'priority': priority
                }

        providers = sorted(seen.values(), key=lambda x: x['priority'])
        return jsonify({'providers': [{'id': p['id'], 'name': p['name'], 'logo_path': p['logo_path']} for p in providers]})

    except Exception as e:
        logging.error(f"Discover providers error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/networks')
@user_required
def search_networks():
    """
    Search for TV networks using cached TMDB daily export.
    Returns proper TMDB network IDs compatible with with_networks discover filter.
    """
    try:
        query = request.args.get('query', '').strip()
        if not query or len(query) < 2:
            return jsonify({'networks': []})

        all_networks = _get_network_cache()
        if not all_networks:
            return jsonify({'error': 'Network list unavailable'}), 503

        tmdb_api_key = get_setting('TMDB', 'api_key', '')

        q = query.lower()
        # Prioritise startswith matches, then contains
        starts = [n for n in all_networks if n['name'].lower().startswith(q)]
        contains = [n for n in all_networks if not n['name'].lower().startswith(q) and q in n['name'].lower()]
        results = (starts + contains)[:20]

        # Enrich with origin_country and logo_path
        results = _enrich_networks(results, tmdb_api_key)
        return jsonify({'networks': results})

    except Exception as e:
        logging.error(f"Discover networks error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/filter')
@user_required
def filter_content():
    """
    Advanced filtering using TMDB discover API
    Supports comprehensive filtering options including:
    - Genre selection (multi-select)
    - Rating ranges (TMDB, IMDb)
    - Vote count filtering
    - Year range with date filtering
    - Runtime range
    - Production company filtering
    - Release date filtering
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Get filter parameters
        media_type = request.args.get('type', 'movie')  # movie or tv
        page = int(request.args.get('page', 1))

        # If 'all' is passed, default to 'movie' (TMDB discover requires specific type)
        if media_type == 'all':
            media_type = 'movie'

        # Sort options
        sort_by = request.args.get('sort_by', 'popularity.desc')

        # Basic filters
        year_from = request.args.get('year_from', '')
        year_to = request.args.get('year_to', '')
        released_within = request.args.get('released_within', '')
        upcoming_days = request.args.get('upcoming_days', '')

        # Rating filters
        tmdb_rating_min = request.args.get('tmdb_rating_min', '')
        tmdb_rating_max = request.args.get('tmdb_rating_max', '')
        tmdb_votes_min = request.args.get('tmdb_votes_min', '')

        # Genre filters (include and exclude)
        selected_genres = request.args.get('genres', '')  # Comma-separated genre IDs to include
        excluded_genres = request.args.get('genres_exclude', '')  # Comma-separated genre IDs to exclude

        # Keyword filters (include and exclude)
        keywords = request.args.get('keywords', '')  # Comma-separated keyword IDs to include
        keywords_exclude = request.args.get('keywords_exclude', '')  # Comma-separated keyword IDs to exclude
        include_video = request.args.get('include_video', '')  # Include video results

        # Language, Country, Watch Provider filters (include and exclude)
        language = request.args.get('language', '')  # ISO 639-1 language codes
        language_exclude = request.args.get('language_exclude', '')
        country = request.args.get('country', '')  # ISO 3166-1 country codes
        country_exclude = request.args.get('country_exclude', '')
        watch_provider = request.args.get('watch_provider', '')  # TMDB watch provider IDs
        watch_provider_exclude = request.args.get('watch_provider_exclude', '')
        watch_region = request.args.get('watch_region', 'US')  # Region for watch providers (default US)

        # Runtime filters
        runtime_min = request.args.get('runtime_min', '')
        runtime_max = request.args.get('runtime_max', '')

        # Revenue filters (box office earnings, in millions)
        # Certification filters (range selector with gte/lte)
        certification_min = request.args.get('certification.gte', '')
        certification_max = request.args.get('certification.lte', '')
        certification_country = request.args.get('certification_country', '')

        # Legacy certification filters (for backward compatibility)
        certification = request.args.get('certification', '')
        certification_exclude = request.args.get('certification_exclude', '')

        # Production company (include and exclude)
        production_company = request.args.get('production_company', '')
        production_company_exclude = request.args.get('production_company_exclude', '')

        # TV Network filter (TV shows only)
        network = request.args.get('network', '')  # TMDB network IDs
        network_exclude = request.args.get('network_exclude', '')

        # Build TMDB discover URL
        if media_type == 'tv':
            base_url = f"https://api.themoviedb.org/3/discover/tv?api_key={tmdb_api_key}&language=en-US"
            date_field = 'first_air_date'
        else:
            base_url = f"https://api.themoviedb.org/3/discover/movie?api_key={tmdb_api_key}&language=en-US"
            date_field = 'primary_release_date'

        # Fix sort_by for release date - TMDB uses different field names per media type
        # Frontend sends "primary_release_date" but TV shows need "first_air_date"
        actual_sort_by = sort_by
        if 'primary_release_date' in sort_by or 'release_date' in sort_by:
            # Extract order (asc/desc)
            order = 'desc'
            if '.asc' in sort_by:
                order = 'asc'
            elif '.desc' in sort_by:
                order = 'desc'
            actual_sort_by = f"{date_field}.{order}"
            logging.info(f"[Discover Filter] Converted sort_by from '{sort_by}' to '{actual_sort_by}'")

        # Add parameters
        params = [f"page={page}", f"sort_by={actual_sort_by}"]

        # Genre filtering (include and exclude)
        # TMDB: comma = AND, pipe = OR. For include we want OR logic (match any genre)
        if selected_genres:
            # Convert comma to pipe for OR logic: "28,35" -> "28|35"
            genres_or = selected_genres.replace(',', '|')
            params.append(f"with_genres={genres_or}")
        if excluded_genres:
            # Exclude uses comma for AND logic (exclude ALL of these)
            params.append(f"without_genres={excluded_genres}")

        # Keyword filtering (include and exclude)
        # TMDB: with_keywords uses pipe for OR logic, comma for AND
        # NOTE: TMDB's discover API has limitations - it doesn't return all movies with a keyword
        # even though they appear on the website's keyword browse page (e.g., /keyword/271585-ufc/movie)
        # This is a known TMDB API limitation. For keyword-only searches, the /keyword/{id}/movies
        # endpoint would be more complete, but it doesn't support combining with other discover filters.
        if keywords:
            # Convert comma to pipe for OR logic: "123,456" -> "123|456"
            keywords_or = keywords.replace(',', '|')
            params.append(f"with_keywords={keywords_or}")
        if keywords_exclude:
            # Exclude uses comma for AND logic (exclude ALL of these)
            params.append(f"without_keywords={keywords_exclude}")
        
        # Include video results
        if include_video and include_video.lower() == 'true':
            params.append("include_video=true")

        # Language filtering (original language of the content)
        # TMDB: with_original_language uses pipe for OR logic
        if language:
            # Convert comma to pipe for OR logic: "en,es" -> "en|es"
            language_or = language.replace(',', '|')
            params.append(f"with_original_language={language_or}")
        # Note: TMDB doesn't have a direct "without_original_language" param

        # Country/Region filtering (origin country)
        # TMDB: with_origin_country uses pipe for OR logic
        if country:
            # Convert comma to pipe for OR logic: "US,GB" -> "US|GB"
            country_or = country.replace(',', '|')
            params.append(f"with_origin_country={country_or}")

        # Watch Provider filtering (streaming services like Netflix, Disney+, etc.)
        # TMDB: with_watch_providers uses pipe for OR logic
        if watch_provider:
            # Convert comma to pipe for OR logic: "8,337" -> "8|337"
            provider_or = watch_provider.replace(',', '|')
            params.append(f"with_watch_providers={provider_or}")
            params.append(f"watch_region={watch_region}")
            # Without this, with_watch_providers matches flatrate (subscription),
            # buy, OR rent — so picking e.g. Netflix also returns titles that are
            # merely purchasable/rentable there, not actually on the subscription.
            params.append("with_watch_monetization_types=flatrate")
        if watch_provider_exclude:
            params.append(f"without_watch_providers={watch_provider_exclude}")

        # TV Network filtering — IDs come from TMDB export, use with_networks (TV only)
        if network and media_type == 'tv':
            network_or = network.replace(',', '|')
            params.append(f"with_networks={network_or}")
        if network_exclude and media_type == 'tv':
            params.append(f"without_networks={network_exclude}")

        # Date filtering - Released Within and Upcoming can work together or separately
        # They take priority over year range
        # 0 means "today" for both filters:
        #   - Released Within 0 = start from today
        #   - Upcoming 0 = end at today
        from datetime import datetime, timedelta

        date_filter_applied = False
        today = datetime.now()
        date_start = None
        date_end = None

        # Parse both values first
        released_within_int = None
        upcoming_days_int = None

        if released_within is not None and released_within != '':
            try:
                released_within_int = int(released_within)
            except ValueError:
                pass

        if upcoming_days is not None and upcoming_days != '':
            try:
                upcoming_days_int = int(upcoming_days)
            except ValueError:
                pass

        # Released Within: items released in the past X days (sets start date)
        # 0 = start from today, positive = start from X days ago
        if released_within_int is not None:
            if released_within_int == 0:
                date_start = today
                date_filter_applied = True
                logging.info(f"[Discover Filter] Released Within 0 days (today): {date_start.strftime('%Y-%m-%d')}")
            elif released_within_int > 0:
                date_start = today - timedelta(days=released_within_int)
                date_filter_applied = True
                logging.info(f"[Discover Filter] Released Within {released_within_int} days from: {date_start.strftime('%Y-%m-%d')}")

        # Upcoming Releases: items releasing in the next X days (sets end date)
        # 0 = end at today, positive = end X days in the future
        if upcoming_days_int is not None:
            if upcoming_days_int == 0:
                date_end = today
                date_filter_applied = True
                logging.info(f"[Discover Filter] Upcoming 0 days (today): {date_end.strftime('%Y-%m-%d')}")
            elif upcoming_days_int > 0:
                date_end = today + timedelta(days=upcoming_days_int)
                date_filter_applied = True
                logging.info(f"[Discover Filter] Upcoming {upcoming_days_int} days until: {date_end.strftime('%Y-%m-%d')}")

        # Apply the combined date range
        if date_start:
            params.append(f"{date_field}.gte={date_start.strftime('%Y-%m-%d')}")
        if date_end:
            params.append(f"{date_field}.lte={date_end.strftime('%Y-%m-%d')}")

        if date_filter_applied:
            logging.info(f"[Discover Filter] Date range: {date_start.strftime('%Y-%m-%d') if date_start else 'any'} to {date_end.strftime('%Y-%m-%d') if date_end else 'any'}")

        # Year range filtering - only apply if no specific date filter is set and user entered values
        year_filter_applied = False
        if not date_filter_applied:
            if year_from:
                try:
                    year_int = int(year_from)
                    params.append(f"{date_field}.gte={year_int}-01-01")
                    year_filter_applied = True
                except ValueError:
                    pass
            if year_to:
                try:
                    year_int = int(year_to)
                    params.append(f"{date_field}.lte={year_int}-12-31")
                    year_filter_applied = True
                except ValueError:
                    pass

        # TMDB Rating filtering
        if tmdb_rating_min:
            try:
                rating = float(tmdb_rating_min)
                if rating > 0:
                    params.append(f"vote_average.gte={rating}")
            except ValueError:
                pass

        if tmdb_rating_max:
            try:
                rating = float(tmdb_rating_max)
                if rating < 10:
                    params.append(f"vote_average.lte={rating}")
            except ValueError:
                pass

        # TMDB Vote count filtering
        if tmdb_votes_min:
            try:
                votes = int(tmdb_votes_min)
                if votes > 0:
                    params.append(f"vote_count.gte={votes}")
            except ValueError:
                pass

        # Runtime filtering (only for movies)
        if media_type == 'movie':
            if runtime_min:
                try:
                    runtime = int(runtime_min)
                    if runtime > 0:
                        params.append(f"with_runtime.gte={runtime}")
                except ValueError:
                    pass

            if runtime_max:
                try:
                    runtime = int(runtime_max)
                    if runtime < 300:
                        params.append(f"with_runtime.lte={runtime}")
                except ValueError:
                    pass

        # Certification filtering (content rating)
        # TMDB: certification parameter has confusing behavior - "certification=G" returns G and LESS restrictive
        # For exact matches, use both .gte and .lte with same value
        # NOTE: Certifications are region-specific and tied to watch_region parameter

        # New range selector format (certification.gte and certification.lte from frontend)
        if certification_min or certification_max:
            country = certification_country or watch_region or 'US'
            if certification_min:
                params.append(f"certification.gte={certification_min}")
                logging.info(f"[Discover Filter] Certification min: {certification_min}")
            if certification_max:
                params.append(f"certification.lte={certification_max}")
                logging.info(f"[Discover Filter] Certification max: {certification_max}")
            params.append(f"certification_country={country}")
        # Legacy format (comma-separated certifications)
        elif certification:
            # Split multiple certifications (comma-separated from frontend)
            certs = [c.strip() for c in certification.split(',') if c.strip()]

            if len(certs) == 1:
                # Single certification - use exact match with .gte and .lte
                params.append(f"certification.gte={certs[0]}")
                params.append(f"certification.lte={certs[0]}")
                params.append(f"certification_country={watch_region}")
                logging.info(f"[Discover Filter] Single certification filter: {certs[0]}")
            else:
                # Multiple certifications - use range approach
                # TMDB API limitation: Can't do exact OR matching, so we get a range
                # Example: Selecting "G" and "PG-13" will return G, PG, and PG-13
                # Define certification order (US ratings)
                cert_order_us = ['G', 'PG', 'PG-13', 'R', 'NC-17']
                cert_order_tv = ['TV-Y', 'TV-Y7', 'TV-G', 'TV-PG', 'TV-14', 'TV-MA']

                # Determine which order to use based on first certification
                if any(c.startswith('TV-') for c in certs):
                    cert_order = cert_order_tv
                else:
                    cert_order = cert_order_us

                # Get indices of selected certifications
                cert_indices = []
                for cert in certs:
                    if cert in cert_order:
                        cert_indices.append(cert_order.index(cert))

                if cert_indices:
                    # Get range from lowest to highest selected certification
                    min_cert = cert_order[min(cert_indices)]
                    max_cert = cert_order[max(cert_indices)]

                    params.append(f"certification.gte={min_cert}")
                    params.append(f"certification.lte={max_cert}")
                    params.append(f"certification_country={watch_region}")
                    logging.info(f"[Discover Filter] Multiple certifications: {','.join(certs)} -> range {min_cert} to {max_cert}")
                else:
                    # Fallback if certifications not recognized
                    logging.warning(f"[Discover Filter] Unrecognized certifications: {','.join(certs)}")
        # Note: TMDB doesn't have a direct "without_certification" param for exclusion

        # Production company filtering (now accepts direct TMDB company IDs)
        # TMDB: with_companies uses pipe for OR logic (match any company)
        if production_company:
            # production_company can now be comma-separated TMDB company IDs
            # Filter out any non-numeric values for safety
            company_ids = [c.strip() for c in production_company.split(',') if c.strip().isdigit()]
            if company_ids:
                # Use pipe for OR logic: "420,2" -> "420|2" (Marvel OR Disney)
                params.append(f"with_companies={'|'.join(company_ids)}")
        if production_company_exclude:
            # Exclude certain production companies (comma = AND = exclude all of these)
            exclude_ids = [c.strip() for c in production_company_exclude.split(',') if c.strip().isdigit()]
            if exclude_ids:
                params.append(f"without_companies={','.join(exclude_ids)}")

        # When filtering by rating, add a minimum vote count floor to exclude
        # items with very few votes (which can have misleading ratings)
        # Only apply this if user hasn't explicitly set their own vote count filter
        # Use a low threshold (10) to be less restrictive
        # IMPORTANT: Don't apply this for upcoming content - future shows have no votes yet!
        if tmdb_rating_min and not tmdb_votes_min and upcoming_days_int is None:
            try:
                rating = float(tmdb_rating_min)
                if rating > 0:
                    params.append("vote_count.gte=10")
            except ValueError:
                pass

        url = base_url + "&" + "&".join(params)

        # Log filters (hide API key for security)
        url_without_key = url.split('api_key=')[0] + 'api_key=***' + (url.split('api_key=')[1].split('&', 1)[1] if '&' in url.split('api_key=')[1] else '')
        cert_info = f"{certification_min}-{certification_max}" if (certification_min or certification_max) else (certification or 'none')
        logging.debug(f"[Discover] Type={media_type}, Page={page}, Genres={selected_genres}, Cert={cert_info}, URL={url_without_key}")

        response = requests.get(url, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        
        # Add database status information
        results = data.get('results', [])
        total_results = data.get('total_results', 0)

        logging.debug(f"[Discover] Page {page}: {len(results)} results (total: {total_results})")
        
        # Enrich with digital release dates
        if results:
            results = enrich_with_digital_dates(results, media_type, tmdb_api_key)
            data['results'] = results
        
        if results:
            # Normalize media_type for frontend
            for item in results:
                if 'media_type' not in item:
                    item['media_type'] = media_type

            # Add database status and episode info
            add_db_status_and_episode_info(results, use_battery=False)
        
        return jsonify(data)

    except requests.exceptions.RequestException as e:
        logging.error(f"Discover filter TMDB API error: {e}")
        return jsonify({'error': f'TMDB API error: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Discover filter error: {e}")
        return jsonify({'error': f'Filter error: {str(e)}'}), 500


@discover_bp.route('/api/add', methods=['POST'])
@user_required
def add_to_library():
    """
    Add discovered content to the library/wanted list
    Integrates with existing content source system
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        tmdb_id = data.get('tmdb_id')
        media_type = data.get('media_type')
        title = data.get('title', '')

        if not tmdb_id or not media_type:
            return jsonify({'error': 'tmdb_id and media_type are required'}), 400

        # Get TMDB API key for fetching additional details
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Fetch IMDB ID from TMDB
        if media_type == 'tv':
            external_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
        else:
            external_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={tmdb_api_key}"

        external_response = requests.get(external_url, timeout=10)
        external_response.raise_for_status()
        external_data = external_response.json()

        imdb_id = external_data.get('imdb_id')

        if not imdb_id:
            return jsonify({'error': 'Could not find IMDB ID for this content'}), 404

        # Return IMDB ID for the frontend to handle via scraper
        # The scraper page already has robust add-to-library functionality
        return jsonify({
            'success': True,
            'message': f'Found IMDB ID for {title}',
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'media_type': media_type,
            'title': title
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB external IDs API error: {e}")
        return jsonify({'error': 'Failed to fetch content details'}), 500
    except Exception as e:
        logging.error(f"Discover add error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@discover_bp.route('/api/details/<int:tmdb_id>')
@user_required
@cache_response('details')
def get_details(tmdb_id):
    """
    Get detailed information about a specific item
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        media_type = request.args.get('type', 'movie')

        if media_type == 'tv':
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,videos,external_ids"
        else:
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,videos,external_ids"

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Fetch digital release date separately — do not overwrite theatrical release_date
        digital_release_date_1895 = None
        if media_type == 'movie':
            digital_date = get_digital_release_date(tmdb_id, 'movie', tmdb_api_key)
            if digital_date:
                digital_release_date_1895 = digital_date

        # Get certification based on user's preferred region
        certification_region = get_setting('TMDB', 'certification_region', 'US')
        certification = get_certification(tmdb_id, media_type, tmdb_api_key, certification_region)

        # Get database status
        tmdb_id_str = str(tmdb_id)
        db_statuses_result = get_cached_db_statuses([tmdb_id_str])  # Returns Dict[str, str]
        db_status = 'missing'
        if db_statuses_result:
            db_status = db_statuses_result.get(tmdb_id_str, 'missing')  # type: ignore[arg-type]

        # Build response
        result = {
            'id': data['id'],
            'title': data.get('title') or data.get('name', ''),
            'overview': data.get('overview', ''),
            'poster_path': data.get('poster_path'),
            'backdrop_path': data.get('backdrop_path'),
            'release_date': data.get('release_date') or data.get('first_air_date', ''),
            'vote_average': data.get('vote_average', 0),
            'vote_count': data.get('vote_count', 0),
            'runtime': data.get('runtime') or data.get('episode_run_time', [None])[0] if data.get('episode_run_time') else None,
            'genres': [g['name'] for g in data.get('genres', [])],
            'status': data.get('status', ''),
            'tagline': data.get('tagline', ''),
            'certification': certification,
            'imdb_id': data.get('external_ids', {}).get('imdb_id') or data.get('imdb_id'),
            'media_type': media_type,
            'db_status': db_status,
            'digital_release_date': digital_release_date_1895,
        }

        # Add TV-specific fields
        if media_type == 'tv':
            result['number_of_seasons'] = data.get('number_of_seasons', 0)
            result['number_of_episodes'] = data.get('number_of_episodes', 0)
            result['in_production'] = data.get('in_production', False)

        # Add credits (limited)
        credits = data.get('credits', {})
        result['cast'] = [
            {'name': c['name'], 'character': c.get('character', ''), 'profile_path': c.get('profile_path')}
            for c in credits.get('cast', [])[:10]
        ]

        # Add trailer
        videos = data.get('videos', {}).get('results', [])
        trailers = [v for v in videos if v.get('type') == 'Trailer' and v.get('site') == 'YouTube']
        if trailers:
            result['trailer_key'] = trailers[0].get('key')

        return jsonify(result)

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB details API error: {e}")
        return jsonify({'error': 'Failed to fetch content details'}), 500
    except Exception as e:
        logging.error(f"Discover details error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


# =============================================================================
# Adaptive List API Endpoints
# =============================================================================

@discover_bp.route('/api/adaptive-lists')
@user_required
def get_adaptive_lists():
    """
    Get all configured adaptive lists
    Returns list of adaptive list configurations
    """
    try:
        from utilities.settings import get_all_settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        adaptive_lists = []
        for source_id, source_data in content_sources.items():
            if source_id.startswith('Adaptive List'):
                lists = source_data.get('lists', [])
                for idx, list_config in enumerate(lists):
                    adaptive_lists.append({
                        'id': f"{source_id}_{idx}",
                        'source_id': source_id,
                        'index': idx,
                        'name': list_config.get('name', 'Unnamed List'),
                        'media_type': list_config.get('media_type', 'movie'),
                        'filters': list_config.get('filters', {}),
                        'enabled': source_data.get('enabled', False)
                    })

        return jsonify({
            'success': True,
            'lists': adaptive_lists
        })

    except Exception as e:
        logging.error(f"Error getting adaptive lists: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/api/adaptive-lists', methods=['POST'])
@user_required
def save_adaptive_list():
    """
    Save a new adaptive list as its own content source.
    Each adaptive list is a separate content source with the list name as display_name.
    Expects JSON body with: name, media_type, filters
    """
    try:
        from utilities.settings import get_all_settings, set_setting

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        name = data.get('name', '').strip()
        media_type = data.get('media_type', 'movie')
        filters = data.get('filters', {})

        if not name:
            return jsonify({'error': 'List name is required'}), 400

        if not filters:
            return jsonify({'error': 'At least one filter is required'}), 400

        # Get current settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        # Find next available Adaptive List ID
        # Format is: "Adaptive List_1", "Adaptive List_2", etc.
        existing_ids = [k for k in content_sources.keys() if k.startswith('Adaptive List_')]
        next_num = 1
        if existing_ids:
            nums = []
            for k in existing_ids:
                # Split by underscore: "Adaptive List_1" -> ["Adaptive List", "1"]
                parts = k.split('_')
                if len(parts) >= 2 and parts[-1].isdigit():
                    nums.append(int(parts[-1]))
            if nums:
                next_num = max(nums) + 1

        new_source_id = f'Adaptive List_{next_num}'
        
        # Check if this ID already exists (shouldn't happen, but be safe)
        if new_source_id in content_sources:
            logging.error(f"[Adaptive List] ID {new_source_id} already exists! This shouldn't happen.")
            return jsonify({'error': 'Failed to generate unique ID. Please try again.'}), 500

        # Create the new adaptive list content source
        new_source = {
            'type': 'Adaptive List',
            'enabled': True,
            'display_name': name,
            'media_type': media_type,
            'filters': filters,
            'versions': ['Default'],  # Array format like other content sources
            'allow_specials': False,
            'custom_symlink_subfolder': '',
            'cutoff_date': '',
            'exclude_genres': [],
            'list_length_limit': 0,
            'plex_labels': {
                'enabled': False,
                'label_mode': 'list_name',
                'fixed_label': ''
            }
        }

        # Save the new content source
        set_setting('Content Sources', new_source_id, new_source)

        logging.info(f"[Adaptive List] Created new adaptive list '{name}' as {new_source_id} (type: {media_type})")

        return jsonify({
            'success': True,
            'message': f"Adaptive list '{name}' created successfully",
            'source_id': new_source_id
        })

    except Exception as e:
        logging.error(f"Error saving adaptive list: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/api/adaptive-lists/<source_id>', methods=['PUT'])
@user_required
def update_adaptive_list(source_id):
    """
    Update an existing adaptive list content source.
    Expects JSON body with: name, media_type, filters
    """
    try:
        from utilities.settings import get_all_settings, set_setting

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        name = data.get('name', '').strip()
        media_type = data.get('media_type', 'movie')
        filters = data.get('filters', {})

        if not name:
            return jsonify({'error': 'List name is required'}), 400

        # Get current settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        if source_id not in content_sources:
            return jsonify({'error': 'Adaptive List source not found'}), 404

        source_config = content_sources[source_id]

        # Verify it's an Adaptive List type
        if source_config.get('type') != 'Adaptive List':
            return jsonify({'error': 'Source is not an Adaptive List'}), 400

        # Update the source - preserve existing settings
        source_config['display_name'] = name
        source_config['media_type'] = media_type
        source_config['filters'] = filters

        # Save the updated settings
        set_setting('Content Sources', source_id, source_config)

        logging.info(f"[Adaptive List] Updated '{name}' ({source_id})")

        return jsonify({
            'success': True,
            'message': f"Adaptive list '{name}' updated successfully"
        })

    except Exception as e:
        logging.error(f"Error updating adaptive list: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/api/adaptive-lists/<source_id>', methods=['DELETE'])
@user_required
def delete_adaptive_list(source_id):
    """
    Delete an adaptive list content source
    """
    try:
        from utilities.settings import get_all_settings, set_setting
        from queues.config_manager import load_config, save_config

        # Get current settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        if source_id not in content_sources:
            return jsonify({'error': 'Adaptive List source not found'}), 404

        source_config = content_sources[source_id]

        # Verify it's an Adaptive List type
        if source_config.get('type') != 'Adaptive List':
            return jsonify({'error': 'Source is not an Adaptive List'}), 400

        deleted_name = source_config.get('display_name', source_id)

        # Remove the content source entirely
        del content_sources[source_id]

        # Save the updated content sources
        config = load_config()
        config['Content Sources'] = content_sources
        save_config(config)

        logging.info(f"[Adaptive List] Deleted '{deleted_name}' ({source_id})")

        return jsonify({
            'success': True,
            'message': f"Adaptive list '{deleted_name}' deleted successfully"
        })

    except Exception as e:
        logging.error(f"Error deleting adaptive list: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/api/adaptive-lists/<source_id>')
@user_required
def get_adaptive_list(source_id):
    """
    Get a specific adaptive list configuration by source_id.
    Used when editing a list in the discover page.
    """
    try:
        from utilities.settings import get_all_settings

        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        if source_id not in content_sources:
            return jsonify({'error': 'Adaptive List source not found'}), 404

        source_config = content_sources[source_id]

        # Verify it's an Adaptive List type
        if source_config.get('type') != 'Adaptive List':
            return jsonify({'error': 'Source is not an Adaptive List'}), 400

        return jsonify({
            'success': True,
            'list': {
                'source_id': source_id,
                'name': source_config.get('display_name', ''),
                'media_type': source_config.get('media_type', 'movie'),
                'filters': source_config.get('filters', {})
            }
        })

    except Exception as e:
        logging.error(f"Error getting adaptive list: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Filter Presets API
# =============================================================================

def load_presets():
    """Load filter presets from JSON file"""
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logging.error(f"Error loading presets: {e}")
        return {}

def save_presets(presets):
    """Save filter presets to JSON file"""
    try:
        # Ensure config directory exists
        config_dir = os.path.dirname(PRESETS_FILE)
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)

        with open(PRESETS_FILE, 'w') as f:
            json.dump(presets, f, indent=2)
        return True
    except Exception as e:
        logging.error(f"Error saving presets: {e}")
        return False

@discover_bp.route('/api/presets', methods=['GET'])
@user_required
def get_presets():
    """Get all saved filter presets"""
    try:
        presets = load_presets()
        return jsonify({
            'success': True,
            'presets': presets
        })
    except Exception as e:
        logging.error(f"Error getting presets: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets', methods=['POST'])
@user_required
def save_preset():
    """Save a new filter preset"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Preset name is required'}), 400

        filters = data.get('filters', {})
        if not filters:
            return jsonify({'error': 'No filters to save'}), 400

        # Generate a unique ID for the preset
        import time
        preset_id = f"preset_{int(time.time() * 1000)}"

        # Load existing presets
        presets = load_presets()

        # Add new preset
        presets[preset_id] = {
            'name': name,
            'filters': filters,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
        }

        # Save presets
        if save_presets(presets):
            return jsonify({
                'success': True,
                'preset_id': preset_id,
                'message': f'Preset "{name}" saved successfully'
            })
        else:
            return jsonify({'error': 'Failed to save preset'}), 500

    except Exception as e:
        logging.error(f"Error saving preset: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets/<preset_id>', methods=['GET'])
@user_required
def get_preset(preset_id):
    """Get a specific filter preset by ID"""
    try:
        presets = load_presets()

        if preset_id not in presets:
            return jsonify({'error': 'Preset not found'}), 404

        return jsonify({
            'success': True,
            'preset': presets[preset_id]
        })
    except Exception as e:
        logging.error(f"Error getting preset: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets/<preset_id>', methods=['DELETE'])
@user_required
def delete_preset(preset_id):
    """Delete a filter preset"""
    try:
        presets = load_presets()

        if preset_id not in presets:
            return jsonify({'error': 'Preset not found'}), 404

        preset_name = presets[preset_id].get('name', preset_id)
        del presets[preset_id]

        if save_presets(presets):
            return jsonify({
                'success': True,
                'message': f'Preset "{preset_name}" deleted successfully'
            })
        else:
            return jsonify({'error': 'Failed to delete preset'}), 500

    except Exception as e:
        logging.error(f"Error deleting preset: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets/<preset_id>', methods=['PUT'])
@user_required
def update_preset(preset_id):
    """Update an existing filter preset's filters"""
    try:
        presets = load_presets()

        if preset_id not in presets:
            return jsonify({'error': 'Preset not found'}), 404

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        if 'filters' in data:
            presets[preset_id]['filters'] = data['filters']
        if 'name' in data:
            presets[preset_id]['name'] = data['name']

        import time
        presets[preset_id]['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')

        if save_presets(presets):
            return jsonify({
                'success': True,
                'message': f'Preset "{presets[preset_id]["name"]}" updated successfully'
            })
        else:
            return jsonify({'error': 'Failed to update preset'}), 500

    except Exception as e:
        logging.error(f"Error updating preset: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/details/<int:tmdb_id>/<media_type>')
@user_required
def details_page(tmdb_id, media_type):
    """
    Detail page for missing/not-in-library content
    Shows TMDB data with option to request/search
    """
    from .utils import is_user_system_enabled

    tmdb_api_key = get_setting('TMDB', 'api_key', '')

    # Check permissions - User and Admin can search, Requester cannot
    if not is_user_system_enabled():
        has_user_permissions = True
    else:
        has_user_permissions = current_user.is_authenticated and current_user.role in ['admin', 'user']

    return render_template('discover_details.html',
                         tmdb_id=tmdb_id,
                         media_type=media_type,
                         tmdb_configured=bool(tmdb_api_key),
                         has_user_permissions=has_user_permissions)

@discover_bp.route('/details/<int:tmdb_id>/<media_type>/data')
@user_required
def details_data(tmdb_id, media_type):
    """
    API endpoint to fetch TMDB details for missing content
    Returns full metadata for display on detail page

    For discover details, always use TMDB API to ensure complete metadata
    (posters, backdrops, ratings, etc.) regardless of library status.
    Battery is better suited for library detail pages.
    """
    try:
        # Get TMDB API key early (needed for all requests)
        tmdb_api_key = get_setting('TMDB', 'api_key', '')

        # Check library status for db_status field
        in_library = False
        try:
            from database.core import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            if media_type == 'tv':
                cursor.execute("SELECT COUNT(*) FROM media_items WHERE tmdb_id = ? AND type = 'episode' LIMIT 1", (str(tmdb_id),))
            else:
                cursor.execute("SELECT COUNT(*) FROM media_items WHERE tmdb_id = ? AND type = 'movie' LIMIT 1", (str(tmdb_id),))
            in_library = cursor.fetchone()[0] > 0
            cursor.close()
            conn.close()
        except Exception as e:
            logging.debug(f"Library check failed for TMDB {tmdb_id}: {e}")

        # Always use TMDB API for discover details to ensure complete metadata
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Determine endpoint based on media type
        if media_type == 'tv':
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,external_ids,content_ratings"
        else:
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,external_ids,release_dates"

        logging.info(f"Fetching TMDB metadata for {tmdb_id}")
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        # For TV shows: Use TVDB for seasons/episodes if TVDB API key is configured
        # This ensures proper season splits (not TMDB absolute numbering for anime)
        tvdb_slug = None
        if media_type == 'tv':
            tvdb_api_key = get_setting('TVDB', 'api_key', '')
            if tvdb_api_key:
                try:
                    from cli_battery.app import tvdb_client

                    # Get IMDb ID from TMDB response
                    imdb_id_from_tmdb = data.get('external_ids', {}).get('imdb_id')
                    if imdb_id_from_tmdb:
                        logging.info(f"Fetching TVDB seasons for {tmdb_id} (IMDb: {imdb_id_from_tmdb}) for proper season structure")

                        # Fetch show data from TVDB (returns full show metadata including seasons)
                        tvdb_show_data = tvdb_client.get_show_data(imdb_id_from_tmdb)

                        if tvdb_show_data:
                            tvdb_slug = tvdb_show_data.get('ids', {}).get('slug')

                        if tvdb_show_data and 'seasons' in tvdb_show_data:
                            # TVDB seasons format: {season_number: {'episode_count': N, 'episodes': {...}}}
                            # TMDB seasons format: [{'season_number': N, 'episode_count': M}, ...]
                            # Convert TVDB dict format to TMDB list format
                            tvdb_seasons_dict = tvdb_show_data['seasons']
                            tvdb_seasons_list = []
                            for season_num, season_data in tvdb_seasons_dict.items():
                                if isinstance(season_data, dict):
                                    tvdb_seasons_list.append({
                                        'season_number': int(season_num) if str(season_num).isdigit() else season_num,
                                        'episode_count': season_data.get('episode_count', 0),
                                        'name': f"Season {season_num}",
                                    })

                            # Sort by season number
                            tvdb_seasons_list.sort(key=lambda s: s['season_number'] if isinstance(s['season_number'], int) else 999)

                            # Replace TMDB seasons with TVDB seasons (proper structure, English titles)
                            data['seasons'] = tvdb_seasons_list
                            data['number_of_seasons'] = len([s for s in tvdb_seasons_list if s.get('season_number', 0) > 0])
                            # Recalculate total episode count from TVDB seasons (exclude season 0)
                            data['number_of_episodes'] = sum([s.get('episode_count', 0) for s in tvdb_seasons_list if s.get('season_number', 0) > 0])
                            logging.info(f"✅ Using TVDB seasons for proper structure ({data['number_of_seasons']} seasons, {data['number_of_episodes']} total episodes)")
                        else:
                            logging.debug(f"TVDB returned no season data, using TMDB seasons as fallback")
                except Exception as e:
                    logging.warning(f"Failed to fetch TVDB seasons for {tmdb_id}: {e}")
                    import traceback
                    logging.debug(f"TVDB error traceback: {traceback.format_exc()}")
                    # Continue with TMDB seasons as fallback

        # Enrich with digital release date if it's a movie — store separately, don't overwrite theatrical date
        digital_release_date = None
        if media_type == 'movie':
            digital_date = get_digital_release_date(tmdb_id, 'movie', tmdb_api_key)
            if digital_date and digital_date.get('date'):
                digital_release_date = digital_date['date']

        # Build response object
        if media_type == 'tv':
            # Get content rating (US)
            content_rating = ''
            if 'content_ratings' in data and 'results' in data['content_ratings']:
                for rating in data['content_ratings']['results']:
                    if rating.get('iso_3166_1') == 'US':
                        content_rating = rating.get('rating', '')
                        break

            # Safe extraction of list fields (handle both TMDB and Battery formats)
            genres = data.get('genres', [])
            genres_list = [g['name'] for g in genres] if isinstance(genres, list) and all(isinstance(g, dict) for g in genres) else (genres if isinstance(genres, list) else [])

            networks = data.get('networks', [])
            networks_list = [n['name'] for n in networks] if isinstance(networks, list) and all(isinstance(n, dict) for n in networks) else (networks if isinstance(networks, list) else [])

            created_by = data.get('created_by', [])
            created_by_list = [c['name'] for c in created_by] if isinstance(created_by, list) and all(isinstance(c, dict) for c in created_by) else (created_by if isinstance(created_by, list) else [])

            result = {
                'id': data.get('id'),
                'title': data.get('name') or data.get('title'),
                'original_title': data.get('original_name') or data.get('original_title'),
                'year': str(data.get('first_air_date', ''))[:4] if data.get('first_air_date') else '',
                'overview': data.get('overview'),
                'poster_path': data.get('poster_path'),
                'backdrop_path': data.get('backdrop_path'),
                'vote_average': data.get('vote_average'),
                'vote_count': data.get('vote_count'),
                'genres': genres_list,
                'first_air_date': data.get('first_air_date'),
                'last_air_date': data.get('last_air_date'),
                'status': data.get('status'),
                'number_of_seasons': data.get('number_of_seasons'),
                'number_of_episodes': data.get('number_of_episodes'),
                'episode_run_time': data.get('episode_run_time', []) if isinstance(data.get('episode_run_time'), list) else [],
                'networks': networks_list,
                'created_by': created_by_list,
                'content_rating': content_rating,
                'media_type': 'tv',
                'tmdb_id': data.get('id'),
                'imdb_id': data.get('external_ids', {}).get('imdb_id') if isinstance(data.get('external_ids'), dict) else None,
                'tvdb_id': data.get('external_ids', {}).get('tvdb_id') if isinstance(data.get('external_ids'), dict) else None,
                'tvdb_slug': tvdb_slug,
                'seasons': data.get('seasons', []) if isinstance(data.get('seasons'), list) else []
            }
        else:
            # Get certification (US)
            certification = ''
            if 'release_dates' in data and 'results' in data['release_dates']:
                for release in data['release_dates']['results']:
                    if release.get('iso_3166_1') == 'US':
                        for rd in release.get('release_dates', []):
                            if rd.get('certification'):
                                certification = rd.get('certification')
                                break
                        break

            # Safe extraction of list fields (handle both TMDB and Battery formats)
            genres = data.get('genres', [])
            genres_list = [g['name'] for g in genres] if isinstance(genres, list) and all(isinstance(g, dict) for g in genres) else (genres if isinstance(genres, list) else [])

            result = {
                'id': data.get('id'),
                'title': data.get('title'),
                'original_title': data.get('original_title'),
                'year': str(data.get('release_date', ''))[:4] if data.get('release_date') else '',
                'overview': data.get('overview'),
                'poster_path': data.get('poster_path'),
                'backdrop_path': data.get('backdrop_path'),
                'vote_average': data.get('vote_average'),
                'vote_count': data.get('vote_count'),
                'genres': genres_list,
                'release_date': data.get('release_date'),
                'runtime': data.get('runtime'),
                'status': data.get('status'),
                'budget': data.get('budget'),
                'revenue': data.get('revenue'),
                'tagline': data.get('tagline'),
                'certification': certification,
                'media_type': 'movie',
                'tmdb_id': data.get('id'),
                'imdb_id': data.get('imdb_id') or (data.get('external_ids', {}).get('imdb_id') if isinstance(data.get('external_ids'), dict) else None),
                'digital_release_date': digital_release_date,
            }

        # Get cast (top 10) - safe extraction
        if 'credits' in data and isinstance(data.get('credits'), dict) and 'cast' in data['credits']:
            cast = data['credits']['cast']
            if isinstance(cast, list):
                result['cast'] = [
                    {'name': c.get('name', ''), 'character': c.get('character', ''), 'profile_path': c.get('profile_path')}
                    for c in cast[:10] if isinstance(c, dict)
                ]

        # Get director/creator - safe extraction
        if 'credits' in data and isinstance(data.get('credits'), dict) and 'crew' in data['credits']:
            crew = data['credits']['crew']
            if isinstance(crew, list):
                directors = [c['name'] for c in crew if isinstance(c, dict) and c.get('job') == 'Director']
                result['directors'] = directors

        # Add default version from settings for scraping
        result['default_version'] = get_default_version()

        # Add database status (use Battery for accurate counts on detail page)
        add_db_status_and_episode_info([result], use_battery=True)

        return jsonify(result)

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB API error for {media_type}/{tmdb_id}: {e}")
        return jsonify({'error': 'Failed to fetch data from TMDB'}), 500
    except Exception as e:
        logging.error(f"Error fetching details for {media_type}/{tmdb_id}: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/details/<int:tmdb_id>/tv/season/<int:season_number>')
@user_required
def season_episodes(tmdb_id, season_number):
    """
    API endpoint to fetch episode details for a specific season
    Uses TVDB if API key configured (for proper season structure), otherwise TMDB
    Returns episode list with air dates, names, and runtime, merged with database info
    """
    try:
        from database.core import get_db_connection

        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Try TVDB first if API key is configured (for proper season structure)
        tvdb_api_key = get_setting('TVDB', 'api_key', '')
        data = None

        logging.info(f"Season episodes request for TMDB {tmdb_id}, season {season_number}")
        logging.info(f"TVDB API key configured: {bool(tvdb_api_key)}")

        # Fetch episode list from TMDB live API.
        # If TVDB is configured and TMDB doesn't have this season number (e.g. anime where TVDB
        # splits seasons differently), fall back to TVDB episode data so that episode numbers
        # shown on Discover always match the TVDB structure used by the scraper/battery.
        logging.info(f"Fetching TMDB episodes for season {season_number} of TMDB ID {tmdb_id}")
        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={tmdb_api_key}&language=en-US"
        response = requests.get(url, timeout=15)

        tmdb_404 = response.status_code == 404
        if not tmdb_404:
            response.raise_for_status()
            data = response.json()
        else:
            data = None
            logging.info(f"TMDB returned 404 for season {season_number} of TMDB ID {tmdb_id} — will try TVDB fallback")

        # Pre-fetch TVDB data once if TVDB is configured (used for supplement and/or fallback)
        tvdb_ep_map = {}
        if tvdb_api_key:
            try:
                from cli_battery.app import tvdb_client
                from cli_battery.app.direct_api import DirectAPI

                imdb_id, source = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type='tv')
                if imdb_id:
                    tvdb_show_data = tvdb_client.get_show_data(imdb_id)
                    if tvdb_show_data and 'seasons' in tvdb_show_data:
                        tvdb_seasons = tvdb_show_data['seasons']
                        season_data = tvdb_seasons.get(season_number) or tvdb_seasons.get(str(season_number))
                        if season_data and 'episodes' in season_data:
                            for ep_num, ep_data in season_data['episodes'].items():
                                if isinstance(ep_data, dict):
                                    key = int(ep_num) if str(ep_num).isdigit() else ep_num
                                    tvdb_ep_map[key] = ep_data
            except Exception as e:
                logging.debug(f"TVDB pre-fetch failed for season {season_number}: {e}")

        if tvdb_ep_map:
            # TVDB is configured and has episode data for this season — TVDB is authoritative for
            # the episode list (correct count and numbering per TVDB structure). TMDB data is used
            # only to supplement stills and overviews where available.
            tmdb_ep_map = {}
            if data:
                for ep in data.get('episodes', []):
                    ep_num = ep.get('episode_number')
                    if ep_num is not None:
                        tmdb_ep_map[ep_num] = ep

            tvdb_episodes_list = []
            for ep_num in sorted(tvdb_ep_map.keys()):
                ep_data = tvdb_ep_map[ep_num]
                air_date = ep_data.get('first_aired', '')
                if air_date and 'T' in air_date:
                    air_date = air_date.split('T')[0]
                tmdb_ep = tmdb_ep_map.get(ep_num, {})
                tvdb_episodes_list.append({
                    'episode_number': ep_num,
                    'name': ep_data.get('title') or ep_data.get('name') or tmdb_ep.get('name'),
                    'air_date': air_date or tmdb_ep.get('air_date') or None,
                    'runtime': ep_data.get('runtime') or tmdb_ep.get('runtime'),
                    'overview': tmdb_ep.get('overview') or ep_data.get('overview'),
                    'still_path': tmdb_ep.get('still_path'),
                })

            data = {
                'season_number': season_number,
                'name': data.get('name') if data else f"Season {season_number}",
                'air_date': data.get('air_date') if data else (tvdb_episodes_list[0]['air_date'] if tvdb_episodes_list else None),
                'episodes': tvdb_episodes_list,
            }
            logging.info(f"Using TVDB episode structure for season {season_number} of TMDB ID {tmdb_id} ({len(tvdb_episodes_list)} episodes)")
        elif tmdb_404:
            # TMDB 404 and no TVDB data — nothing to show
            logging.warning(f"TMDB 404 and no TVDB data for season {season_number} of TMDB ID {tmdb_id}")
            return jsonify({'error': 'Season not found on TMDB or TVDB'}), 404

        # Query database for episodes in this show/season
        conn = get_db_connection()
        db_episodes_query = """
            SELECT
                id,
                season_number,
                episode_number,
                episode_title,
                state,
                version,
                filled_by_file,
                collected_at,
                release_date,
                airtime,
                content_source,
                content_source_detail,
                imdb_id,
                location_basename,
                location_on_disk,
                size
            FROM media_items
            WHERE tmdb_id = ? AND season_number = ? AND type = 'episode' AND (ghostlisted = 0 OR ghostlisted IS NULL)
            ORDER BY episode_number ASC
        """
        try:
            db_episodes = conn.execute(db_episodes_query, (str(tmdb_id), season_number)).fetchall()
        finally:
            conn.close()

        # Create a lookup dict for DB episodes by episode_number
        # Handle multiple files per episode (store as list)
        db_episodes_map = {}
        for db_ep in db_episodes:
            ep_num = db_ep['episode_number']
            if ep_num not in db_episodes_map:
                db_episodes_map[ep_num] = []
            db_episodes_map[ep_num].append(dict(db_ep))

        # Build episode list merged with DB data
        episodes = []
        for ep in data.get('episodes', []):
            ep_num = ep.get('episode_number')
            episode_data = {
                'episode_number': ep_num,
                'name': ep.get('name'),
                'air_date': ep.get('air_date'),
                'runtime': ep.get('runtime'),
                'overview': ep.get('overview'),
                'still_path': ep.get('still_path')
            }

            # Merge DB data if this episode exists in database
            if ep_num in db_episodes_map:
                db_files = db_episodes_map[ep_num]
                # Use the first file's data for primary episode info
                primary_file = db_files[0]

                episode_data['db_data'] = {
                    'state': primary_file['state'],
                    'version': primary_file['version'],
                    'collected_at': primary_file['collected_at'],
                    'content_source': primary_file['content_source'],
                    'content_source_detail': primary_file['content_source_detail'],
                    'imdb_id': primary_file['imdb_id'],
                    'files': [
                        {
                            'id': f['id'],
                            'basename': f['location_basename'],
                            'path': f['location_on_disk'],
                            'size': f['size'],
                            'version': f['version'],
                            'filled_by_file': f['filled_by_file'],
                            'collected_at': f['collected_at']
                        }
                        for f in db_files
                    ]
                }

            episodes.append(episode_data)

        return jsonify({
            'season_number': data.get('season_number'),
            'name': data.get('name'),
            'air_date': data.get('air_date'),
            'episodes': episodes
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB API error for tv/{tmdb_id}/season/{season_number}: {e}")
        return jsonify({'error': 'Failed to fetch season data from TMDB'}), 500
    except Exception as e:
        logging.error(f"Error fetching season {season_number} for tv/{tmdb_id}: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# MDBList Integration API
# =============================================================================

@discover_bp.route('/api/mdblist/status')
@user_required
def mdblist_status():
    """
    Check if MDBList is configured and test connection
    """
    try:
        if not is_mdblist_configured():
            return jsonify({
                'configured': False,
                'message': 'MDBList API key not configured'
            })

        # Test the connection
        result = test_mdblist_connection()
        return jsonify({
            'configured': True,
            'connected': result.get('success', False),
            'message': result.get('message') or result.get('error', '')
        })

    except Exception as e:
        logging.error(f"MDBList status check error: {e}")
        return jsonify({
            'configured': False,
            'error': str(e)
        })


@discover_bp.route('/api/mdblist/lists')
@user_required
def mdblist_available_lists():
    """
    Get available MDBList curated lists.
    Note: Public curated lists don't require an MDBList API key.
    """
    try:
        result = fetch_mdblist_top_lists()
        return jsonify(result)

    except Exception as e:
        logging.error(f"Error fetching MDBList available lists: {e}")
        return jsonify({'error': str(e), 'lists': []}), 500


@discover_bp.route('/api/mdblist/list/<list_key>')
@user_required
def mdblist_list_items(list_key):
    """
    Get items from a specific MDBList curated list.
    Returns items with TMDB IDs that can be used to fetch full metadata.
    Note: Public curated lists don't require an MDBList API key.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        limit = request.args.get('limit', 20, type=int)
        media_type_filter = request.args.get('type', 'all')  # all, movie, tv

        # Fetch list items from MDBList
        result = fetch_list_items(list_key, limit=min(limit, 50))

        if 'error' in result:
            return jsonify(result), 400

        items = result.get('items', [])

        # Filter by media type if requested
        if media_type_filter != 'all':
            items = [item for item in items if item.get('media_type') == media_type_filter]

        # Enrich items with TMDB data and database status
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            # Get database status for all items upfront
            tmdb_ids = [str(item['tmdb_id']) for item in items if item.get('tmdb_id')]
            db_statuses = get_cached_db_statuses(tmdb_ids) if tmdb_ids else {}

            def enrich_item(item, idx):
                """Helper function to enrich a single item with TMDB data"""
                tmdb_id = item.get('tmdb_id')
                if not tmdb_id:
                    return None

                media_type = item.get('media_type', 'movie')
                rank = item.get('rank', idx + 1)

                try:
                    if media_type == 'tv':
                        tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    else:
                        tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"

                    tmdb_response = requests.get(tmdb_url, timeout=5)
                    if tmdb_response.status_code == 200:
                        tmdb_data = tmdb_response.json()
                        # Extract genre IDs from genres array (TMDB details returns genres as objects)
                        genres = tmdb_data.get('genres', [])
                        genre_ids = [g['id'] for g in genres if isinstance(g, dict) and 'id' in g]
                        # Extract production company IDs
                        companies = tmdb_data.get('production_companies', [])
                        company_ids = [c['id'] for c in companies if isinstance(c, dict) and 'id' in c]
                        return {
                            'id': tmdb_id,
                            'title': tmdb_data.get('title') or tmdb_data.get('name'),
                            'overview': tmdb_data.get('overview', ''),
                            'poster_path': tmdb_data.get('poster_path'),
                            'backdrop_path': tmdb_data.get('backdrop_path'),
                            'release_date': tmdb_data.get('release_date') or tmdb_data.get('first_air_date', ''),
                            'vote_average': tmdb_data.get('vote_average', 0),
                            'vote_count': tmdb_data.get('vote_count', 0),
                            'media_type': media_type,
                            'genre_ids': genre_ids,
                            'original_language': tmdb_data.get('original_language', ''),
                            'origin_country': tmdb_data.get('origin_country', []),
                            'company_ids': company_ids,
                            'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                            'rank': rank,
                            '_idx': idx  # Keep for ordering
                        }
                    else:
                        return {
                            'id': tmdb_id,
                            'title': item.get('title', 'Unknown'),
                            'media_type': media_type,
                            'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                            'rank': rank,
                            '_idx': idx
                        }
                except Exception as e:
                    logging.debug(f"TMDB lookup failed for {tmdb_id}: {e}")
                    return {
                        'id': tmdb_id,
                        'title': item.get('title', 'Unknown'),
                        'media_type': media_type,
                        'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                        'rank': rank,
                        '_idx': idx
                    }

            # Use ThreadPoolExecutor for parallel TMDB requests
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(enrich_item, item, idx): idx for idx, item in enumerate(items)}
                results_map = {}

                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        enriched = future.result()
                        if enriched:
                            results_map[idx] = enriched
                    except Exception as e:
                        logging.warning(f"Error enriching MDBList item: {e}")

                # Sort by original index to maintain order
                enriched_items = [results_map[k] for k in sorted(results_map.keys())]
                # Clean up internal index field
                for item in enriched_items:
                    item.pop('_idx', None)

        return jsonify({
            'success': True,
            'list_name': result.get('list_name', list_key),
            'results': enriched_items,
            'total_results': len(enriched_items)
        })

    except Exception as e:
        logging.error(f"Error fetching MDBList list {list_key}: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# MDBList Personal Lists API (Requires API Key)
# =============================================================================

@discover_bp.route('/api/mdblist/personal-lists')
@user_required
def mdblist_personal_lists():
    """Return the user's personal MDBList lists, cached for 10 minutes."""
    try:
        with _mdblist_personal_cache_lock:
            entry = _mdblist_personal_cache.get('index')
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True})

        if not is_mdblist_configured():
            return jsonify({'success': False, 'lists': [], 'error': 'MDBList API key not configured'})

        result = get_mdblist_user_lists()
        if 'error' in result and not result.get('lists'):
            return jsonify({'success': False, 'lists': [], 'error': result['error']})

        # MDBList returns a separate entry per mediatype for mixed lists — deduplicate by slug,
        # keeping the entry with the highest item count (which covers both types).
        seen_slugs = {}
        for lst in result.get('lists', []):
            slug = lst.get('slug', '')
            if slug not in seen_slugs or lst.get('items_count', 0) > seen_slugs[slug].get('items_count', 0):
                seen_slugs[slug] = lst
        lists = list(seen_slugs.values())
        with _mdblist_personal_cache_lock:
            _mdblist_personal_cache['index'] = {
                'data': lists,
                'expires': datetime.now() + _MDBLIST_PERSONAL_INDEX_TTL,
            }
        return jsonify({'success': True, 'lists': lists})
    except Exception as e:
        logging.error(f"[MDBList Personal] Failed to fetch personal lists: {e}")
        with _mdblist_personal_cache_lock:
            entry = _mdblist_personal_cache.get('index')
            if entry:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True, 'stale': True})
        return jsonify({'success': False, 'lists': [], 'error': str(e)}), 500


@discover_bp.route('/api/mdblist/personal-list/<list_id>')
@user_required
def mdblist_personal_list_items(list_id):
    """Fetch items from a user's personal MDBList list by ID, enriched with TMDB data."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        cache_key = str(list_id)

        with _mdblist_personal_cache_lock:
            entry = _mdblist_personal_cache.get(cache_key)
            if entry and datetime.now() < entry['expires'] and entry['data']:
                return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})
            elif entry and not entry['data']:
                # Evict empty cache entry so we re-fetch with correct endpoint
                del _mdblist_personal_cache[cache_key]

        # Look up slug + username from the index cache
        slug = None
        username = None
        with _mdblist_personal_cache_lock:
            index_entry = _mdblist_personal_cache.get('index')
            if index_entry:
                for lst in index_entry.get('data', []):
                    if str(lst.get('id')) == str(list_id):
                        slug = lst.get('slug')
                        username = lst.get('username')
                        break

        logging.info(f"[MDBList Personal] Fetching list_id={list_id} slug={slug} username={username}")
        result = fetch_custom_list_items(list_id, limit=100, username=username, slug=slug)
        logging.info(f"[MDBList Personal] fetch_custom_list_items returned: success={result.get('success')}, items={len(result.get('items', []))}, error={result.get('error')}")
        if 'error' in result and not result.get('items'):
            return jsonify({'success': False, 'results': [], 'error': result.get('error', 'Unknown error')}), 400

        items = result.get('items', [])

        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            tmdb_ids = [str(i['tmdb_id']) for i in items if i.get('tmdb_id')]
            db_statuses = get_cached_db_statuses(tmdb_ids) if tmdb_ids else {}

            def enrich_item(item, idx):
                tmdb_id = item.get('tmdb_id')
                if not tmdb_id:
                    return None
                media_type = item.get('media_type', 'movie')
                endpoint = 'tv' if media_type == 'tv' else 'movie'
                try:
                    r = requests.get(
                        f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                        timeout=8
                    )
                    if r.ok:
                        d = r.json()
                        return {
                            'id': tmdb_id,
                            'title': d.get('title') or d.get('name', ''),
                            'name': d.get('name') or d.get('title', ''),
                            'overview': d.get('overview', ''),
                            'poster_path': d.get('poster_path'),
                            'backdrop_path': d.get('backdrop_path'),
                            'release_date': d.get('release_date', ''),
                            'first_air_date': d.get('first_air_date', ''),
                            'vote_average': d.get('vote_average', 0),
                            'vote_count': d.get('vote_count', 0),
                            'media_type': media_type,
                            'genre_ids': [g['id'] for g in d.get('genres', [])],
                            'original_language': d.get('original_language', ''),
                            'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                            'rank': item.get('rank', idx + 1),
                            '_idx': idx,
                        }
                except Exception:
                    pass
                return {
                    'id': tmdb_id,
                    'title': item.get('title', 'Unknown'),
                    'media_type': media_type,
                    'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                    'rank': item.get('rank', idx + 1),
                    '_idx': idx,
                }

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(enrich_item, item, idx): idx for idx, item in enumerate(items)}
                results_map = {}
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        enriched = future.result()
                        if enriched:
                            results_map[idx] = enriched
                    except Exception as e:
                        logging.warning(f"Error enriching MDBList personal list item: {e}")

            enriched_items = [results_map[k] for k in sorted(results_map.keys())]
            for item in enriched_items:
                item.pop('_idx', None)

        if enriched_items:  # Don't cache empty results — likely upstream API error
            with _mdblist_personal_cache_lock:
                _mdblist_personal_cache[cache_key] = {
                    'data': enriched_items,
                    'expires': datetime.now() + _MDBLIST_PERSONAL_ITEMS_TTL,
                }
            _save_mdblist_personal_file_cache()

        logging.info(f"[MDBList Personal] Returning {len(enriched_items)} enriched items for list_id={list_id}")
        return jsonify({'success': True, 'results': enriched_items, 'total_results': len(enriched_items)})

    except Exception as e:
        logging.error(f"[MDBList Personal] Error fetching list {list_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


def _scrob_items_to_discover(raw_items, media_type_filter, tmdb_api_key):
    """Shared helper: take raw Scrob media items, enrich with TMDB, return
    discover-shaped results. Mirrors _trakt_items_to_discover — Discover display
    only needs tmdb_id (unlike the Wanted-queue path, which needs imdb_id via
    process_scrob_items), so this intentionally skips that conversion.
    """
    from concurrent.futures import ThreadPoolExecutor

    candidates = []
    for item in raw_items:
        # Scrob's list-item shape wraps the media dict under 'media'; discover/
        # special-list endpoints return the media dict directly (same as Trakt).
        media = item.get('media', item)
        raw_type = (media.get('type') or '').lower()
        if raw_type == 'movie':
            mt = 'movie'
            tmdb_id = media.get('tmdb_id')
        elif raw_type == 'episode':
            # Episode tmdb_id is a separate ID namespace from shows — use the
            # parent show's tmdb_id, same reasoning as process_scrob_items.
            mt = 'tv'
            tmdb_id = media.get('show_tmdb_id')
        elif raw_type in ('series', 'show', 'tv'):
            mt = 'tv'
            tmdb_id = media.get('tmdb_id')
        else:
            continue

        if not tmdb_id:
            continue
        if media_type_filter not in ('all', '') and mt != media_type_filter:
            continue
        candidates.append({'tmdb_id': tmdb_id, 'media_type': mt})

    if not tmdb_api_key or not candidates:
        return []

    def fetch_one(c):
        tmdb_id, mt = c['tmdb_id'], c['media_type']
        endpoint = 'tv' if mt == 'tv' else 'movie'
        try:
            r = requests.get(
                f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                timeout=8
            )
            if not r.ok:
                return None
            d = r.json()
            return {
                'id': tmdb_id,
                'title': d.get('title') or d.get('name', ''),
                'name': d.get('name') or d.get('title', ''),
                'overview': d.get('overview', ''),
                'poster_path': d.get('poster_path'),
                'backdrop_path': d.get('backdrop_path'),
                'release_date': d.get('release_date', ''),
                'first_air_date': d.get('first_air_date', ''),
                'vote_average': d.get('vote_average', 0),
                'vote_count': d.get('vote_count', 0),
                'media_type': mt,
                'genre_ids': [g['id'] for g in d.get('genres', [])],
                'original_language': d.get('original_language', ''),
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as ex:
        enriched = list(ex.map(fetch_one, candidates))

    results = [r for r in enriched if r is not None]
    add_db_status_and_episode_info(results, use_battery=False)
    return results


@discover_bp.route('/api/scrob/lists')
@user_required
def scrob_personal_lists():
    """Return the user's Scrob custom lists, cached for 10 minutes."""
    try:
        with _scrob_cache_lock:
            entry = _scrob_cache.get('lists-index')
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True})

        from content_checkers.scrob import get_scrob_config, _scrob_get
        if not get_scrob_config():
            return jsonify({'success': False, 'lists': [], 'error': 'Scrob not configured'})

        data = _scrob_get('/lists')
        if data is None:
            return jsonify({'success': False, 'lists': [], 'error': 'Failed to fetch Scrob lists'})

        lists = [
            {'id': lst['id'], 'name': lst.get('name', f"List {lst['id']}"), 'item_count': lst.get('item_count', 0)}
            for lst in data.get('lists', [])
        ]
        with _scrob_cache_lock:
            _scrob_cache['lists-index'] = {'data': lists, 'expires': datetime.now() + _SCROB_LISTS_INDEX_TTL}
        return jsonify({'success': True, 'lists': lists})
    except Exception as e:
        logging.error(f"[Scrob Personal] Failed to fetch lists: {e}")
        with _scrob_cache_lock:
            entry = _scrob_cache.get('lists-index')
            if entry:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True, 'stale': True})
        return jsonify({'success': False, 'lists': [], 'error': str(e)}), 500


@discover_bp.route('/api/scrob/list/<list_id>')
@user_required
def scrob_list_items(list_id):
    """Fetch items from a Scrob custom list by ID, enriched with TMDB data."""
    try:
        media_type_filter = request.args.get('type', 'all')
        cache_key = f'list:{list_id}:{media_type_filter}'

        with _scrob_cache_lock:
            entry = _scrob_cache.get(cache_key)
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        from content_checkers.scrob import get_scrob_config, _scrob_get
        if not get_scrob_config():
            return jsonify({'success': False, 'results': [], 'error': 'Scrob not configured'})

        data = _scrob_get(f'/lists/{list_id}')
        if data is None:
            return jsonify({'success': False, 'results': [], 'error': 'Failed to fetch Scrob list'})

        raw_items = data.get('items', [])
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        results = _scrob_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        if results:  # Don't cache empty results — likely upstream error or genuinely-empty list
            with _scrob_cache_lock:
                _scrob_cache[cache_key] = {'data': results, 'expires': datetime.now() + _SCROB_LIST_ITEMS_TTL}

        return jsonify({'success': True, 'results': results, 'total_results': len(results)})
    except Exception as e:
        logging.error(f"[Scrob Personal] Error fetching list {list_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500


@discover_bp.route('/api/scrob/special/<list_type>')
@user_required
def scrob_special_list(list_type):
    """Fetch a Scrob special list (Trending, Popular, etc.) and return discover-shaped results."""
    try:
        from content_checkers.scrob import get_scrob_config, _scrob_get, SPECIAL_LIST_ENDPOINTS

        if list_type not in SPECIAL_LIST_ENDPOINTS:
            return jsonify({'success': False, 'results': [], 'error': f"Unknown special list type '{list_type}'"}), 404

        media_type_filter = request.args.get('type', 'all')  # all, movie, tv
        cache_key = f'special:{list_type}:{media_type_filter}'

        with _scrob_cache_lock:
            entry = _scrob_cache.get(cache_key)
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        if not get_scrob_config():
            return jsonify({'success': False, 'results': [], 'error': 'Scrob not configured'})

        api_paths = SPECIAL_LIST_ENDPOINTS[list_type]
        endpoints_to_call = []
        if media_type_filter in ('all', 'movie') and api_paths.get('movies'):
            endpoints_to_call.append(api_paths['movies'])
        if media_type_filter in ('all', 'tv') and api_paths.get('shows'):
            endpoints_to_call.append(api_paths['shows'])

        raw_items = []
        for endpoint_path, endpoint_params in endpoints_to_call:
            data = _scrob_get(endpoint_path, params=endpoint_params)
            if data:
                raw_items.extend(data.get('results', []))

        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        results = _scrob_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        if results:
            with _scrob_cache_lock:
                _scrob_cache[cache_key] = {'data': results, 'expires': datetime.now() + _SCROB_SPECIAL_TTL}

        return jsonify({'success': True, 'results': results, 'total_results': len(results)})
    except Exception as e:
        logging.error(f"[Scrob Special] Error fetching '{list_type}': {e}", exc_info=True)
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500


# =============================================================================
# FlixPatrol Integration API (No API Key Required)
# =============================================================================

@discover_bp.route('/api/trakt/lists')
@user_required
def trakt_personal_lists():
    """Return the user's personal Trakt lists, cached for 10 minutes."""
    try:
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get('index')
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True})

        from content_checkers.trakt import get_trakt_config
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')
        if not access_token or not client_id:
            return jsonify({'success': False, 'lists': [], 'error': 'Trakt not authenticated'})
        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}',
        }
        resp = requests.get('https://api.trakt.tv/users/me/lists', headers=headers, timeout=15)
        resp.raise_for_status()
        lists = []
        for item in resp.json():
            lists.append({
                'name': item.get('name', ''),
                'slug': item.get('ids', {}).get('slug', ''),
                'item_count': item.get('item_count', 0),
                'description': item.get('description', ''),
                'updated_at': item.get('updated_at', ''),
            })
        with _trakt_cache_lock:
            _trakt_lists_cache['index'] = {
                'data': lists,
                'expires': datetime.now() + _TRAKT_LISTS_INDEX_TTL,
            }
        return jsonify({'success': True, 'lists': lists})
    except Exception as e:
        logging.error(f"[Personal] Failed to fetch Trakt personal lists: {e}")
        # Return stale cache if available rather than an error
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get('index')
            if entry:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True, 'stale': True})
        return jsonify({'success': False, 'lists': [], 'error': str(e)}), 500


def _trakt_items_to_discover(items, media_type_filter, tmdb_api_key):
    """Shared helper: take raw Trakt list items, enrich with TMDB, return discover-shaped results."""
    from concurrent.futures import ThreadPoolExecutor

    # Normalise — Trakt list items wrap the media object under 'movie'/'show'
    candidates = []
    for item in items:
        mt = item.get('type', '')  # 'movie' or 'show' or 'episode'
        if mt == 'episode':
            mt = 'show'
        if media_type_filter not in ('all', '') and mt != media_type_filter:
            continue
        media_obj = item.get('movie') or item.get('show') or item  # top-level for special lists
        ids = media_obj.get('ids', {})
        tmdb_id = ids.get('tmdb')
        if not tmdb_id:
            continue
        candidates.append({'tmdb_id': tmdb_id, 'media_type': 'tv' if mt in ('show', 'tv') else 'movie'})

    if not tmdb_api_key or not candidates:
        return []

    def fetch_one(c):
        tmdb_id, mt = c['tmdb_id'], c['media_type']
        endpoint = 'tv' if mt == 'tv' else 'movie'
        try:
            r = requests.get(
                f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                timeout=8
            )
            if not r.ok:
                return None
            d = r.json()
            return {
                'id': tmdb_id,
                'title': d.get('title') or d.get('name', ''),
                'name': d.get('name') or d.get('title', ''),
                'overview': d.get('overview', ''),
                'poster_path': d.get('poster_path'),
                'backdrop_path': d.get('backdrop_path'),
                'release_date': d.get('release_date', ''),
                'first_air_date': d.get('first_air_date', ''),
                'vote_average': d.get('vote_average', 0),
                'vote_count': d.get('vote_count', 0),
                'media_type': mt,
                'genre_ids': [g['id'] for g in d.get('genres', [])],
                'original_language': d.get('original_language', ''),
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as ex:
        enriched = list(ex.map(fetch_one, candidates))

    results = [r for r in enriched if r is not None]
    add_db_status_and_episode_info(results, use_battery=False)
    return results


@discover_bp.route('/api/trakt/special/<list_type>')
@user_required
def trakt_special_list(list_type):
    """Fetch a Trakt special list (trending, popular, etc.) and return discover-shaped results.

    Caching strategy: content-hash fingerprint + 1-hour hard TTL.
    Special lists have no updated_at timestamp, so we hash the ordered list of
    TMDB IDs to detect real changes. If the content hash is unchanged we extend
    the TTL instead of re-fetching. On any fetch failure we return stale cache.
    """
    try:
        from content_checkers.trakt import get_trakt_config
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')
        if not access_token or not client_id:
            return jsonify({'success': False, 'results': [], 'error': 'Trakt not authenticated'})

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}',
        }
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        media_type_filter = request.args.get('type', 'all')  # all, movie, tv

        SPECIAL_ENDPOINTS = {
            'trending':    {'movie': '/movies/trending',          'show': '/shows/trending'},
            'popular':     {'movie': '/movies/popular',           'show': '/shows/popular'},
            'favorited':   {'movie': '/movies/favorited/weekly',  'show': '/shows/favorited/weekly'},
            'played':      {'movie': '/movies/played/weekly',     'show': '/shows/played/weekly'},
            'watched':     {'movie': '/movies/watched/weekly',    'show': '/shows/watched/weekly'},
            'collected':   {'movie': '/movies/collected/weekly',  'show': '/shows/collected/weekly'},
            'anticipated': {'movie': '/movies/anticipated',       'show': '/shows/anticipated'},
            'boxoffice':   {'movie': '/movies/boxoffice',         'show': None},
            'recommendations': {'movie': '/recommendations/movies', 'show': '/recommendations/shows'},
        }

        if list_type not in SPECIAL_ENDPOINTS:
            return jsonify({'success': False, 'results': [], 'error': f'Unknown list type: {list_type}'}), 400

        cache_key = f'special:{list_type}:{media_type_filter}'

        # Check hard TTL — serve cache if still fresh
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get(cache_key)
        if entry and datetime.now() < entry.get('expires', datetime.min):
            logging.debug(f"[Personal] Special list '{list_type}' serving from cache (TTL fresh)")
            return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        endpoints = SPECIAL_ENDPOINTS[list_type]
        fetch_types = []
        if media_type_filter in ('all', 'movie') and endpoints.get('movie'):
            fetch_types.append(('movie', endpoints['movie']))
        if media_type_filter in ('all', 'tv') and endpoints.get('show'):
            fetch_types.append(('tv', endpoints['show']))

        raw_items = []
        fetch_ok = True
        for mt, ep in fetch_types:
            try:
                r = requests.get(
                    f'https://api.trakt.tv{ep}?limit=40&extended=full',
                    headers=headers, timeout=15
                )
                r.raise_for_status()
                for item in r.json():
                    media_obj = item.get('movie') or item.get('show') or item
                    raw_items.append({'type': mt, 'movie' if mt == 'movie' else 'show': media_obj})
            except Exception as e:
                logging.warning(f"[Personal] Special list {list_type}/{mt} fetch error: {e}")
                fetch_ok = False

        # On fetch failure, return stale cache if available
        if not fetch_ok and not raw_items and entry:
            logging.warning(f"[Personal] Special list '{list_type}' fetch failed, returning stale cache")
            return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'stale': True})

        # Compute content hash from ordered TMDB IDs to detect real list changes
        tmdb_ids = []
        for item in raw_items:
            media_obj = item.get('movie') or item.get('show') or {}
            tid = (media_obj.get('ids') or {}).get('tmdb')
            if tid:
                tmdb_ids.append(str(tid))
        content_hash = hashlib.md5(','.join(tmdb_ids).encode()).hexdigest()

        # If content unchanged since last fetch, just extend TTL without re-enriching
        if entry and entry.get('content_hash') == content_hash:
            logging.debug(f"[Personal] Special list '{list_type}' content unchanged, extending TTL")
            with _trakt_cache_lock:
                _trakt_lists_cache[cache_key]['expires'] = datetime.now() + _TRAKT_SPECIAL_TTL
            return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        results = _trakt_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        with _trakt_cache_lock:
            _trakt_lists_cache[cache_key] = {
                'data': results,
                'content_hash': content_hash,
                'expires': datetime.now() + _TRAKT_SPECIAL_TTL,
            }
        logging.info(f"[Personal] Special list '{list_type}' cached ({len(results)} results, hash={content_hash[:8]})")
        return jsonify({'success': True, 'results': results, 'total_results': len(results)})

    except Exception as e:
        logging.error(f"[Personal] Special list error ({list_type}): {e}", exc_info=True)
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500


@discover_bp.route('/api/trakt/mylist/<slug>')
@user_required
def trakt_mylist(slug):
    """Fetch items from a user's personal Trakt list by slug, with updated_at-based cache invalidation."""
    try:
        from content_checkers.trakt import get_trakt_config
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')
        if not access_token or not client_id:
            return jsonify({'success': False, 'results': [], 'error': 'Trakt not authenticated'})

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}',
        }
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        media_type_filter = request.args.get('type', 'all')
        cache_key = f'{slug}:{media_type_filter}'
        remote_updated_at = ''  # resolved lazily below

        # Check cache
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get(cache_key)

        if entry:
            ttl_fresh = datetime.now() < entry.get('expires', datetime.min)
            if ttl_fresh:
                # TTL still valid — serve immediately without any network call
                logging.debug(f"[Personal] Serving cached mylist '{slug}' (TTL fresh)")
                return jsonify({'success': True, 'results': entry['data'],
                               'total_results': len(entry['data']), 'cached': True})

            # TTL expired — check updated_at before doing full re-fetch
            cached_updated_at = entry.get('updated_at', '')
            remote_updated_at = ''
            try:
                meta_r = requests.get(
                    f'https://api.trakt.tv/users/me/lists/{slug}',
                    headers=headers, timeout=10
                )
                if meta_r.status_code == 200:
                    remote_updated_at = meta_r.json().get('updated_at', '')
            except Exception:
                pass

            if cached_updated_at and remote_updated_at and cached_updated_at == remote_updated_at:
                # Content unchanged — extend TTL and serve cache
                with _trakt_cache_lock:
                    _trakt_lists_cache[cache_key]['expires'] = datetime.now() + _TRAKT_MYLIST_TTL
                _save_mylist_file_cache()
                logging.debug(f"[Personal] Mylist '{slug}' updated_at unchanged, extending TTL")
                return jsonify({'success': True, 'results': entry['data'],
                               'total_results': len(entry['data']), 'cached': True})

        # Full fetch — fetch updated_at if not already retrieved (no prior cache existed)
        if not remote_updated_at:
            try:
                meta_r = requests.get(
                    f'https://api.trakt.tv/users/me/lists/{slug}',
                    headers=headers, timeout=10
                )
                if meta_r.status_code == 200:
                    remote_updated_at = meta_r.json().get('updated_at', '')
            except Exception:
                pass

        raw_items = []
        page = 1
        while True:
            r = requests.get(
                f'https://api.trakt.tv/users/me/lists/{slug}/items?extended=full&limit=100&page={page}',
                headers=headers, timeout=15
            )
            r.raise_for_status()
            page_items = r.json()
            if not page_items:
                break
            raw_items.extend(page_items)
            total_pages = int(r.headers.get('X-Pagination-Page-Count', 1))
            if page >= total_pages:
                break
            page += 1

        results = _trakt_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        with _trakt_cache_lock:
            _trakt_lists_cache[cache_key] = {
                'data': results,
                'updated_at': remote_updated_at,
                'expires': datetime.now() + _TRAKT_MYLIST_TTL,
            }
        _save_mylist_file_cache()

        return jsonify({'success': True, 'results': results, 'total_results': len(results)})

    except Exception as e:
        logging.error(f"[Personal] My list error ({slug}): {e}", exc_info=True)
        # Return stale cache if available
        cache_key = f'{slug}:{request.args.get("type", "all")}'
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get(cache_key)
            if entry:
                return jsonify({'success': True, 'results': entry['data'],
                               'total_results': len(entry['data']), 'cached': True, 'stale': True})
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500


@discover_bp.route('/api/flixpatrol/cache/clear', methods=['POST'])
@user_required
def flixpatrol_cache_clear():
    """Clear the FlixPatrol in-memory cache."""
    with _flixpatrol_cache_lock:
        _flixpatrol_cache.clear()
    return jsonify({'success': True, 'message': 'FlixPatrol cache cleared'})


@discover_bp.route('/api/flixpatrol/platforms')
@user_required
def flixpatrol_platforms():
    """
    Get available streaming platforms for FlixPatrol Top 10.
    No API key required.
    """
    try:
        result = get_flixpatrol_platforms()
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error getting FlixPatrol platforms: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/api/flixpatrol/top10/<platform>')
@user_required
def flixpatrol_top10(platform):
    """
    Get Top 10 content from FlixPatrol for a specific streaming platform.
    No API key required - scrapes public FlixPatrol data.

    Args:
        platform: Platform key (netflix, disney, amazon, hbo, apple, paramount, hulu, peacock)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        media_type_filter = request.args.get('type', 'all')  # all, movie, tv

        # Serve from cache if fresh
        fp_cache_key = f'{platform}:{media_type_filter}'
        with _flixpatrol_cache_lock:
            fp_entry = _flixpatrol_cache.get(fp_cache_key)
        if fp_entry and datetime.now() < fp_entry['expires']:
            logging.debug(f"[FlixPatrol] Serving from cache ({fp_cache_key})")
            return jsonify({**fp_entry['data'], 'cached': True})

        # Fetch Top 10 from FlixPatrol
        result = fetch_flixpatrol_top10(platform, media_type=media_type_filter)

        if 'error' in result:
            return jsonify(result), 400

        items = result.get('items', [])

        # Enrich items with TMDB data and database status
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            def enrich_item(item):
                """Helper function to enrich a single item with TMDB data"""
                title = item.get('title', '')
                media_type = item.get('media_type', 'movie')
                rank = item.get('rank')
                flixpatrol_url = item.get('flixpatrol_url')

                # Search TMDB by title directly (faster than FlixPatrol ID lookup)
                tmdb_id = None
                try:
                    # For unknown media_type (US platforms), try movie first then tv
                    search_types = ['movie', 'tv'] if media_type == 'unknown' else [media_type if media_type == 'tv' else 'movie']

                    for search_type in search_types:
                        search_url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={tmdb_api_key}&query={requests.utils.quote(title)}&page=1"
                        search_response = requests.get(search_url, timeout=5)
                        if search_response.status_code == 200:
                            search_data = search_response.json()
                            if search_data.get('results'):
                                tmdb_id = search_data['results'][0]['id']
                                media_type = search_type
                                break
                except Exception as e:
                    logging.debug(f"TMDB search failed for {title}: {e}")

                if not tmdb_id:
                    return {
                        'id': None,
                        'title': title,
                        'media_type': media_type if media_type != 'unknown' else 'movie',
                        'rank': rank,
                        'db_status': 'unknown',
                        'flixpatrol_url': flixpatrol_url
                    }

                # Fetch TMDB details
                try:
                    if media_type == 'tv':
                        tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    else:
                        tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"

                    tmdb_response = requests.get(tmdb_url, timeout=5)
                    if tmdb_response.status_code == 200:
                        tmdb_data = tmdb_response.json()
                        # Extract genre IDs from genres array (TMDB details returns genres as objects)
                        genres = tmdb_data.get('genres', [])
                        genre_ids = [g['id'] for g in genres if isinstance(g, dict) and 'id' in g]
                        # Extract production company IDs
                        companies = tmdb_data.get('production_companies', [])
                        company_ids = [c['id'] for c in companies if isinstance(c, dict) and 'id' in c]
                        return {
                            'id': tmdb_id,
                            'title': tmdb_data.get('title') or tmdb_data.get('name'),
                            'overview': tmdb_data.get('overview', ''),
                            'poster_path': tmdb_data.get('poster_path'),
                            'backdrop_path': tmdb_data.get('backdrop_path'),
                            'release_date': tmdb_data.get('release_date') or tmdb_data.get('first_air_date', ''),
                            'vote_average': tmdb_data.get('vote_average', 0),
                            'vote_count': tmdb_data.get('vote_count', 0),
                            'media_type': media_type,
                            'genre_ids': genre_ids,
                            'original_language': tmdb_data.get('original_language', ''),
                            'origin_country': tmdb_data.get('origin_country', []),
                            'company_ids': company_ids,
                            'rank': rank,
                            'flixpatrol_url': flixpatrol_url,
                            '_tmdb_id': tmdb_id  # Keep for db status lookup
                        }
                except Exception as e:
                    logging.debug(f"TMDB details failed for {title}: {e}")

                return {
                    'id': tmdb_id,
                    'title': title,
                    'media_type': media_type,
                    'db_status': 'unknown',
                    'rank': rank,
                    'flixpatrol_url': flixpatrol_url,
                    '_tmdb_id': tmdb_id
                }

            # Use ThreadPoolExecutor for parallel TMDB requests
            with ThreadPoolExecutor(max_workers=10) as executor:
                # Use index as key to handle duplicate ranks (movies 1-10 + shows 1-10)
                future_to_idx = {executor.submit(enrich_item, item): idx for idx, item in enumerate(items)}
                results_map = {}

                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        enriched = future.result()
                        results_map[idx] = enriched
                    except Exception as e:
                        logging.warning(f"Error enriching item: {e}")

                # Sort by original index to maintain order
                enriched_items = [results_map[k] for k in sorted(results_map.keys())]

            # Clean up internal fields and normalize IDs
            for item in enriched_items:
                # Move _tmdb_id to id if needed
                if '_tmdb_id' in item and not item.get('id'):
                    item['id'] = item['_tmdb_id']
                item.pop('_tmdb_id', None)

            # Add database status and episode info
            add_db_status_and_episode_info(enriched_items, use_battery=False)

        response_data = {
            'success': True,
            'platform': result.get('platform'),
            'platform_name': result.get('platform_name'),
            'date': result.get('date'),
            'results': enriched_items,
            'total_results': len(enriched_items)
        }

        # Only cache if we actually got results — don't poison cache with empty/error responses
        if enriched_items:
            with _flixpatrol_cache_lock:
                _flixpatrol_cache[fp_cache_key] = {
                    'data': response_data,
                    'expires': datetime.now() + _FLIXPATROL_TTL,
                }

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error fetching FlixPatrol Top 10 for {platform}: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/api/flixpatrol/top10/<platform>/weekly')
@user_required
def flixpatrol_top10_weekly(platform):
    """
    Get weekly aggregated Top 10 from FlixPatrol (last 7 days, scored by rank).
    Cached for 24 hours since the data changes daily at most.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        media_type_filter = request.args.get('type', 'all')

        fp_cache_key = f'weekly:{platform}:{media_type_filter}'
        with _flixpatrol_cache_lock:
            fp_entry = _flixpatrol_cache.get(fp_cache_key)
        if fp_entry and datetime.now() < fp_entry['expires']:
            logging.debug(f"[FlixPatrol Weekly] Serving from cache ({fp_cache_key})")
            return jsonify({**fp_entry['data'], 'cached': True})

        result = fetch_flixpatrol_top10_weekly(platform, media_type=media_type_filter)

        if 'error' in result:
            return jsonify(result), 400

        items = result.get('items', [])
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            def enrich_item(item):
                title = item.get('title', '')
                media_type = item.get('media_type', 'movie')
                rank = item.get('rank')
                flixpatrol_url = item.get('flixpatrol_url')
                tmdb_id = None
                try:
                    search_types = ['movie', 'tv'] if media_type == 'unknown' else [media_type if media_type == 'tv' else 'movie']
                    for search_type in search_types:
                        sr = requests.get(
                            f"https://api.themoviedb.org/3/search/{search_type}?api_key={tmdb_api_key}&query={requests.utils.quote(title)}&page=1",
                            timeout=5)
                        if sr.ok and sr.json().get('results'):
                            tmdb_id = sr.json()['results'][0]['id']
                            media_type = search_type
                            break
                except Exception:
                    pass
                if not tmdb_id:
                    return {'id': None, 'title': title, 'media_type': media_type, 'rank': rank, 'db_status': 'unknown', 'flixpatrol_url': flixpatrol_url}
                try:
                    ep = 'tv' if media_type == 'tv' else 'movie'
                    dr = requests.get(f"https://api.themoviedb.org/3/{ep}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US", timeout=5)
                    if dr.ok:
                        d = dr.json()
                        return {
                            'id': tmdb_id,
                            'title': d.get('title') or d.get('name'),
                            'overview': d.get('overview', ''),
                            'poster_path': d.get('poster_path'),
                            'backdrop_path': d.get('backdrop_path'),
                            'release_date': d.get('release_date') or d.get('first_air_date', ''),
                            'vote_average': d.get('vote_average', 0),
                            'vote_count': d.get('vote_count', 0),
                            'media_type': media_type,
                            'genre_ids': [g['id'] for g in d.get('genres', [])],
                            'original_language': d.get('original_language', ''),
                            'origin_country': d.get('origin_country', []),
                            'company_ids': [c['id'] for c in d.get('production_companies', [])],
                            'rank': rank,
                            'flixpatrol_url': flixpatrol_url,
                            '_tmdb_id': tmdb_id,
                        }
                except Exception:
                    pass
                return {'id': tmdb_id, 'title': title, 'media_type': media_type, 'db_status': 'unknown', 'rank': rank, 'flixpatrol_url': flixpatrol_url, '_tmdb_id': tmdb_id}

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_idx = {executor.submit(enrich_item, item): idx for idx, item in enumerate(items)}
                results_map = {}
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results_map[idx] = future.result()
                    except Exception as e:
                        logging.warning(f"Error enriching weekly FlixPatrol item: {e}")
            enriched_items = [results_map[k] for k in sorted(results_map.keys())]
            for item in enriched_items:
                if '_tmdb_id' in item and not item.get('id'):
                    item['id'] = item['_tmdb_id']
                item.pop('_tmdb_id', None)
            add_db_status_and_episode_info(enriched_items, use_battery=False)

        response_data = {
            'success': True,
            'platform': result.get('platform'),
            'platform_name': result.get('platform_name'),
            'period': 'weekly',
            'date': result.get('date'),
            'results': enriched_items,
            'total_results': len(enriched_items),
        }
        if enriched_items:
            with _flixpatrol_cache_lock:
                _flixpatrol_cache[fp_cache_key] = {
                    'data': response_data,
                    'expires': datetime.now() + _FLIXPATROL_WEEKLY_TTL,
                }
        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error fetching FlixPatrol weekly Top 10 for {platform}: {e}")
        return jsonify({'error': str(e)}), 500


@discover_bp.route('/addmedia')
@user_required
def addmedia():
    """
    Dedicated page for adding TV shows from discover page
    Displays season/episode selection without search functionality
    """
    # Get parameters from query string
    media_id = request.args.get('id', type=int)
    title = request.args.get('title', '')
    year = request.args.get('year', '')
    media_type = request.args.get('type', 'tv')
    rating = request.args.get('rating', 0, type=float)
    vote_average = request.args.get('vote_average', 0, type=float)
    genre_ids = request.args.get('genres', '[]')  # JSON array as string
    backdrop_path = request.args.get('backdrop', '')
    overview = request.args.get('overview', 'No overview available')
    
    # Check TMDB API configuration
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    tmdb_api_key_set = bool(tmdb_api_key)

    # Check user permissions
    from .utils import is_user_system_enabled
    if not is_user_system_enabled():
        is_requester = False
        has_user_permissions = True
    else:
        is_requester = current_user.is_authenticated and current_user.role == 'requester'
        has_user_permissions = current_user.is_authenticated and current_user.role in ['admin', 'user']

    return render_template('discover_addmedia.html',
                         media_id=media_id,
                         title=title,
                         year=year,
                         media_type=media_type,
                         rating=rating,
                         vote_average=vote_average,
                         genre_ids=genre_ids,
                         backdrop_path=backdrop_path,
                         overview=overview,
                         tmdb_api_key_set=tmdb_api_key_set,
                         is_requester=is_requester,
                         has_user_permissions=has_user_permissions)


# ── Startup cache prewarming ───────────────────────────────────────────────────

def _prewarm_discover_cache():
    """
    Pre-populate the in-memory caches for trending, FlixPatrol top10, Trakt
    special lists, and Trakt my-list index in a background thread at startup.
    Runs 15 seconds after import to give the app time to finish initialising.
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            logging.info("[Prewarm] TMDB not configured, skipping discover cache prewarm")
            return

        logging.info("[Prewarm] Starting discover cache prewarm...")

        # ── Trending (movie, tv, anime) ────────────────────────────────────────
        for media_type, anime in [('movie', 'include'), ('tv', 'include'), ('tv', 'only')]:
            cache_key = f'{media_type}:{anime}'
            with _trending_cache_lock:
                if cache_key in _trending_cache and datetime.now() < _trending_cache[cache_key].get('expires', datetime.min):
                    continue
            try:
                if anime == 'only':
                    url = (f"https://api.themoviedb.org/3/discover/tv?api_key={tmdb_api_key}"
                           f"&sort_by=popularity.desc&page=1&with_keywords=210024")
                    resp = requests.get(url, timeout=15)
                    resp.raise_for_status()
                    raw = resp.json().get('results', [])[:20]
                    results = [{'id': t['id'], 'name': t.get('name', ''), 'overview': t.get('overview', ''),
                                'poster_path': t.get('poster_path'), 'backdrop_path': t.get('backdrop_path'),
                                'first_air_date': t.get('first_air_date', ''), 'vote_average': t.get('vote_average', 0),
                                'vote_count': t.get('vote_count', 0), 'media_type': 'tv',
                                'genre_ids': t.get('genre_ids', [])} for t in raw]
                else:
                    base = f"https://api.themoviedb.org/3/trending/{media_type}/week?api_key={tmdb_api_key}"
                    pages = 2 if media_type == 'tv' else 1
                    raw = []
                    for page in range(1, pages + 1):
                        resp = requests.get(f"{base}&page={page}", timeout=15)
                        resp.raise_for_status()
                        raw.extend(resp.json().get('results', []))
                    limit = 35 if media_type == 'tv' else 20
                    raw = raw[:limit]
                    if media_type == 'movie':
                        results = [{'id': i['id'], 'title': i.get('title', ''), 'overview': i.get('overview', ''),
                                    'poster_path': i.get('poster_path'), 'backdrop_path': i.get('backdrop_path'),
                                    'release_date': i.get('release_date', ''), 'vote_average': i.get('vote_average', 0),
                                    'vote_count': i.get('vote_count', 0), 'media_type': 'movie',
                                    'genre_ids': i.get('genre_ids', [])} for i in raw]
                    else:
                        results = [{'id': i['id'], 'name': i.get('name', ''), 'overview': i.get('overview', ''),
                                    'poster_path': i.get('poster_path'), 'backdrop_path': i.get('backdrop_path'),
                                    'first_air_date': i.get('first_air_date', ''), 'vote_average': i.get('vote_average', 0),
                                    'vote_count': i.get('vote_count', 0), 'media_type': 'tv',
                                    'genre_ids': i.get('genre_ids', [])} for i in raw]

                results = enrich_with_digital_dates(results, 'all', tmdb_api_key)
                add_db_status_and_episode_info(results, use_battery=False)
                with _trending_cache_lock:
                    _trending_cache[cache_key] = {'data': results, 'expires': datetime.now() + _TRENDING_TTL}
                logging.info(f"[Prewarm] Trending {cache_key}: {len(results)} items cached")
            except Exception as e:
                logging.warning(f"[Prewarm] Trending {cache_key} failed: {e}")

        # ── Trakt special lists ────────────────────────────────────────────────
        try:
            from content_checkers.trakt import get_trakt_config
            trakt_config = get_trakt_config()
            access_token = trakt_config.get('OAUTH_TOKEN', '')
            client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')

            if access_token and client_id:
                trakt_headers = {
                    'Content-Type': 'application/json',
                    'trakt-api-version': '2',
                    'trakt-api-key': client_id,
                    'Authorization': f'Bearer {access_token}',
                }

                SPECIAL_ENDPOINTS = {
                    'trending':        {'movie': '/movies/trending',           'show': '/shows/trending'},
                    'popular':         {'movie': '/movies/popular',            'show': '/shows/popular'},
                    'favorited':       {'movie': '/movies/favorited/weekly',   'show': '/shows/favorited/weekly'},
                    'played':          {'movie': '/movies/played/weekly',      'show': '/shows/played/weekly'},
                    'watched':         {'movie': '/movies/watched/weekly',     'show': '/shows/watched/weekly'},
                    'collected':       {'movie': '/movies/collected/weekly',   'show': '/shows/collected/weekly'},
                    'anticipated':     {'movie': '/movies/anticipated',        'show': '/shows/anticipated'},
                    'boxoffice':       {'movie': '/movies/boxoffice',          'show': None},
                    'recommendations': {'movie': '/recommendations/movies',    'show': '/recommendations/shows'},
                }

                for list_type, endpoints in SPECIAL_ENDPOINTS.items():
                    cache_key = f'special:{list_type}:all'
                    with _trakt_cache_lock:
                        existing = _trakt_lists_cache.get(cache_key)
                    if existing and datetime.now() < existing.get('expires', datetime.min):
                        continue
                    try:
                        raw_items = []
                        for mt, ep in [('movie', endpoints.get('movie')), ('tv', endpoints.get('show'))]:
                            if not ep:
                                continue
                            r = requests.get(f'https://api.trakt.tv{ep}?limit=40&extended=full',
                                             headers=trakt_headers, timeout=20)
                            r.raise_for_status()
                            for item in r.json():
                                media_obj = item.get('movie') or item.get('show') or item
                                raw_items.append({'type': mt, ('movie' if mt == 'movie' else 'show'): media_obj})

                        tmdb_ids = [str((item.get('movie') or item.get('show') or {}).get('ids', {}).get('tmdb', ''))
                                    for item in raw_items]
                        tmdb_ids = [t for t in tmdb_ids if t]
                        content_hash = hashlib.md5(','.join(tmdb_ids).encode()).hexdigest()

                        if existing and existing.get('content_hash') == content_hash:
                            with _trakt_cache_lock:
                                _trakt_lists_cache[cache_key]['expires'] = datetime.now() + _TRAKT_SPECIAL_TTL
                            logging.info(f"[Prewarm] Special '{list_type}': unchanged, TTL extended")
                            continue

                        results = _trakt_items_to_discover(raw_items, 'all', tmdb_api_key)
                        with _trakt_cache_lock:
                            _trakt_lists_cache[cache_key] = {
                                'data': results,
                                'content_hash': content_hash,
                                'expires': datetime.now() + _TRAKT_SPECIAL_TTL,
                            }
                        logging.info(f"[Prewarm] Special '{list_type}': {len(results)} items cached")
                    except Exception as e:
                        logging.warning(f"[Prewarm] Special '{list_type}' failed: {e}")

                # ── Trakt my-list index ────────────────────────────────────────
                try:
                    with _trakt_cache_lock:
                        idx = _trakt_lists_cache.get('index')
                    if not idx or datetime.now() >= idx.get('expires', datetime.min):
                        r = requests.get('https://api.trakt.tv/users/me/lists',
                                         headers=trakt_headers, timeout=15)
                        r.raise_for_status()
                        lists = [{'name': l.get('name', ''), 'slug': l.get('ids', {}).get('slug', ''),
                                  'item_count': l.get('item_count', 0), 'description': l.get('description', ''),
                                  'updated_at': l.get('updated_at', '')} for l in r.json()]
                        with _trakt_cache_lock:
                            _trakt_lists_cache['index'] = {'data': lists,
                                                            'expires': datetime.now() + _TRAKT_LISTS_INDEX_TTL}
                        logging.info(f"[Prewarm] My-list index: {len(lists)} lists cached")
                    else:
                        lists = idx['data']
                        logging.debug(f"[Prewarm] My-list index already cached, using {len(lists)} lists")

                    # Prewarm each personal list (always runs regardless of index cache state)
                    for lst in lists:
                        slug = lst.get('slug', '')
                        if not slug:
                            continue
                        ml_key = f'{slug}:all'
                        with _trakt_cache_lock:
                            ml_entry = _trakt_lists_cache.get(ml_key)
                        if ml_entry and datetime.now() < ml_entry.get('expires', datetime.min):
                            logging.debug(f"[Prewarm] My list '{slug}' already cached, skipping")
                            continue
                        try:
                            raw_items = []
                            page = 1
                            while True:
                                pr = requests.get(
                                    f'https://api.trakt.tv/users/me/lists/{slug}/items?extended=full&limit=100&page={page}',
                                    headers=trakt_headers, timeout=20)
                                pr.raise_for_status()
                                page_items = pr.json()
                                if not page_items:
                                    break
                                raw_items.extend(page_items)
                                if page >= int(pr.headers.get('X-Pagination-Page-Count', 1)):
                                    break
                                page += 1
                            ml_results = _trakt_items_to_discover(raw_items, 'all', tmdb_api_key)
                            with _trakt_cache_lock:
                                _trakt_lists_cache[ml_key] = {
                                    'data': ml_results,
                                    'updated_at': lst.get('updated_at', ''),
                                    'expires': datetime.now() + _TRAKT_MYLIST_TTL,
                                }
                            _save_mylist_file_cache()
                            logging.info(f"[Prewarm] My list '{slug}': {len(ml_results)} items cached")
                        except Exception as e:
                            logging.warning(f"[Prewarm] My list '{slug}' failed: {e}")
                except Exception as e:
                    logging.warning(f"[Prewarm] My-list index failed: {e}")
            else:
                logging.info("[Prewarm] Trakt not configured, skipping special/my-list prewarm")
        except Exception as e:
            logging.warning(f"[Prewarm] Trakt prewarm failed: {e}")

        # ── FlixPatrol top10 (all platforms, type=all) ─────────────────────────
        try:
            for platform_key in ['netflix', 'disney', 'amazon', 'hbo', 'apple', 'paramount', 'hulu', 'peacock']:
                fp_cache_key = f'{platform_key}:all'
                with _flixpatrol_cache_lock:
                    fp_existing = _flixpatrol_cache.get(fp_cache_key)
                if fp_existing and datetime.now() < fp_existing.get('expires', datetime.min):
                    continue
                try:
                    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
                    result = fetch_flixpatrol_top10(platform_key, media_type='all')
                    if 'error' in result:
                        continue
                    items = result.get('items', [])
                    enriched_items = []
                    if items:
                        def _enrich(item):
                            title = item.get('title', '')
                            mt = item.get('media_type', 'movie')
                            rank = item.get('rank')
                            search_types = ['movie', 'tv'] if mt == 'unknown' else [mt if mt == 'tv' else 'movie']
                            tmdb_id = None
                            for st in search_types:
                                try:
                                    sr = requests.get(
                                        f'https://api.themoviedb.org/3/search/{st}?api_key={tmdb_api_key}&query={requests.utils.quote(title)}&page=1',
                                        timeout=5)
                                    if sr.ok and sr.json().get('results'):
                                        tmdb_id = sr.json()['results'][0]['id']
                                        mt = st
                                        break
                                except Exception:
                                    pass
                            if not tmdb_id:
                                return None
                            try:
                                ep = 'tv' if mt == 'tv' else 'movie'
                                dr = requests.get(
                                    f'https://api.themoviedb.org/3/{ep}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                                    timeout=5)
                                if dr.ok:
                                    d = dr.json()
                                    return {
                                        'id': tmdb_id,
                                        'title': d.get('title') or d.get('name', ''),
                                        'overview': d.get('overview', ''),
                                        'poster_path': d.get('poster_path'),
                                        'backdrop_path': d.get('backdrop_path'),
                                        'release_date': d.get('release_date') or d.get('first_air_date', ''),
                                        'vote_average': d.get('vote_average', 0),
                                        'vote_count': d.get('vote_count', 0),
                                        'media_type': mt,
                                        'genre_ids': [g['id'] for g in d.get('genres', [])],
                                        'original_language': d.get('original_language', ''),
                                        'rank': rank,
                                    }
                            except Exception:
                                pass
                            return None

                        with _TPE(max_workers=10) as ex:
                            enriched_items = [r for r in ex.map(_enrich, items) if r is not None]
                        add_db_status_and_episode_info(enriched_items, use_battery=False)

                    response_data = {
                        'success': True,
                        'platform': result.get('platform'),
                        'platform_name': result.get('platform_name'),
                        'date': result.get('date'),
                        'results': enriched_items,
                        'total_results': len(enriched_items),
                    }
                    if enriched_items:
                        with _flixpatrol_cache_lock:
                            _flixpatrol_cache[fp_cache_key] = {
                                'data': response_data,
                                'expires': datetime.now() + _FLIXPATROL_TTL,
                            }
                    logging.info(f"[Prewarm] FlixPatrol {platform_key}: {len(enriched_items)} items cached")
                except Exception as e:
                    logging.warning(f"[Prewarm] FlixPatrol {platform_key} failed: {e}")
        except Exception as e:
            logging.warning(f"[Prewarm] FlixPatrol prewarm failed: {e}")

        logging.info("[Prewarm] Discover cache prewarm complete")
    except Exception as e:
        logging.error(f"[Prewarm] Unexpected error during discover prewarm: {e}", exc_info=True)


# Fire 15 seconds after module import so Flask has time to finish starting up
threading.Timer(15, _prewarm_discover_cache).start()
