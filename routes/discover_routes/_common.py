"""Discover blueprint plus the list/network caches shared by the discover route modules."""

import logging
import requests
import json
import os
import gzip
import io
import threading
from datetime import datetime, timedelta
from flask import Blueprint

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
