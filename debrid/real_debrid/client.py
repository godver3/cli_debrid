import logging
from typing import Dict, List, Optional, Union, Tuple, Any
from datetime import datetime, timedelta
import tempfile
import os
import time
from urllib.parse import unquote
import hashlib
import bencodepy
import inspect
import asyncio
import math
import json

from ..base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError, TorrentAdditionError
from ..common import (
    extract_hash_from_magnet,
    download_and_extract_hash,
    timed_lru_cache,
    torrent_to_magnet,
    is_video_file,
    is_unwanted_file
)
from ..status import TorrentStatus
from .api import make_request, get_all_torrents, get_all_downloads
from database.not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
from utilities.settings import get_setting
from .exceptions import RealDebridAuthError, RealDebridAPIError

# Import the new types and function from the .torrent module
from .torrent import (
    TorrentInfoStatus,
    get_torrent_info_status as rd_get_torrent_info_status
)

# Define the path for the size cache file
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
SIZE_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'library_size_cache.json')

# Helper function to write the cache
def _write_size_cache(size_str: str):
    try:
        data = {
            'size_str': size_str,
            'timestamp': datetime.utcnow().isoformat() # Store timestamp
        }
        os.makedirs(os.path.dirname(SIZE_CACHE_FILE), exist_ok=True) # Ensure dir exists
        with open(SIZE_CACHE_FILE, 'w') as f:
            json.dump(data, f, indent=2) # Use indent for readability
        logging.debug(f"Successfully wrote library size '{size_str}' to cache file: {SIZE_CACHE_FILE}")
    except Exception as e:
        logging.error(f"Failed to write library size cache to {SIZE_CACHE_FILE}: {e}")

class RealDebridProvider(DebridProvider):
    """Real-Debrid implementation of the DebridProvider interface"""
    
    API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
    MAX_DOWNLOADS = 25
    
    def __init__(self):
        super().__init__()
        self._cached_torrent_ids = {}  # Store torrent IDs for cached content
        self._cached_torrent_titles = {}  # Store torrent titles for cached content
        self._all_torrent_ids = {}  # Store all torrent IDs for tracking
        # Only initialize phalanx cache if enabled
        self.phalanx_enabled = get_setting('UI Settings', 'enable_phalanx_db', default=False)
        self.phalanx_cache = PhalanxDBClassManager() if self.phalanx_enabled else None
        
    def check_connectivity(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Check provider connectivity and return a tuple of (ok, error_detail)."""
        try:
            # Simple auth-protected endpoint to validate API key and connectivity
            _ = make_request('GET', '/user', self.api_key)
            return True, None
        except RealDebridAuthError as e:
            return False, {
                "service": "Debrid Provider API",
                "type": "AUTH_ERROR",
                "status_code": None,
                "message": str(e)
            }
        except (ProviderUnavailableError, RealDebridAPIError) as e:
            return False, {
                "service": "Debrid Provider API",
                "type": "CONNECTION_ERROR",
                "status_code": None,
                "message": str(e)
            }
        except Exception as e:
            return False, {
                "service": "Debrid Provider API",
                "type": "CONNECTION_ERROR",
                "status_code": None,
                "message": str(e)
            }

    def _load_api_key(self) -> str:
        """Load API key from settings"""
        try:
            from .api import get_api_key
            return get_api_key()
        except Exception as e:
            logging.error(f"Failed to load API key: {str(e)}", exc_info=True)
            raise ProviderUnavailableError(f"Failed to load API key: {str(e)}")

    @property
    def api_key(self) -> str:
        """Provides access to the API key by fetching it from settings on each call."""
        return self._load_api_key()

    def get_subscription_status(self) -> Dict[str, Any]:
        """
        Return Real-Debrid subscription info with days remaining.
        Note: This method is called by statistics layer which handles caching.

        Response example:
        {
          'days_remaining': 12,
          'expiration': '2025-09-30T12:34:56Z',
          'premium': True
        }
        """
        try:
            from utilities.settings import get_setting
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'days_remaining': None, 'expiration': None, 'premium': False}

            user_info = make_request('GET', '/user', self.api_key) or {}
            premium = bool(user_info.get('premium', False))
            expiration = user_info.get('expiration') or user_info.get('premium_until')

            days_remaining = None
            if expiration:
                try:
                    # RD returns ISO8601 like '2025-09-30T12:34:56Z' or with milliseconds '2025-09-30T12:34:56.000Z'
                    exp = expiration
                    exp_dt = None
                    if isinstance(exp, str):
                        if exp.endswith('Z'):
                            # Try with fractional seconds, then without
                            try:
                                exp_dt = datetime.strptime(exp, '%Y-%m-%dT%H:%M:%S.%fZ')
                            except ValueError:
                                try:
                                    exp_dt = datetime.strptime(exp, '%Y-%m-%dT%H:%M:%SZ')
                                except ValueError:
                                    # As a last resort, convert Z to +00:00 and use fromisoformat
                                    try:
                                        exp_dt = datetime.fromisoformat(exp.replace('Z', '+00:00')).replace(tzinfo=None)
                                    except Exception:
                                        exp_dt = None
                        else:
                            try:
                                exp_dt = datetime.fromisoformat(exp)
                            except Exception:
                                exp_dt = None
                    if exp_dt is not None:
                        now = datetime.utcnow()
                        delta = exp_dt - now
                        days_remaining = max(0, delta.days)
                except Exception as e:
                    logging.warning(f"Failed parsing RD expiration '{expiration}': {e}")

            return {
                'days_remaining': days_remaining,
                'expiration': expiration,
                'premium': premium
            }
        except Exception as e:
            logging.error(f"Error fetching subscription status: {str(e)}")
            # Return a safe default
            return {
                'days_remaining': None,
                'expiration': None,
                'premium': None,
                'error': str(e)
            }

    async def is_cached(self, magnet_links: Union[str, List[str]], temp_file_path: Optional[str] = None, result_title: Optional[str] = None, result_index: Optional[str] = None, remove_uncached: bool = True, remove_cached: bool = False, skip_phalanx_db: bool = False, imdb_id: Optional[str] = None) -> Union[bool, Dict[str, bool], None]:
        """
        Check if one or more magnet links or torrent files are cached on Real-Debrid.
        First checks PhalanxDB for cached results, falls back to Real-Debrid API if needed.
        
        Args:
            magnet_links: Either a magnet link or list of magnet links
            temp_file_path: Optional path to torrent file
            result_title: Optional title of the result being checked (for logging)
            result_index: Optional index of the result in the list (for logging)
            remove_uncached: Whether to remove uncached torrents after checking (default: True)
            remove_cached: Whether to remove cached torrents after checking (default: False)
            skip_phalanx_db: Whether to skip checking PhalanxDB (default: False)
            imdb_id: Optional IMDb ID to check against the database to prevent removal.
            
        Returns:
            - True: Torrent is cached
            - False: Torrent is not cached
            - None: Error occurred during check (invalid magnet, no video files, etc)
        """
        # Build log prefix
        log_prefix = ""
        if result_index:
            log_prefix = f"[Result {result_index}]"
            if result_title:
                log_prefix += f" [{result_title}]"
        elif result_title:
            log_prefix = f"[{result_title}]"
        
        
        # If single magnet link, convert to list
        if isinstance(magnet_links, str):
            magnet_links = [magnet_links]
            return_single = True
        else:
            return_single = False

        # Initialize results
        results = {}
        
        # Process each magnet link
        for magnet_link in magnet_links:
            logging.debug(f"{log_prefix} Processing magnet/URL: {magnet_link[:60]}...")
            
            # Extract hash at the beginning to ensure it's always available
            hash_value = None
            
            # Prioritize temp files over magnet links
            if temp_file_path:
                try:
                    with open(temp_file_path, 'rb') as f:
                        torrent_data = bencodepy.decode(f.read())
                        info = torrent_data[b'info']
                        hash_value = hashlib.sha1(bencodepy.encode(info)).hexdigest()
                        magnet_link = None  # Clear magnet link if we have a valid temp file
                except Exception as e:
                    logging.error(f"{log_prefix} Could not extract hash from torrent file: {str(e)}")
            # Only process magnet_link if we don't have a hash_value yet and magnet_link exists
            elif not hash_value and magnet_link:
                if magnet_link.startswith('magnet:'):
                    hash_value = extract_hash_from_magnet(magnet_link)
                elif len(magnet_link) == 40 and all(c in '0123456789abcdefABCDEF' for c in magnet_link):
                    hash_value = magnet_link
                    magnet_link = f"magnet:?xt=urn:btih:{magnet_link}"
            
            if not hash_value:
                logging.error(f"{log_prefix} Could not extract hash from input: {magnet_link}")
                try:
                    add_to_not_wanted(magnet_link)
                except Exception as e:
                    logging.error(f"{log_prefix} Failed to add to not wanted list: {str(e)}")
                results[magnet_link] = None
                continue
                
            logging.debug(f"{log_prefix} Extracted hash: {hash_value}")
            
            # Check PhalanxDB cache first if enabled and not skipped
            phalanx_cache_hit = False
            try:
                if not skip_phalanx_db and self.phalanx_enabled and self.phalanx_cache:
                    phalanx_cache_result = self.phalanx_cache.get_cache_status(hash_value)
                    if phalanx_cache_result is not None:
                        if phalanx_cache_result['is_cached']:
                            logging.info(f"{log_prefix} Found cached status in PhalanxDB: {phalanx_cache_result['is_cached']}")
                            results[hash_value] = True
                            phalanx_cache_hit = True
                            continue
                        else:
                            logging.debug(f"{log_prefix} Found uncached status in PhalanxDB, verifying with Real-Debrid")
            except Exception as e:
                logging.error(f"{log_prefix} Error checking PhalanxDB cache: {str(e)}")
                # Continue with normal cache check if PhalanxDB fails
            
            # If not cached in PhalanxDB or PhalanxDB failed, check Real-Debrid
            torrent_id = None
            try:
                # Add the magnet/torrent to RD with retry for 429 errors
                max_retries = 3
                retry_delay = 5  # Start with 5 seconds delay
                for retry_attempt in range(max_retries):
                    try:
                        torrent_id = self.add_torrent(magnet_link if magnet_link and magnet_link.startswith('magnet:') else None, temp_file_path)
                        break  # Success, exit retry loop
                    except ProviderUnavailableError as e:
                        if "429" in str(e) and retry_attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                            logging.warning(f"{log_prefix} Rate limit (429) hit when adding torrent. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                            time.sleep(wait_time)
                        else:
                            # Re-raise if it's not a 429 error or we've exhausted retries
                            raise
                
                if not torrent_id:
                    # If add_torrent returns None, the torrent might already be added
                    # Try to get the hash and look up existing torrent
                    if hash_value:
                        # Search for existing torrent with this hash
                        torrents = make_request('GET', '/torrents', self.api_key) or []
                        for torrent in torrents:
                            if torrent.get('hash', '').lower() == hash_value.lower():
                                torrent_id = torrent['id']
                                break
                    
                    if not torrent_id:
                        results[hash_value] = False
                        # Update PhalanxDB with uncached status if enabled
                        if self.phalanx_enabled and self.phalanx_cache:
                            try:
                                self.phalanx_cache.update_cache_status(hash_value, False)
                            except Exception as e:
                                logging.error(f"{log_prefix} Failed to update PhalanxDB: {str(e)}")
                        continue
                    
                # Get torrent info with retry for 429 errors
                max_retries = 3
                retry_delay = 5  # Start with 5 seconds delay
                info = None
                for retry_attempt in range(max_retries):
                    try:
                        info = self.get_torrent_info(torrent_id)
                        break  # Success, exit retry loop
                    except ProviderUnavailableError as e:
                        if "429" in str(e) and retry_attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                            logging.warning(f"{log_prefix} Rate limit (429) hit when getting torrent info. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                            time.sleep(wait_time)
                        else:
                            # Not a 429 error or we've exhausted retries
                            break
                
                if not info:
                    logging.error(f"{log_prefix} Failed to get torrent info for ID: {torrent_id}")
                    try:
                        add_to_not_wanted(hash_value)
                        self.remove_torrent(torrent_id, "Failed to get torrent info during cache check")
                    except Exception as e:
                        logging.error(f"{log_prefix} Error in cleanup after info fetch failure: {str(e)}")
                        self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                    results[hash_value] = None
                    continue
                    
                # Check if it's already cached
                status = info.get('status', '')
                
                # Handle error statuses
                if status in ['magnet_error', 'error', 'virus', 'dead']:
                    logging.error(f"{log_prefix} Torrent has error status: {status}")
                    try:
                        add_to_not_wanted(hash_value)
                        self.remove_torrent(torrent_id, f"Torrent has error status: {status}")
                    except Exception as e:
                        logging.error(f"{log_prefix} Error in cleanup after error status: {str(e)}")
                        self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                    results[hash_value] = None
                    continue
                
                # If there are no video files, return None to indicate error
                video_files = [f for f in info.get('files', []) if is_video_file(f.get('path', '') or f.get('name', ''))]
                if not video_files:
                    logging.error(f"{log_prefix} No video files found in torrent")
                    self.update_status(torrent_id, TorrentStatus.ERROR)
                    try:
                        add_to_not_wanted(hash_value)
                        self.remove_torrent(torrent_id, "No video files found in torrent")
                    except Exception as e:
                        logging.error(f"{log_prefix} Error in cleanup after no video files: {str(e)}")
                        self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                    results[hash_value] = None
                    continue
                
                is_cached = status == 'downloaded'
                
                # --- START EDIT: Check against DB using filenames before deciding on removal ---
                is_in_db = False
                if imdb_id:
                    from database.database_reading import is_any_file_in_db_for_item
                    torrent_filenames = [f.get('path', '') for f in info.get('files', [])]
                    logging.info(f"Comparing {torrent_filenames} to DB for IMDb ID {imdb_id}")
                    if is_any_file_in_db_for_item(imdb_id, torrent_filenames):
                        is_in_db = True
                        if is_cached:
                            logging.info(f"{log_prefix} A file from this torrent matches a DB entry for IMDb ID {imdb_id} and is cached. Disabling removal.")
                        else:
                            logging.info(f"{log_prefix} A file from this torrent matches a DB entry for IMDb ID {imdb_id} but is not cached. Allowing removal.")

                # Override removal flags if item is in DB AND cached
                local_remove_uncached = remove_uncached
                local_remove_cached = remove_cached
                if is_in_db and is_cached:
                    local_remove_uncached = False
                    local_remove_cached = False
                # --- END EDIT ---

                # Update PhalanxDB with new cache status if enabled
                if self.phalanx_enabled and self.phalanx_cache:
                    try:
                        self.phalanx_cache.update_cache_status(hash_value, is_cached)
                    except Exception as e:
                        logging.error(f"{log_prefix} Failed to update PhalanxDB: {str(e)}")
                
                # Update status tracking
                self.update_status(
                    torrent_id,
                    TorrentStatus.CACHED if is_cached else TorrentStatus.NOT_CACHED
                )
                
                # Store all torrent IDs for tracking
                self._all_torrent_ids[hash_value] = torrent_id
                
                # Store torrent ID if cached, remove if not cached
                if is_cached:
                    self._cached_torrent_ids[hash_value] = torrent_id
                    self._cached_torrent_titles[hash_value] = info.get('filename', '')
                    
                    # Remove cached torrents if requested
                    if local_remove_cached:
                        try:
                            self.remove_torrent(torrent_id, "Torrent is cached - removed after cache check due to remove_cached=True")
                        except Exception as e:
                            logging.error(f"{log_prefix} Error removing cached torrent: {str(e)}")
                            self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                else:
                    if local_remove_uncached:
                        try:
                            self.remove_torrent(torrent_id, "Torrent is not cached - removed after cache check")
                            from database.torrent_tracking import update_cache_check_removal
                            update_cache_check_removal(hash_value)
                        except Exception as e:
                            logging.error(f"{log_prefix} Error removing uncached torrent: {str(e)}")
                            self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                
                results[hash_value] = is_cached
                
            except Exception as e:
                logging.error(f"{log_prefix} Error checking cache: {str(e)}")
                if torrent_id:
                    self.update_status(torrent_id, TorrentStatus.ERROR)
                    try:
                        self.remove_torrent(torrent_id, f"Error during cache check: {str(e)}")
                    except Exception as rm_err:
                        logging.error(f"{log_prefix} Error removing torrent after unhandled error: {str(rm_err)}")
                        self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                try:
                    add_to_not_wanted(hash_value)
                except Exception as add_err:
                    logging.error(f"{log_prefix} Failed to add to not wanted list: {str(add_err)}")
                results[hash_value] = None

        logging.debug(f"{log_prefix} Cache check complete. Results: {results}")
        
        # Return single result if input was single magnet, otherwise return dict
        return results[list(results.keys())[0]] if return_single else results
        
    def get_cached_torrent_id(self, hash_value: str) -> Optional[str]:
        """Get stored torrent ID for a cached hash"""
        return self._cached_torrent_ids.get(hash_value)

    def get_cached_torrent_title(self, hash_value: str) -> Optional[str]:
        """Get stored torrent title for a cached hash"""
        return self._cached_torrent_titles.get(hash_value)

    def list_active_torrents(self) -> List[Dict]:
        """List all active torrents"""
        from .torrent import list_active_torrents
        return list_active_torrents(self.api_key)

    def add_torrent(self, magnet_link: Optional[str], temp_file_path: Optional[str] = None) -> Optional[str]:
        """Add a torrent to Real-Debrid"""
        try:
            # Extract hash value at the beginning
            hash_value = None
            
            # Prioritize temp files over magnet links for hash extraction
            if temp_file_path:
                try:
                    with open(temp_file_path, 'rb') as f:
                        torrent_data = bencodepy.decode(f.read())
                        info = torrent_data[b'info']
                        hash_value = hashlib.sha1(bencodepy.encode(info)).hexdigest()
                        magnet_link = None  # Clear magnet link if we have a valid temp file
                except Exception as e:
                    logging.error(f"Could not extract hash from torrent file: {str(e)}")
            elif magnet_link:
                hash_value = extract_hash_from_magnet(magnet_link)
            
            if not hash_value:
                logging.error("Could not extract hash from magnet link or torrent file")
            
            # Handle torrent file upload
            if temp_file_path:
                if not os.path.exists(temp_file_path):
                    logging.error(f"Temp file does not exist: {temp_file_path}")
                    raise ValueError(f"Temp file does not exist: {temp_file_path}")
                    
                # Add the torrent file directly
                with open(temp_file_path, 'rb') as f:
                    file_content = f.read()
                    result = make_request('PUT', '/torrents/addTorrent', self.api_key, data=file_content)
            # Handle magnet link only if no temp file was used
            elif magnet_link:
                # URL decode the magnet link if needed
                if '%' in magnet_link:
                    magnet_link = unquote(magnet_link)

                # Check if torrent already exists
                if hash_value:
                    torrents = make_request('GET', '/torrents', self.api_key) or []
                    for torrent in torrents:
                        if torrent.get('hash', '').lower() == hash_value.lower():
                            logging.info(f"Torrent already exists with ID {torrent['id']}")
                            # Cache the filename for existing torrent
                            self._cached_torrent_titles[hash_value] = torrent.get('filename', '')
                            return torrent['id']

                # Add magnet link
                data = {'magnet': magnet_link}
                result = make_request('POST', '/torrents/addMagnet', self.api_key, data=data)
            else:
                logging.error("Neither magnet_link nor temp_file_path provided")
                raise ValueError("Either magnet_link or temp_file_path must be provided")

            if not result or 'id' not in result:
                logging.error(f"Failed to add torrent - response: {result}")
                raise TorrentAdditionError(f"Failed to add torrent - response: {result}")
                
            torrent_id = result['id']
            
            # Wait for files to be available
            max_attempts = 30  # Increase timeout to 30 seconds
            success = False
            selected_files = []
            for attempt in range(max_attempts):
                info = self.get_torrent_info(torrent_id)
                if not info:
                    logging.error("Failed to get torrent info")
                    raise TorrentAdditionError("Failed to get torrent info")
                
                status = info.get('status', '')
                
                # Early exit for invalid magnets
                if status == 'magnet_error':
                    logging.error(f"Magnet error detected: {info.get('filename')}")
                    self.remove_torrent(torrent_id, f"Magnet error: {info.get('filename')}")
                    raise TorrentAdditionError(f"Magnet error: {info.get('filename')}")
                
                if status == 'magnet_conversion':
                    time.sleep(1)
                    continue
                
                if status == 'waiting_files_selection':
                    # Select only video files
                    files = info.get('files', [])
                    if files:
                        # Get list of file IDs for video files
                        video_file_ids = []
                        selected_files = []
                        for i, file_info in enumerate(files, start=1):
                            filename = file_info.get('path', '') or file_info.get('name', '')
                            if filename and is_video_file(filename) and not is_unwanted_file(filename):
                                video_file_ids.append(str(i))
                                selected_files.append({
                                    'path': filename,
                                    'bytes': file_info.get('bytes', 0),
                                    'selected': True
                                })
                                
                        if video_file_ids:
                            data = {'files': ','.join(video_file_ids)}
                            # Add retry mechanism for file selection
                            max_selection_retries = 5
                            for selection_attempt in range(max_selection_retries):
                                try:
                                    make_request('POST', f'/torrents/selectFiles/{torrent_id}', self.api_key, data=data)
                                    logging.info(f"Selected video files: {video_file_ids}")
                                    # Get updated info after file selection
                                    updated_info = self.get_torrent_info(torrent_id)
                                    if updated_info and hash_value:
                                        # Cache the filename after file selection
                                        self._cached_torrent_titles[hash_value] = updated_info.get('filename', '')
                                        
                                        # Record torrent addition with detailed metadata
                                        from database.torrent_tracking import record_torrent_addition, update_torrent_tracking
                                        try:
                                            # Get the largest selected file
                                            largest_file = max(
                                                (f for f in selected_files if f['selected']), 
                                                key=lambda x: x['bytes']
                                            )
                                            
                                            # Update the existing tracking record with file info
                                            updated_item_data = {
                                                'filled_by_title': updated_info.get('filename'),
                                                'filled_by_file': largest_file['path']
                                            }
                                            
                                            # Update trigger details with file selection info
                                            updated_trigger_details = {
                                                'source': 'real_debrid',
                                                'status': status,
                                                'selected_files': selected_files
                                            }
                                            
                                            # Update additional metadata with debrid info
                                            updated_metadata = {
                                                'debrid_info': {
                                                    'provider': 'real_debrid',
                                                    'torrent_id': torrent_id,
                                                    'status': status,
                                                    'filename': updated_info.get('filename'),
                                                    'original_filename': updated_info.get('original_filename')
                                                }
                                            }
                                            
                                            # Try to update existing record first
                                            if not update_torrent_tracking(
                                                torrent_hash=hash_value,
                                                item_data=updated_item_data,
                                                trigger_details=updated_trigger_details,
                                                additional_metadata=updated_metadata
                                            ):
                                                # If no existing record, create a new one
                                                record_torrent_addition(
                                                    torrent_hash=hash_value,
                                                    trigger_source='MISSING_TRIGGER',
                                                    rationale='Torrent did not trigger tracking',
                                                    item_data=updated_item_data,
                                                    trigger_details=updated_trigger_details,
                                                    additional_metadata=updated_metadata
                                                )
                                        except Exception as track_err:
                                            logging.error(f"Failed to record torrent addition: {str(track_err)}")
                                    success = True
                                    break
                                except Exception as e:
                                    if selection_attempt < max_selection_retries - 1:
                                        logging.warning(f"File selection attempt {selection_attempt + 1} failed, retrying in 1s: {str(e)}")
                                        time.sleep(1)
                                    else:
                                        logging.error(f"All file selection attempts failed: {str(e)}")
                                        raise
                        else:
                            logging.error("No video files found in torrent")
                            self.remove_torrent(torrent_id, "No video files found during file selection")
                            raise TorrentAdditionError("No video files found in torrent")
                    else:
                        logging.error("No files available in torrent info")
                        time.sleep(1)
                        continue
                        
                elif status in ['downloaded', 'downloading']:
                    success = True
                    break
                else:
                    time.sleep(1)
                    
                if not success:
                    # Only raise timeout if we didn't succeed
                    logging.error("Timed out waiting for torrent files")
                    self.remove_torrent(torrent_id, "Timed out waiting for torrent files")
                    raise TorrentAdditionError("Timed out waiting for torrent files")
                    
                return torrent_id
                
        except Exception as e:
            logging.error(f"Error adding torrent: {str(e)}")
            raise

    def get_available_hosts(self) -> Optional[list]:
        """Get list of available torrent hosts"""
        try:
            result = make_request('GET', '/torrents/availableHosts', self.api_key)
            return result
        except Exception as e:
            logging.error(f"Error getting available hosts: {str(e)}")
            return None

    @timed_lru_cache(seconds=1)
    def get_active_downloads(self) -> Tuple[int, int]:
        """Get number of active downloads and download limit"""
        try:
            # Get active torrents count and limit
            from utilities.settings import get_setting
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return 0, 0

            active_data = make_request('GET', '/torrents/activeCount', self.api_key)
            
            active_count = active_data.get('nb', 0)
            raw_max_downloads = active_data.get('limit', self.MAX_DOWNLOADS)

            # Calculate adjusted max downloads (75% of limit)
            max_downloads = round(raw_max_downloads * 0.75)
            
            if active_count >= max_downloads:
                logging.warning(f"Active downloads ({active_count}) exceeds adjusted limit ({max_downloads})")
                raise TooManyDownloadsError(
                    f"Too many active downloads ({active_count}/{max_downloads})"
                )
                
            return active_count, max_downloads
            
        except TooManyDownloadsError:
            raise
        except Exception as e:
            logging.error(f"Error getting active downloads: {str(e)}", exc_info=True)
            raise ProviderUnavailableError(f"Failed to get active downloads: {str(e)}")

    def get_user_traffic(self) -> Dict:
        """Get user traffic information"""
        try:
            from utilities.settings import get_setting
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'downloaded': 0, 'limit': None}

            traffic_info = make_request('GET', '/traffic/details', self.api_key)
            overall_traffic = make_request('GET', '/traffic', self.api_key)

            # Validate traffic_info response
            if not isinstance(traffic_info, dict):
                logging.error(f"Invalid traffic_info response type: {type(traffic_info)}")
                return {'downloaded': 0, 'limit': None}

            try:
                # Get today in UTC since Real-Debrid uses UTC dates
                today_utc = datetime.utcnow().strftime("%Y-%m-%d")
                
                # Get today's traffic
                daily_traffic = traffic_info.get(today_utc, {})
                
                # If no data for today, try yesterday as fallback
                if not daily_traffic:
                    yesterday_utc = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
                    daily_traffic = traffic_info.get(yesterday_utc, {})
                    if daily_traffic:
                        logging.info(f"No traffic data found for {today_utc}, using yesterday's data ({yesterday_utc})")
                    else:
                        # Try to find the most recent available date
                        available_dates = sorted(traffic_info.keys(), reverse=True)
                        if available_dates:
                            most_recent_date = available_dates[0]
                            daily_traffic = traffic_info.get(most_recent_date, {})
                            logging.info(f"Using most recent available traffic data from {most_recent_date}")
                        else:
                            # Log available dates for debugging
                            logging.warning(f"No traffic data found for {today_utc} or {yesterday_utc}. No available dates found.")
                            return {'downloaded': 0, 'limit': None}
                    
                daily_bytes = daily_traffic.get('bytes', 0)
                # Convert bytes to GB (1 GB = 1024^3 bytes)
                daily_gb = daily_bytes / (1024 * 1024 * 1024)  # Convert bytes to GB
                
                # Get daily limit from traffic info
                daily_limit = 2000

                return {
                    'downloaded': round(daily_gb, 2),
                    'limit': round(daily_limit, 2) if daily_limit is not None else 2000
                }

            except Exception as e:
                logging.error(f"Error calculating daily usage: {e}")
                logging.exception("Full traceback:")
                return {'downloaded': 0, 'limit': 2000}

        except Exception as e:
            logging.error(f"Error getting user traffic: {str(e)}")
            raise ProviderUnavailableError(f"Failed to get user traffic: {str(e)}")

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict]:
        """Get information about a specific torrent"""
        try:
            # Use the api_key property (or self._internal_api_key directly)
            info = make_request('GET', f'/torrents/info/{torrent_id}', self.api_key)
            
            # Update status based on response
            if info:
                status = info.get('status', '')
                hash_value = info.get('hash', '').lower()
                
                # Update torrent tracking status
                from database.torrent_tracking import mark_torrent_removed
                
                if status == 'downloaded':
                    self.update_status(torrent_id, TorrentStatus.CACHED)
                elif status == 'downloading':
                    self.update_status(torrent_id, TorrentStatus.DOWNLOADING)
                elif status == 'waiting_files_selection':
                    self.update_status(torrent_id, TorrentStatus.SELECTING)
                elif status == 'magnet_error':
                    self.update_status(torrent_id, TorrentStatus.ERROR)
                    if hash_value:
                        mark_torrent_removed(hash_value, f"Magnet error: {info.get('filename', '')}")
                elif status == 'error':
                    self.update_status(torrent_id, TorrentStatus.ERROR)
                    if hash_value:
                        mark_torrent_removed(hash_value, f"Torrent error: {info.get('filename', '')}")
                    
            return info
            
        except Exception as e:
            if "404" in str(e):
                # Torrent not found in Real-Debrid
                self.update_status(torrent_id, TorrentStatus.REMOVED)
                # Try to get hash from our cache to mark as removed
                for hash_value, tid in self._cached_torrent_ids.items():
                    if tid == torrent_id:
                        from database.torrent_tracking import mark_torrent_removed
                        mark_torrent_removed(hash_value, "Torrent no longer exists in Real-Debrid")
                        break
            else:
                logging.error(f"Error getting torrent info: {str(e)}")
                self.update_status(torrent_id, TorrentStatus.ERROR)
            return None

    def get_torrent_info_with_status(self, torrent_id: str) -> TorrentInfoStatus:
        """
        Get detailed information about a torrent, including fetch status,
        using the Real-Debrid provider.
        """
        # Import here if needed for type hint resolution at runtime and not just static analysis
        from debrid.status import TorrentInfoStatus 
        # Use the api_key property (or self._internal_api_key directly)
        return rd_get_torrent_info_status(self.api_key, torrent_id)

    def verify_torrent_presence(self, hash_value: str = None) -> bool:
        """
        Verify if a torrent is still present in Real-Debrid.
        If hash_value is None, verifies all tracked torrents.
        
        Args:
            hash_value: Optional specific torrent hash to verify
            
        Returns:
            bool: True if torrent(s) verified successfully
        """
        try:
            # Get all active torrents from Real-Debrid
            active_torrents = make_request('GET', '/torrents', self.api_key) or []
            active_hashes = {t['hash'].lower(): t['id'] for t in active_torrents}
            
            from database.torrent_tracking import mark_torrent_removed
            
            if hash_value:
                # Verify specific torrent
                if hash_value.lower() not in active_hashes:
                    mark_torrent_removed(hash_value, "Torrent no longer exists in Real-Debrid")
                    return False
                return True
            else:
                # Verify all tracked torrents
                success = True
                for h, tid in self._cached_torrent_ids.items():
                    if h.lower() not in active_hashes:
                        mark_torrent_removed(h, "Torrent no longer exists in Real-Debrid")
                        success = False
                return success
                
        except Exception as e:
            logging.error(f"Error verifying torrent presence: {str(e)}")
            return False

    def remove_torrent(self, torrent_id: str, removal_reason: str = "Manual removal") -> None:
        """
        Remove a torrent from Real-Debrid
        
        Args:
            torrent_id: ID of the torrent to remove
            removal_reason: Reason for removal (for tracking)
        """
        try:
            # Get torrent info before removal to get the hash
            hash_value = None
            try:
                # Use the api_key property
                info = self.get_torrent_info(torrent_id) # This already uses self.api_key (property)
                if info:
                    hash_value = info.get('hash', '').lower()
            except Exception as e:
                logging.warning(f"Could not get torrent info before removal: {str(e)}")

            # Make the deletion request with retries for rate limiting
            max_retries = 3
            retry_delay = 5  # Start with 5 seconds delay
            removal_successful = False
            
            logging.info(f"Attempting to remove torrent {torrent_id} from Real-Debrid (reason: {removal_reason})")
            
            for retry_attempt in range(max_retries):
                try:
                    # Use the api_key property
                    result = make_request('DELETE', f'/torrents/delete/{torrent_id}', self.api_key)
                    # Check if the request was successful (either None for backward compatibility or success dict)
                    if result is None or (isinstance(result, dict) and result.get('success')):
                        removal_successful = True
                        logging.info(f"Successfully removed torrent {torrent_id} from Real-Debrid (attempt {retry_attempt + 1})")
                        break  # Success, exit retry loop
                    else:
                        # Unexpected response format
                        logging.error(f"Unexpected response format from DELETE request: {result}")
                        raise ProviderUnavailableError(f"Unexpected response format: {result}")
                except ProviderUnavailableError as e:
                    if "429" in str(e) and retry_attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                        logging.warning(f"Rate limit (429) hit when removing torrent {torrent_id}. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                        time.sleep(wait_time)
                    else:
                        # If it's a 429 error and we've exhausted retries, treat as removed anyway
                        if "429" in str(e):
                            logging.warning(f"Rate limit hit when removing torrent {torrent_id} after all retries. Will mark as removed anyway.")
                            removal_successful = True
                            break
                        # Re-raise if it's not a 429 error
                        raise

            if not removal_successful:
                logging.error(f"Failed to remove torrent {torrent_id} after {max_retries} attempts")
                raise ProviderUnavailableError(f"Failed to remove torrent {torrent_id} after {max_retries} attempts")

            # Verify the torrent was actually removed
            try:
                verification_result = make_request('GET', f'/torrents/info/{torrent_id}', self.api_key)
                if verification_result is not None:
                    logging.warning(f"Torrent {torrent_id} still exists after removal attempt. This may indicate a Real-Debrid API issue.")
                else:
                    logging.info(f"Verified torrent {torrent_id} was successfully removed from Real-Debrid")
            except Exception as verify_e:
                if "404" in str(verify_e):
                    logging.info(f"Verified torrent {torrent_id} was successfully removed from Real-Debrid (404 response)")
                else:
                    logging.warning(f"Could not verify removal of torrent {torrent_id}: {str(verify_e)}")

            # Update status and tracking
            self.update_status(torrent_id, TorrentStatus.REMOVED)
            
            # Record removal in tracking database if we have the hash
            if hash_value:
                from database.torrent_tracking import mark_torrent_removed
                mark_torrent_removed(hash_value, removal_reason)
                
                # Clean up cached data
                if hash_value in self._cached_torrent_ids:
                    del self._cached_torrent_ids[hash_value]
                if hash_value in self._cached_torrent_titles:
                    del self._cached_torrent_titles[hash_value]
                if hash_value in self._all_torrent_ids:
                    del self._all_torrent_ids[hash_value]
                    
        except Exception as e:
            if "404" in str(e):
                logging.warning(f"Torrent {torrent_id} already removed from Real-Debrid")
                # Still try to update tracking if we have the hash
                if hash_value:
                    from database.torrent_tracking import mark_torrent_removed
                    mark_torrent_removed(hash_value, f"{removal_reason} (already removed)")
            else:
                logging.error(f"Error removing torrent {torrent_id}: {str(e)}", exc_info=True)
                # Get caller information for debugging
                caller_frame = inspect.currentframe().f_back
                caller_info = f"{caller_frame.f_code.co_filename}:{caller_frame.f_code.co_name}:{caller_frame.f_lineno}"
                logging.error(f"Remove torrent failed when called from {caller_info}")
            raise

    def get_torrent_file_list(self, magnet_link: str) -> Optional[Tuple[List[Dict], str, str]]:
        """
        Adds a torrent via magnet link, retrieves its file list and basic info, 
        and then removes it.

        Args:
            magnet_link: The magnet link of the torrent.

        Returns:
            A tuple containing: (list of file dictionaries, torrent filename, torrent ID), 
            or None if an error occurs.
        """
        torrent_id = None
        info = None
        try:
            logging.info(f"Adding torrent for file listing: {magnet_link[:60]}...")
            torrent_id = self.add_torrent(magnet_link)
            if not torrent_id:
                logging.error("Failed to add torrent for file listing.")
                return None

            # Wait a moment for RD to process the torrent before getting info
            # Increased wait time slightly for potentially larger torrents
            time.sleep(3) 

            logging.info(f"Getting info for torrent ID: {torrent_id}")
            info = self.get_torrent_info(torrent_id)
            if not info:
                logging.error(f"Failed to get torrent info for ID: {torrent_id}")
                # Attempt removal even if info fetch failed
                return None 

            files = info.get('files', [])
            filename = info.get('filename', 'Unknown Filename') # Get filename
            
            # Ensure files is a list
            if isinstance(files, dict):
                files = list(files.values())
            elif not isinstance(files, list):
                files = []
                
            logging.info(f"Successfully retrieved {len(files)} files for torrent ID: {torrent_id} (Filename: {filename})")
            # Return files, filename, and torrent_id BEFORE the finally block removes it
            return files, filename, torrent_id

        except TorrentAdditionError as e:
            logging.error(f"Error adding torrent during file listing: {str(e)}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred during torrent file listing: {str(e)}")
            return None
        finally:
            # Ensure removal happens even if info retrieval failed but torrent_id was obtained
            if torrent_id:
                try:
                    logging.info(f"Removing temporary torrent ID: {torrent_id} (after file listing)")
                    # Use a specific removal reason
                    reason = "Temporary add for file listing" 
                    if not info: # Add context if info fetch failed
                        reason += " (info fetch failed)"
                    self.remove_torrent(torrent_id, reason)
                except Exception as e:
                    logging.error(f"Error removing temporary torrent {torrent_id} after file listing: {str(e)}")
                    self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED) 

    def cleanup(self) -> None:
        """Clean up status tracking only"""
        try:
            # Verify tracked torrents are still present
            self.verify_torrent_presence()
        except Exception as e:
            logging.error(f"Error during status tracking cleanup: {str(e)}")
        finally:
            # Always clean up status tracking
            super().cleanup()

    def is_cached_sync(self, magnet_link: str, temp_file_path: Optional[str] = None, result_title: Optional[str] = None, result_index: Optional[str] = None, remove_uncached: bool = True, remove_cached: bool = False, skip_phalanx_db: bool = False, imdb_id: Optional[str] = None) -> Union[bool, Dict[str, bool], None]:
        """Synchronous version of is_cached"""
        try:
            # Create a new event loop for this thread if one doesn't exist
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # Run the async method in the event loop
            return loop.run_until_complete(
                self.is_cached(magnet_link, temp_file_path, result_title, result_index, remove_uncached, remove_cached, skip_phalanx_db, imdb_id)
            )
        except Exception as e:
            logging.error(f"Error in is_cached_sync: {str(e)}")
            return None

    # Add a helper method to verify removal state
    def verify_removal_state(self) -> None:
        """Verify removal state by listing all torrents in the account"""
        try:
            torrents = make_request('GET', '/torrents', self.api_key) or []
            
            for torrent in torrents[:5]:  # Log the first 5 torrents for debugging
                torrent_id = torrent.get('id', 'unknown')
                hash_value = torrent.get('hash', 'unknown')
                status = torrent.get('status', 'unknown')
                filename = torrent.get('filename', 'unknown')
        except Exception as e:
            logging.error(f"Error verifying removal state: {str(e)}")

    # Keep cache decorator commented out as requested
    # @timed_lru_cache(seconds=3600)
    async def get_total_library_size(self) -> Optional[str]:
        """
        Calculates the total size of the user's library on Real-Debrid asynchronously
        by fetching and summing all items in the /torrents list. Writes successful
        result to a cache file.

        Returns:
            Optional[str]: Human-readable total size, or specific error strings.
        """
        total_size_bytes = 0
        calculated_size_str = None # Variable to hold the successful result before returning
        error_result = None # Variable to hold error string

        try:
            logging.info("Executing get_total_library_size (fetching /torrents - all)...")
            torrents = await get_all_torrents(self.api_key)

            if torrents is None:
                logging.error("Failed to fetch torrents from Real-Debrid API for size calculation.")
                error_result = "Error (API)"
                # Don't return yet, let the finally block (outside try) handle logic

            elif not torrents:
                 logging.info("No torrents found in the account.")
                 calculated_size_str = "0 B" # Successful calculation of 0

            else:
                # Calculation logic remains the same
                processed_hashes = set()
                item_count = 0
                for item in torrents:
                    item_hash = item.get('hash')
                    if item_hash and item_hash in processed_hashes:
                        continue

                    item_size = item.get('bytes', 0)

                    if isinstance(item_size, (int, float)) and item_size >= 0:
                        total_size_bytes += item_size
                        item_count += 1
                        if item_hash:
                            processed_hashes.add(item_hash)
                    else:
                         logging.warning(f"Invalid or missing 'bytes' field value '{item_size}' for torrent: {item.get('filename', item.get('id', 'N/A'))}")

                logging.info(f"Processed {item_count} unique torrent items. Total calculated size: {total_size_bytes} bytes.")

                # Convert bytes to human-readable format
                if total_size_bytes == 0:
                    calculated_size_str = "0 B"
                else:
                    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
                    if total_size_bytes <= 0:
                         calculated_size_str = "0 B"
                    else:
                        try:
                            i = int(math.floor(math.log(total_size_bytes, 1024)))
                            i = max(0, min(i, len(size_name) - 1))
                            p = math.pow(1024, i)
                            s = round(total_size_bytes / p, 2)
                            calculated_size_str = f"{s} {size_name[i]}" # Store successful result
                        except ValueError:
                             logging.error(f"Math error converting bytes: {total_size_bytes}", exc_info=True)
                             error_result = "Error (Math)"

        except Exception as e:
            logging.error(f"Error fetching/processing torrents for library size: {e}", exc_info=True)
            error_result = "Error (Server)"
            # Don't return yet

        # --- Post-calculation Logic ---
        if calculated_size_str is not None:
             # Write successful result to cache
             _write_size_cache(calculated_size_str)
             return calculated_size_str # Return the fresh calculation
        else:
             # Return the specific error encountered
             # The caller (API route) will handle reading from cache if needed
             return error_result if error_result else "Error (Unknown)"
