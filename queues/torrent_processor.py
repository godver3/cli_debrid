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

from debrid import get_debrid_providers
from debrid.base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError
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
        Initialize the processor.

        debrid_provider is the primary provider (kept for backward compat).
        The full ordered provider chain is loaded via get_debrid_providers().
        """
        self.debrid_provider = debrid_provider
        self._last_direct_checks = {}
        self._direct_check_interval = timedelta(minutes=5)

    @property
    def _providers(self):
        """Ordered list of providers: primary first, then fallbacks."""
        return get_debrid_providers()
        
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
        
    def check_cache_status(self, magnet_or_url: str, temp_file: Optional[str] = None, remove_cached: bool = False, item: Optional[Dict] = None, provider: Optional[DebridProvider] = None) -> Tuple[bool, str]:
        """
        Enhanced cache status checking with forced verification for uncached items
        
        Args:
            magnet_or_url: Magnet link or URL to check
            temp_file: Optional path to temporary torrent file
            remove_cached: Whether to remove cached torrents (default: False)
            item: Optional media item dictionary for context.
            
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

            # Use passed provider or fall back to self.debrid_provider
            _provider = provider if provider is not None else self.debrid_provider
            # usenet-only setup: no debrid provider to cache-check against. Treat
            # as not-cached rather than dereferencing None.
            if _provider is None:
                return (False, 'no_provider')

            # Extract imdb_id and title from item
            imdb_id = item.get('imdb_id') if item else None
            result_title = item.get('title') if item else None

            # Extract hash for cache lookup
            hash_value = None
            if magnet_or_url and magnet_or_url.startswith('magnet:'):
                hash_value = extract_hash_from_magnet(magnet_or_url)
            elif temp_file:
                hash_value = extract_hash_from_file(temp_file)
                
            if not hash_value:
                logging.warning("Could not extract hash for cache check, falling back to direct check")
                direct_check = _provider.is_cached_sync(
                    magnet_or_url if not temp_file else "",
                    temp_file,
                    remove_uncached=True,
                    remove_cached=remove_cached,
                    result_title=result_title,
                    imdb_id=imdb_id
                )
                return direct_check, 'direct_check'

            # Check if phalanx db is enabled using settings
            phalanx_enabled = get_setting('UI Settings', 'enable_phalanx_db', default=False)

            # Check if we have a cached status
            if phalanx_enabled and hasattr(_provider, 'get_cached_status'):
                db_cache_status = _provider.get_cached_status(hash_value)

                if db_cache_status:
                    if db_cache_status.get('is_cached', False):
                        return True, 'db_cached'
                    else:
                        if self._should_direct_check(hash_value):
                            direct_check = _provider.is_cached_sync(
                                magnet_or_url if not temp_file else "",
                                temp_file,
                                remove_uncached=True,
                                remove_cached=remove_cached,
                                result_title=result_title,
                                imdb_id=imdb_id
                            )
                            if direct_check != db_cache_status.get('is_cached', False):
                                if phalanx_enabled and hasattr(_provider, 'update_cached_status'):
                                    _provider.update_cached_status(hash_value, direct_check)
                            return direct_check, 'db_uncached_verified'
                        else:
                            return False, 'rate_limited'

            # Direct check
            direct_check = _provider.is_cached_sync(
                magnet_or_url if not temp_file else "",
                temp_file,
                remove_uncached=True,
                remove_cached=remove_cached,
                result_title=result_title,
                imdb_id=imdb_id
            )

            if phalanx_enabled and hasattr(_provider, 'update_cached_status'):
                _provider.update_cached_status(hash_value, direct_check)

            return direct_check, 'direct_check'

        except Exception as e:
            logging.error(f"Error in enhanced cache check: {str(e)}", exc_info=True)
            direct_check = _provider.is_cached_sync(
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
            
    def check_cache_for_url(self, url: str, remove_cached: bool = False, item: Optional[Dict] = None) -> Optional[bool]:
        """
        Download a torrent file from URL and check if it's cached
        
        Args:
            url: URL to the torrent file
            remove_cached: Whether to remove cached torrents (default: False)
            item: Optional media item dictionary for context.
            
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
                        is_cached_bool, _ = self.check_cache_status(magnet_or_url=None, temp_file=temp_file_path, remove_cached=remove_cached, item=item)
                        return is_cached_bool

                    elif response.status_code >= 300 and response.status_code < 400 and 'Location' in response.headers:
                        redirect_url = response.headers['Location']
                        if redirect_url.startswith('magnet:'):
                            logging.info(f"Redirected to magnet link: {redirect_url}")
                            # Process this magnet_link by calling check_cache_status
                            # temp_file should be None as we are now dealing with a magnet link.
                            # The original temp_file_path (for the downloaded .torrent from the initial URL)
                            # will be cleaned up by the `finally` block.
                            is_cached_status, _ = self.check_cache_status(redirect_url, temp_file=None, remove_cached=remove_cached, item=item)
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
            
    def check_cache(self, magnet_or_url: str, temp_file: Optional[str] = None, remove_cached: bool = False, item: Optional[Dict] = None) -> Optional[bool]:
        """
        Check if a magnet link or torrent file is cached
        
        Args:
            magnet_or_url: Magnet link or URL
            temp_file: Optional path to torrent file
            remove_cached: Whether to remove cached torrents (default: False)
            item: Optional media item dictionary for context.
            
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
                return self.check_cache_for_url(magnet_or_url, remove_cached=remove_cached, item=item)
            
            # Use enhanced cache checking
            is_cached, cache_source = self.check_cache_status(magnet_or_url, temp_file, remove_cached=remove_cached, item=item)
            logging.debug(f"Cache check result: {is_cached} (source: {cache_source})")
            
            # Check if this is a RealDebridProvider and explicitly verify removal
            if not is_cached and hasattr(self.debrid_provider, 'verify_removal_state'):
                self.debrid_provider.verify_removal_state()
            
            return is_cached
            
        except Exception as e:
            logging.error(f"Error checking cache: {str(e)}", exc_info=True)
            return None
        
    def add_to_account(self, magnet_or_url: str) -> Optional[Dict]:
        """Add a magnet/torrent to the debrid account, trying each provider in order.

        Tries the primary provider first with 429 retry logic.
        On 451 (DMCA) or exhausted retries it moves on to the next fallback provider.
        Returns torrent info dict from whichever provider succeeded, or None.
        """
        temp_file = None
        get_info_max_retries = 3
        get_info_retry_delay = 2

        caller_frame = inspect.currentframe().f_back
        caller_info = f"{caller_frame.f_code.co_filename}:{caller_frame.f_code.co_name}:{caller_frame.f_lineno}"
        logging.info(f"TorrentProcessor.add_to_account called from {caller_info}")

        try:
            magnet, temp_file = self.process_torrent(magnet_or_url)
            if not magnet and not temp_file:
                logging.warning(f"Could not process {magnet_or_url}. Aborting add_to_account.")
                return None

            providers = self._providers
            last_error = None

            for provider in providers:
                torrent_id = None
                info = None
                add_max_retries = 3
                add_retry_delay_seconds = 5

                logging.info(f"[add_to_account] Trying provider: {provider.PROVIDER_NAME}")

                for attempt in range(add_max_retries):
                    try:
                        add_response = provider.add_torrent(magnet if magnet else None, temp_file)
                        if add_response:
                            torrent_id = add_response
                            logging.info(f"[{provider.PROVIDER_NAME}] Added torrent (ID: {torrent_id}) on attempt {attempt + 1}.")
                            break
                        else:
                            logging.error(f"[{provider.PROVIDER_NAME}] Attempt {attempt + 1}: no torrent ID returned.")
                            if attempt == add_max_retries - 1:
                                last_error = "no ID returned"
                    except ProviderUnavailableError as pue:
                        err_str = str(pue)
                        if "451" in err_str:
                            logging.warning(f"[{provider.PROVIDER_NAME}] 451 DMCA block — trying next provider.")
                            last_error = f"451 DMCA ({provider.PROVIDER_NAME})"
                            break  # Skip remaining retries, move to next provider
                        elif "429" in err_str and attempt < add_max_retries - 1:
                            wait_time = add_retry_delay_seconds * (2 ** attempt)
                            logging.warning(f"[{provider.PROVIDER_NAME}] 429 rate limit, waiting {wait_time}s.")
                            time.sleep(wait_time)
                        else:
                            logging.error(f"[{provider.PROVIDER_NAME}] ProviderUnavailableError: {err_str}", exc_info=True)
                            last_error = err_str
                            break
                    except Exception as ex:
                        err_str = str(ex)
                        if "space is full" in err_str.lower():
                            # Account-storage exhaustion — an action only the user can take
                            # (free up space / upgrade plan), not a code-level failure. A full
                            # traceback here is just noise; a short warning is enough to act on.
                            logging.warning(f"[{provider.PROVIDER_NAME}] Account storage is full — cannot add torrent. Free up space or upgrade your plan.")
                        else:
                            logging.error(f"[{provider.PROVIDER_NAME}] Unexpected error: {ex}", exc_info=True)
                        last_error = err_str
                        break

                if not torrent_id:
                    logging.warning(f"[{provider.PROVIDER_NAME}] Failed to add torrent, trying next provider.")
                    continue

                # Got a torrent_id — fetch info
                try:
                    for attempt in range(get_info_max_retries):
                        info = provider.get_torrent_info(torrent_id)
                        if info and len(info.get('files', [])) > 0:
                            # Tag info with which provider handled this
                            info['_provider'] = provider.PROVIDER_NAME
                            # Update primary provider reference so checking queue uses same provider
                            self.debrid_provider = provider
                            logging.info(f"[{provider.PROVIDER_NAME}] Successfully got torrent info.")
                            return info
                        time.sleep(get_info_retry_delay)

                    # Info fetched but empty/bad
                    if torrent_id:
                        try:
                            provider.remove_torrent(torrent_id, removal_reason="Empty torrent during processing")
                        except Exception:
                            pass
                    logging.warning(f"[{provider.PROVIDER_NAME}] Torrent added but no files found.")
                except Exception as e:
                    logging.error(f"[{provider.PROVIDER_NAME}] Error fetching torrent info: {e}", exc_info=True)

            logging.error(f"All providers failed to add torrent. Last error: {last_error}")
            return None

        except Exception as e:
            logging.error(f"Error in add_to_account for {magnet_or_url}: {e}", exc_info=True)
            return None

        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    logging.error(f"Error cleaning up temp file {temp_file}: {e}")
                    
    def _process_nzb_result(self, result: Dict, item: Optional[Dict] = None, adding_queue_items: Optional[list] = None) -> Optional[Tuple]:
        """Submit an NZB result to cli_mount and return a synthetic torrent_info tuple."""
        from usenet.climount_client import get_climount_client, reset_climount_client
        reset_climount_client()
        client = get_climount_client()

        if not client.is_enabled():
            logging.debug('[NZB] cli_mount not enabled, skipping NZB result')
            return None

        nzb_url = result.get('nzb_url') or result.get('magnet') or ''
        title = result.get('title', '')
        item_identifier = item.get('title', 'Unknown') if item else 'Unknown'

        if not nzb_url:
            logging.warning(f'[{item_identifier}] NZB result has no URL, skipping')
            return None

        # Equivalent of debrid's _all_torrent_ids check: if another episode of the same
        # show/season already has an NZB job (in any active or completed state), reuse it
        # instead of submitting a duplicate season pack NZB.
        if item and item.get('type') == 'episode':
            _imdb = item.get('imdb_id')
            _season = item.get('season_number')
            _parsed = result.get('parsed_info', {}) or {}
            _is_pack = bool(_parsed.get('seasons')) and not _parsed.get('episodes')
            if _imdb and _season is not None:
                # In-memory check: scan Adding queue items for same imdb+season with nzb: job.
                # This catches jobs submitted this tick before they're written to DB.
                import re as _re_mem
                if adding_queue_items:
                    _item_version = (item or {}).get('version', 'Default')
                    for _mem_item in adding_queue_items:
                        if (_mem_item.get('id') == item.get('id') or
                                _mem_item.get('imdb_id') != _imdb or
                                _mem_item.get('season_number') != _season or
                                _mem_item.get('version', 'Default') != _item_version or
                                not str(_mem_item.get('filled_by_torrent_id', '')).startswith('nzb:')):
                            continue
                        _mem_file = _mem_item.get('filled_by_file') or ''
                        _mem_is_pack = not _re_mem.search(r'[Ss]\d{2}[Ee]\d{2}', _mem_file)
                        _mem_job = _mem_item.get('filled_by_torrent_id')
                        _mem_id = _mem_job[4:] if _mem_job and _mem_job.startswith('nzb:') else _mem_job
                        if _is_pack and _mem_is_pack:
                            logging.info(f'[{item_identifier}] [Memory] Season pack already submitted for S{_season:02d} '
                                         f'(job={_mem_job}) — reusing')
                            return {'id': _mem_id, 'filename': title, 'original_title': title,
                                    'status': 'downloading', 'files': [], 'progress': 0,
                                    '_provider': 'cli_mount', '_is_nzb': True, '_nzb_url': nzb_url}, nzb_url, result
                        elif not _is_pack and _mem_is_pack:
                            logging.info(f'[{item_identifier}] [Memory] Season pack already submitted for S{_season:02d} '
                                         f'(job={_mem_job}) — skipping individual episode submission')
                            return {'id': _mem_id, 'filename': title, 'original_title': title,
                                    'status': 'downloading', 'files': [], 'progress': 0,
                                    '_provider': 'cli_mount', '_is_nzb': True, '_nzb_url': nzb_url}, nzb_url, result
                        elif _is_pack and not _mem_is_pack:
                            # New result is a pack, existing job is individual — fall through to submit pack
                            break

                try:
                    from database import get_db_connection as _gdb
                    _conn = _gdb()
                    import re as _re_dedup
                    # Matches SQL's REPLACE(COALESCE(version,''),'*','') exactly — fall back
                    # to '' (not a literal like 'Default') so NULL/empty version rows still compare equal.
                    _sibling_ver = (item.get('version') or '').rstrip('*')
                    try:
                        _sibling = _conn.execute(
                            "SELECT filled_by_torrent_id, filled_by_file FROM media_items "
                            "WHERE imdb_id=? AND season_number=? AND type='episode' "
                            "AND id!=? AND filled_by_torrent_id LIKE 'nzb:%' "
                            "AND REPLACE(COALESCE(version,''),'*','')=? "
                            "AND state IN ('Adding','Checking','Collected','Upgrading') LIMIT 1",
                            (_imdb, _season, item.get('id', -1), _sibling_ver)
                        ).fetchone()
                    finally:
                        _conn.close()
                    if _sibling:
                        _sibling_is_pack = not _re_dedup.search(r'[Ss]\d{2}[Ee]\d{2}', _sibling[1] or '')
                        if _is_pack and _sibling_is_pack:
                            # New result is a pack, existing job is a pack — reuse existing
                            _existing_job = _sibling[0]
                            _existing_file = title
                            _existing_id = _existing_job[4:] if _existing_job.startswith('nzb:') else _existing_job
                            logging.info(f'[{item_identifier}] Season pack already submitted for S{_season:02d} '
                                         f'(job={_existing_job}) — reusing instead of duplicate submission')
                            return {'id': _existing_id, 'filename': _existing_file,
                                    'original_title': _existing_file, 'status': 'downloading',
                                    'files': [], 'progress': 0,
                                    '_provider': 'cli_mount', '_is_nzb': True,
                                    '_nzb_url': nzb_url}, nzb_url, result
                        elif not _is_pack and _sibling_is_pack:
                            # New result is individual episode, existing job is a season pack — skip individual
                            _existing_job = _sibling[0]
                            _existing_id = _existing_job[4:] if _existing_job.startswith('nzb:') else _existing_job
                            logging.info(f'[{item_identifier}] Season pack already submitted for S{_season:02d} '
                                         f'(job={_existing_job}) — skipping individual episode submission')
                            return {'id': _existing_id, 'filename': title,
                                    'original_title': title, 'status': 'downloading',
                                    'files': [], 'progress': 0,
                                    '_provider': 'cli_mount', '_is_nzb': True,
                                    '_nzb_url': nzb_url}, nzb_url, result
                        elif _is_pack and not _sibling_is_pack:
                            # New result is a pack, existing jobs are individual episodes.
                            # Cancel the individual cli_mount jobs so we don't end up with
                            # both individual files AND a season pack folder on disk.
                            try:
                                from database import get_db_connection as _gdb2
                                _conn2 = _gdb2()
                                try:
                                    _individuals = _conn2.execute(
                                        "SELECT id, filled_by_torrent_id FROM media_items "
                                        "WHERE imdb_id=? AND season_number=? AND type='episode' "
                                        "AND filled_by_torrent_id LIKE 'nzb:%' "
                                        "AND REPLACE(COALESCE(version,''),'*','')=? "
                                        "AND state IN ('Adding','Checking')",
                                        (_imdb, _season, _sibling_ver)
                                    ).fetchall()
                                finally:
                                    _conn2.close()
                                if _individuals:
                                    from usenet.climount_client import get_climount_client as _get_dc
                                    _dc = _get_dc()
                                    _cancelled = set()
                                    for _ind_id, _ind_tid in _individuals:
                                        _ind_hash = _ind_tid[4:] if _ind_tid.startswith('nzb:') else _ind_tid
                                        if _ind_hash not in _cancelled:
                                            try:
                                                _dc.remove_nzb(_ind_hash)
                                                _cancelled.add(_ind_hash)
                                                logging.info(f'[{item_identifier}] Cancelled individual episode job {_ind_hash} — season pack will replace it')
                                            except Exception:
                                                pass
                            except Exception as _ce:
                                logging.debug(f'[{item_identifier}] Could not cancel individual jobs: {_ce}')
                            # Fall through to submit the pack
                except Exception as _se:
                    logging.debug(f'[{item_identifier}] Season pack DB dedup check failed: {_se}')

        # Build structured job title if NZB naming is enabled.
        # Must happen BEFORE the dedup check so we check the actual submitted name.
        try:
            from routes.scraper_routes import _build_nzb_title
            _item_type = (item or {}).get('type', '')
            _media_type = 'tv' if _item_type == 'episode' else _item_type
            # Season packs (one NZB for whole season) must NOT include SxxExx in the job title.
            # All episodes in Scraping that share the same pack NZB must produce the identical
            # cli_mount job name so they land in a single folder and the dedup check fires.
            # Detection: parsed_info has seasons but no episodes (PTT leaves episodes empty for packs).
            _parsed = result.get('parsed_info', {}) or {}
            _parsed_seasons = _parsed.get('seasons') or []
            _parsed_episodes = _parsed.get('episodes') or []
            _is_season_pack = bool(_parsed_seasons) and not _parsed_episodes
            job_title = _build_nzb_title(
                title=(item or {}).get('title', '') or title,
                year=(item or {}).get('year', ''),
                imdb_id=(item or {}).get('imdb_id'),
                version=(item or {}).get('version', ''),
                original_scraped_torrent_title=title,
                media_type=_media_type,
                season=(item or {}).get('season_number'),
                episode=None if _is_season_pack else (item or {}).get('episode_number'),
                episode_title=None if _is_season_pack else (item or {}).get('episode_title'),
                tags=(item or {}).get('tags') or None,
            ) or title
        except Exception as _bnt_exc:
            # Falling back to the raw scraped title silently here means the
            # resulting cli_mount entry has no imdb tag, no version tag, and no
            # structured name at all — indistinguishable from naming being
            # disabled. Log it so a real bug (bad settings lookup, unexpected
            # season/episode type, etc.) is visible instead of masquerading as
            # "naming worked but produced a plain title".
            logging.warning(f'[{item_identifier}] _build_nzb_title failed, falling back to raw title: {_bnt_exc}', exc_info=True)
            job_title = title

        # Check if same NZB title already in cli_mount/NzbDAV to avoid duplicates.
        # Two match levels:
        #   1. Exact title match — catches same release resubmitted
        #   2. Prefix match — catches same show/episode submitted with a different
        #      release group (e.g. RAWR vs Kitsune). The structured title format is:
        #      "Show (year) - SxxExx - Title - {imdb-ttXXX} - Version - (release.title)"
        #      Stripping the trailing " - (release.title)" gives a stable prefix.
        import re as _re_dc
        def _title_prefix(t):
            return _re_dc.sub(r'\s*-\s*\([^)]*\)\s*$', '', t).strip()

        _job_prefix = _title_prefix(job_title)

        # DB-level dedup: check if same item already in Adding/Checking with nzb: torrent ID.
        # Works for both cli_mount and NzbDAV since it uses the DB, not provider API.
        # Version-scoped: different versions (e.g. 1080p vs 4k) of the same movie/episode
        # must never be treated as duplicates of each other — reusing another version's
        # job id corrupts both DB rows and can lead to one version's file being deleted
        # when the other's lifecycle (health check, cleanup, repair) acts on that job id.
        try:
            from database.core import get_db_connection as _get_dbc_dd
            _item_imdb = (item or {}).get('imdb_id')
            _item_type = (item or {}).get('type', '')
            _item_id = (item or {}).get('id', -1)
            # Matches SQL's REPLACE(COALESCE(version,''),'*','') exactly.
            _item_ver = ((item or {}).get('version') or '').rstrip('*')
            if _item_imdb and _item_type:
                _dd_q = ("SELECT filled_by_torrent_id FROM media_items "
                         "WHERE imdb_id=? AND type=? AND state IN ('Adding','Checking') "
                         "AND filled_by_torrent_id LIKE 'nzb:%' "
                         "AND REPLACE(COALESCE(version,''),'*','')=? AND id!=?")
                _dd_p = (_item_imdb, _item_type, _item_ver, _item_id)
                if _item_type == 'episode':
                    _dd_q = ("SELECT filled_by_torrent_id FROM media_items "
                             "WHERE imdb_id=? AND type=? AND season_number=? AND episode_number=? "
                             "AND state IN ('Adding','Checking') AND filled_by_torrent_id LIKE 'nzb:%' "
                             "AND REPLACE(COALESCE(version,''),'*','')=? AND id!=?")
                    _dd_p = (_item_imdb, _item_type,
                             (item or {}).get('season_number'), (item or {}).get('episode_number'),
                             _item_ver, _item_id)
                with _get_dbc_dd() as _dbc:
                    _dd_row = _dbc.execute(_dd_q, _dd_p).fetchone()
                if _dd_row:
                    _existing_nzb_id = _dd_row[0][4:]  # strip 'nzb:'
                    logging.info(f'[{item_identifier}] NZB already in-flight (DB dedup): {_existing_nzb_id} — reusing job')
                    return {'id': _existing_nzb_id, 'filename': job_title, 'original_title': job_title,
                            'status': 'downloading', 'files': [], 'progress': 0,
                            '_provider': 'Usenet', '_is_nzb': True, '_nzb_url': nzb_url}, nzb_url, result
        except Exception:
            pass

        # cli_mount-only: also check provider queue by title for exact/prefix match.
        # NzbDAV has no /api/torrents endpoint — silently skipped via except.
        try:
            from routes.api_tracker import api as _check_api
            from utilities.settings import get_setting as _gs_check
            _dcy_url = _gs_check('Usenet Provider', 'url', default='').rstrip('/')
            _dcy_token = _gs_check('Usenet Provider', 'api_token', default='')
            _ch = {'Authorization': f'Bearer {_dcy_token}'} if _dcy_token else {}
            _page_dc = 1
            _found_dc = False
            while not _found_dc:
                _er = _check_api.get(f'{_dcy_url}/api/torrents', headers=_ch,
                                     params={'page': _page_dc, 'limit': 100,
                                             'sort_by': 'added_on', 'sort_order': 'desc'},
                                     timeout=5)
                if _er.status_code != 200:
                    break
                _data_dc = _er.json()
                _torrents_dc = _data_dc.get('torrents', [])
                for _t in _torrents_dc:
                    _t_name = _t.get('name', '')
                    _exact = (_t_name == job_title or _t.get('original_filename', '') == job_title)
                    _prefix = (bool(_job_prefix) and _title_prefix(_t_name) == _job_prefix)
                    if _exact or _prefix:
                        _existing_hash = _t.get('info_hash', '')
                        _match_type = 'exact' if _exact else 'prefix'
                        logging.info(f'[{item_identifier}] NZB already in cli_mount ({_match_type} match): {_t_name} (hash={_existing_hash}) — reusing job')
                        _found_dc = True
                        return {'id': _existing_hash, 'filename': job_title, 'original_title': job_title,
                                'status': _t.get('status', 'downloading'), 'files': [], 'progress': 0,
                                '_provider': 'cli_mount', '_is_nzb': True, '_nzb_url': nzb_url}, nzb_url, result
                if not _data_dc.get('has_next'):
                    break
                _page_dc += 1
        except Exception:
            pass

        # Fetch NZB XML to check segment ID against not-wanted list
        _nzb_xml = None
        try:
            from routes.api_tracker import api as _nzb_api2
            _nr = _nzb_api2.get(nzb_url, timeout=15, allow_redirects=True)
            if _nr.status_code == 200 and '<nzb' in _nr.text.lower():
                _nzb_xml = _nr.text
                from database.not_wanted_magnets import is_nzb_segment_not_wanted
                if is_nzb_segment_not_wanted(_nzb_xml):
                    logging.info(f'[{item_identifier}] Skipping NZB {title!r} — segment ID in not-wanted list')
                    return None
        except Exception as _nzb_check_err:
            logging.debug(f'[{item_identifier}] Could not pre-check NZB segment: {_nzb_check_err}')

        _item = item or {}
        # Derive is_anime: prefer trigger_is_anime DB flag, fall back to genres
        # genres may be a list or a JSON string from the DB
        _genres_raw = _item.get('genres') or _item.get('trigger_genres') or []
        if isinstance(_genres_raw, str):
            try:
                import json as _json
                _genres_raw = _json.loads(_genres_raw)
            except Exception:
                _genres_raw = [_genres_raw]
        _is_anime = bool(_item.get('trigger_is_anime')) or any(
            'anime' in (g or '').lower() for g in _genres_raw
        )
        _item_media_type = _item.get('type', '')
        _tags = _item.get('tags') or None
        # tags_exclusive: check content source config
        _tags_exclusive = False
        try:
            from utilities.settings import get_setting as _gs_tags
            _cs_id = _item.get('content_source', '')
            if _cs_id:
                _cs_cfg = (_gs_tags('Content Sources') or {}).get(_cs_id, {})
                _tags_exclusive = bool(_cs_cfg.get('tags_exclusive', False))
        except Exception:
            pass

        logging.info(f'[{item_identifier}] Submitting NZB to cli_mount: {job_title}')
        if _nzb_xml:
            job_id = client.add_nzb_content(nzb_content=_nzb_xml, title=job_title,
                                            is_anime=_is_anime, media_type=_item_media_type,
                                            tags=_tags, tags_exclusive=_tags_exclusive)
            if not job_id and client.last_missing_segments:
                logging.warning(f'[{item_identifier}] cli_mount server missing segments for {job_title!r} — adding NZB URL to not-wanted')
                try:
                    from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_nw_seg
                    if nzb_url:
                        _add_nw_seg(nzb_url)
                        logging.info(f'[{item_identifier}] Added missing-segments NZB URL to not-wanted')
                except Exception:
                    pass
                # Flag on item so adding_queue knows this was a missing-segments failure
                if item:
                    item['_nzb_all_missing_segments'] = True
                return None
        else:
            job_id = client.add_nzb(nzb_url=nzb_url, title=job_title,
                                    is_anime=_is_anime, media_type=_item_media_type,
                                    tags=_tags, tags_exclusive=_tags_exclusive)

        if not job_id:
            # Fallback: download NZB and upload directly
            logging.info(f'[{item_identifier}] URL submission failed, trying direct upload: {job_title}')
            try:
                from routes.api_tracker import api as _nzb_api
                _r = _nzb_api.get(nzb_url, timeout=15, allow_redirects=True)
                if _r.status_code == 200 and '<nzb' in _r.text.lower():
                    job_id = client.add_nzb_content(nzb_content=_r.text, title=job_title,
                                                    is_anime=_is_anime, media_type=_item_media_type,
                                                    tags=_tags, tags_exclusive=_tags_exclusive)
                    if not job_id and client.last_missing_segments:
                        logging.warning(f'[{item_identifier}] cli_mount server missing segments on fallback for {job_title!r}')
                        if item:
                            item['_nzb_all_missing_segments'] = True
                        return None
                    if job_id:
                        logging.info(f'[{item_identifier}] Direct upload succeeded: {job_title}')
            except Exception as _fe:
                logging.warning(f'[{item_identifier}] Direct upload fallback failed: {_fe}')

        if not job_id:
            logging.warning(f'[{item_identifier}] cli_mount rejected NZB: {title}')
            return None

        # Verify job is actually in cli_mount's queue — brief wait for processing
        time.sleep(1)
        status = client.get_job_status(job_id)
        if status and status.get('state') == 'failed':
            logging.warning(f'[{item_identifier}] cli_mount failed NZB immediately after submission: {title} (job_id={job_id})')
            return None

        logging.info(f'[{item_identifier}] NZB submitted successfully, job_id={job_id}')

        # Extract segment ID for not-wanted fingerprinting
        _segment_id = ''
        if _nzb_xml:
            try:
                from database.not_wanted_magnets import extract_nzb_segment_id
                _segment_id = extract_nzb_segment_id(_nzb_xml)
            except Exception:
                pass

        # Return a synthetic torrent_info dict so the caller can treat this like a torrent result
        torrent_info = {
            'id': job_id,
            'filename': job_title,
            'original_title': title,  # original NZB release name preserved for reference
            'status': 'downloading',
            'files': [],
            'progress': 0,
            '_provider': 'cli_mount',
            '_is_nzb': True,
            '_nzb_url': nzb_url,
            '_nzb_segment_id': _segment_id,
        }
        return torrent_info, nzb_url, result

    def process_results(
        self,
        results: list[Dict],
        accept_uncached: bool = False,
        item: Optional[Dict] = None,
        adding_queue_items: Optional[list] = None,
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
                # NZB results are handled by cli_mount, not debrid — route them separately
                if result.get('protocol') == 'nzb' or result.get('nzb_url'):
                    nzb_result = self._process_nzb_result(result, item, adding_queue_items=adding_queue_items)
                    if nzb_result:
                        return nzb_result
                    # NZB rejected — pop this result from scrape_results in DB so the next
                    # tick tries the next candidate, then return so the Adding queue can
                    # move on to other items immediately (one attempt per item per tick).
                    if item:
                        try:
                            import json as _json_tp
                            from database.database_writing import update_media_item as _umi_tp
                            _sr = item.get('scrape_results', [])
                            if isinstance(_sr, str):
                                _sr = _json_tp.loads(_sr)
                            if isinstance(_sr, list) and _sr:
                                _sr = _sr[1:]
                                item['scrape_results'] = _sr
                                _umi_tp(item['id'], scrape_results=_json_tp.dumps(_sr))
                        except Exception:
                            pass
                    return None, None, None

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

                # Create a temporary item dict for passing to check_cache_status
                temp_item_for_check = item.copy() if item else {}
                temp_item_for_check['title'] = result_title
                if 'imdb_id' not in temp_item_for_check and item and item.get('imdb_id'):
                    temp_item_for_check['imdb_id'] = item.get('imdb_id')

                # Check cache across all providers in parallel — use first hit
                from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
                providers = self._providers
                is_cached = False
                cache_source = 'direct_check'
                winning_provider = self.debrid_provider  # default to primary

                def _check_one(prov):
                    try:
                        cached, src = self.check_cache_status(
                            magnet if not temp_file else "",
                            temp_file,
                            item=temp_item_for_check,
                            provider=prov,
                        )
                        return prov, cached, src
                    except Exception as _e:
                        logging.warning(f"[{prov.PROVIDER_NAME}] cache check error: {_e}")
                        return prov, None, 'error'

                if not providers:
                    # usenet-only setup: no debrid provider to cache-check a
                    # torrent result. Leave is_cached False (ThreadPoolExecutor
                    # would raise on max_workers=0).
                    is_cached = False
                elif len(providers) == 1:
                    winning_provider, is_cached, cache_source = _check_one(providers[0])
                else:
                    with ThreadPoolExecutor(max_workers=len(providers)) as _ex:
                        _futures = {_ex.submit(_check_one, p): p for p in providers}
                        for _fut in _as_completed(_futures):
                            _prov, _cached, _src = _fut.result()
                            logging.info(f"[{_prov.PROVIDER_NAME}] cache={_cached} src={_src}")
                            if _cached and not is_cached:
                                is_cached = True
                                cache_source = _src
                                winning_provider = _prov
                                # Update processor's active provider so add_to_account uses same one
                                self.debrid_provider = _prov

                if is_cached:
                    logging.info(f"[{item_identifier}] Cached on {winning_provider.PROVIDER_NAME}")
                    
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
                            existing_info = self.debrid_provider.get_torrent_info(existing_torrent_id)
                            existing_status = existing_info.get('status') if existing_info else None
                            _error_statuses = ('error', 'magnet_error', 'virus', 'dead')
                            if existing_status in _error_statuses:
                                logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Existing torrent {existing_torrent_id} has status '{existing_status}', removing and re-adding")
                                try:
                                    self.debrid_provider.remove_torrent(existing_torrent_id, removal_reason=f"Error status '{existing_status}', re-adding")
                                except Exception as remove_err:
                                    logging.warning(f"[{item_identifier}] Could not remove errored torrent {existing_torrent_id}: {remove_err}")
                                logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] PHASE: Addition - Adding to debrid service (after removing errored torrent)")
                                info = self.add_to_account(original_link)
                            else:
                                logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Reusing existing torrent ID: {existing_torrent_id}")
                                info = existing_info
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

                        # Debrid File Naming: rename the cli_mount DFS folder using
                        # the structured CLI name if the setting is enabled.
                        # Runs asynchronously so it doesn't block the adding flow.
                        # Retries up to 3 times with 30s delay to handle cli_mount's
                        # periodic sync window (default 10 min).
                        if item and definitive_hash:
                            try:
                                from utilities.settings import get_setting as _dbn_gs
                                if _dbn_gs('Debrid Provider', 'enable_debrid_naming', False):
                                    from routes.scraper_routes import _build_debrid_title
                                    _dbn_type = item.get('type', '')
                                    _dbn_media_type = 'tv' if _dbn_type == 'episode' else _dbn_type
                                    _parsed_dbn = result.get('parsed_info', {}) or {}
                                    _dbn_seasons = _parsed_dbn.get('seasons') or []
                                    _dbn_episodes = _parsed_dbn.get('episodes') or []
                                    _dbn_is_pack = bool(_dbn_seasons) and not _dbn_episodes
                                    # Fallback: if no parsed_info (e.g. upgrade hub candidate),
                                    # detect season pack by checking if title has SXX but no SXXEXX
                                    if not _dbn_is_pack and not _parsed_dbn and _dbn_type == 'episode':
                                        import re as _re_dbn
                                        _t = result.get('title') or result.get('original_title') or ''
                                        if _re_dbn.search(r'[Ss]\d{2}(?![Ee]\d)', _t):
                                            _dbn_is_pack = True
                                    _dbn_title = _build_debrid_title(
                                        title=item.get('title', '') or result_title,
                                        year=item.get('year', ''),
                                        imdb_id=item.get('imdb_id'),
                                        version=item.get('version', ''),
                                        original_scraped_torrent_title=result.get('original_title') or result_title,
                                        media_type=_dbn_media_type,
                                        season=item.get('season_number'),
                                        episode=None if _dbn_is_pack else item.get('episode_number'),
                                        episode_title=None if _dbn_is_pack else item.get('episode_title'),
                                        tags=item.get('tags') or None,
                                        content_source_display_name=item.get('content_source_detail') or item.get('content_source'),
                                    )
                                    logging.info(f'[DebridNaming] title={_dbn_title!r} orig={result.get("original_title") or result_title!r} will_rename={bool(_dbn_title and _dbn_title != (result.get("original_title") or result_title))}')
                                    if _dbn_title and _dbn_title != (result.get('original_title') or result_title):
                                        import threading as _dbn_threading
                                        _dbn_hash = definitive_hash
                                        _dbn_name = _dbn_title
                                        _dbn_id = item_identifier
                                        _dbn_item_id = item.get('id')
                                        def _do_debrid_rename(h, name, ident, item_id):
                                            import time as _t
                                            logging.info(f'[DebridNaming] Thread started for {ident!r} hash={h!r}')
                                            try:
                                                from usenet.climount_client import get_climount_client
                                                _dc = get_climount_client()
                                                if not hasattr(_dc, 'rename_nzb'):
                                                    logging.info(f'[DebridNaming] Client has no rename_nzb for {ident!r}')
                                                    return  # active usenet provider (e.g. nzbdav) has no rename semantics
                                                # cli_mount only registers an entry as queryable-by-hash after its
                                                # own periodic sync (default ~10 min) — a 404 in the first several
                                                # attempts is expected, not proof the entry is gone. Only treat 404
                                                # as final once it's persisted for that long (20 attempts x 30s).
                                                _consecutive_404 = 0
                                                _confirmed_gone_after = 20
                                                for _attempt in range(100):
                                                    _renamed, _not_found = _dc.rename_nzb_with_status(h, name)
                                                    if _not_found:
                                                        _consecutive_404 += 1
                                                        if _consecutive_404 >= _confirmed_gone_after:
                                                            logging.warning(f'[DebridNaming] {h!r} not found in cli_mount (404) for {_consecutive_404} consecutive attempts — giving up for {ident}')
                                                            return
                                                    else:
                                                        _consecutive_404 = 0
                                                    if _renamed:
                                                        logging.info(f'[DebridNaming] Renamed {h!r} -> {name!r} for {ident}')
                                                        if item_id:
                                                            try:
                                                                from database.database_writing import update_media_item as _umi
                                                                _umi(item_id, debrid_folder_name=name, filled_by_title=name)
                                                            except Exception as _db_err:
                                                                logging.debug(f'[DebridNaming] DB update failed for {ident}: {_db_err}')
                                                        # Register cli_debrid IDs for all siblings sharing this torrent
                                                        try:
                                                            from database.core import get_db_connection as _gdb_dn
                                                            import os as _os_dn
                                                            _VIDEO_EXTS_DN = {'.mkv','.mp4','.avi','.mov','.wmv','.m4v','.ts'}
                                                            with _gdb_dn() as _dbc:
                                                                # Find all live items sharing this torrent by provider_id
                                                                _torrent_id_val = None
                                                                _r = _dbc.execute(
                                                                    'SELECT filled_by_torrent_id FROM media_items WHERE id=?', (item_id,)
                                                                ).fetchone()
                                                                if _r:
                                                                    _torrent_id_val = _r[0]
                                                                if _torrent_id_val:
                                                                    _sibs = _dbc.execute(
                                                                        "SELECT id, filled_by_file FROM media_items "
                                                                        "WHERE filled_by_torrent_id=? AND state IN ('Checking','Collected','Upgrading')",
                                                                        (_torrent_id_val,)
                                                                    ).fetchall()
                                                                    _cli_ids_dn = {
                                                                        s[1]: s[0]
                                                                        for s in _sibs
                                                                        if s[1] and _os_dn.path.splitext(s[1])[1].lower() in _VIDEO_EXTS_DN
                                                                    }
                                                                    if _cli_ids_dn:
                                                                        _dc.register_cli_ids(h, _cli_ids_dn)
                                                                        logging.info(f'[DebridNaming] Registered {len(_cli_ids_dn)} cli_debrid IDs for {h!r}')
                                                            if item_id:
                                                                _dc.push_tags_for_item(h, item_id)
                                                        except Exception as _reg_dn_err:
                                                            logging.debug(f'[DebridNaming] cli_ids registration error: {_reg_dn_err}')
                                                        return
                                                    _t.sleep(30)
                                                logging.warning(f'[DebridNaming] Could not rename {h!r} after 100 attempts for {ident}')
                                            except Exception as _dbn_err:
                                                logging.info(f'[DebridNaming] Rename error for {ident}: {_dbn_err}')
                                        _dbn_threading.Thread(target=_do_debrid_rename, args=(_dbn_hash, _dbn_name, _dbn_id, _dbn_item_id), daemon=True).start()
                                        logging.info(f'[DebridNaming] Thread launched for {item_identifier!r} hash={definitive_hash!r}')
                            except Exception as _dbn_ex:
                                import traceback as _dbn_tb
                                logging.info(f'[DebridNaming] Setup error for {item_identifier}: {_dbn_ex}\n{_dbn_tb.format_exc()}')

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
