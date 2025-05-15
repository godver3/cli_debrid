"""
Handles processing of torrent files and magnet links, including cache checking
and addition to debrid service accounts.
"""

import logging
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
import tempfile
import requests
import os
import bencodepy
import hashlib
import inspect
from datetime import datetime, timedelta

from debrid.base import DebridProvider, TooManyDownloadsError
from debrid.common import (
    extract_hash_from_magnet,
    extract_hash_from_file,
    is_video_file,
    is_unwanted_file,
    download_and_extract_hash
)
from debrid.status import TorrentStatus
from database.not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
from utilities.settings import get_setting

class TorrentProcessingError(Exception):
    """Base exception for torrent processing errors"""
    pass

class NoVideoFilesError(TorrentProcessingError):
    """Raised when a torrent has no valid video files"""
    pass

class TorrentAdditionError(TorrentProcessingError):
    """Raised when a torrent fails to be added to the debrid service"""
    pass

class TorrentProcessor:
    """Handles torrent file/magnet processing and caching checks"""
    
    def __init__(self, debrid_provider: DebridProvider):
        """
        Initialize the processor
        
        Args:
            debrid_provider: Debrid service provider to use
        """
        self.debrid_provider = debrid_provider
        self._last_direct_checks = {}
        self._direct_check_interval = timedelta(minutes=5)  # Configurable
        
    def _should_direct_check(self, hash_value: str) -> bool:
        """
        Determine if we should perform a direct cache check based on rate limiting
        
        Args:
            hash_value: Torrent hash to check
            
        Returns:
            bool: True if direct check should be performed
        """
        now = datetime.now()
        if hash_value in self._last_direct_checks:
            if now - self._last_direct_checks[hash_value] < self._direct_check_interval:
                return False
        self._last_direct_checks[hash_value] = now
        return True
        
    def check_cache_status(self, magnet_or_url: str, temp_file: Optional[str] = None, remove_cached: bool = False) -> Tuple[bool, str]:
        """
        Enhanced cache status checking with forced verification for uncached items
        
        Args:
            magnet_or_url: Magnet link or URL to check
            temp_file: Optional path to temporary torrent file
            remove_cached: Whether to remove cached torrents (default: False)
            
        Returns:
            Tuple[bool, str]: (is_cached, cache_source)
            cache_source can be:
                - 'db_cached': Found cached in database
                - 'db_uncached_verified': Found uncached in database and verified
                - 'direct_check': Direct check with debrid provider
                - 'rate_limited': Using cached uncached status due to rate limit
        """
        try:
            logging.debug(f"Starting enhanced cache_status check with remove_uncached=True and remove_cached={remove_cached}")
            # Extract hash for cache lookup
            hash_value = None
            if magnet_or_url and magnet_or_url.startswith('magnet:'):
                hash_value = extract_hash_from_magnet(magnet_or_url)
            elif temp_file:
                hash_value = extract_hash_from_file(temp_file)
                
            if not hash_value:
                logging.warning("Could not extract hash for cache check, falling back to direct check")
                direct_check = self.debrid_provider.is_cached_sync(
                    magnet_or_url if not temp_file else "",
                    temp_file,
                    remove_uncached=True,
                    remove_cached=remove_cached
                )
                return direct_check, 'direct_check'
            
            # Check if phalanx db is enabled using settings
            phalanx_enabled = get_setting('UI Settings', 'enable_phalanx_db', default=False)
            
            # Check if we have a cached status
            if phalanx_enabled and hasattr(self.debrid_provider, 'get_cached_status'):
                db_cache_status = self.debrid_provider.get_cached_status(hash_value)
                
                if db_cache_status:
                    if db_cache_status.get('is_cached', False):
                        # Trust cached status
                        return True, 'db_cached'
                    else:
                        # For uncached status, verify if rate limiting allows
                        if self._should_direct_check(hash_value):
                            direct_check = self.debrid_provider.is_cached_sync(
                                magnet_or_url if not temp_file else "",
                                temp_file,
                                remove_uncached=True,
                                remove_cached=remove_cached
                            )
                            
                            if direct_check != db_cache_status.get('is_cached', False):
                                if phalanx_enabled and hasattr(self.debrid_provider, 'update_cached_status'):
                                    self.debrid_provider.update_cached_status(hash_value, direct_check)
                            
                            return direct_check, 'db_uncached_verified'
                        else:
                            return False, 'rate_limited'
            
            # If no cached status or provider doesn't support caching or phalanx is disabled
            direct_check = self.debrid_provider.is_cached_sync(
                magnet_or_url if not temp_file else "",
                temp_file,
                remove_uncached=True,
                remove_cached=remove_cached
            )
            
            # Store the result if provider supports it and phalanx is enabled
            if phalanx_enabled and hasattr(self.debrid_provider, 'update_cached_status'):
                self.debrid_provider.update_cached_status(hash_value, direct_check)
            
            return direct_check, 'direct_check'
            
        except Exception as e:
            logging.error(f"Error in enhanced cache check: {str(e)}", exc_info=True)
            # Fall back to direct check on error
            direct_check = self.debrid_provider.is_cached_sync(
                magnet_or_url if not temp_file else "",
                temp_file,
                remove_uncached=True,
                remove_cached=remove_cached
            )
            return direct_check, 'direct_check'
        
    def process_torrent(self, magnet_or_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Process a magnet link or torrent URL
        
        Args:
            magnet_or_url: Either a magnet link or URL to a torrent file
            
        Returns:
            Tuple of (magnet_link, temp_file_path) where:
                - For magnet links: (magnet_link, None)
                - For torrent files: (None, temp_file_path)
                - For URLs resolving to magnets: (magnet_link, None)
                - Returns (None, None) on error
        """
        try:
            # Handle magnet links first and return early
            if magnet_or_url.startswith('magnet:'):
                return magnet_or_url, None
                
            # Only try URL processing for non-magnet links
            parsed = urlparse(magnet_or_url)
            if not parsed.scheme or not parsed.netloc:
                logging.error(f"Invalid URL: {magnet_or_url}")
                return None, None
                
            with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as tmp:
                temp_file_path = tmp.name # Store path early for cleanup
                try:
                    # Disable automatic redirects to handle magnet redirects manually
                    response = requests.get(magnet_or_url, timeout=30, allow_redirects=False)

                    # Check if server responded with a redirect
                    if response.is_redirect or response.is_permanent_redirect:
                        location = response.headers.get('Location')
                        # Check if the redirect location is a magnet link
                        if location and location.startswith('magnet:'):
                            logging.info(f"URL {magnet_or_url} redirected to a magnet link.")
                            # Clean up the temp file we created but won't use
                            try:
                                os.unlink(temp_file_path)
                            except OSError as e:
                                logging.warning(f"Could not delete temporary file {temp_file_path} after finding magnet redirect: {e}")
                            return location.strip(), None # Return the extracted magnet link

                    # If it wasn't a redirect to a magnet, raise errors for non-2xx status codes
                    # This will catch issues like 404 Not Found, 500 Server Error, etc.
                    # It will also catch redirects to non-magnet URLs if we don't handle them further,
                    # though requests would normally follow those if allow_redirects was True.
                    response.raise_for_status()

                    # Check if the response body starts with 'magnet:'
                    # Decode safely, only need the first few bytes to check
                    content_start = response.content[:10].decode('utf-8', errors='ignore').strip()
                    if content_start.startswith('magnet:'):
                        logging.info(f"URL {magnet_or_url} resolved to a magnet link (content).")
                        # Clean up the temp file we created but won't use
                        try:
                            os.unlink(temp_file_path)
                        except OSError as e:
                            logging.warning(f"Could not delete temporary file {temp_file_path} after finding magnet link in content: {e}")
                        # Return the full response text as the magnet link
                        return response.text.strip(), None
                    
                    # If not a magnet link, assume it's torrent file content
                    tmp.write(response.content)
                    tmp.flush()
                    return None, temp_file_path # Return None for magnet, path for file
                except requests.exceptions.RequestException as e:
                    # Catch specific requests errors for better logging
                    logging.error(f"Failed request for URL {magnet_or_url}: {str(e)}")
                    try:
                        os.unlink(temp_file_path)
                    except OSError:
                        pass # File might not exist or other issues
                    return None, None
                except Exception as e:
                    logging.error(f"Failed to process content from URL {magnet_or_url}: {str(e)}")
                    try:
                        os.unlink(temp_file_path)
                    except OSError:
                        pass # File might not exist or other issues
                    return None, None
            
        except Exception as e:
            logging.error(f"Error processing magnet/URL {magnet_or_url}: {str(e)}", exc_info=True)
            # Clean up temp file if it exists and path was assigned
            if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
                 try:
                     os.unlink(temp_file_path)
                 except OSError:
                     pass
            return None, None
            
    def check_cache_for_url(self, url: str, remove_cached: bool = False) -> Optional[bool]:
        """
        Download a torrent file from URL and check if it's cached
        
        Args:
            url: URL to the torrent file
            remove_cached: Whether to remove cached torrents (default: False)
            
        Returns:
            - True: Torrent is cached
            - False: Torrent is not cached
            - None: Error occurred
        """
        torrent_id_to_remove = None
        
        try:
            import tempfile
            import requests
            import os
            import bencodepy
            import hashlib
            from debrid.common.torrent import torrent_to_magnet
            
            # Create a temporary file to store the torrent
            with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as tmp:
                temp_file_path = tmp.name
                
                try:
                    # Download the torrent file
                    logging.info(f"Downloading torrent file from {url} to {temp_file_path}")
                    response = requests.get(url, timeout=30, allow_redirects=False)
                    
                    if response.status_code >= 200 and response.status_code < 300:
                        # It's a direct download or successful response
                        # ... (original code to save and parse torrent file) ...
                        # ... then extract hash/magnet and check cache with debrid provider ...
                        # This path implies the URL directly gave a .torrent file content
                        # We need to write it and then check it.
                        # For now, assuming this part leads to temp_file_path being used correctly later
                        # by the caller or that this path is not taken in the reported issue.
                        # The original issue is about HTTP -> magnet redirect.
                        tmp.write(response.content)
                        tmp.flush()
                        # Now that we have the file, we should check its cache status.
                        # The current structure of check_cache_for_url returning Optional[bool]
                        # and check_cache using check_cache_status suggests we should leverage check_cache_status.
                        # However, check_cache_status expects a magnet or a temp_file.
                        # We have temp_file_path here.
                        is_cached_bool, _ = self.check_cache_status(magnet_or_url=None, temp_file=temp_file_path, remove_cached=remove_cached)
                        return is_cached_bool

                    elif response.status_code >= 300 and response.status_code < 400 and 'Location' in response.headers:
                        redirect_url = response.headers['Location']
                        if redirect_url.startswith('magnet:'):
                            logging.info(f"Redirected to magnet link: {redirect_url}")
                            # Process this magnet_link by calling check_cache_status
                            # temp_file should be None as we are now dealing with a magnet link.
                            # The original temp_file_path (for the downloaded .torrent from the initial URL)
                            # will be cleaned up by the `finally` block.
                            is_cached_status, _ = self.check_cache_status(redirect_url, temp_file=None, remove_cached=remove_cached)
                            return is_cached_status
                            
                        elif redirect_url.startswith('http://') or redirect_url.startswith('https://'):
                            logging.info(f"Redirected to another HTTP/S URL: {redirect_url}. Consider handling chained redirects or re-calling with new URL.")
                            # For now, treat as unhandled and likely not cached for this step's purpose.
                            # A more robust solution might re-call check_cache_for_url or similar.
                            return False # Or None, depending on desired behavior for unhandled http-to-http redirect
                        else:
                            logging.error(f"Unhandled redirect location: {redirect_url}")
                            return None # Error or unhandled case
                    else:
                        response.raise_for_status() # Raise an exception for other error codes

                finally:
                    # Clean up temporary file
                    try:
                        os.unlink(temp_file_path)
                        logging.info(f"Removed temporary file: {temp_file_path}")
                    except Exception as e:
                        logging.warning(f"Failed to delete temporary file: {str(e)}")
            
            return None
            
        except Exception as e:
            logging.error(f"Error checking cache for URL: {str(e)}", exc_info=True)
            return None
            
    def check_cache(self, magnet_or_url: str, temp_file: Optional[str] = None, remove_cached: bool = False) -> Optional[bool]:
        """
        Check if a magnet link or torrent file is cached
        
        Args:
            magnet_or_url: Magnet link or URL
            temp_file: Optional path to torrent file
            remove_cached: Whether to remove cached torrents (default: False)
            
        Returns:
            - True: Torrent is cached
            - False: Torrent is not cached
            - None: Error occurred (no video files, invalid magnet, etc)
        """
        try:
            
            # Handle URLs that are torrent files
            if not magnet_or_url.startswith('magnet:') and (
                magnet_or_url.startswith('http') or 
                'jackett' in magnet_or_url.lower() or 
                'prowlarr' in magnet_or_url.lower()):
                logging.debug("Processing URL as a potential torrent file")
                return self.check_cache_for_url(magnet_or_url, remove_cached=remove_cached)
            
            # Use enhanced cache checking
            is_cached, cache_source = self.check_cache_status(magnet_or_url, temp_file, remove_cached=remove_cached)
            logging.debug(f"Cache check result: {is_cached} (source: {cache_source})")
            
            # Check if this is a RealDebridProvider and explicitly verify removal
            if not is_cached and hasattr(self.debrid_provider, 'verify_removal_state'):
                self.debrid_provider.verify_removal_state()
            
            return is_cached
            
        except Exception as e:
            logging.error(f"Error checking cache: {str(e)}", exc_info=True)
            return None
        
    def add_to_account(self, magnet_or_url: str) -> Optional[Dict]:
        """
        Add a magnet or torrent to the debrid account
        
        Args:
            magnet_or_url: Magnet link or torrent URL to add
            
        Returns:
            Torrent info if successful, None otherwise
        """
        torrent_id = None
        info = None
        max_retries = 3
        retry_delay = 2  # seconds
        temp_file = None

        # Get caller information
        caller_frame = inspect.currentframe().f_back
        caller_info = f"{caller_frame.f_code.co_filename}:{caller_frame.f_code.co_name}:{caller_frame.f_lineno}"
        logging.info(f"TorrentProcessor.add_to_account called from {caller_info}")
        
        try:
            magnet, temp_file = self.process_torrent(magnet_or_url)
            add_response = self.debrid_provider.add_torrent(magnet if magnet else None, temp_file)
            
            torrent_id = add_response
            if not torrent_id:
                logging.error("Failed to add torrent - no torrent ID returned")
                return None
                
            logging.info(f"Successfully added torrent (ID: {torrent_id})")
            
            for attempt in range(max_retries):
                info = self.debrid_provider.get_torrent_info(torrent_id)
                
                if not info or len(info.get('files', [])) == 0:
                    time.sleep(retry_delay)
                    continue
                
                return info
            
            if not info:
                logging.error(f"Failed to get info for torrent {torrent_id} after {max_retries} attempts")
            else:
                logging.error(f"No files found in torrent {torrent_id} after {max_retries} attempts")
            return info
            
        except Exception as e:
            logging.error(f"Error adding magnet: {str(e)}", exc_info=True)
            return None
            
        finally:
            if temp_file:
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    logging.error(f"Error cleaning up temp file: {str(e)}")
            
            if torrent_id and (not info or len(info.get('files', [])) == 0):
                try:
                    logging.info(f"Attempting to remove empty/failed torrent {torrent_id}")
                    self.debrid_provider.remove_torrent(
                        torrent_id,
                        removal_reason="Empty or failed torrent during processing"
                    )
                    logging.info(f"Successfully removed empty/failed torrent {torrent_id}")
                except Exception as e:
                    logging.error(f"Error cleaning up empty/failed torrent {torrent_id}: {str(e)}", exc_info=True)
                    
    def process_results(
        self,
        results: list[Dict],
        accept_uncached: bool = False,
        item: Optional[Dict] = None
    ) -> Tuple[Optional[Dict], Optional[str], Optional[Dict]]:
        """
        Process a list of results to find the best match
        
        Args:
            results: List of results to process
            accept_uncached: Whether to accept uncached results
            item: Optional media item for tracking successful results
            
        Returns:
            Tuple of (torrent_info, magnet_link, chosen_result) if successful, (None, None, None) otherwise
        """
        item_identifier = item.get('title', 'Unknown') if item else 'Unknown'
        logging.info(f"[{item_identifier}] Starting to process {len(results)} results (accept_uncached={accept_uncached})")
        
        for idx, result in enumerate(results, 1):
            chosen_result_for_return = None # Initialize variable to hold the chosen result
            try:
                original_link = result.get('magnet') or result.get('link')
                if not original_link:
                    continue
                    
                result_title = result.get('title', 'Unknown title')
                logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Processing: {result_title}")
                logging.debug(f"[{item_identifier}] [Result {idx}/{len(results)}] Raw result data: {result}")
                
                magnet, temp_file = self.process_torrent(original_link)
                if not magnet and not temp_file:
                    logging.warning(f"[{item_identifier}] [Result {idx}/{len(results)}] Failed to process magnet/torrent")
                    continue
                    
                logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] PHASE: Cache Check - Starting cache status check")
                is_cached, cache_source = self.check_cache_status(
                    magnet if not temp_file else "",
                    temp_file
                )
                    
                if is_cached is None:
                    logging.warning(f"[{item_identifier}] [Result {idx}/{len(results)}] Cache check returned None, skipping result")
                    continue
                    
                logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Cache status: {'Cached' if is_cached else 'Not cached'}")
                
                if not accept_uncached and not is_cached:
                    logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Skipping uncached result (accept_uncached=False)")
                    continue
                
                if not is_cached:
                    try:
                        active_downloads, download_limit = self.debrid_provider.get_active_downloads()
                        if active_downloads >= download_limit:
                            logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Download limit reached ({active_downloads}/{download_limit}). Moving to pending uncached queue.")
                            if item:
                                from database import update_media_item_state
                                update_media_item_state(item['id'], "Pending Uncached", 
                                    filled_by_magnet=original_link,
                                    filled_by_title=result.get('title', ''))
                                item['filled_by_magnet'] = original_link
                                item['filled_by_title'] = result.get('title', '')
                            # Return None, original_link, AND the result that triggered this
                            return None, original_link, result
                    except TooManyDownloadsError:
                        logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Download limit reached. Moving to pending uncached queue.")
                        if item:
                            from database import update_media_item_state
                            update_media_item_state(item['id'], "Pending Uncached",
                                filled_by_magnet=original_link,
                                filled_by_title=result.get('title', ''))
                            item['filled_by_magnet'] = original_link
                            item['filled_by_title'] = result.get('title', '')
                        # Return None, original_link, AND the result that triggered this
                        return None, original_link, result
                    except Exception as e:
                        logging.error(f"[{item_identifier}] [Result {idx}/{len(results)}] Error checking download limits: {str(e)}")
                        continue

                info = None
                torrent_title = None
                if is_cached:
                    hash_value = None
                    if magnet:
                        hash_value = extract_hash_from_magnet(magnet)
                    elif temp_file:
                        hash_value = extract_hash_from_file(temp_file)
                        
                    if hash_value:
                        torrent_id = self.debrid_provider.get_cached_torrent_id(hash_value)
                        if torrent_id:
                            logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] PHASE: Info Fetch - Getting info for cached torrent")
                            info = self.debrid_provider.get_torrent_info(torrent_id)
                            torrent_title = self.debrid_provider.get_cached_torrent_title(hash_value)
                
                if not info:
                    try:
                        # Extract hash to check if it already exists
                        hash_value = None
                        if magnet:
                            hash_value = extract_hash_from_magnet(magnet)
                        elif temp_file:
                            hash_value = extract_hash_from_file(temp_file)
                            
                        # Check if this torrent was already added during cache check
                        existing_torrent_id = None
                        if hash_value:
                            existing_torrent_id = self.debrid_provider._all_torrent_ids.get(hash_value)
                            
                        if existing_torrent_id:
                            logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Reusing existing torrent ID: {existing_torrent_id}")
                            info = self.debrid_provider.get_torrent_info(existing_torrent_id)
                        else:
                            logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] PHASE: Addition - Adding to debrid service")
                            info = self.add_to_account(original_link)
                        
                        if info:
                            # Extract hash after successful addition
                            hash_value = None
                            if magnet:
                                hash_value = extract_hash_from_magnet(magnet)
                            elif temp_file:
                                hash_value = extract_hash_from_file(temp_file)

                            if hash_value and item:
                                from database.torrent_tracking import record_torrent_addition, update_torrent_tracking, get_torrent_history
                                # Prepare item data
                                item_data = {
                                    'title': item.get('title'),
                                    'type': item.get('type'),
                                    'version': item.get('version'),
                                    'tmdb_id': item.get('tmdb_id'),
                                    'state': item.get('state')
                                }
                                
                                # Check recent history for this hash
                                history = get_torrent_history(hash_value)
                                
                                # If there's a recent entry, update it instead of creating new one
                                if history:
                                    update_torrent_tracking(
                                        torrent_hash=hash_value,
                                        item_data=item_data,
                                        trigger_details={
                                            'source': 'adding_queue',
                                            'queue_initiated': True,
                                            'accept_uncached': accept_uncached,
                                            'torrent_info': {
                                                'id': info.get('id'),
                                                'filename': info.get('filename'),
                                                'is_cached': is_cached
                                            }
                                        },
                                        trigger_source='queue_add',
                                        rationale='Added via adding queue processing'
                                    )
                                    logging.info(f"[{item_identifier}] Updated existing torrent tracking entry for hash {hash_value}")
                                else:
                                    # Record new addition if no history exists
                                    record_torrent_addition(
                                        torrent_hash=hash_value,
                                        trigger_source='queue_add',
                                        rationale='Added via adding queue processing',
                                        item_data=item_data,
                                        trigger_details={
                                            'source': 'adding_queue',
                                            'queue_initiated': True,
                                            'accept_uncached': accept_uncached,
                                            'torrent_info': {
                                                'id': info.get('id'),
                                                'filename': info.get('filename'),
                                                'is_cached': is_cached
                                            }
                                        }
                                    )
                                    logging.info(f"[{item_identifier}] Recorded new torrent addition for hash {hash_value}")
                                    
                            torrent_title = info.get('filename', '')
                            logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Successfully added torrent with ID: {info.get('id')}")
                    finally:
                        if temp_file and os.path.exists(temp_file):
                            try:
                                os.unlink(temp_file)
                            except Exception as e:
                                logging.error(f"[{item_identifier}] [Result {idx}/{len(results)}] Error cleaning up temp file: {str(e)}")
            
                if info:
                    info['title'] = torrent_title or result.get('title', '')
                    info['original_scraped_torrent_title'] = result.get('original_title')
                    info['downloading'] = not is_cached
                    logging.debug(f"[{item_identifier}] [Result {idx}/{len(results)}] Full torrent info response: {info}")
                    if len(info.get('files', [])) > 0:
                        definitive_hash = info.get('hash')

                        # original_link is from `result.get('magnet') or result.get('link')` from loop start
                        if item and definitive_hash:
                            try:
                                # Add to not_wanted list using the definitive_hash
                                add_to_not_wanted(definitive_hash)
                                if original_link and original_link.startswith('http'): # original_link was defined at the start of the loop iteration
                                    add_to_not_wanted_urls(original_link)

                                # Record torrent tracking using the definitive_hash
                                from database.torrent_tracking import record_torrent_addition, update_torrent_tracking, get_torrent_history
                                # Prepare item data
                                item_data = {
                                    'title': item.get('title'),
                                    'type': item.get('type'),
                                    'version': item.get('version'),
                                    'tmdb_id': item.get('tmdb_id'),
                                    'state': item.get('state')
                                }
                                
                                # Check recent history for this hash
                                history = get_torrent_history(definitive_hash)
                                
                                trigger_details={
                                    'source': 'adding_queue',
                                    'queue_initiated': True,
                                    'accept_uncached': accept_uncached,
                                    'torrent_info': {
                                        'id': info.get('id'),
                                        'filename': info.get('filename'),
                                        'is_cached': is_cached # This is_cached is from the earlier check_cache_status
                                    }
                                }

                                if history:
                                    update_torrent_tracking(
                                        torrent_hash=definitive_hash,
                                        item_data=item_data,
                                        trigger_details=trigger_details,
                                        trigger_source='queue_add',
                                        rationale='Added via adding queue processing'
                                    )
                                    logging.info(f"[{item_identifier}] Updated existing torrent tracking entry for hash {definitive_hash}")
                                else:
                                    # Record new addition if no history exists
                                    record_torrent_addition(
                                        torrent_hash=definitive_hash,
                                        trigger_source='queue_add',
                                        rationale='Added via adding queue processing',
                                        item_data=item_data,
                                        trigger_details=trigger_details
                                    )
                                    logging.info(f"[{item_identifier}] Recorded new torrent addition for hash {definitive_hash}")
                            
                            except Exception as e:
                                logging.error(f"[{item_identifier}] [Result {idx}/{len(results)}] Error in post-addition processing (not_wanted/tracking) for hash {definitive_hash if definitive_hash else 'N/A'}: {str(e)}")
                        
                        elif item and not definitive_hash:
                             logging.warning(f"[{item_identifier}] [Result {idx}/{len(results)}] No definitive_hash in torrent info. Skipping not_wanted and tracking. Original link: {original_link if original_link else 'N/A'}")

                        logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Successfully processed and added")
                        chosen_result_for_return = result # Store the successful result
                        return info, original_link, chosen_result_for_return # Return all three
                    else:
                        try:
                            if info.get('id'):
                                logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Removing empty torrent {info.get('id')}")
                                self.debrid_provider.remove_torrent(
                                    info['id'],
                                    removal_reason="No files found in torrent after addition"
                                )
                        except Exception as e:
                            logging.error(f"[{item_identifier}] [Result {idx}/{len(results)}] Error removing empty torrent {info.get('id')}: {str(e)}")
                else:
                    logging.error(f"[{item_identifier}] [Result {idx}/{len(results)}] Failed to add torrent")
                
            except Exception as e:
                logging.error(f"[{item_identifier}] [Result {idx}/{len(results)}] Error processing result: {str(e)}", exc_info=True)
                continue
                
        logging.info(f"[{item_identifier}] No suitable results found after processing all options")
        return None, None, None # Return None for all three if no suitable result found
