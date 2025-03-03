import logging
from typing import Dict, List, Optional, Union, Tuple
from datetime import datetime, timedelta
import tempfile
import os
import time
from urllib.parse import unquote
import hashlib
import bencodepy
import inspect

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
from .api import make_request
from not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls

class RealDebridProvider(DebridProvider):
    """Real-Debrid implementation of the DebridProvider interface"""
    
    API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
    MAX_DOWNLOADS = 25
    
    def __init__(self):
        super().__init__()
        self._cached_torrent_ids = {}  # Store torrent IDs for cached content
        self._cached_torrent_titles = {}  # Store torrent titles for cached content
        self._all_torrent_ids = {}  # Store all torrent IDs for tracking
        
    def _load_api_key(self) -> str:
        """Load API key from settings"""
        try:
            from .api import get_api_key
            return get_api_key()
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to load API key: {str(e)}")

    def is_cached(self, magnet_links: Union[str, List[str]], temp_file_path: Optional[str] = None, result_title: Optional[str] = None, result_index: Optional[str] = None, remove_uncached: bool = True) -> Union[bool, Dict[str, bool], None]:
        """
        Check if one or more magnet links or torrent files are cached on Real-Debrid.
        If a single input is provided, returns a boolean or None (for error).
        If a list of inputs is provided, returns a dict mapping hashes to booleans or None (for error).
        
        Args:
            magnet_links: Either a magnet link or list of magnet links
            temp_file_path: Optional path to torrent file
            result_title: Optional title of the result being checked (for logging)
            result_index: Optional index of the result in the list (for logging)
            remove_uncached: Whether to remove uncached torrents after checking (default: True)
            
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
        
        logging.debug(f"{log_prefix} Starting cache check for {len([magnet_links] if isinstance(magnet_links, str) else magnet_links)} magnet(s)")
        logging.debug(f"{log_prefix} Temp file path: {temp_file_path}")
        
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
            elif magnet_link and magnet_link.startswith('magnet:'):
                hash_value = extract_hash_from_magnet(magnet_link)
            elif magnet_link and len(magnet_link) == 40 and all(c in '0123456789abcdefABCDEF' for c in magnet_link):
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
            
            torrent_id = None
            try:
                # Add the magnet/torrent to RD
                logging.info(f"{log_prefix} PHASE: Addition - Adding to Real-Debrid for cache check")
                torrent_id = self.add_torrent(magnet_link if magnet_link and magnet_link.startswith('magnet:') else None, temp_file_path)
                
                if not torrent_id:
                    # If add_torrent returns None, the torrent might already be added
                    # Try to get the hash and look up existing torrent
                    if hash_value:
                        logging.info(f"{log_prefix} PHASE: Lookup - Checking for existing torrent")
                        # Search for existing torrent with this hash
                        torrents = make_request('GET', '/torrents', self.api_key) or []
                        for torrent in torrents:
                            if torrent.get('hash', '').lower() == hash_value.lower():
                                torrent_id = torrent['id']
                                logging.info(f"{log_prefix} Found existing torrent with ID {torrent_id}")
                                break
                    
                    if not torrent_id:
                        results[hash_value] = False
                        continue
                    
                # Get torrent info
                logging.info(f"{log_prefix} PHASE: Info Fetch - Getting torrent info")
                info = self.get_torrent_info(torrent_id)
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
                logging.debug(f"{log_prefix} Torrent status: {status}")
                
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
                logging.info(f"{log_prefix} Cache status: {'Cached' if is_cached else 'Not cached'}")
                
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
                else:
                    if remove_uncached:
                        try:
                            self.remove_torrent(torrent_id, "Torrent is not cached - removed after cache check")
                            from database.torrent_tracking import update_cache_check_removal
                            update_cache_check_removal(hash_value)
                        except Exception as e:
                            logging.error(f"{log_prefix} Error removing uncached torrent: {str(e)}")
                            self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                    else:
                        # Keep the torrent for later use
                        logging.info(f"{log_prefix} Keeping uncached torrent (ID: {torrent_id}) for later use")
                
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
            from settings import get_setting
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
            from settings import get_setting
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'downloaded': 0, 'limit': None}

            traffic_info = make_request('GET', '/traffic/details', self.api_key)
            overall_traffic = make_request('GET', '/traffic', self.api_key)

            try:
                # Get today in UTC since Real-Debrid uses UTC dates
                today_utc = datetime.utcnow().strftime("%Y-%m-%d")
                
                # Get today's traffic
                daily_traffic = traffic_info.get(today_utc, {})
                
                if not daily_traffic:
                    logging.error(f"No traffic data found for {today_utc}")
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
                info = self.get_torrent_info(torrent_id)
                if info:
                    hash_value = info.get('hash', '').lower()
            except Exception as e:
                logging.warning(f"Could not get torrent info before removal: {str(e)}")

            logging.info(f"Attempting to remove torrent {torrent_id} from Real-Debrid")
            make_request('DELETE', f'/torrents/delete/{torrent_id}', self.api_key)
            logging.info(f"Successfully removed torrent {torrent_id} from Real-Debrid")
            
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
