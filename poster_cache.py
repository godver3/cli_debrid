import os
import pickle
from datetime import datetime, timedelta
import logging

CACHE_FILE = 'db_content/poster_cache.pkl'
CACHE_EXPIRY_DAYS = 7  # Cache expires after 7 days

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache, f)

def get_cached_poster_url(tmdb_id, media_type):
    cache = load_cache()
    cache_item = cache.get(f"{tmdb_id}_{media_type}")
    if cache_item:
        url, timestamp = cache_item
        if datetime.now() - timestamp < timedelta(days=CACHE_EXPIRY_DAYS):
            return url
    return None

def cache_poster_url(tmdb_id, media_type, url):
    cache = load_cache()
    cache[f"{tmdb_id}_{media_type}"] = (url, datetime.now())
    save_cache(cache)

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
