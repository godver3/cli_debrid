import logging
from typing import Dict, List, Optional, Union, Tuple, Any
from datetime import datetime
import time
from urllib.parse import unquote
import hashlib
import bencodepy
import asyncio

from ..base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError, TorrentAdditionError
from ..common import (
    extract_hash_from_magnet,
    timed_lru_cache,
    is_video_file,
    is_unwanted_file
)
from ..status import TorrentStatus
from .api import make_request, get_all_magnets
from database.not_wanted_magnets import add_to_not_wanted
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
from utilities.settings import get_setting
from .exceptions import AllDebridAuthError, AllDebridAPIError

from debrid.status import TorrentInfoStatus, TorrentFetchStatus


# AllDebrid status code mapping
# 0: Queued, 1: Downloading, 2: Compressing, 3: Uploading, 4: Ready
# 5: Upload fail, 6: Internal error, 7: Not downloaded in 20 min
# 8: File too big, 9: Internal error, 10: Download took more than 72h
# 11: Deleted on the hoster website
STATUS_CODE_MAP = {
    0: TorrentStatus.QUEUED,
    1: TorrentStatus.DOWNLOADING,
    2: TorrentStatus.DOWNLOADING,  # Compressing
    3: TorrentStatus.DOWNLOADING,  # Uploading
    4: TorrentStatus.DOWNLOADED,   # Ready/Cached
    5: TorrentStatus.ERROR,
    6: TorrentStatus.ERROR,
    7: TorrentStatus.ERROR,
    8: TorrentStatus.ERROR,
    9: TorrentStatus.ERROR,
    10: TorrentStatus.ERROR,
    11: TorrentStatus.ERROR,
}


class AllDebridProvider(DebridProvider):
    """AllDebrid implementation of the DebridProvider interface"""

    API_BASE_URL = "https://api.alldebrid.com/v4"
    MAX_DOWNLOADS = 20  # AllDebrid typical limit

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
            # Use the /user endpoint with query auth to validate API key
            _ = make_request('GET', '/user', self.api_key, use_query_auth=True)
            return True, None
        except AllDebridAuthError as e:
            return False, {
                "service": "Debrid Provider API",
                "type": "AUTH_ERROR",
                "status_code": None,
                "message": str(e)
            }
        except (ProviderUnavailableError, AllDebridAPIError) as e:
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
        Return AllDebrid subscription info with days remaining.

        Response example:
        {
          'days_remaining': 12,
          'expiration': '2025-09-30T12:34:56Z',
          'premium': True
        }
        """
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'days_remaining': None, 'expiration': None, 'premium': False}

            # AllDebrid /user endpoint uses query param auth
            result = make_request('GET', '/user', self.api_key, use_query_auth=True)
            user_info = result.get('data', {}).get('user', {})

            premium = bool(user_info.get('isPremium', False))
            # AllDebrid returns premiumUntil as Unix timestamp
            premium_until = user_info.get('premiumUntil')

            days_remaining = None
            expiration = None
            if premium_until:
                try:
                    exp_dt = datetime.fromtimestamp(premium_until)
                    expiration = exp_dt.isoformat()
                    now = datetime.utcnow()
                    delta = exp_dt - now
                    days_remaining = max(0, delta.days)
                except Exception as e:
                    logging.warning(f"Failed parsing AllDebrid expiration '{premium_until}': {e}")

            return {
                'days_remaining': days_remaining,
                'expiration': expiration,
                'premium': premium
            }
        except Exception as e:
            logging.error(f"Error fetching subscription status: {str(e)}")
            return {
                'days_remaining': None,
                'expiration': None,
                'premium': None,
                'error': str(e)
            }

    async def is_cached(
        self,
        magnet_links: Union[str, List[str]],
        temp_file_path: Optional[str] = None,
        result_title: Optional[str] = None,
        result_index: Optional[str] = None,
        remove_uncached: bool = True,
        remove_cached: bool = False,
        skip_phalanx_db: bool = False,
        imdb_id: Optional[str] = None
    ) -> Union[bool, Dict[str, bool], None]:
        """
        Check if one or more magnet links or torrent files are cached on AllDebrid.
        Uses the instant availability endpoint first, then falls back to add+check if needed.

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
            - None: Error occurred during check
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

            # Extract hash at the beginning
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
            elif magnet_link:
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
            try:
                if not skip_phalanx_db and self.phalanx_enabled and self.phalanx_cache:
                    phalanx_cache_result = self.phalanx_cache.get_cache_status(hash_value)
                    if phalanx_cache_result is not None:
                        if phalanx_cache_result['is_cached']:
                            logging.info(f"{log_prefix} Found cached status in PhalanxDB: {phalanx_cache_result['is_cached']}")
                            results[hash_value] = True
                            continue
                        else:
                            logging.debug(f"{log_prefix} Found uncached status in PhalanxDB, verifying with AllDebrid")
            except Exception as e:
                logging.error(f"{log_prefix} Error checking PhalanxDB cache: {str(e)}")

            # Note: AllDebrid has no /magnet/instant endpoint
            # Cache status is determined via /magnet/upload response (ready field) or status check

            # Fall back to add torrent + check status
            torrent_id = None
            try:
                # Add the magnet/torrent to AllDebrid
                torrent_id = self.add_torrent(
                    magnet_link if magnet_link and magnet_link.startswith('magnet:') else None,
                    temp_file_path
                )

                if not torrent_id:
                    results[hash_value] = False
                    if self.phalanx_enabled and self.phalanx_cache:
                        try:
                            self.phalanx_cache.update_cache_status(hash_value, False)
                        except Exception as e:
                            logging.error(f"{log_prefix} Failed to update PhalanxDB: {str(e)}")
                    continue

                # Get torrent info
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

                # Check status code
                status_code = info.get('statusCode', -1)

                # Handle error statuses (5-11)
                if status_code >= 5:
                    logging.error(f"{log_prefix} Torrent has error status code: {status_code}")
                    try:
                        add_to_not_wanted(hash_value)
                        self.remove_torrent(torrent_id, f"Torrent has error status code: {status_code}")
                    except Exception as e:
                        logging.error(f"{log_prefix} Error in cleanup after error status: {str(e)}")
                        self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)
                    results[hash_value] = None
                    continue

                # Check for video files
                # Note: get_torrent_info already normalizes files into info['files']
                files = info.get('files', [])
                video_files = [f for f in files if is_video_file(f.get('path', '') or f.get('name', ''))]
                logging.debug(f"{log_prefix} Found {len(files)} files, {len(video_files)} video files")
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

                # Status code 4 = Ready/Downloaded
                is_cached = status_code == 4

                # Check against DB using filenames before deciding on removal
                is_in_db = False
                if imdb_id:
                    from database.database_reading import is_any_file_in_db_for_item
                    torrent_filenames = [f.get('path', '') for f in files]
                    logging.info(f"Comparing {torrent_filenames} to DB for IMDb ID {imdb_id}")
                    if is_any_file_in_db_for_item(imdb_id, torrent_filenames):
                        is_in_db = True
                        if is_cached:
                            logging.info(f"{log_prefix} A file from this torrent matches a DB entry for IMDb ID {imdb_id} and is cached. Disabling removal.")

                # Override removal flags if item is in DB AND cached
                local_remove_uncached = remove_uncached
                local_remove_cached = remove_cached
                if is_in_db and is_cached:
                    local_remove_uncached = False
                    local_remove_cached = False

                # Update PhalanxDB
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

    def _extract_files_from_info(self, info: Dict) -> List[Dict]:
        """
        Extract file list from AllDebrid torrent info.
        AllDebrid v4.1 returns nested file tree structure.
        """
        files = []

        # Try to get files from the links array (v4 format)
        links = info.get('links', [])
        if links:
            for link in links:
                files.append({
                    'path': link.get('filename', ''),
                    'name': link.get('filename', ''),
                    'bytes': link.get('size', 0),
                    'link': link.get('link', '')
                })
            return files

        # Try to get from files array (may be nested in v4.1)
        raw_files = info.get('files', [])
        if raw_files:
            files = self._flatten_file_tree(raw_files)

        return files

    def _flatten_file_tree(self, nodes: List, path_prefix: str = "") -> List[Dict]:
        """Recursively flatten AllDebrid's nested file tree structure."""
        files = []
        for node in nodes:
            # Check if it's a file or directory
            if 'e' in node:  # Directory with entries
                subpath = f"{path_prefix}/{node.get('n', '')}" if path_prefix else node.get('n', '')
                files.extend(self._flatten_file_tree(node['e'], subpath))
            else:  # File
                name = node.get('n', '')
                full_path = f"{path_prefix}/{name}" if path_prefix else name
                files.append({
                    'path': full_path,
                    'name': name,
                    'bytes': node.get('s', 0),
                    'link': node.get('l', '')
                })
        return files

    def get_cached_torrent_id(self, hash_value: str) -> Optional[str]:
        """Get stored torrent ID for a cached hash"""
        return self._cached_torrent_ids.get(hash_value)

    def get_cached_torrent_title(self, hash_value: str) -> Optional[str]:
        """Get stored torrent title for a cached hash"""
        return self._cached_torrent_titles.get(hash_value)

    def list_active_torrents(self) -> List[Dict]:
        """List all active torrents"""
        try:
            # v4 is discontinued, use v4.1
            result = make_request('GET', '/v4.1/magnet/status', self.api_key, use_query_auth=True)
            if not result or result.get('status') != 'success':
                return []

            data = result.get('data', {})
            # v4.1 returns magnets as dict when single, list when multiple, or empty
            magnets_data = data.get('magnets', [])
            magnets = magnets_data if isinstance(magnets_data, list) else [magnets_data] if magnets_data else []

            # Convert to standard format
            torrents = []
            for magnet in magnets:
                status_code = magnet.get('statusCode', 0)
                status = STATUS_CODE_MAP.get(status_code, TorrentStatus.UNKNOWN)

                # Calculate progress (AllDebrid provides downloaded/size)
                downloaded = magnet.get('downloaded', 0)
                size = magnet.get('size', 1)  # Avoid division by zero
                progress = (downloaded / size * 100) if size > 0 else 0

                torrents.append({
                    'id': str(magnet.get('id', '')),
                    'filename': magnet.get('filename', ''),
                    'hash': magnet.get('hash', '').lower(),
                    'status': status.value if hasattr(status, 'value') else str(status),
                    'progress': progress,
                    'bytes': size,
                    'original_filename': magnet.get('filename', '')
                })

            return torrents
        except Exception as e:
            logging.error(f"Error listing active torrents: {str(e)}")
            return []

    def add_torrent(self, magnet_link: Optional[str], temp_file_path: Optional[str] = None) -> Optional[str]:
        """Add a torrent to AllDebrid"""
        try:
            hash_value = None

            # Extract hash from temp file or magnet
            if temp_file_path:
                try:
                    with open(temp_file_path, 'rb') as f:
                        torrent_data = bencodepy.decode(f.read())
                        info = torrent_data[b'info']
                        hash_value = hashlib.sha1(bencodepy.encode(info)).hexdigest()
                except Exception as e:
                    logging.error(f"Could not extract hash from torrent file: {str(e)}")
            elif magnet_link:
                hash_value = extract_hash_from_magnet(magnet_link)

            if not hash_value and not temp_file_path:
                logging.error("Could not extract hash from magnet link")

            # Handle torrent file upload
            if temp_file_path:
                import os
                if not os.path.exists(temp_file_path):
                    logging.error(f"Temp file does not exist: {temp_file_path}")
                    raise ValueError(f"Temp file does not exist: {temp_file_path}")

                # Upload torrent file
                with open(temp_file_path, 'rb') as f:
                    files = {'files[]': (os.path.basename(temp_file_path), f)}
                    result = make_request('POST', '/magnet/upload/file', self.api_key, files=files, use_query_auth=True)
                    logging.debug(f"File upload response: {result}")

            elif magnet_link:
                # URL decode the magnet link if needed
                if '%' in magnet_link:
                    magnet_link = unquote(magnet_link)

                # Check if torrent already exists
                if hash_value:
                    existing = self._find_existing_torrent(hash_value)
                    if existing:
                        logging.info(f"Torrent already exists with ID {existing['id']}")
                        self._cached_torrent_titles[hash_value] = existing.get('filename', '')
                        return str(existing['id'])

                # Add magnet link
                data = {'magnets[]': magnet_link}
                result = make_request('POST', '/magnet/upload', self.api_key, data=data, use_query_auth=True)
                logging.debug(f"Magnet upload response: {result}")
            else:
                logging.error("Neither magnet_link nor temp_file_path provided")
                raise ValueError("Either magnet_link or temp_file_path must be provided")

            if not result or result.get('status') != 'success':
                error_msg = result.get('error', {}).get('message', 'Unknown error') if result else 'No response'
                logging.error(f"Failed to add torrent - response: {error_msg}")
                raise TorrentAdditionError(f"Failed to add torrent: {error_msg}")

            # Extract torrent info from response
            # Note: magnet uploads return 'magnets', file uploads return 'files'
            data = result.get('data', {})
            items = data.get('magnets', []) or data.get('files', [])

            if not items:
                logging.error(f"No magnets or files in response: {result}")
                raise TorrentAdditionError("No magnets or files in response")

            item_info = items[0]
            torrent_id = str(item_info.get('id', ''))

            if not torrent_id:
                logging.error("No torrent ID in response")
                raise TorrentAdditionError("No torrent ID in response")

            # AllDebrid doesn't require file selection - it auto-processes all files
            # Just wait for the torrent to be processed
            max_attempts = 30
            for attempt in range(max_attempts):
                info = self.get_torrent_info(torrent_id)
                if not info:
                    logging.error("Failed to get torrent info")
                    raise TorrentAdditionError("Failed to get torrent info")

                status_code = info.get('statusCode', -1)

                # Error status
                if status_code >= 5:
                    error_msg = f"Torrent error (status code {status_code})"
                    self.remove_torrent(torrent_id, error_msg)
                    raise TorrentAdditionError(error_msg)

                # Ready or downloading - success
                if status_code >= 1:  # Downloading or ready
                    # Cache the filename
                    if hash_value:
                        self._cached_torrent_titles[hash_value] = info.get('filename', '')

                    # Record torrent addition
                    try:
                        from database.torrent_tracking import update_torrent_tracking, record_torrent_addition
                        # Note: get_torrent_info already normalizes files into info['files']
                        files = info.get('files', [])
                        video_files = [f for f in files if is_video_file(f.get('path', '') or f.get('name', ''))]

                        if video_files:
                            largest_file = max(video_files, key=lambda x: x.get('bytes', 0))
                            updated_item_data = {
                                'filled_by_title': info.get('filename'),
                                'filled_by_file': largest_file.get('path', '')
                            }
                            updated_trigger_details = {
                                'source': 'alldebrid',
                                'status_code': status_code
                            }
                            updated_metadata = {
                                'debrid_info': {
                                    'provider': 'alldebrid',
                                    'torrent_id': torrent_id,
                                    'status_code': status_code,
                                    'filename': info.get('filename')
                                }
                            }

                            if hash_value and not update_torrent_tracking(
                                torrent_hash=hash_value,
                                item_data=updated_item_data,
                                trigger_details=updated_trigger_details,
                                additional_metadata=updated_metadata
                            ):
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

                    return torrent_id

                # Still queued, wait
                time.sleep(1)

            # Timeout
            logging.error("Timed out waiting for torrent to process")
            self.remove_torrent(torrent_id, "Timed out waiting for torrent to process")
            raise TorrentAdditionError("Timed out waiting for torrent to process")

        except Exception as e:
            logging.error(f"Error adding torrent: {str(e)}")
            raise

    def _find_existing_torrent(self, hash_value: str) -> Optional[Dict]:
        """Find an existing torrent by hash"""
        try:
            # v4 is discontinued, use v4.1
            result = make_request('GET', '/v4.1/magnet/status', self.api_key, use_query_auth=True)
            if not result or result.get('status') != 'success':
                return None

            magnets_data = result.get('data', {}).get('magnets', [])
            magnets = magnets_data if isinstance(magnets_data, list) else [magnets_data] if magnets_data else []
            for magnet in magnets:
                if magnet.get('hash', '').lower() == hash_value.lower():
                    return magnet
            return None
        except Exception as e:
            logging.error(f"Error finding existing torrent: {str(e)}")
            return None

    @timed_lru_cache(seconds=1)
    def get_active_downloads(self) -> Tuple[int, int]:
        """Get number of active downloads and download limit"""
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return 0, 0

            # v4 is discontinued, use v4.1
            result = make_request('GET', '/v4.1/magnet/status', self.api_key, use_query_auth=True)
            if not result or result.get('status') != 'success':
                return 0, self.MAX_DOWNLOADS

            magnets_data = result.get('data', {}).get('magnets', [])
            magnets = magnets_data if isinstance(magnets_data, list) else [magnets_data] if magnets_data else []

            # Count active (downloading) torrents
            active_count = sum(1 for m in magnets if m.get('statusCode', 0) in [0, 1, 2, 3])

            # AllDebrid doesn't expose a limit via API, use default
            max_downloads = self.MAX_DOWNLOADS

            if active_count >= max_downloads:
                logging.warning(f"Active downloads ({active_count}) at or above limit ({max_downloads})")
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
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'downloaded': 0, 'limit': None}

            # AllDebrid doesn't have same daily traffic concept as Real-Debrid
            # Return a compatible structure
            result = make_request('GET', '/user', self.api_key, use_query_auth=True)
            if not result or result.get('status') != 'success':
                return {'downloaded': 0, 'limit': None}

            user_info = result.get('data', {}).get('user', {})

            # AllDebrid has fidelityPoints instead of traffic
            return {
                'downloaded': 0,  # AllDebrid doesn't track daily downloads
                'limit': None,    # No daily limit
                'fidelity_points': user_info.get('fidelityPoints', 0)
            }

        except Exception as e:
            logging.error(f"Error getting user traffic: {str(e)}")
            raise ProviderUnavailableError(f"Failed to get user traffic: {str(e)}")

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict]:
        """Get information about a specific torrent"""
        try:
            # Use v4.1 for nested file tree with query auth
            result = make_request('GET', f'/v4.1/magnet/status', self.api_key, params={'id': torrent_id}, use_query_auth=True)
            logging.debug(f"v4.1 status response for torrent {torrent_id}: {result}")

            if not result or result.get('status') != 'success':
                return None

            data = result.get('data', {})
            magnets = data.get('magnets', data)  # Could be dict or in magnets array

            # Handle different response formats
            if isinstance(magnets, dict):
                info = magnets
            elif isinstance(magnets, list) and magnets:
                info = magnets[0]
            else:
                return None

            # Update status based on response
            status_code = info.get('statusCode', -1)
            hash_value = info.get('hash', '').lower()

            if status_code == 4:
                self.update_status(torrent_id, TorrentStatus.CACHED)
            elif status_code in [1, 2, 3]:
                self.update_status(torrent_id, TorrentStatus.DOWNLOADING)
            elif status_code == 0:
                self.update_status(torrent_id, TorrentStatus.QUEUED)
            elif status_code >= 5:
                self.update_status(torrent_id, TorrentStatus.ERROR)
                if hash_value:
                    from database.torrent_tracking import mark_torrent_removed
                    mark_torrent_removed(hash_value, f"AllDebrid error status: {status_code}")

            # In v4.1, files are fetched from a separate endpoint
            # Try to get files from status response first, then fetch from /magnet/files if needed
            normalized_files = self._extract_files_from_info(info)

            if not normalized_files:
                # Fetch files from dedicated endpoint (required for v4.1)
                logging.debug(f"No files in status response for torrent {torrent_id}, fetching from /magnet/files")
                try:
                    files_result = make_request('POST', '/magnet/files', self.api_key, data={'id[]': torrent_id}, use_query_auth=True)
                    logging.debug(f"/magnet/files response: {files_result}")
                    if files_result and files_result.get('status') == 'success':
                        files_data = files_result.get('data', {})
                        files_magnets = files_data.get('magnets', [])
                        if files_magnets:
                            # Find matching magnet by ID
                            for fm in files_magnets:
                                if str(fm.get('id', '')) == str(torrent_id):
                                    # Extract files from the nested tree structure
                                    raw_files = fm.get('files', [])
                                    logging.debug(f"Raw files from /magnet/files for torrent {torrent_id}: {raw_files}")
                                    if raw_files:
                                        normalized_files = self._flatten_file_tree(raw_files)
                                        logging.debug(f"Normalized files: {normalized_files}")
                                    break
                except Exception as files_err:
                    logging.warning(f"Could not fetch files from /magnet/files: {files_err}")

            info['files'] = normalized_files

            # Calculate progress for checking queue compatibility
            # AllDebrid provides downloaded/size, but checking queue expects 'progress' field
            downloaded = info.get('downloaded', 0)
            size = info.get('size', 1)  # Avoid division by zero
            # Status code 4 = Ready/Downloaded = 100%
            if status_code == 4:
                info['progress'] = 100
            else:
                info['progress'] = (downloaded / size * 100) if size > 0 else 0

            return info

        except Exception as e:
            if "404" in str(e):
                self.update_status(torrent_id, TorrentStatus.REMOVED)
                for hash_value, tid in self._cached_torrent_ids.items():
                    if tid == torrent_id:
                        from database.torrent_tracking import mark_torrent_removed
                        mark_torrent_removed(hash_value, "Torrent no longer exists in AllDebrid")
                        break
            else:
                logging.error(f"Error getting torrent info: {str(e)}")
                self.update_status(torrent_id, TorrentStatus.ERROR)
            return None

    def get_torrent_info_with_status(self, torrent_id: str) -> TorrentInfoStatus:
        """Get detailed information about a torrent, including fetch status."""
        try:
            info = self.get_torrent_info(torrent_id)
            if info:
                return TorrentInfoStatus(
                    status=TorrentFetchStatus.OK,
                    data=info,
                    message=None,
                    http_status_code=200
                )
            else:
                return TorrentInfoStatus(
                    status=TorrentFetchStatus.NOT_FOUND,
                    data=None,
                    message="Torrent not found",
                    http_status_code=404
                )
        except Exception as e:
            return TorrentInfoStatus(
                status=TorrentFetchStatus.UNKNOWN_ERROR,
                data=None,
                message=str(e),
                http_status_code=None
            )

    def remove_torrent(self, torrent_id: str, removal_reason: str = "Manual removal") -> None:
        """Remove a torrent from AllDebrid"""
        try:
            # Get torrent info before removal to get the hash
            hash_value = None
            try:
                info = self.get_torrent_info(torrent_id)
                if info:
                    hash_value = info.get('hash', '').lower()
            except Exception as e:
                logging.warning(f"Could not get torrent info before removal: {str(e)}")

            # Make the deletion request
            logging.info(f"Attempting to remove torrent {torrent_id} from AllDebrid (reason: {removal_reason})")

            data = {'id': torrent_id}
            result = make_request('POST', '/magnet/delete', self.api_key, data=data, use_query_auth=True)

            if result and result.get('status') == 'success':
                logging.info(f"Successfully removed torrent {torrent_id} from AllDebrid")
            else:
                error_msg = result.get('error', {}).get('message', 'Unknown error') if result else 'No response'
                logging.warning(f"Removal request returned: {error_msg}")

            # Update status and tracking
            self.update_status(torrent_id, TorrentStatus.REMOVED)

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
            if "404" in str(e) or "MAGNET_INVALID_ID" in str(e):
                logging.warning(f"Torrent {torrent_id} already removed from AllDebrid")
                if hash_value:
                    from database.torrent_tracking import mark_torrent_removed
                    mark_torrent_removed(hash_value, f"{removal_reason} (already removed)")
            else:
                logging.error(f"Error removing torrent {torrent_id}: {str(e)}", exc_info=True)
            raise

    def cleanup(self) -> None:
        """Clean up status tracking"""
        try:
            # Verify tracked torrents are still present
            self.verify_torrent_presence()
        except Exception as e:
            logging.error(f"Error during status tracking cleanup: {str(e)}")
        finally:
            super().cleanup()

    def verify_torrent_presence(self, hash_value: str = None) -> bool:
        """Verify if a torrent is still present in AllDebrid."""
        try:
            # v4 is discontinued, use v4.1
            result = make_request('GET', '/v4.1/magnet/status', self.api_key, use_query_auth=True)
            if not result or result.get('status') != 'success':
                return False

            magnets_data = result.get('data', {}).get('magnets', [])
            magnets = magnets_data if isinstance(magnets_data, list) else [magnets_data] if magnets_data else []
            active_hashes = {m['hash'].lower(): str(m['id']) for m in magnets if m.get('hash')}

            from database.torrent_tracking import mark_torrent_removed

            if hash_value:
                if hash_value.lower() not in active_hashes:
                    mark_torrent_removed(hash_value, "Torrent no longer exists in AllDebrid")
                    return False
                return True
            else:
                success = True
                for h, tid in self._cached_torrent_ids.items():
                    if h.lower() not in active_hashes:
                        mark_torrent_removed(h, "Torrent no longer exists in AllDebrid")
                        success = False
                return success

        except Exception as e:
            logging.error(f"Error verifying torrent presence: {str(e)}")
            return False

    def is_cached_sync(
        self,
        magnet_link: str,
        temp_file_path: Optional[str] = None,
        result_title: Optional[str] = None,
        result_index: Optional[str] = None,
        remove_uncached: bool = True,
        remove_cached: bool = False,
        skip_phalanx_db: bool = False,
        imdb_id: Optional[str] = None
    ) -> Union[bool, Dict[str, bool], None]:
        """Synchronous version of is_cached"""
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            return loop.run_until_complete(
                self.is_cached(magnet_link, temp_file_path, result_title, result_index, remove_uncached, remove_cached, skip_phalanx_db, imdb_id)
            )
        except Exception as e:
            logging.error(f"Error in is_cached_sync: {str(e)}")
            return None
