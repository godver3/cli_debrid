import logging
from plexapi.myplex import MyPlexAccount
from typing import List, Dict, Any, Tuple
from settings import get_setting
from database.database_reading import get_media_item_presence
from config_manager import load_config
import os
import pickle
from datetime import datetime, timedelta

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
PLEX_WATCHLIST_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'plex_watchlist_cache.pkl')
OTHER_PLEX_WATCHLIST_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'other_plex_watchlist_cache.pkl')
CACHE_EXPIRY_DAYS = 7

def load_plex_cache(cache_file):
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as f:
                return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading Plex watchlist cache: {e}. Creating a new cache.")
    return {}

def save_plex_cache(cache, cache_file):
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving Plex watchlist cache: {e}")

def get_plex_client():
    if get_setting('Plex', 'token'):
        plex_token = get_setting('Plex', 'token')
    else:
        plex_token = get_setting('File Management', 'plex_token_for_symlink')
    
    if not plex_token:
        logging.error("Plex token not configured. Please add Plex token in settings.")
        return None
    
    try:
        account = MyPlexAccount(token=plex_token)
        return account
    except Exception as e:
        logging.error(f"Error connecting to Plex: {e}")
        return None

def get_wanted_from_plex_watchlist(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    processed_items = []
    cache = load_plex_cache(PLEX_WATCHLIST_CACHE_FILE)
    current_time = datetime.now()
    
    account = get_plex_client()
    if not account:
        return [([], versions)]
    
    try:
        # Check if watchlist removal is enabled
        should_remove = get_setting('Debug', 'plex_watchlist_removal', False)
        keep_series = get_setting('Debug', 'plex_watchlist_keep_series', False)

        if should_remove:
            logging.debug(f"Plex watchlist removal is enabled")
        if keep_series:
            logging.debug(f"Plex watchlist keep series is enabled, remove only movies")
        
        # Get the watchlist directly from PlexAPI
        watchlist = account.watchlist()
        
        # Process each item in the watchlist
        for item in watchlist:
            # Extract IMDB ID from the guids
            imdb_id = None
            for guid in item.guids:
                if 'imdb://' in guid.id:
                    imdb_id = guid.id.split('//')[1]
                    break
            
            if not imdb_id:
                logging.warning(f"Skipping item due to missing IMDB ID: {item.title}")
                continue
            
            media_type = 'movie' if item.type == 'movie' else 'tv'
            
            # Check if the item is already collected
            item_state = get_media_item_presence(imdb_id=imdb_id)
            if item_state == "Collected" and should_remove:
                # If it's a TV show and we want to keep series, skip removal
                if media_type == 'tv' and keep_series:
                    logging.info(f"Keeping collected TV series in watchlist: {item.title} ({imdb_id})")
                else:
                    # Remove from watchlist using the PlexAPI object directly
                    try:
                        account.removeFromWatchlist([item])
                        logging.info(f"Removed collected item from watchlist: {item.title} ({imdb_id})")
                        continue
                    except Exception as e:
                        logging.error(f"Failed to remove collected item from watchlist: {item.title} ({imdb_id}): {str(e)}")
            
            # Check cache for this item
            cache_key = f"{imdb_id}_{media_type}"
            cache_item = cache.get(cache_key)
            
            if cache_item:
                last_processed = cache_item['timestamp']
                if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                    logging.debug(f"Skipping recently processed item: {cache_key}")
                    continue
            
            # Add or update cache entry
            cache[cache_key] = {
                'timestamp': current_time,
                'data': {
                    'imdb_id': imdb_id,
                    'media_type': media_type
                }
            }
            
            wanted_item = {
                'imdb_id': imdb_id,
                'media_type': media_type
            }
            
            processed_items.append(wanted_item)
            
        logging.info(f"Retrieved {len(processed_items)} total items from Plex watchlist")
        
    except Exception as e:
        logging.error(f"Error fetching Plex watchlist: {str(e)}")
        return [([], versions)]
    
    # Save updated cache
    save_plex_cache(cache, PLEX_WATCHLIST_CACHE_FILE)
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items

def get_wanted_from_other_plex_watchlist(username: str, token: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    processed_items = []
    cache = load_plex_cache(OTHER_PLEX_WATCHLIST_CACHE_FILE)
    current_time = datetime.now()

    try:
        # Connect to Plex using the provided token
        account = MyPlexAccount(token=token)
        if not account:
            logging.error(f"Could not connect to Plex account with provided token for user {username}")
            return [([], versions)]
        
        # Verify the username matches
        if account.username != username:
            logging.error(f"Token does not match provided username. Expected {username}, got {account.username}")
            return [([], versions)]
                    
        # Get the watchlist directly from PlexAPI
        watchlist = account.watchlist()
        
        # Process each item in the watchlist
        for item in watchlist:
            # Extract IMDB ID from the guids
            imdb_id = None
            for guid in item.guids:
                if 'imdb://' in guid.id:
                    imdb_id = guid.id.split('//')[1]
                    break
            
            if not imdb_id:
                logging.warning(f"Skipping item from {username}'s watchlist due to missing IMDB ID: {item.title}")
                continue
            
            media_type = 'movie' if item.type == 'movie' else 'tv'
            
            # Check cache for this item
            cache_key = f"{username}_{imdb_id}_{media_type}"  # Include username in cache key
            cache_item = cache.get(cache_key)
            
            if cache_item:
                last_processed = cache_item['timestamp']
                if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                    logging.debug(f"Skipping recently processed item: {cache_key}")
                    continue
            
            # Add or update cache entry
            cache[cache_key] = {
                'timestamp': current_time,
                'data': {
                    'imdb_id': imdb_id,
                    'media_type': media_type
                }
            }
            
            wanted_item = {
                'imdb_id': imdb_id,
                'media_type': media_type
            }
            
            processed_items.append(wanted_item)
            
        logging.info(f"Retrieved {len(processed_items)} total items from {username}'s Plex watchlist")
        
    except Exception as e:
        logging.error(f"Error fetching {username}'s Plex watchlist: {str(e)}")
        return [([], versions)]
    
    # Save updated cache
    save_plex_cache(cache, OTHER_PLEX_WATCHLIST_CACHE_FILE)
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items
