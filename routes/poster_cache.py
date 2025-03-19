import os
import pickle
from datetime import datetime, timedelta
import logging

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')

# Update the path to use the environment variable
CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'poster_cache.pkl')
CACHE_EXPIRY_DAYS = 7  # Cache expires after 7 days

UNAVAILABLE_POSTER = "/static/images/placeholder.png"

def is_cache_healthy():
    """Check if the cache file is valid and can be loaded"""
    if not os.path.exists(CACHE_FILE):
        return False
        
    try:
        with open(CACHE_FILE, 'rb') as f:
            # Try to read and unpickle the first few bytes
            pickle.load(f)
        return True
    except (EOFError, pickle.UnpicklingError, UnicodeDecodeError, FileNotFoundError) as e:
        logging.error(f"Cache file is corrupted: {e}")
        try:
            # If corrupted, attempt to remove the file
            os.remove(CACHE_FILE)
            logging.info("Removed corrupted cache file")
        except Exception as e:
            logging.error(f"Failed to remove corrupted cache file: {e}")
        return False

def load_cache():
    """Load the cache, performing a health check first"""
    if not is_cache_healthy():
        logging.info("Creating new cache due to health check failure")
        return {}
        
    try:
        with open(CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    except Exception as e:
        logging.warning(f"Error loading cache: {e}. Creating a new cache.")
        return {}

def save_cache(cache):
    """Save the cache to disk with validation and atomic writing"""
    if not isinstance(cache, dict):
        logging.error("Invalid cache format: cache must be a dictionary")
        return False
        
    # Create a temporary file to write to first
    temp_file = f"{CACHE_FILE}.tmp"
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        
        # Write to temporary file first
        with open(temp_file, 'wb') as f:
            pickle.dump(cache, f)
            
        # Validate the temporary file can be read back
        try:
            with open(temp_file, 'rb') as f:
                pickle.load(f)
        except Exception as e:
            logging.error(f"Validation of temporary cache file failed: {e}")
            os.remove(temp_file)
            return False
            
        # If validation passed, move the temporary file to the real location
        if os.path.exists(CACHE_FILE):
            os.replace(temp_file, CACHE_FILE)  # atomic on most systems
        else:
            os.rename(temp_file, CACHE_FILE)
            
        return True
    except Exception as e:
        logging.error(f"Error saving cache: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass
        return False

def normalize_media_type(media_type):
    """Normalize media type to either 'tv' or 'movie'"""
    return 'tv' if media_type.lower() in ['tv', 'show', 'series'] else 'movie'

def get_cached_poster_url(tmdb_id, media_type):
    if not tmdb_id:
        return UNAVAILABLE_POSTER
        
    cache = load_cache()
    normalized_type = normalize_media_type(media_type)
    cache_key = f"{tmdb_id}_{normalized_type}"
    cache_item = cache.get(cache_key)
    
    if cache_item:
        url, timestamp = cache_item
        if datetime.now() - timestamp < timedelta(days=CACHE_EXPIRY_DAYS):
            return url
        else:
            logging.info(f"Cache expired for {cache_key}")
            
    return None

def cache_poster_url(tmdb_id, media_type, url):
    if not tmdb_id:
        return
        
    cache = load_cache()
    normalized_type = normalize_media_type(media_type)
    cache_key = f"{tmdb_id}_{normalized_type}"
    cache[cache_key] = (url, datetime.now())
    save_cache(cache)
    logging.info(f"Cached poster URL for {cache_key}: {url}")

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
    cache_poster_url(tmdb_id, media_type, UNAVAILABLE_POSTER)