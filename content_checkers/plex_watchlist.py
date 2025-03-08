import logging
from plexapi.myplex import MyPlexAccount
from typing import List, Dict, Any, Tuple
from settings import get_setting
from database.database_reading import get_media_item_presence
from config_manager import load_config
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.direct_api import DirectAPI
import os
import pickle
from datetime import datetime, timedelta
from .plex_token_manager import update_token_status, get_token_status

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
        logging.info("Connecting to Plex.tv cloud service using token authentication")
        account = MyPlexAccount(token=plex_token)
        logging.info(f"Successfully connected to Plex.tv as user: {account.username}")
        logging.debug(f"Account details - Username: {account.username}, Email: {account.email}")
        logging.debug(f"Connection details - Using Plex.tv API, endpoint: {account._server}")
        return account
    except Exception as e:
        logging.error(f"Error connecting to Plex.tv cloud service: {e}")
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
    disable_caching = True  # Hardcoded to True
    cache = {} if disable_caching else load_plex_cache(PLEX_WATCHLIST_CACHE_FILE)
    current_time = datetime.now()
    
    logging.info("Starting Plex.tv cloud watchlist retrieval")
    account = get_plex_client()
    if not account:
        logging.error("Failed to get Plex client - no account available")
        return [([], versions)]
    
    logging.info(f"Using Plex account: {account.username}")
    
    try:
        # Check if watchlist removal is enabled
        should_remove = get_setting('Debug', 'plex_watchlist_removal', False)
        keep_series = get_setting('Debug', 'plex_watchlist_keep_series', False)

        if should_remove:
            logging.debug("Plex watchlist removal enabled")
            if keep_series:
                logging.debug("Keeping TV series in watchlist")
        
        # Get the watchlist directly from PlexAPI
        logging.info("Fetching watchlist from Plex.tv cloud service")
        watchlist = account.watchlist()
        logging.debug(f"API Response - Connected to: {account._server}")
        logging.debug(f"API Response - Service Type: {'Plex.tv Cloud' if 'plex.tv' in str(account._server) else 'Unknown'}")
        logging.debug(f"Retrieved {len(watchlist)} items from Plex.tv cloud watchlist")
        
        total_items = len(watchlist)
        skipped_count = 0  # Items skipped due to missing IMDB ID
        removed_count = 0  # Items removed from watchlist
        cache_skipped = 0  # Items skipped due to cache
        collected_skipped = 0  # Items skipped because they're already collected
        
        # Process each item in the watchlist
        for item in watchlist:
            # Extract IMDB ID and TMDB ID from the guids
            imdb_id = None
            tmdb_id = None
            for guid in item.guids:
                if 'imdb://' in guid.id:
                    imdb_id = guid.id.split('//')[1]
                    break
                elif 'tmdb://' in guid.id:
                    tmdb_id = guid.id.split('//')[1]
            
            # If no IMDB ID but we have TMDB ID, try to convert
            if not imdb_id and tmdb_id:
                logging.debug(f"No IMDB ID found for '{item.title}', attempting to convert TMDB ID: {tmdb_id}")
                media_type = 'movie' if item.type == 'movie' else 'show'
                try:
                    api = DirectAPI()
                    imdb_id, source = api.tmdb_to_imdb(tmdb_id, media_type=media_type)
                    if imdb_id:
                        logging.info(f"Successfully converted TMDB ID {tmdb_id} to IMDB ID {imdb_id} for '{item.title}'")
                except Exception as e:
                    logging.error(f"Error converting TMDB ID {tmdb_id} to IMDB ID: {str(e)}")
            
            if not imdb_id:
                skipped_count += 1
                logging.debug(f"Skipping item '{item.title}' - no IMDB ID found and TMDB conversion failed")
                continue
            
            media_type = 'movie' if item.type == 'movie' else 'tv'
            #logging.debug(f"Processing {media_type} '{item.title}' (IMDB: {imdb_id})")
            
            # Check if the item is already collected
            item_state = get_media_item_presence(imdb_id=imdb_id)
            if item_state == "Collected" and should_remove:
                if media_type == 'tv':
                    if keep_series:
                        logging.debug(f"Keeping TV series: {imdb_id} ('{item.title}') - keep_series is enabled")
                        collected_skipped += 1
                        continue
                    else:
                        # Check if the show has ended before removing
                        show_status = get_show_status(imdb_id)
                        if show_status != 'ended':
                            logging.debug(f"Keeping ongoing TV series: {imdb_id} ('{item.title}') - status: {show_status}")
                            collected_skipped += 1
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
                        #logging.debug(f"Skipping {media_type} '{item.title}' (IMDB: {imdb_id}) - cached {cache_age.days} days ago")
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
                'media_type': media_type,
                'content_source_detail': account.username
            })
            #logging.debug(f"Added {media_type} '{item.title}' (IMDB: {imdb_id}) to processed items with source: {account.username}")

        # Log detailed statistics
        logging.info(f"Plex.tv cloud watchlist processing complete:")
        logging.info(f"Total items in watchlist: {total_items}")
        logging.info(f"Items skipped (no IMDB): {skipped_count}")
        logging.info(f"Items removed: {removed_count}")
        logging.info(f"Items skipped (cached): {cache_skipped}")
        logging.info(f"Items skipped (collected): {collected_skipped}")
        logging.info(f"New items processed: {len(processed_items)}")
        
        logging.info(f"Retrieved {len(processed_items)} wanted items from Plex watchlist source")
        
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
    disable_caching = True  # Hardcoded to True
    cache = {} if disable_caching else load_plex_cache(OTHER_PLEX_WATCHLIST_CACHE_FILE)
    current_time = datetime.now()
    cache_skipped = 0

    try:
        # Connect to Plex using the provided token
        logging.info(f"Connecting to Plex.tv cloud service for user {username}")
        account = MyPlexAccount(token=token)
        if not account:
            logging.error(f"Could not connect to Plex.tv cloud service with provided token for user {username}")
            return [([], versions)]
        
        logging.debug(f"API Response - Connected to: {account._server}")
        logging.debug(f"API Response - Service Type: {'Plex.tv Cloud' if 'plex.tv' in str(account._server) else 'Unknown'}")
        
        # Verify the username matches
        if account.username != username:
            logging.error(f"Plex.tv cloud token does not match provided username. Expected {username}, got {account.username}")
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
                #logging.warning(f"Skipping item from {username}'s watchlist due to missing IMDB ID: {item.title}")
                continue
            
            media_type = 'movie' if item.type == 'movie' else 'tv'
            
            if not disable_caching:
                # Check cache for this item
                cache_key = f"{username}_{imdb_id}_{media_type}"  # Include username in cache key
                cache_item = cache.get(cache_key)
                
                if cache_item:
                    last_processed = cache_item['timestamp']
                    if current_time - last_processed < timedelta(days=CACHE_EXPIRY_DAYS):
                        #logging.debug(f"Skipping recently processed item: {cache_key}")
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
                'media_type': media_type,
                'content_source_detail': account.username
            }
            
            processed_items.append(wanted_item)
            
        logging.info(f"Retrieved {len(processed_items)} wanted items from {username}'s Plex watchlist source")
        
    except Exception as e:
        logging.error(f"Error fetching {username}'s Plex watchlist: {str(e)}")
        return [([], versions)]
    
    # Save updated cache only if caching is enabled
    if not disable_caching:
        save_plex_cache(cache, OTHER_PLEX_WATCHLIST_CACHE_FILE)
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items

def validate_plex_tokens():
    """Validate all Plex tokens and return their status."""
    token_status = {}
    
    # Validate main user's token
    try:
        plex_token = get_setting('Plex', 'token')
        if plex_token:
            account = MyPlexAccount(token=plex_token)
            # Ping to refresh the auth token
            account.ping()
            # The expiration is stored in the account object directly
            token_status['main'] = {
                'valid': True,
                'expires_at': account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                'username': account.username
            }
            update_token_status('main', True, 
                              expires_at=account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                              plex_username=account.username)
    except Exception as e:
        logging.error(f"Error validating main Plex token: {e}")
        token_status['main'] = {'valid': False, 'expires_at': None, 'username': None}
        update_token_status('main', False)
    
    # Validate other users' tokens
    config = load_config()
    content_sources = config.get('Content Sources', {})
    
    for source_id, source in content_sources.items():
        if source.get('type') == 'Other Plex Watchlist':
            username = source.get('username')
            token = source.get('token')
            
            if username and token:
                try:
                    account = MyPlexAccount(token=token)
                    # Ping to refresh the auth token
                    account.ping()
                    token_status[username] = {
                        'valid': True,
                        'expires_at': account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                        'username': account.username
                    }
                    update_token_status(username, True,
                                      expires_at=account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                                      plex_username=account.username)
                except Exception as e:
                    logging.error(f"Error validating Plex token for user {username}: {e}")
                    token_status[username] = {'valid': False, 'expires_at': None, 'username': None}
                    update_token_status(username, False)
    
    return token_status
