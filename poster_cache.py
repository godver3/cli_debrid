import os
import pickle
from datetime import datetime, timedelta
import logging

CACHE_FILE = '/user/db_content/poster_cache.pkl'
CACHE_EXPIRY_DAYS = 7  # Cache expires after 7 days

UNAVAILABLE_POSTER = "/static/images/placeholder.png"

def load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'rb') as f:
                return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading cache: {e}. Creating a new cache.")
    return {}

def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving cache: {e}")

def get_cached_poster_url(tmdb_id, media_type):
    cache = load_cache()
    cache_key = f"{tmdb_id}_{media_type}"
    cache_item = cache.get(cache_key)
    if cache_item:
        url, timestamp = cache_item
        if url == UNAVAILABLE_POSTER:
            return UNAVAILABLE_POSTER  # Return the UNAVAILABLE_POSTER URL instead of None
        if datetime.now() - timestamp < timedelta(days=CACHE_EXPIRY_DAYS):
            return url
        else:
            logging.info(f"Cache expired for {cache_key}")
    else:
        logging.info(f"Cache miss for {cache_key}")
    return None

def cache_poster_url(tmdb_id, media_type, url):
    cache = load_cache()
    cache_key = f"{tmdb_id}_{media_type}"
    cache[cache_key] = (url, datetime.now())
    save_cache(cache)
    logging.info(f"Cached poster URL for {cache_key}")

def clean_expired_cache():
    cache = load_cache()
    current_time = datetime.now()
    expired_keys = [
        key for key, (_, timestamp) in cache.items()
        if current_time - timestamp > timedelta(days=CACHE_EXPIRY_DAYS)
    ]
    for key in expired_keys:
        del cache[key]
    save_cache(cache)

def get_cached_media_meta(tmdb_id, media_type):
    cache = load_cache()
    cache_key = f"{tmdb_id}_{media_type}_meta"
    cache_item = cache.get(cache_key)
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
    cache = load_cache()
    cache_key = f"{tmdb_id}_{media_type}_meta"
    cache[cache_key] = (media_meta, datetime.now())
    save_cache(cache)
    logging.info(f"Cached media meta for {cache_key}")

def cache_unavailable_poster(tmdb_id, media_type):
    cache_poster_url(tmdb_id, media_type, UNAVAILABLE_POSTER)  # Use the existing cache_poster_url function