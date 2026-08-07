import os
import pickle
import threading
import time as _time
from datetime import datetime, timedelta
import logging
import uuid

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')

# Update the path to use the environment variable
CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'poster_cache.pkl')
CACHE_EXPIRY_DAYS = 7  # Cache expires after 7 days

UNAVAILABLE_POSTER = "/static/images/placeholder.png"

# ---------------------------------------------------------------------------
# In-memory cache — loaded once from disk, kept in RAM, saved periodically.
# All reads/writes go through the module-level dict; disk I/O is batched.
# ---------------------------------------------------------------------------
_cache: dict = {}
_cache_lock = threading.Lock()
_cache_dirty = False          # True when in-memory cache differs from disk
_cache_loaded = False         # True after first load from disk
_save_interval_seconds = 60   # Flush to disk at most every 60 s
_expiry_sweep_interval_seconds = 3600  # Purge expired entries at most every hour


def _load_from_disk() -> dict:
    """Load the pickle file from disk, returning an empty dict on any error."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, UnicodeDecodeError, FileNotFoundError):
        logging.error("poster_cache: cache file corrupted, starting fresh")
        try:
            os.remove(CACHE_FILE)
        except Exception:
            pass
        return {}
    except Exception as e:
        logging.warning(f"poster_cache: could not load cache from disk: {e}")
        return {}


def _ensure_loaded():
    """Load the cache from disk the first time it is accessed."""
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    with _cache_lock:
        if not _cache_loaded:   # double-checked locking
            _cache = _load_from_disk()
            _cache_loaded = True
            logging.info(f"poster_cache: loaded {len(_cache)} entries from disk")
            _start_background_saver()


def _save_to_disk(cache_snapshot: dict) -> bool:
    """Atomically write a snapshot of the cache to disk."""
    cache_dir = os.path.dirname(CACHE_FILE)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception as e:
        logging.error(f"poster_cache: cannot create cache dir: {e}")
        return False

    temp_file = os.path.join(
        cache_dir,
        f"{os.path.basename(CACHE_FILE)}.{os.getpid()}.{int(_time.time()*1000)}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(temp_file, 'wb') as f:
            pickle.dump(cache_snapshot, f)
        # Validate
        with open(temp_file, 'rb') as f:
            pickle.load(f)
        os.replace(temp_file, CACHE_FILE)
        return True
    except Exception as e:
        logging.error(f"poster_cache: failed to save cache to disk: {e}")
        try:
            os.remove(temp_file)
        except Exception:
            pass
        return False


def _background_saver():
    """Daemon thread: flushes the in-memory cache to disk every 60 seconds,
    and purges expired entries once an hour so the cache doesn't grow forever."""
    global _cache_dirty
    elapsed_since_sweep = 0
    while True:
        _time.sleep(_save_interval_seconds)
        elapsed_since_sweep += _save_interval_seconds

        if elapsed_since_sweep >= _expiry_sweep_interval_seconds:
            elapsed_since_sweep = 0
            clean_expired_cache()

        if _cache_dirty:
            with _cache_lock:
                if _cache_dirty:
                    snapshot = dict(_cache)
                    _cache_dirty = False
            _save_to_disk(snapshot)
            logging.debug("poster_cache: flushed to disk")


def _start_background_saver():
    t = threading.Thread(target=_background_saver, daemon=True, name="poster-cache-saver")
    t.start()


# ---------------------------------------------------------------------------
# Public API — same signatures as before, now backed by the in-memory dict
# ---------------------------------------------------------------------------

def is_cache_healthy():
    """Returns True if the in-memory cache is populated (always True after load)."""
    _ensure_loaded()
    return True


def load_cache() -> dict:
    """Return the live in-memory cache dict (read-only snapshot not needed for callers)."""
    _ensure_loaded()
    return _cache


def save_cache(cache: dict):
    """Replace the in-memory cache and mark it dirty for background flush.

    Kept for backwards compatibility — callers that previously called save_cache()
    after modifying a local copy will still work correctly.
    """
    global _cache, _cache_dirty
    if not isinstance(cache, dict):
        logging.error("poster_cache: save_cache received non-dict; ignoring")
        return False
    with _cache_lock:
        _cache = cache
        _cache_dirty = True
    return True


def normalize_media_type(media_type):
    """Normalize media type to either 'tv' or 'movie'"""
    return 'tv' if media_type.lower() in ['tv', 'show', 'series'] else 'movie'


def get_cached_poster_url(tmdb_id, media_type):
    if not tmdb_id:
        return UNAVAILABLE_POSTER
    _ensure_loaded()
    normalized_type = normalize_media_type(media_type)
    cache_key = f"{tmdb_id}_{normalized_type}"
    with _cache_lock:
        cache_item = _cache.get(cache_key)
    if cache_item:
        url, timestamp = cache_item
        if datetime.now() - timestamp < timedelta(days=CACHE_EXPIRY_DAYS):
            return url
    return None


def cache_poster_url(tmdb_id, media_type, url):
    global _cache_dirty
    if not tmdb_id:
        return
    _ensure_loaded()
    normalized_type = normalize_media_type(media_type)
    cache_key = f"{tmdb_id}_{normalized_type}"
    with _cache_lock:
        _cache[cache_key] = (url, datetime.now())
        _cache_dirty = True


def clean_expired_cache():
    global _cache_dirty
    _ensure_loaded()
    current_time = datetime.now()
    with _cache_lock:
        expired_keys = [
            key for key, (_, timestamp) in _cache.items()
            if current_time - timestamp > timedelta(days=CACHE_EXPIRY_DAYS)
        ]
        for key in expired_keys:
            del _cache[key]
        if expired_keys:
            _cache_dirty = True


def get_cached_media_meta(tmdb_id, media_type):
    _ensure_loaded()
    cache_key = f"{tmdb_id}_{media_type}_meta"
    with _cache_lock:
        cache_item = _cache.get(cache_key)
    if cache_item:
        media_meta, timestamp = cache_item
        if datetime.now() - timestamp < timedelta(days=CACHE_EXPIRY_DAYS):
            return media_meta
        else:
            logging.info(f"Cache expired for media meta {cache_key}")
    else:
        logging.info(f"Cache miss for media meta {cache_key}")
    return None


def cache_media_meta(tmdb_id, media_type, media_meta):
    global _cache_dirty
    _ensure_loaded()
    cache_key = f"{tmdb_id}_{media_type}_meta"
    with _cache_lock:
        _cache[cache_key] = (media_meta, datetime.now())
        _cache_dirty = True
    logging.info(f"Cached media meta for {cache_key}")


def cache_unavailable_poster(tmdb_id, media_type):
    cache_poster_url(tmdb_id, media_type, UNAVAILABLE_POSTER)


def get_cached_trending_response():
    """Get cached trending response if available and not expired"""
    _ensure_loaded()
    cache_key = "all_trending_response"
    with _cache_lock:
        cache_item = _cache.get(cache_key)
    if cache_item:
        response_data, timestamp = cache_item
        if datetime.now() - timestamp < timedelta(minutes=15):
            logging.info("Using cached trending response")
            return response_data
        else:
            logging.info("Cached trending response expired")
    return None


def cache_trending_response(response_data):
    """Cache the entire trending response for 15 minutes"""
    global _cache_dirty
    _ensure_loaded()
    cache_key = "all_trending_response"
    with _cache_lock:
        _cache[cache_key] = (response_data, datetime.now())
        _cache_dirty = True
    logging.info("Cached trending response for 15 minutes")


def clear_all_cache():
    """Clear the entire poster and artwork cache"""
    global _cache, _cache_dirty
    try:
        with _cache_lock:
            _cache = {}
            _cache_dirty = True
        # Also remove the file immediately so it doesn't reload on restart
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            logging.info("Successfully cleared all poster/artwork cache")
        return True
    except Exception as e:
        logging.error(f"Error clearing cache: {e}")
        return False
