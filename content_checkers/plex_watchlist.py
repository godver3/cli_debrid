import logging
from plexapi.myplex import MyPlexAccount
from typing import List, Dict, Any, Tuple
from settings import get_setting
from database.database_reading import get_media_item_presence
from config_manager import load_config
from cli_battery.app.trakt_metadata import TraktMetadata
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

def get_show_status(imdb_id: str) -> str:
    """Get the status of a TV show from Trakt."""
    try:
        trakt = TraktMetadata()
        search_result = trakt._search_by_imdb(imdb_id)
        if search_result and search_result['type'] == 'show':
            show = search_result['show']
            slug = show['ids']['slug']
            
            # Get the full show data using the slug
            url = f"{trakt.base_url}/shows/{slug}?extended=full"
            response = trakt._make_request(url)
            if response and response.status_code == 200:
                show_data = response.json()
                return show_data.get('status', '').lower()
    except Exception as e:
        logging.error(f"Error getting show status for {imdb_id}: {str(e)}")
    return ''

def get_wanted_from_plex_watchlist(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    processed_items = []
    disable_caching = get_setting('Debug', 'disable_content_source_caching', 'False')
    cache = {} if disable_caching else load_plex_cache(PLEX_WATCHLIST_CACHE_FILE)
    current_time = datetime.now()
    
    account = get_plex_client()
    if not account:
        return [([], versions)]
    
    try:
        # Check if watchlist removal is enabled
        should_remove = get_setting('Debug', 'plex_watchlist_removal', False)
        keep_series = get_setting('Debug', 'plex_watchlist_keep_series', False)

        if should_remove:
            logging.debug("Plex watchlist removal enabled")
            if keep_series:
                logging.debug("Keeping TV series in watchlist")
        
        # Get the watchlist directly from PlexAPI
        watchlist = account.watchlist()
        skipped_count = 0
        removed_count = 0
        cache_skipped = 0
        
        # Process each item in the watchlist
        for item in watchlist:
            # Extract IMDB ID from the guids
            imdb_id = None
            for guid in item.guids:
                if 'imdb://' in guid.id:
                    imdb_id = guid.id.split('//')[1]
                    break
            
            if not imdb_id:
                skipped_count += 1
                logging.debug(f"Skipping item '{item.title}' - no IMDB ID found")
                continue
            
            media_type = 'movie' if item.type == 'movie' else 'tv'
            logging.debug(f"Processing {media_type} '{item.title}' (IMDB: {imdb_id})")
            
            # Check if the item is already collected
            item_state = get_media_item_presence(imdb_id=imdb_id)
            if item_state == "Collected" and should_remove:
                if media_type == 'tv':
                    if keep_series:
                        logging.debug(f"Keeping TV series: {imdb_id} ('{item.title}') - keep_series is enabled")
                        continue
                    else:
                        # Check if the show has ended before removing
                        show_status = get_show_status(imdb_id)
                        if show_status != 'ended':
                            logging.debug(f"Keeping ongoing TV series: {imdb_id} ('{item.title}') - status: {show_status}")
                            continue
                        logging.debug(f"Removing ended TV series: {imdb_id} ('{item.title}') - status: {show_status}")
                else:
                    logging.debug(f"Removing collected {media_type}: {imdb_id} ('{item.title}')")
                
                # Remove from watchlist using the PlexAPI object directly
                try:
                    account.removeFromWatchlist([item])
                    removed_count += 1
                    logging.info(f"Successfully removed {media_type} '{item.title}' (IMDB: {imdb_id}) from watchlist")
                    continue
                except Exception as e:
                    logging.error(f"Failed to remove {imdb_id} ('{item.title}') from watchlist: {e}")
                    continue
            
            if not disable_caching:
                # Check cache for this item
                cache_key = f"{imdb_id}_{media_type}"
                cache_item = cache.get(cache_key)
                
                if cache_item:
                    last_processed = cache_item['timestamp']
                    cache_age = current_time - last_processed
                    if cache_age < timedelta(days=CACHE_EXPIRY_DAYS):
                        logging.debug(f"Skipping {media_type} '{item.title}' (IMDB: {imdb_id}) - cached {cache_age.days} days ago")
                        cache_skipped += 1
                        continue
                    else:
                        logging.debug(f"Cache expired for {media_type} '{item.title}' (IMDB: {imdb_id}) - last processed {cache_age.days} days ago")
                else:
                    logging.debug(f"New item found: {media_type} '{item.title}' (IMDB: {imdb_id})")
                
                # Add or update cache entry
                cache[cache_key] = {
                    'timestamp': current_time,
                    'data': {
                        'imdb_id': imdb_id,
                        'media_type': media_type
                    }
                }
            
            processed_items.append({
                'imdb_id': imdb_id,
                'media_type': media_type
            })
            logging.debug(f"Added {media_type} '{item.title}' (IMDB: {imdb_id}) to processed items")

        if skipped_count > 0:
            logging.info(f"Skipped {skipped_count} items due to missing IMDB IDs")
        if removed_count > 0:
            logging.info(f"Removed {removed_count} collected items from watchlist")
        
        if not disable_caching:
            logging.info(f"Found {len(processed_items)} new items from Plex watchlist. Skipped {cache_skipped} items in cache.")
        else:
            logging.info(f"Found {len(processed_items)} items from Plex watchlist. Caching disabled.")
        all_wanted_items.append((processed_items, versions))
        
        # Save updated cache only if caching is enabled
        if not disable_caching:
            save_plex_cache(cache, PLEX_WATCHLIST_CACHE_FILE)
        return all_wanted_items
        
    except Exception as e:
        logging.error(f"Error processing Plex watchlist: {e}")
        return [([], versions)]

def get_wanted_from_other_plex_watchlist(username: str, token: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    processed_items = []
    disable_caching = get_setting('Debug', 'disable_content_source_caching', 'False')
    cache = {} if disable_caching else load_plex_cache(OTHER_PLEX_WATCHLIST_CACHE_FILE)
    current_time = datetime.now()
    cache_skipped = 0

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
            
            if not disable_caching:
                # Check cache for this item
                cache_key = f"{username}_{imdb_id}_{media_type}"  # Include username in cache key
                cache_item = cache.get(cache_key)
                
                if cache_item:
                    last_processed = cache_item['timestamp']
                    if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                        logging.debug(f"Skipping recently processed item: {cache_key}")
                        cache_skipped += 1
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
            
        if not disable_caching:
            logging.info(f"Retrieved {len(processed_items)} items from {username}'s Plex watchlist. Skipped {cache_skipped} items in cache.")
        else:
            logging.info(f"Retrieved {len(processed_items)} items from {username}'s Plex watchlist. Caching disabled.")
        
    except Exception as e:
        logging.error(f"Error fetching {username}'s Plex watchlist: {str(e)}")
        return [([], versions)]
    
    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_plex_cache(cache, OTHER_PLEX_WATCHLIST_CACHE_FILE)
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items
