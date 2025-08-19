import logging
import os

# --- Temporary logging setup for debugging the patch ---
LOG_FLAG_PATCH = "[PLEX_PATCH_DEBUG]"
patch_logger = logging.getLogger('plex_patch_debugger')
patch_logger.setLevel(logging.INFO)
log_dir_debug = os.environ.get('USER_LOGS', '/user/logs')
os.makedirs(log_dir_debug, exist_ok=True)
debug_log_path = os.path.join(log_dir_debug, 'patch_debug.log')
fh = logging.FileHandler(debug_log_path)
fh.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
patch_logger.addHandler(fh)
patch_logger.propagate = False

patch_logger.info(f"{LOG_FLAG_PATCH} Logger initialized in plex_watchlist.py")
# --- End temporary logging setup ---

patch_logger.info(f"{LOG_FLAG_PATCH} Attempting to patch plexapi query methods for logging...")
try:
    import plexapi.myplex
    
    # Patch PlexServer.query
    original_plexserver_query = plexapi.myplex.PlexServer.query
    def patched_plexserver_query(self, key, method=None, headers=None, params=None, timeout=None, **kwargs):
        # Fix the endpoint URL if it's using the old metadata.provider.plex.tv
        if 'metadata.provider.plex.tv' in str(key):
            fixed_key = str(key).replace('metadata.provider.plex.tv', 'discover.provider.plex.tv')
            patch_logger.info(f"{LOG_FLAG_PATCH} PLEXSERVER QUERY: FIXED ENDPOINT - Original: {key} -> Fixed: {fixed_key}")
            patch_logger.info(f"{LOG_FLAG_PATCH} PLEXSERVER QUERY: method={method}, params={params}, kwargs={kwargs}")
            return original_plexserver_query(self, fixed_key, method, headers, params, timeout, **kwargs)
        else:
            patch_logger.info(f"{LOG_FLAG_PATCH} PLEXSERVER QUERY: key={key}, method={method}, params={params}, kwargs={kwargs}")
            return original_plexserver_query(self, key, method, headers, params, timeout, **kwargs)

    plexapi.myplex.PlexServer.query = patched_plexserver_query
    patch_logger.info(f"{LOG_FLAG_PATCH} SUCCESS: Patched plexapi.myplex.PlexServer.query to log requests.")

    # Patch MyPlexAccount.query
    original_myplex_query = plexapi.myplex.MyPlexAccount.query
    def patched_myplex_query(self, url, method=None, headers=None, timeout=None, **kwargs):
        # Fix the endpoint URL if it's using the old metadata.provider.plex.tv
        if 'metadata.provider.plex.tv' in url:
            fixed_url = url.replace('metadata.provider.plex.tv', 'discover.provider.plex.tv')
            patch_logger.info(f"{LOG_FLAG_PATCH} MYPLEX QUERY: FIXED ENDPOINT - Original: {url} -> Fixed: {fixed_url}")
            patch_logger.info(f"{LOG_FLAG_PATCH} MYPLEX QUERY: method={method}, kwargs={kwargs}")
            return original_myplex_query(self, fixed_url, method, headers, timeout, **kwargs)
        else:
            patch_logger.info(f"{LOG_FLAG_PATCH} MYPLEX QUERY: url={url}, method={method}, kwargs={kwargs}")
            return original_myplex_query(self, url, method, headers, timeout, **kwargs)

    plexapi.myplex.MyPlexAccount.query = patched_myplex_query
    patch_logger.info(f"{LOG_FLAG_PATCH} SUCCESS: Patched plexapi.myplex.MyPlexAccount.query to log requests.")

except (ImportError, AttributeError) as e:
    patch_logger.error(f"{LOG_FLAG_PATCH} FAILED: Could not patch plexapi query methods. Error: {e}", exc_info=True)

patch_logger.info(f"{LOG_FLAG_PATCH} plex_watchlist.py module execution continues...")

from plexapi.myplex import MyPlexAccount
patch_logger.info(f"{LOG_FLAG_PATCH} Imported MyPlexAccount from plexapi.myplex.")

from typing import List, Dict, Any, Tuple
from utilities.settings import get_setting
from database.database_reading import get_media_item_presence
from queues.config_manager import load_config
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.direct_api import DirectAPI
import os
import pickle
from datetime import datetime, timedelta
from .plex_token_manager import update_token_status, get_token_status
import time
import aiohttp
import asyncio
import xml.etree.ElementTree as ET

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

async def fetch_item_details_and_extract_ids(session, item_data, plex_token_str):
    """
    Fetches full metadata for a single Plex item and extracts IMDB ID, TMDB ID, and media type.
    item_data is a dict: {'title': str, 'url': str, 'original_plex_item': PlexAPIObject}
    """
    headers = {'X-Plex-Token': plex_token_str, 'Accept': 'application/xml'}
    url = item_data['url']
    title = item_data['title']
    original_plex_item = item_data['original_plex_item']

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as response:
            if response.status == 200:
                xml_text = await response.text()
                root = ET.fromstring(xml_text) # MediaContainer is the typical root

                media_element = None
                # Plex usually wraps the single item in MediaContainer
                if root.tag == 'MediaContainer':
                    media_element = root.find('./Video')  # For movies
                    if not media_element:
                        media_element = root.find('./Directory')  # For shows
                
                if not media_element: # Should not happen if XML is as expected
                    logging.warning(f"AsyncFetch: Could not find Video or Directory tag in XML for {title} from {url}")
                    return {'imdb_id': None, 'tmdb_id': None, 'media_type': None, 'original_plex_item': original_plex_item, 'error': 'XMLParseError'}

                imdb_id_found = None
                tmdb_id_found = None
                fetched_media_type = media_element.get('type') # 'movie' or 'show'

                for guid_tag in media_element.findall('./Guid'):
                    guid_str = guid_tag.get('id')
                    if guid_str:
                        if guid_str.startswith('imdb://'):
                            imdb_id_found = guid_str.split('//')[1]
                        elif guid_str.startswith('tmdb://'):
                            tmdb_id_found = guid_str.split('//')[1]
                
                logging.debug(f"AsyncFetch: For '{title}', found IMDB: {imdb_id_found}, TMDB: {tmdb_id_found}, Type: {fetched_media_type}")
                return {'imdb_id': imdb_id_found, 'tmdb_id': tmdb_id_found, 'media_type': fetched_media_type, 'original_plex_item': original_plex_item}

            else:
                logging.error(f"AsyncFetch: Error fetching details for {title} from {url}. Status: {response.status}, Response: {await response.text()[:200]}")
                return {'imdb_id': None, 'tmdb_id': None, 'media_type': None, 'original_plex_item': original_plex_item, 'error': f'HTTP{response.status}'}
    except asyncio.TimeoutError:
        logging.error(f"AsyncFetch: Timeout fetching details for {title} from {url}")
        return {'imdb_id': None, 'tmdb_id': None, 'media_type': None, 'original_plex_item': original_plex_item, 'error': 'Timeout'}
    except Exception as e:
        logging.error(f"AsyncFetch: Exception fetching or parsing details for {title} from {url}: {e}")
        return {'imdb_id': None, 'tmdb_id': None, 'media_type': None, 'original_plex_item': original_plex_item, 'error': str(e)}

async def run_async_fetches(watchlist_items_with_urls, plex_token_str):
    """
    Runs all async fetch tasks concurrently.
    watchlist_items_with_urls is a list of dicts: [{'title': str, 'url': str, 'original_plex_item': PlexAPIObject}]
    """
    conn = aiohttp.TCPConnector(limit_per_host=10) # Limit concurrent connections to the same host
    async with aiohttp.ClientSession(connector=conn) as session:
        tasks = [
            fetch_item_details_and_extract_ids(session, item_data, plex_token_str)
            for item_data in watchlist_items_with_urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False) # Errors handled in fetch_item_details
        return results

def get_plex_client():
    start_time = time.time()
    # Prefer main Plex token, fallback to symlink token if primary not set
    plex_token = get_setting('Plex', 'token')
    if not plex_token:
        plex_token = get_setting('File Management', 'plex_token_for_symlink')
    
    if not plex_token:
        logging.error("Plex token not configured. Please add Plex token in settings (Plex:token or File Management:plex_token_for_symlink).")
        return None, None # Return None for account and token string
    
    try:
        logging.info("Connecting to Plex.tv cloud service using token authentication")
        account = MyPlexAccount(token=plex_token)
        logging.info(f"Successfully connected to Plex.tv as user: {account.username}")
        logging.debug(f"Account details - Username: {account.username}, Email: {account.email}")
        logging.debug(f"Connection details - Using Plex.tv API, endpoint: {account._server}")
        logging.info(f"Plex client connection took {time.time() - start_time:.4f} seconds")
        return account, plex_token # Return account object and token string
    except Exception as e:
        logging.error(f"Error connecting to Plex.tv cloud service: {e}")
        logging.error(f"Plex client connection attempt took {time.time() - start_time:.4f} seconds before failing")
        return None, None

def get_show_status(imdb_id: str) -> str:
    """Get the status of a TV show from Trakt."""
    start_time = time.time()
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
                status = show_data.get('status', '').lower()
                logging.debug(f"Getting show status for {imdb_id} took {time.time() - start_time:.4f} seconds. Status: {status}")
                if status == 'canceled':
                    return 'ended'
                return status
    except Exception as e:
        logging.error(f"Error getting show status for {imdb_id}: {str(e)}")
        logging.debug(f"Getting show status for {imdb_id} took {time.time() - start_time:.4f} seconds before error.")
    return ''

def get_wanted_from_plex_watchlist(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    overall_start_time = time.time()
    all_wanted_items = []
    processed_items_for_current_run = [] # Changed variable name
    # Caching is effectively disabled by this hardcoded value.
    # The new async fetch approach bypasses the old cache mechanism anyway.
    # disable_caching = True 
    
    logging.info("Starting Plex.tv cloud watchlist retrieval")
    
    client_start_time = time.time()
    account, plex_token_str = get_plex_client() # Now also gets token string
    client_end_time = time.time()
    logging.info(f"get_plex_client() call took {client_end_time - client_start_time:.4f} seconds")

    if not account or not plex_token_str:
        logging.error("Failed to get Plex client or token - no account available or token missing")
        return [([], versions)]
    
    logging.info(f"Using Plex account: {account.username}")
    
    try:
        should_remove = get_setting('Debug', 'plex_watchlist_removal', False)
        keep_series = get_setting('Debug', 'plex_watchlist_keep_series', False)

        logging.info("Fetching initial watchlist from Plex.tv cloud service")
        fetch_watchlist_start_time = time.time()
        initial_watchlist = account.watchlist() # This is the plexapi list
        fetch_watchlist_end_time = time.time()
        logging.info(f"Fetching initial watchlist from Plex API took {fetch_watchlist_end_time - fetch_watchlist_start_time:.4f} seconds. Found {len(initial_watchlist)} items.")
        
        if not initial_watchlist:
            logging.info("Plex watchlist is empty.")
            return [([], versions)]

        items_to_process_async = []
        for item_obj in initial_watchlist:
            if hasattr(item_obj, 'key') and item_obj.key and hasattr(item_obj, '_server'):
                try:
                    details_url = item_obj._server.url(item_obj.key)
                    items_to_process_async.append({
                        'title': item_obj.title,
                        'url': details_url,
                        'original_plex_item': item_obj # Keep original item for removal etc.
                    })
                except Exception as e_url:
                     logging.error(f"Error constructing details URL for {item_obj.title}: {e_url}")
            else:
                logging.warning(f"Skipping item {getattr(item_obj, 'title', 'Unknown Title')} due to missing key or _server attribute.")

        logging.info(f"Prepared {len(items_to_process_async)} items for async fetching of details.")
        
        async_fetch_start_time = time.time()
        # Run the asynchronous fetching
        fetched_data_list = asyncio.run(run_async_fetches(items_to_process_async, plex_token_str))
        async_fetch_end_time = time.time()
        logging.info(f"Async fetching of all item details took {async_fetch_end_time - async_fetch_start_time:.4f} seconds.")
        
        total_items_from_async = len(fetched_data_list)
        skipped_count = 0
        removed_count = 0
        collected_skipped = 0
        
        processing_loop_start_time = time.time()
        for item_details in fetched_data_list:
            original_plex_item = item_details['original_plex_item']
            title = original_plex_item.title # Use title from original object for consistency in logs

            if item_details.get('error'):
                logging.warning(f"Skipping item '{title}' due to error during async fetch: {item_details['error']}")
                skipped_count +=1
                continue

            imdb_id = item_details['imdb_id']
            tmdb_id = item_details['tmdb_id']
            # Use media_type from fetched details, fallback to original plex item type
            media_type = item_details['media_type'] if item_details['media_type'] else original_plex_item.type


            if not imdb_id and tmdb_id and media_type:
                logging.info(f"No IMDB ID for '{title}', attempting TMDB ({tmdb_id}, type: {media_type}) to IMDB conversion.")
                conversion_start_time = time.time()
                try:
                    api = DirectAPI()
                    converted_imdb_id, source = api.tmdb_to_imdb(tmdb_id, media_type=media_type)
                    if converted_imdb_id:
                        imdb_id = converted_imdb_id
                        logging.info(f"Successfully converted TMDB ID {tmdb_id} to IMDB ID {imdb_id} for '{title}' via {source}. Took {time.time() - conversion_start_time:.4f}s.")
                    else:
                        logging.warning(f"TMDB to IMDB conversion failed for '{title}' (TMDB: {tmdb_id}). Took {time.time() - conversion_start_time:.4f}s.")
                except Exception as e_conv:
                    logging.error(f"Error during TMDB to IMDB conversion for '{title}': {e_conv}. Took {time.time() - conversion_start_time:.4f}s.")
            
            if not imdb_id:
                skipped_count += 1
                logging.debug(f"Skipping item '{title}' - no IMDB ID found after async fetch and potential conversion.")
                continue
            
            # Ensure media_type is 'tv' or 'movie' for consistency downstream
            if media_type == 'show': media_type = 'tv' 
            
            item_state = get_media_item_presence(imdb_id=imdb_id)
            logging.debug(f"Item '{title}' (IMDB: {imdb_id}) - Presence: {item_state}")

            if item_state == "Collected" and should_remove:
                if media_type == 'tv':
                    if keep_series:
                        logging.debug(f"Keeping collected TV series: '{title}' (IMDB: {imdb_id}) - keep_series is enabled.")
                        collected_skipped += 1
                        continue
                    else:
                        show_status = get_show_status(imdb_id)
                        if show_status != 'ended':
                            logging.debug(f"Keeping collected ongoing TV series: '{title}' (IMDB: {imdb_id}) - status: {show_status}.")
                            collected_skipped += 1
                            continue
                        logging.debug(f"Identified collected and ended TV series for removal: '{title}' (IMDB: {imdb_id}) - status: {show_status}.")
                else: # movie
                    logging.debug(f"Identified collected movie for removal: '{title}' (IMDB: {imdb_id}).")
                
                try:
                    remove_item_start_time = time.time()
                    account.removeFromWatchlist([original_plex_item]) # Use the original PlexAPI object
                    removed_count += 1
                    logging.info(f"Successfully removed '{title}' (IMDB: {imdb_id}) from watchlist. Took {time.time() - remove_item_start_time:.4f}s.")
                    continue
                except Exception as e_remove:
                    logging.error(f"Failed to remove '{title}' (IMDB: {imdb_id}) from watchlist: {e_remove}")
                    # Continue processing other items even if removal fails
            
            processed_items_for_current_run.append({
                'imdb_id': imdb_id,
                'media_type': media_type,
                'content_source_detail': account.username
            })
            logging.debug(f"Added '{title}' (IMDB: {imdb_id}, Type: {media_type}) to processed items from source: {account.username}")

        processing_loop_end_time = time.time()
        logging.info(f"Main processing loop for {total_items_from_async} fetched items took {processing_loop_end_time - processing_loop_start_time:.4f} seconds.")

        logging.info(f"Plex.tv cloud watchlist processing complete:")
        logging.info(f"Total items in initial watchlist: {len(initial_watchlist)}")
        logging.info(f"Items prepared for async fetch: {len(items_to_process_async)}")
        logging.info(f"Items successfully processed from async results: {len(fetched_data_list) - skipped_count - collected_skipped - removed_count}")
        logging.info(f"Items skipped (no IMDB ID or fetch error): {skipped_count}")
        logging.info(f"Items removed from watchlist: {removed_count}")
        logging.info(f"Items skipped (already collected and kept): {collected_skipped}")
        logging.info(f"New items added to wanted list: {len(processed_items_for_current_run)}")
        
        all_wanted_items.append((processed_items_for_current_run, versions))
        
        overall_end_time = time.time()
        logging.info(f"get_wanted_from_plex_watchlist completed in {overall_end_time - overall_start_time:.4f} seconds.")
        return all_wanted_items
        
    except Exception as e:
        logging.error(f"Error processing Plex watchlist: {e}", exc_info=True)
        overall_end_time = time.time()
        logging.error(f"get_wanted_from_plex_watchlist failed after {overall_end_time - overall_start_time:.4f} seconds due to: {e}")
        return [([], versions)]

def get_wanted_from_other_plex_watchlist(username: str, token: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    overall_start_time = time.time()
    all_wanted_items = []
    processed_items_for_current_run = [] # Renamed for clarity

    logging.info(f"Starting watchlist retrieval for other Plex user: {username}")

    try:
        logging.info(f"Connecting to Plex.tv cloud service for user {username}")
        client_start_time = time.time()
        account = MyPlexAccount(token=token) # Token is passed directly for other users
        client_end_time = time.time()
        logging.info(f"Plex client connection for user {username} took {client_end_time - client_start_time:.4f} seconds.")

        if not account:
            logging.error(f"Could not connect to Plex.tv cloud service with provided token for user {username}")
            return [([], versions)]
        
        if account.username != username: # Verify token belongs to the expected user
            logging.error(f"Plex.tv cloud token for user {username} seems to belong to {account.username} (expected {username}). Aborting.")
            return [([], versions)]
                    
        logging.info(f"Fetching initial watchlist for user {username} from Plex.tv cloud service")
        fetch_watchlist_start_time = time.time()
        initial_watchlist = account.watchlist() # This is the plexapi list
        fetch_watchlist_end_time = time.time()
        logging.info(f"Fetching initial watchlist for {username} from Plex API took {fetch_watchlist_end_time - fetch_watchlist_start_time:.4f} seconds. Found {len(initial_watchlist)} items.")
        
        if not initial_watchlist:
            logging.info(f"Plex watchlist for user {username} is empty.")
            return [([], versions)]
                    
        items_to_process_async = []
        for item_obj in initial_watchlist:
            if hasattr(item_obj, 'key') and item_obj.key and hasattr(item_obj, '_server'):
                try:
                    details_url = item_obj._server.url(item_obj.key)
                    items_to_process_async.append({
                        'title': item_obj.title,
                        'url': details_url,
                        'original_plex_item': item_obj
                    })
                except Exception as e_url:
                     logging.error(f"User {username}: Error constructing details URL for {item_obj.title}: {e_url}")
            else:
                logging.warning(f"User {username}: Skipping item {getattr(item_obj, 'title', 'Unknown Title')} due to missing key or _server attribute.")
        
        logging.info(f"User {username}: Prepared {len(items_to_process_async)} items for async fetching of details.")
        
        async_fetch_start_time = time.time()
        # Run the asynchronous fetching using the provided 'token' for this user
        fetched_data_list = asyncio.run(run_async_fetches(items_to_process_async, token))
        async_fetch_end_time = time.time()
        logging.info(f"User {username}: Async fetching of all item details took {async_fetch_end_time - async_fetch_start_time:.4f} seconds.")

        items_processed_count = 0
        items_skipped_no_imdb = 0

        for item_details in fetched_data_list:
            original_plex_item = item_details['original_plex_item']
            title = original_plex_item.title

            if item_details.get('error'):
                logging.warning(f"User {username}: Skipping item '{title}' due to error during async fetch: {item_details['error']}")
                items_skipped_no_imdb +=1 # Count as skipped if we can't get ID
                continue

            imdb_id = item_details['imdb_id']
            # This function does not typically do TMDB to IMDB conversion.
            # If imdb_id is None, we skip.
            
            if not imdb_id:
                items_skipped_no_imdb += 1
                logging.debug(f"User {username}: Skipping item '{title}' - no IMDB ID found after async fetch.")
                continue
            
            media_type = item_details['media_type'] if item_details['media_type'] else original_plex_item.type
            if media_type == 'show': media_type = 'tv' # Normalize 'show' to 'tv'
            
            wanted_item = {
                'imdb_id': imdb_id,
                'media_type': media_type,
                'content_source_detail': account.username # Username of the other Plex account
            }
            processed_items_for_current_run.append(wanted_item)
            items_processed_count += 1
            logging.debug(f"User {username}: Added '{title}' (IMDB: {imdb_id}, Type: {media_type}) to processed items.")
            
        logging.info(f"User {username}: Retrieved {items_processed_count} wanted items from watchlist. Skipped {items_skipped_no_imdb} (no IMDB or fetch error).")
        
    except Exception as e:
        logging.error(f"Error fetching {username}'s Plex watchlist: {str(e)}", exc_info=True)
        return [([], versions)] # Return empty on error
    
    all_wanted_items.append((processed_items_for_current_run, versions))
    logging.info(f"get_wanted_from_other_plex_watchlist for user {username} completed in {time.time() - overall_start_time:.4f} seconds.")
    return all_wanted_items

def validate_plex_tokens():
    """Validate all Plex tokens and return their status."""
    overall_start_time = time.time()
    token_status = {}
    
    # Validate main user's token
    try:
        plex_token_validation_start_time = time.time()
        plex_token = get_setting('Plex', 'token')
        if plex_token:
            account = MyPlexAccount(token=plex_token)
            # Ping to refresh the auth token
            ping_start_time = time.time()
            account.ping()
            logging.debug(f"Main token ping took {time.time() - ping_start_time:.4f} seconds.")
            # The expiration is stored in the account object directly
            token_status['main'] = {
                'valid': True,
                'expires_at': account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                'username': account.username
            }
            update_token_status('main', True, 
                              expires_at=account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                              plex_username=account.username)
            logging.info(f"Main Plex token validation took {time.time() - plex_token_validation_start_time:.4f} seconds. Valid: True, User: {account.username}")
    except Exception as e:
        logging.error(f"Error validating main Plex token: {e}")
        token_status['main'] = {'valid': False, 'expires_at': None, 'username': None}
        update_token_status('main', False)
        logging.info(f"Main Plex token validation took {time.time() - plex_token_validation_start_time:.4f} seconds. Valid: False")
    
    # Validate other users' tokens
    config = load_config()
    content_sources = config.get('Content Sources', {})
    
    for source_id, source in content_sources.items():
        if source.get('type') == 'Other Plex Watchlist':
            username = source.get('username')
            token = source.get('token')
            
            if username and token:
                other_token_validation_start_time = time.time()
                try:
                    account = MyPlexAccount(token=token)
                    # Ping to refresh the auth token
                    ping_start_time = time.time()
                    account.ping()
                    logging.debug(f"Other token ping for user {username} took {time.time() - ping_start_time:.4f} seconds.")
                    token_status[username] = {
                        'valid': True,
                        'expires_at': account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                        'username': account.username
                    }
                    update_token_status(username, True,
                                      expires_at=account.rememberExpiresAt if hasattr(account, 'rememberExpiresAt') else None,
                                      plex_username=account.username)
                    logging.info(f"Plex token validation for user {username} took {time.time() - other_token_validation_start_time:.4f} seconds. Valid: True, User: {account.username}")
                except Exception as e:
                    logging.error(f"Error validating Plex token for user {username}: {e}")
                    token_status[username] = {'valid': False, 'expires_at': None, 'username': None}
                    update_token_status(username, False)
                    logging.info(f"Plex token validation for user {username} took {time.time() - other_token_validation_start_time:.4f} seconds. Valid: False")
    
    logging.info(f"validate_plex_tokens completed in {time.time() - overall_start_time:.4f} seconds.")
    return token_status