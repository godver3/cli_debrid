import logging
import json
import re
import os
import time
import tempfile
import requests
import hashlib
import bencodepy
from typing import Dict, List, Optional, Tuple, Union
from .base import DebridProvider, ProviderUnavailableError, TooManyDownloadsError
from .status import TorrentStatus, get_status_flags
from settings import get_setting
from api_tracker import api
import functools
from functools import lru_cache, wraps

class TorboxUnavailableError(ProviderUnavailableError):
    pass

class TorboxTooManyDownloadsError(TooManyDownloadsError):
    pass

class RateLimiter:
    """Rate limiter to prevent API abuse"""
    def __init__(self, calls_per_second=1):
        self.calls_per_second = calls_per_second
        self.last_call = 0

    def wait(self):
        """Wait if necessary to respect rate limit"""
        now = time.time()
        time_since_last = now - self.last_call
        if time_since_last < (1.0 / self.calls_per_second):
            time.sleep((1.0 / self.calls_per_second) - time_since_last)
        self.last_call = time.time()

def rate_limited_request(func):
    """Decorator to rate limit API requests"""
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if hasattr(self, 'rate_limiter'):
            self.rate_limiter.wait()
        return func(self, *args, **kwargs)
    return wrapper

def retry_on_error(func):
    """Decorator to retry API calls with exponential backoff"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = 3
        retry_count = 0
        base_delay = 1
        max_delay = 10
        
        while retry_count < max_retries:
            try:
                return func(*args, **kwargs)
            except (api.exceptions.RequestException, TorboxUnavailableError) as e:
                retry_count += 1
                if retry_count == max_retries:
                    raise
                
                delay = min(base_delay * (2 ** (retry_count - 1)), max_delay)
                time.sleep(delay)
        return func(*args, **kwargs)
    return wrapper

def timed_lru_cache(seconds: int, maxsize: int = 128):
    """Decorator that provides an LRU cache with time-based expiration"""
    def decorator(func):
        func = lru_cache(maxsize=maxsize)(func)
        func.lifetime = seconds
        func.expiration = time.time() + seconds

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            if time.time() >= func.expiration:
                func.cache_clear()
                func.expiration = time.time() + func.lifetime
            return func(*args, **kwargs)

        wrapped.cache_info = func.cache_info
        wrapped.cache_clear = func.cache_clear
        return wrapped
    return decorator

class TorboxProvider(DebridProvider):
    """Torbox implementation of the DebridProvider interface"""
    
    API_BASE_URL = "https://api.torbox.app/v1"  # Base URL without specific endpoints
    MAX_DOWNLOADS = 5
    
    def __init__(self):
        self.api_key = self.get_api_key()
        self.rate_limiter = RateLimiter(calls_per_second=0.5)  # Match RealDebrid's rate limit

    def get_api_key(self) -> str:
        """Get API key from settings"""
        api_key = get_setting('Debrid Provider', 'api_key')
        if not api_key:
            raise ValueError("Debrid Provider API key not found in settings")
        return api_key

    @retry_on_error
    @rate_limited_request
    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make a request to the Torbox API"""
        try:
            # Add API key to headers if not present
            headers = kwargs.get('headers', {})
            headers['Authorization'] = f'Bearer {self.api_key}'
            kwargs['headers'] = headers

            # Make request
            url = f"{self.API_BASE_URL}/{endpoint}"
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            
            # Parse response
            data = response.json()
            
            # Check for API errors
            if not data.get('success'):
                error = data.get('error', 'UNKNOWN_ERROR')
                detail = data.get('detail', 'No error details provided')
                logging.error(f"API error: {response.status_code} - {data}")
                
                if error == 'UNKNOWN_ERROR':
                    # For unknown errors, wait a bit and retry
                    time.sleep(2)
                    logging.info("Retrying request after unknown error...")
                    response = requests.request(method, url, **kwargs)
                    response.raise_for_status()
                    data = response.json()
                    if not data.get('success'):
                        raise Exception(f"API error after retry: {error} - {detail}")
                else:
                    raise Exception(f"API error: {error} - {detail}")
            
            return data
            
        except requests.exceptions.RequestException as e:
            logging.error(f"Request failed: {str(e)}")
            raise
        except Exception as e:
            logging.error(f"Error making request: {str(e)}")
            raise

    def is_cached(self, hashes: Union[str, List[str]]) -> Dict[str, bool]:
        """Check if hash(es) are cached on Torbox"""
        if isinstance(hashes, str):
            hashes = [hashes]
            
        params = {
            'hash': ','.join(hashes)  # Changed from 'hashes' to 'hash'
        }
        
        try:
            response = self._make_request('GET', 'api/torrents/checkcached', params=params)
            logging.info(f"Raw cache check response: {response}")
            
            result = {}
            if response.get('success') and 'data' in response:
                data = response['data']
                for hash_ in hashes:
                    # Store the full cache data for later use
                    self._cache_data = getattr(self, '_cache_data', {})
                    if hash_ in data:
                        self._cache_data[hash_] = data[hash_]
                        result[hash_] = True
                        logging.info(f"Cache status for hash {hash_}: True (data: {data[hash_]})")
                    else:
                        result[hash_] = False
                        logging.info(f"Cache status for hash {hash_}: False (data: {data})")
            
            logging.info(f"Final cache status result: {result}")
            return result  # Always return dict, even for single hash
            
        except Exception as e:
            logging.error(f"Error checking cache status: {str(e)}")
            return {h: False for h in hashes}  # Always return dict

    def get_torrent_files(self, hash_value: str, torrent_id: Optional[str] = None) -> List[Dict]:
        """Get list of files in a torrent using the torrentinfo endpoint"""
        try:
            params = {
                'hash': hash_value,
                'timeout': 5
            }
            if torrent_id:
                params['torrent_id'] = torrent_id

            logging.info(f"Requesting torrentinfo with params: {params}")
            response = self._make_request('GET', 'api/torrents/torrentinfo', params=params)
            
            if not response or not isinstance(response, dict):
                raise Exception("Invalid response format")
                
            files = []
            if 'files' in response:
                for file_data in response['files']:
                    file_info = {
                        'path': file_data.get('path', ''),
                        'bytes': file_data.get('bytes', 0),
                        'selected': file_data.get('selected', 1)
                    }
                    files.append(file_info)
                    
                if files:
                    logging.info(f"Successfully got files from torrentinfo: {files}")
                    return files
                    
            raise Exception("No valid files found in response")
                
        except Exception as e:
            logging.error(f"Error getting files from torrentinfo: {str(e)}")
            raise

    def _extract_files_from_torrent_data(self, torrent_data: bytes) -> List[Dict]:
        """Extract file information from torrent data"""
        try:
            decoded = bencodepy.decode(torrent_data)
            if not isinstance(decoded, dict):
                raise ValueError("Invalid torrent data format")

            info = decoded.get(b'info', {})
            if not info:
                raise ValueError("No info dict in torrent data")

            files = []
            # Single file torrent
            if b'length' in info:
                name = info.get(b'name', b'').decode('utf-8', errors='ignore')
                files.append({
                    'path': name,
                    'bytes': info[b'length'],
                    'selected': 1
                })
            # Multi file torrent
            elif b'files' in info:
                for file_info in info[b'files']:
                    if not isinstance(file_info, dict):
                        continue
                    path_parts = [p.decode('utf-8', errors='ignore') for p in file_info.get(b'path', [])]
                    files.append({
                        'path': os.path.join(*path_parts),
                        'bytes': file_info.get(b'length', 0),
                        'selected': 1
                    })

            return files
        except Exception as e:
            logging.error(f"Error extracting files from torrent data: {str(e)}")
            return []

    def _fetch_files_from_magnet(self, magnet_link: str, timeout: int = 30) -> List[Dict]:
        """
        Fetch files from a magnet link using libtorrent
        Returns list of files in the same format as torrentinfo endpoint
        """
        try:
            import libtorrent
            
            # Create session
            sess = libtorrent.session()
            sess.listen_on(6881, 6891)
            
            # Add magnet
            params = libtorrent.parse_magnet_uri(magnet_link)
            params.save_path = '/tmp'  # Temporary save path
            handle = sess.add_torrent(params)
            
            logging.info(f"Fetching metadata for magnet {magnet_link}")
            
            # Try to fetch metadata with timeout
            timeout_counter = 0
            while not handle.has_metadata() and timeout_counter < timeout:
                time.sleep(1)
                timeout_counter += 1
            
            if not handle.has_metadata():
                logging.warning("Timeout while fetching magnet metadata")
                sess.remove_torrent(handle)
                return []
            
            # Get torrent info
            torrent_info = handle.get_torrent_info()
            
            # Extract files
            files = []
            for f in torrent_info.files():
                files.append({
                    'path': f.path,
                    'bytes': f.size,
                    'selected': 1
                })
            
            # Cleanup
            sess.remove_torrent(handle)
            
            logging.info(f"Successfully extracted {len(files)} files from magnet")
            return files
            
        except ImportError as e:
            logging.warning(f"libtorrent not available: {str(e)}, cannot fetch magnet metadata")
            return []
        except Exception as e:
            logging.error(f"Error fetching magnet metadata: {str(e)}")
            return []

    def add_torrent(self, magnet_link: str, temp_file_path: Optional[str] = None) -> Dict:
        """Add a torrent/magnet link to Torbox"""
        logging.info("Adding torrent to Torbox")
        torrent_files = []
        
        try:
            # Default parameters
            data = {
                'seed': 1,  # auto seeding
                'allow_zip': True,  # allow zipping for large torrents
                'as_queued': False  # process normally
            }
            
            if temp_file_path:
                try:
                    # Read torrent file and extract files before uploading
                    with open(temp_file_path, 'rb') as f:
                        torrent_data = f.read()
                        torrent_files = self._extract_files_from_torrent_data(torrent_data)
                        
                    with open(temp_file_path, 'rb') as f:
                        files = {'file': ('torrent.torrent', f, 'application/x-bittorrent')}
                        response = self._make_request('POST', 'api/torrents/createtorrent', files=files, data=data)
                finally:
                    # Clean up temp file if we created it
                    if temp_file_path and 'jackett' in magnet_link.lower():
                        try:
                            if os.path.exists(temp_file_path):
                                os.unlink(temp_file_path)
                                logging.debug(f"Successfully deleted temporary file: {temp_file_path}")
                        except Exception as e:
                            logging.warning(f"Error deleting temporary file: {str(e)}")
            else:
                # For magnet links, try to fetch files first
                if magnet_link:
                    torrent_files = self._fetch_files_from_magnet(magnet_link)
                
                data['magnet'] = magnet_link
                response = self._make_request('POST', 'api/torrents/createtorrent', data=data)
            
            logging.info(f"Add torrent response: {response}")
            
            # Extract torrent info from response
            if response.get('success') and 'data' in response:
                torrent_data = response['data']
                logging.info(f"Full torrent data: {torrent_data}")
                
                hash_value = torrent_data.get('hash')
                queued_id = str(torrent_data.get('queued_id'))
                
                # First try using the files we extracted
                if torrent_files:
                    logging.info("Using files extracted from torrent/magnet")
                    torrent_data['files'] = torrent_files
                    response['files'] = torrent_files
                # If no files extracted, try getting them from API
                elif hash_value and queued_id:
                    try:
                        logging.info("Attempting to get files from torrentinfo endpoint")
                        files = self.get_torrent_files(hash_value, queued_id)
                        if files:
                            logging.info("Successfully got files from torrentinfo")
                            torrent_data['files'] = files
                            response['files'] = files
                    except Exception as e:
                        logging.warning(f"Failed to get files from torrentinfo: {str(e)}")
                        torrent_data['files'] = []
                        response['files'] = []
                
                return response
            
            return None
            
        except Exception as e:
            logging.error(f"Error adding torrent: {str(e)}")
            return None

    def get_active_downloads(self, check: bool = False) -> Tuple[int, List[Dict]]:
        """Get list of active downloads"""
        try:
            response = self._make_request('GET', 'api/torrents/mylist')
            active_torrents = [
                torrent for torrent in response.get('torrents', [])
                if torrent.get('status') in ['downloading', 'queued']
            ]
            return len(active_torrents), active_torrents
        except Exception as e:
            logging.error(f"Error getting active downloads from Torbox: {str(e)}")
            return 0, []

    def get_torrent_info(self, hash_value: str) -> Optional[Dict]:
        """Get information about a specific torrent by its hash"""
        try:
            response = self._make_request('GET', 'api/torrents/mylist')
            for torrent in response.get('torrents', []):
                if torrent.get('hash') == hash_value:
                    status = torrent.get('status', 'unknown')
                    logging.debug(f"Torrent {hash_value} status: {status}")

                    # Map Torbox statuses to common statuses
                    tb_status_map = {
                        'error': TorrentStatus.ERROR.value,
                        'failed': TorrentStatus.ERROR.value,
                        'dead': TorrentStatus.ERROR.value,
                        'completed': TorrentStatus.DOWNLOADED.value,
                        'downloading': TorrentStatus.DOWNLOADING.value,
                        'queued': TorrentStatus.QUEUED.value,
                        'processing': TorrentStatus.QUEUED.value,
                        'preparing': TorrentStatus.QUEUED.value
                    }

                    common_status = tb_status_map.get(status, TorrentStatus.UNKNOWN.value)
                    status_flags = get_status_flags(common_status)

                    if status_flags['is_error']:
                        logging.error(f"Torrent {hash_value} in error state: {status}")
                    elif status_flags['is_cached']:
                        logging.info(f"Torrent {hash_value} is cached")
                    elif status_flags['is_queued']:
                        logging.info(f"Torrent {hash_value} is queued/downloading")

                    return {
                        'id': str(torrent.get('torrent_id')),  # Convert to string for consistency
                        'status': common_status,
                        'seeders': torrent.get('seeders', 0),
                        'progress': torrent.get('progress', 0),
                        'files': torrent.get('files', []),
                        **status_flags
                    }
            return None
        except Exception as e:
            logging.error(f"Error getting torrent info from Torbox: {str(e)}")
            return None

    def control_torrent(self, torrent_id: str, action: str) -> bool:
        """Control a torrent (reannounce, pause, resume, delete)"""
        try:
            # Send as JSON data with 'torrent_id' instead of 'hash'
            data = {
                'torrent_id': int(torrent_id),  # API expects torrent_id
                'operation': action  # API expects 'operation' field
            }
            logging.info(f"Sending control command {action} for torrent ID {torrent_id}")
            response = self._make_request('POST', 'api/torrents/controltorrent', json=data)
            success = response.get('success', False)
            logging.info(f"Control torrent response: {response}")
            return success
        except Exception as e:
            logging.error(f"Error controlling torrent on Torbox: {str(e)}")
            return False

    def remove_torrent(self, torrent_id: str) -> bool:
        """Remove a torrent from Torbox"""
        try:
            return self.control_torrent(torrent_id, 'delete')
        except Exception as e:
            logging.error(f"Error removing torrent from Torbox: {str(e)}")
            return False

    def cleanup_stale_torrents(self, max_age_hours: int = 24) -> None:
        """Remove torrents that haven't been accessed in the specified time period"""
        try:
            active_torrents = self.get_active_downloads()
            current_time = time.time()
            
            for torrent in active_torrents:
                # Skip if torrent is still downloading
                if torrent.get('status') == 'downloading':
                    continue
                    
                last_accessed = torrent.get('last_accessed', 0)
                age_hours = (current_time - last_accessed) / 3600
                
                if age_hours > max_age_hours:
                    try:
                        self.remove_torrent(torrent['id'])
                        logging.info(f"Removed stale torrent: {torrent['id']}")
                    except Exception as e:
                        logging.error(f"Failed to remove stale torrent {torrent['id']}: {str(e)}")
                        
        except Exception as e:
            logging.error(f"Error during stale torrent cleanup: {str(e)}")

    def download_and_extract_hash(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Download a torrent file and extract its hash"""
        try:
            response = api.get(url)
            if response.status_code != 200:
                return None, None

            with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as temp_file:
                temp_file.write(response.content)
                temp_file_path = temp_file.name

            try:
                torrent_data = bencodepy.decode_from_file(temp_file_path)
                info = torrent_data.get(b'info', {})
                info_str = bencodepy.encode(info)
                hash_value = hashlib.sha1(info_str).hexdigest()
                return hash_value, temp_file_path
            except Exception as e:
                logging.error(f"Error extracting hash from torrent file: {str(e)}")
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                return None, None

        except Exception as e:
            logging.error(f"Error downloading torrent file: {str(e)}")
            return None, None

    @timed_lru_cache(seconds=300)  # Cache for 5 minutes
    def get_cached_files(self, hash_: str) -> List[Dict]:
        """Get available cached files for a hash with caching"""
        response = self._make_request('GET', f'torrents/cached/{hash_}')
        return response.get('files', [])

    @timed_lru_cache(seconds=60)  # Cache for 1 minute
    def get_user_traffic(self) -> Dict:
        """Get user traffic/usage information with caching"""
        response = self._make_request('GET', 'user/traffic')
        return response
