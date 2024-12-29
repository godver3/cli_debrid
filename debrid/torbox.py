import logging
from typing import Dict, List, Optional, Tuple, Union
from .base import DebridProvider, ProviderUnavailableError, TooManyDownloadsError
from .status import TorrentStatus, get_status_flags
from settings import get_setting
from api_tracker import api
import os
import tempfile
import bencodepy
import hashlib
import time
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
            except (requests.exceptions.RequestException, TorboxUnavailableError) as e:
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
        url = f"{self.API_BASE_URL}/{endpoint}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        if 'headers' in kwargs:
            kwargs['headers'].update(headers)
        else:
            kwargs['headers'] = headers

        try:
            if method.lower() == 'get':
                response = api.get(url, **kwargs)
            elif method.lower() == 'post':
                response = api.post(url, **kwargs)
            elif method.lower() == 'put':
                response = api.put(url, **kwargs)
            elif method.lower() == 'delete':
                response = api.delete(url, **kwargs)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            if response.status_code == 429:
                raise TorboxTooManyDownloadsError("Too many active downloads")
            elif response.status_code == 503:
                raise TorboxUnavailableError("Service temporarily unavailable")
            elif response.status_code >= 400:
                raise TorboxUnavailableError(f"API error: {response.status_code} - {response.text}")

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise TorboxUnavailableError(f"Request failed: {str(e)}")

    def add_torrent(self, magnet_link: str, temp_file_path: Optional[str] = None) -> Dict:
        """Add a torrent/magnet link to Torbox"""
        logging.info("Adding torrent to Torbox")
        try:
            # Default parameters
            data = {
                'seed': 1,  # auto seeding
                'allow_zip': True,  # allow zipping for large torrents
                'as_queued': False  # process normally
            }
            
            if temp_file_path:
                with open(temp_file_path, 'rb') as f:
                    files = {'file': f}
                    data.update(files)
                    response = self._make_request('POST', 'api/torrents/createtorrent', files=files, data=data)
            else:
                data['magnet'] = magnet_link
                response = self._make_request('POST', 'api/torrents/createtorrent', data=data)
            
            logging.info(f"Add torrent response: {response}")
            
            # Extract torrent info from response
            if response.get('success') and 'data' in response:
                torrent_data = response['data']
                is_cached = 'Found Cached Torrent' in response.get('detail', '')
                hash_value = torrent_data.get('hash')
                torrent_id = str(torrent_data.get('torrent_id'))
                
                # Get files if it's a cached torrent
                files = []
                if is_cached and hash_value and torrent_id:
                    files = self.get_torrent_files(hash_value, torrent_id)
                    logging.info(f"Retrieved files for cached torrent: {files}")
                
                return {
                    'id': torrent_id,
                    'hash': hash_value,
                    'status': 'downloaded' if is_cached else 'queued',  # Set status based on cache
                    'files': files  # Include files in response
                }
            else:
                logging.error(f"Failed to add torrent. Response: {response}")
                return {}
                
        except Exception as e:
            logging.error(f"Error adding torrent to Torbox: {str(e)}")
            raise

    def is_cached(self, hashes: Union[str, List[str]]) -> Dict:
        """Check if hash(es) are cached on Torbox"""
        if isinstance(hashes, str):
            hashes = [hashes]
        
        try:
            # Build params with hash and format
            params = {
                'hash': ','.join(hashes),  # API accepts comma-separated hashes
                'format': 'object',  # Get response in object format
                'list_files': False  # Don't need file listings for cache check
            }
            
            logging.info(f"Checking cache status for hashes: {hashes}")
            response = self._make_request('GET', 'api/torrents/checkcached', params=params)
            logging.info(f"Raw cache check response: {response}")
            
            # Convert response to expected format
            result = {}
            if response.get('success') and 'data' in response:
                response_data = response['data']
                for hash_ in hashes:
                    # A hash is considered cached if it exists in the response data
                    hash_data = response_data.get(hash_, {})
                    is_cached = bool(hash_data)  # True if hash data exists
                    logging.info(f"Cache status for hash {hash_}: {is_cached} (data: {hash_data})")
                    result[hash_] = is_cached
            else:
                logging.warning(f"Unexpected response format: {response}")
                for hash_ in hashes:
                    result[hash_] = False
            
            logging.info(f"Final cache status result: {result}")
            return result
        except Exception as e:
            logging.error(f"Error checking cache status on Torbox: {str(e)}")
            return {hash_: False for hash_ in hashes}

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

    def get_torrent_files(self, hash_value: str, torrent_id: Optional[str] = None) -> List[Dict]:
        """Get list of files in a torrent"""
        max_retries = 3
        retry_delay = 2  # seconds
        
        def try_torrentinfo():
            params = {
                'hash': hash_value,
                'torrent_id': torrent_id
            }
            response = self._make_request('GET', 'api/torrents/torrentinfo', params=params)
            logging.info(f"Get torrent files response (torrentinfo): {response}")
            
            if response.get('success') and 'data' in response:
                torrent_data = response['data']
                files_data = torrent_data.get('files', [])
                # Convert to expected format
                files = []
                for file_data in files_data:
                    files.append({
                        'path': file_data.get('name', ''),
                        'bytes': file_data.get('size', 0),
                        'selected': True  # All files are selected by default
                    })
                return files
            return []
            
        def try_requestdl():
            params = {
                'hash': hash_value,
                'torrent_id': torrent_id,
                'token': self.api_key
            }
            response = self._make_request('GET', 'api/torrents/requestdl', params=params)
            logging.info(f"Get torrent files response (requestdl): {response}")
            
            if response.get('success') and 'data' in response:
                # If we get a download URL, try to get the file info from the torrent info again
                # The metadata might be available now that we've requested the download
                time.sleep(1)  # Give the server a moment to process
                return try_torrentinfo()
            return []
        
        try:
            for attempt in range(max_retries):
                try:
                    # Try torrentinfo first
                    files = try_torrentinfo()
                    if files:
                        return files
                        
                    # If torrentinfo fails or returns no files, try requestdl
                    logging.info("Torrentinfo returned no files, trying requestdl...")
                    files = try_requestdl()
                    if files:
                        return files
                        
                    if attempt < max_retries - 1:
                        logging.info(f"Retry {attempt + 1}/{max_retries} failed, waiting {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    
                except Exception as e:
                    if "DOWNLOAD_SERVER_ERROR" in str(e) and attempt < max_retries - 1:
                        logging.warning(f"Server error on attempt {attempt + 1}/{max_retries}, retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    else:
                        raise
                        
            logging.warning("All attempts to get torrent files failed")
            return []
            
        except Exception as e:
            logging.error(f"Error getting torrent files from Torbox: {str(e)}")
            return []

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
