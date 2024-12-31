from api_tracker import requests
import json
import re
import logging
from settings import get_setting
from types import SimpleNamespace
from time import sleep
from functools import lru_cache
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import time
from functools import wraps
from datetime import datetime, timedelta
from functools import lru_cache
from .base import DebridProvider, ProviderUnavailableError, TooManyDownloadsError
from .status import TorrentStatus, get_status_flags
import tempfile
import bencodepy
import hashlib
import os
from typing import Optional, Dict, List, Tuple, Union

class RealDebridUnavailableError(ProviderUnavailableError):
    pass

class RealDebridTooManyDownloadsError(TooManyDownloadsError):
    pass

API_BASE_URL = "https://api.real-debrid.com/rest/1.0"

# Common video file extensions
VIDEO_EXTENSIONS = [
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'webm', 'mpg', 'mpeg', 'm2ts', 'ts'
]

_cache = {}

def get_api_key():
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise ValueError("Debrid Provider API key not found in settings")
    if api_key == 'demo_key':
        logging.warning("Running in demo mode with demo API key")
        return api_key
    return api_key

def timed_lru_cache(seconds: int, maxsize: int = 128):
    def wrapper_cache(func):
        func = lru_cache(maxsize=maxsize)(func)
        func.lifetime = timedelta(seconds=seconds)
        func.expiration = datetime.utcnow() + func.lifetime

        @wraps(func)
        def wrapped_func(*args, **kwargs):
            if datetime.utcnow() >= func.expiration:
                func.cache_clear()
                func.expiration = datetime.utcnow() + func.lifetime

            return func(*args, **kwargs)

        return wrapped_func

    return wrapper_cache

def is_video_file(filename):
    result = any(filename.lower().endswith(f'.{ext}') for ext in VIDEO_EXTENSIONS)
    logging.debug(f"is_video_file check for {filename}: {result}")
    logging.debug(f"Checking extensions: {[f'.{ext}' for ext in VIDEO_EXTENSIONS]}")
    return result

def is_unwanted_file(filename):
    result = 'sample' in filename.lower()
    logging.debug(f"is_unwanted_file check for {filename}: {result}")
    return result

class RateLimiter:
    def __init__(self, calls_per_second=1):
        self.calls_per_second = calls_per_second
        self.last_call = 0

    def wait(self):
        current_time = time.time()
        time_since_last_call = current_time - self.last_call
        if time_since_last_call < 1 / self.calls_per_second:
            time.sleep((1 / self.calls_per_second) - time_since_last_call)
        self.last_call = time.time()

rate_limiter = RateLimiter(calls_per_second=0.5)  # Adjust this value as needed

def rate_limited_request(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        rate_limiter.wait()
        return func(*args, **kwargs)
    return wrapper

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception))
)
@rate_limited_request
def add_to_real_debrid(magnet_link, temp_file_path=None):
    """Add a magnet link to Real-Debrid"""
    headers = {
        'Authorization': f'Bearer {get_api_key()}'
    }
    
    # Step 1: Add magnet/torrent
    max_retries = 3
    retry_count = 0
    while True:
        try:
            if temp_file_path:
                # For torrent files, use the /torrents/addTorrent endpoint
                with open(temp_file_path, 'rb') as torrent_file:
                    torrent_response = requests.put(f"{API_BASE_URL}/torrents/addTorrent", headers=headers, data=torrent_file, timeout=60)
            else:
                # For magnet links, use the /torrents/addMagnet endpoint
                add_data = {'magnet': magnet_link}
                torrent_response = requests.post(f"{API_BASE_URL}/torrents/addMagnet", data=add_data, headers=headers, timeout=60)
                
            if torrent_response.status_code not in [200, 201]:
                logging.error(f"Failed to add torrent/magnet. Status code: {torrent_response.status_code}, Response: {torrent_response.text}")
                return None
            break
        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                raise
            time.sleep(5 * retry_count)
    
    torrent_id = torrent_response.json().get('id')
    if not torrent_id:
        logging.error("No torrent ID in response")
        return None
        
    # Step 2: Get torrent info
    retry_count = 0
    while True:
        try:
            info_response = requests.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
            if info_response.status_code != 200:
                logging.error(f"Failed to get torrent info. Status code: {info_response.status_code}")
                return None
            break
        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                raise
            time.sleep(5 * retry_count)

    torrent_info = info_response.json()

    # Add debug logging for all files in the torrent
    if 'files' in torrent_info:
        logging.debug(f"Files in torrent: {torrent_info['files']}")
    else:
        logging.warning("No files found in torrent info")

    # Step 3: Select files
    if 'files' not in torrent_info or not torrent_info['files']:
        logging.error("No files available in torrent")
        return None

    # Get list of video files
    video_files = []
    for file_info in torrent_info['files']:
        if is_video_file(file_info['path']) and not is_unwanted_file(file_info['path']):
            video_files.append(file_info)

    if not video_files:
        logging.error("No valid video files found in torrent")
        return None

    # Select all video files
    file_ids = [str(f['id']) for f in video_files]
    select_data = {'files': ','.join(file_ids)}
    
    retry_count = 0
    while True:
        try:
            select_response = requests.post(
                f"{API_BASE_URL}/torrents/selectFiles/{torrent_id}",
                data=select_data,
                headers=headers,
                timeout=60
            )
            if select_response.status_code != 204:
                logging.error(f"Failed to select files. Status code: {select_response.status_code}")
                return None
            break
        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                raise
            time.sleep(5 * retry_count)

    # Return torrent info with ID
    return {
        'torrent_id': torrent_id,
        'files': video_files
    }

@lru_cache(maxsize=1000)
def extract_hash_from_magnet(magnet_link):
    match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet_link)
    if match:
        return match.group(1).lower()
    else:
        return None

def namespace_to_dict(obj):
    if isinstance(obj, SimpleNamespace):
        return {k: namespace_to_dict(v) for k, v in vars(obj).items()}
    elif isinstance(obj, list):
        return [namespace_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: namespace_to_dict(v) for k, v in obj.items()}
    else:
        return obj

def is_valid_hash(hash_string):
    return bool(re.match(r'^[a-fA-F0-9]{40}$', hash_string))

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception))
)
@rate_limited_request
def is_cached_on_rd(hashes):
    if not hashes:
        logging.warning("Empty hashes input to is_cached_on_rd")
        return {}

    if isinstance(hashes, str):
        hashes = [hashes]
    elif isinstance(hashes, (tuple, list)):
        hashes = list(hashes)
    else:
        logging.error(f"Invalid input type for hashes: {type(hashes)}")
        return {}

    # Filter out any non-string elements, None values, and invalid hashes
    hashes = [h for h in hashes if isinstance(h, str) and h is not None and is_valid_hash(h)]

    if not hashes:
        logging.warning("No valid hashes after filtering")
        return {}

    # Return all hashes as cached since instant availability endpoint is no longer available
    cache_status = {hash_: True for hash_ in hashes}
    logging.debug(f"Cache status for hashes {hashes}: {cache_status}")
    return cache_status

def get_cached_files(hash_):
    url = f'{API_BASE_URL}/torrents/instantAvailability/{hash_}'
    response = get(url)
    if not response:
        return None

    response = namespace_to_dict(response)
    hash_data = response.get(hash_.lower(), [])

    if isinstance(hash_data, list) and hash_data:
        files = hash_data[0].get('rd', [])
    elif isinstance(hash_data, dict):
        files = hash_data.get('rd', [])
    else:
        return None

    if isinstance(files, list) and files:
        all_files = {k: v for file_dict in files for k, v in file_dict.items()}
    elif isinstance(files, dict):
        all_files = files
    else:
        return None

    return {'cached_files': [file_info['filename'] for file_info in all_files.values()]}

@rate_limited_request
def get(url):
    api_key = get_api_key()
    #print(api_key)
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Authorization': f'Bearer {api_key}'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(response)
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 5))
            logging.warning(f"Rate limited. Waiting for {retry_after} seconds.")
            time.sleep(retry_after)
            return get(url)  # Retry the request
        response.raise_for_status()  # Raise an exception for bad status codes
        return json.loads(json.dumps(response.json()), object_hook=lambda d: SimpleNamespace(**d))
    except requests.exceptions.Timeout:
        logging.error(f"Timeout error accessing {url}")
        return None
    except requests.exceptions.ConnectionError:
        logging.error(f"Connection error accessing {url}")
        return None
    except Exception as e:
        logging.error(f"[realdebrid] error: {e}")
        raise ProviderUnavailableError(f"[realdebrid] error: {e}") from e

def get_magnet_files(magnet_link):
    api_key = get_api_key()
    torrent_id = add_to_real_debrid(magnet_link)
    if not torrent_id:
        return None

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    try:
        info_response = requests.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
        info_response.raise_for_status()
        torrent_info = info_response.json()

        return [f['path'] for f in torrent_info['files'] if is_video_file(f['path']) and not is_unwanted_file(f['path'])]

    except requests.exceptions.RequestException as e:
        logging.error(f"Error getting magnet files from Real-Debrid: {str(e)}")
        raise ProviderUnavailableError(f"Error getting magnet files from Real-Debrid: {str(e)}") from e

    except Exception as e:
        logging.error(f"Unexpected error in get_magnet_files: {str(e)}")
        raise ProviderUnavailableError(f"Unexpected error in get_magnet_files: {str(e)}") from e

def process_hashes(hashes, batch_size=100):
    results = {}
    for i in range(0, len(hashes), batch_size):
        batch = hashes[i:i+batch_size]
        cache_status = is_cached_on_rd(batch)
        for hash_ in batch:
            if cache_status.get(hash_, False):
                results[hash_] = get_cached_files(hash_)
            else:
                results[hash_] = None
    return results

@timed_lru_cache(seconds=10)  # Reduce cache time from 30 to 10 seconds
def get_active_downloads(check=False):
    api_key = get_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    try:
        # Get list of all torrents
        response = requests.get(f"{API_BASE_URL}/torrents", headers=headers, timeout=60)
        response.raise_for_status()
        torrents = response.json()
        
        # Count torrents that are actually active (downloading, queued, or in magnet conversion)
        active_statuses = ['downloading', 'queued', 'magnet_conversion']
        active_count = len([t for t in torrents if t.get('status') in active_statuses])
        
        # Real-Debrid's default limit is 25, we use 75% of that to be safe
        limit = round(25 * 0.75)
        
        logging.debug(f"Real-Debrid active downloads (from torrent list): {active_count}")
        
        if check:
            return active_count < limit
        else:
            return active_count, limit
    except Exception as e:
        logging.error(f"An error occurred while fetching active downloads: {e}")
        if check:
            return True  # Allow downloads when we can't check the count
        else:
            return 0, 25  # Return 0 active downloads and default limit

@timed_lru_cache(seconds=300)  # Cache for 5 minutes
def get_user_traffic():
    api_key = get_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    try:
        response = requests.get(f"{API_BASE_URL}/traffic/details", headers=headers, timeout=60)
        response.raise_for_status()
        traffic_info = response.json()
        
        logging.debug(f"Real-Debrid traffic response: {traffic_info}")
        return traffic_info
    except Exception as e:
        logging.error(f"An error occurred while fetching traffic info: {e}")
        raise ProviderUnavailableError(f"An error occurred while fetching traffic info: {e}") from e

def check_daily_usage():
    server_time, _ = get_server_time()
    if not server_time:
        logging.error("Failed to get server time")
        return {'used': 0, 'limit': 2000}  # Default values

    current_date_str = server_time.strftime("%Y-%m-%d")
    
    traffic_info = get_user_traffic()
    if not traffic_info:
        logging.error("Failed to get traffic information")
        return {'used': 0, 'limit': 2000}  # Default values

    daily_usage = traffic_info.get(current_date_str, {}).get('bytes', 0)
    daily_usage_gb = daily_usage / (1024 * 1024 * 1024)
    is_over_limit = daily_usage_gb > 2000

    return {
        'used': round(daily_usage_gb, 2),
        'limit': 2000,
        'is_over_limit': is_over_limit
    }

@timed_lru_cache(seconds=300)
def get_server_time():
    api_key = get_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    try:
        response = requests.get(f"{API_BASE_URL}/time", headers=headers, timeout=60)
        response.raise_for_status()
        server_time = datetime.strptime(response.text.strip(), "%Y-%m-%d %H:%M:%S")
        return server_time, datetime.now()
    except Exception as e:
        logging.error(f"Error getting server time: {str(e)}")
        raise ProviderUnavailableError(f"Error getting server time: {str(e)}") from e

def file_matches_item(filename, item):
    filename = filename.lower()
    
    if 'sample' in filename:
        return False

    quality = item['version'].lower()

    if item['type'] == 'movie':
        title = item['title'].lower().replace(' ', '.')
        year = item['year']
        movie_pattern = rf"(?i){re.escape(title)}.*{year}"
        return bool(re.search(movie_pattern, filename))
    elif item['type'] == 'episode':
        season_number = int(item['season_number'])
        episode_number = int(item['episode_number'])

        # Regular episode matching
        single_ep_pattern = rf"(?i)s0*{season_number}(?:[.-]?|\s*)e0*{episode_number}(?!\d)"
        range_pattern = rf"(?i)s0*{season_number}(?:[.-]?|\s*)e0*(\d+)(?:-|-)0*(\d+)"

        single_match = re.search(single_ep_pattern, filename)
        range_match = re.search(range_pattern, filename)

        if single_match:
            return True
        elif range_match:
            start_ep, end_ep = map(int, range_match.groups())
            return start_ep <= episode_number <= end_ep

        # Anime-specific matching
        if 'anime' in item.get('genres', []):
            absolute_pattern = rf"(?i)(?:[-_.\s]|^)(?:e0*)?{episode_number}(?:[-_.\s]|$)"
            return bool(re.search(absolute_pattern, filename))

    return False

def get_torrent_info(torrent_id):
    api_key = get_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    try:
        info_response = requests.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
        info_response.raise_for_status()
        torrent_info = info_response.json()

        status = torrent_info.get('status', 'unknown')
        logging.debug(f"Torrent {torrent_id} status: {status}")
        
        # Map Real-Debrid statuses to common statuses
        rd_status_map = {
            'magnet_error': TorrentStatus.ERROR.value,
            'error': TorrentStatus.ERROR.value,
            'virus': TorrentStatus.ERROR.value,
            'dead': TorrentStatus.ERROR.value,
            'downloaded': TorrentStatus.DOWNLOADED.value,
            'downloading': TorrentStatus.DOWNLOADING.value,
            'queued': TorrentStatus.QUEUED.value,
            'waiting_files_selection': TorrentStatus.QUEUED.value,
            'magnet_conversion': TorrentStatus.QUEUED.value,
            'compressing': TorrentStatus.QUEUED.value,
            'uploading': TorrentStatus.QUEUED.value
        }

        common_status = rd_status_map.get(status, TorrentStatus.UNKNOWN.value)
        status_flags = get_status_flags(common_status)

        if status_flags['is_error']:
            logging.error(f"Torrent {torrent_id} in error state: {status}")
        elif status_flags['is_cached']:
            logging.info(f"Torrent {torrent_id} is cached")
        elif status_flags['is_queued']:
            logging.info(f"Torrent {torrent_id} is queued/downloading")

        return {
            'id': torrent_id,
            'status': common_status,
            'seeders': torrent_info.get('seeders', 0),
            'progress': torrent_info.get('progress', 0),
            'files': torrent_info.get('files', []),
            **status_flags
        }
    except requests.exceptions.RequestException as e:
        logging.error(f"Error getting torrent info from Real-Debrid: {str(e)}")
        raise ProviderUnavailableError(f"Error getting torrent info from Real-Debrid: {str(e)}") from e
    except Exception as e:
        logging.error(f"Unexpected error in get_torrent_info: {str(e)}")
        raise ProviderUnavailableError(f"Unexpected error in get_torrent_info: {str(e)}") from e

class RealDebridProvider(DebridProvider):
    """RealDebrid implementation of the DebridProvider interface"""
    
    API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
    MAX_DOWNLOADS = 25  # RealDebrid typically allows 25 simultaneous downloads
    
    def __init__(self):
        self.rate_limiter = RateLimiter(calls_per_second=0.5)
        self.api_key = self.get_api_key()

    def get_api_key(self):
        api_key = get_setting('Debrid Provider', 'api_key')
        if not api_key:
            raise ValueError("Debrid Provider API key not found in settings")
        if api_key == 'demo_key':
            logging.warning("Running in demo mode with demo API key")
            return api_key
        return api_key

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Simplified API request method similar to Torbox's implementation"""
        url = f"{self.API_BASE_URL}/{endpoint}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        if 'headers' in kwargs:
            kwargs['headers'].update(headers)
        else:
            kwargs['headers'] = headers

        try:
            if method.lower() == 'get':
                response = requests.get(url, **kwargs)
            elif method.lower() == 'post':
                response = requests.post(url, **kwargs)
            elif method.lower() == 'delete':
                response = requests.delete(url, **kwargs)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if response.status_code == 429:
                raise RealDebridTooManyDownloadsError("Too many active downloads")
            elif response.status_code == 503:
                raise RealDebridUnavailableError("Service temporarily unavailable")
            elif response.status_code >= 400:
                raise RealDebridUnavailableError(f"API error: {response.status_code} - {response.text}")

            # For DELETE requests or empty responses, return empty dict
            if method.lower() == 'delete' or not response.text:
                return {}
                
            return response.json()
            
        except requests.exceptions.RequestException as e:
            raise RealDebridUnavailableError(f"Request failed: {str(e)}")

    @property
    def supports_direct_cache_check(self) -> bool:
        """RealDebrid doesn't support direct cache checking"""
        return False

    def add_torrent(self, magnet_link, temp_file_path=None):
        return add_to_real_debrid(magnet_link, temp_file_path)
        
    def is_cached(self, hashes: Union[str, List[str]]) -> Dict:
        """Check if hash(es) are cached on Real-Debrid
        
        Note: Real-Debrid doesn't support direct cache checking.
        We need to add the torrent and check its status.
        """
        if isinstance(hashes, str):
            hashes = [hashes]
        
        result = {}
        for hash_ in hashes:
            try:
                logging.info(f"Checking cache status for hash: {hash_}")
                # Create a magnet link from the hash
                magnet = f"magnet:?xt=urn:btih:{hash_}"
                
                # Add the torrent to check its status
                logging.debug(f"Adding torrent with magnet: {magnet}")
                add_response = self.add_torrent(magnet)
                logging.debug(f"Add torrent response: {json.dumps(add_response, indent=2) if add_response else None}")
                
                if not add_response or not isinstance(add_response, dict):
                    logging.warning(f"Invalid add_response: {add_response}")
                    result[hash_] = {'cached': False}
                    continue
                
                # Check for either 'id' or 'torrent_id' in response
                torrent_id = add_response.get('id') or add_response.get('torrent_id')
                if not torrent_id:
                    logging.warning("No torrent_id or id found in add_response")
                    result[hash_] = {'cached': False}
                    continue
                
                # Get torrent info to check status
                logging.debug(f"Getting torrent info for ID: {torrent_id}")
                torrent_info = self.get_torrent_info(torrent_id)
                logging.debug(f"Torrent info response: {json.dumps(torrent_info, indent=2) if torrent_info else None}")
                
                if not torrent_info:
                    logging.warning(f"No torrent info returned for ID: {torrent_id}")
                    result[hash_] = {'cached': False}
                    continue
                
                # If status is 'downloaded', it's cached
                status = torrent_info.get('status', '').lower()
                is_cached = status == 'downloaded'
                
                # Include file information in the response
                result[hash_] = {
                    'cached': is_cached,
                    'files': add_response.get('files', []),
                    'torrent_id': torrent_id
                }
                
                logging.info(f"Hash {hash_} status: {status} (cached: {is_cached})")
                if not is_cached:
                    logging.info(f"Torrent not cached. Full status info: {torrent_info.get('status_detail', 'No status detail available')}")
                
            except Exception as e:
                logging.error(f"Error checking cache status for hash {hash_}: {str(e)}")
                result[hash_] = {'cached': False}
        
        return result

    def list_torrents(self) -> List[Dict]:
        """Get list of torrents from Real-Debrid"""
        api_key = self.get_api_key()
        headers = {
            'Authorization': f'Bearer {api_key}'
        }
        try:
            response = requests.get(f"{self.API_BASE_URL}/torrents", headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Error listing torrents: {str(e)}")
            raise ProviderUnavailableError(f"Error listing torrents: {str(e)}") from e

    def get_cached_files(self, hash_):
        return get_cached_files(hash_)
        
    def get_active_downloads(self, check=False):
        return get_active_downloads(check)
        
    def get_user_traffic(self):
        return get_user_traffic()

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict]:
        """Get torrent info from Real-Debrid"""
        try:
            response = self._make_request('get', f'torrents/info/{torrent_id}')
            if not response:
                return None

            torrent_info = namespace_to_dict(response)
            status = torrent_info.get('status', 'unknown')
            
            # Map Real-Debrid statuses to common statuses
            rd_status_map = {
                'magnet_error': TorrentStatus.ERROR.value,
                'error': TorrentStatus.ERROR.value,
                'virus': TorrentStatus.ERROR.value,
                'dead': TorrentStatus.ERROR.value,
                'downloaded': TorrentStatus.DOWNLOADED.value,
                'downloading': TorrentStatus.DOWNLOADING.value,
                'queued': TorrentStatus.QUEUED.value,
                'waiting_files_selection': TorrentStatus.QUEUED.value,
                'magnet_conversion': TorrentStatus.QUEUED.value,
                'compressing': TorrentStatus.QUEUED.value,
                'uploading': TorrentStatus.QUEUED.value
            }

            common_status = rd_status_map.get(status, TorrentStatus.UNKNOWN.value)
            status_flags = get_status_flags(common_status)

            # Add status flags to response
            torrent_info.update(status_flags)
            torrent_info['status'] = common_status
            
            if status_flags['is_error']:
                logging.error(f"Torrent {torrent_id} in error state: {status}")
            elif status_flags['is_cached']:
                logging.info(f"Torrent {torrent_id} is cached")
                
            return torrent_info

        except Exception as e:
            logging.error(f"Unexpected error in get_torrent_info: {str(e)}")
            raise ProviderUnavailableError(f"Unexpected error in get_torrent_info: {str(e)}") from e

    def get_torrent_files(self, hash_value: str) -> List[Dict]:
        info = self.get_torrent_info(hash_value)
        if info:
            return info.get('files', [])
        return []

    def remove_torrent(self, torrent_id: str) -> bool:
        """Remove a torrent from Real-Debrid"""
        try:
            self._make_request('delete', f'torrents/delete/{torrent_id}')
            return True
        except Exception as e:
            logging.error(f"Error removing torrent {torrent_id}: {e}")
            raise ProviderUnavailableError(f"Error removing torrent {torrent_id}: {e}") from e

    def download_and_extract_hash(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            response = requests.get(url)
            if response.status_code != 200:
                return None, None

            # Create a temporary file to store the torrent data
            with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as temp_file:
                temp_file.write(response.content)
                temp_file_path = temp_file.name

            # Extract hash from the torrent file
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
            raise ProviderUnavailableError(f"Error downloading torrent file: {str(e)}") from e

def list_active_torrents():
    api_key = get_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    try:
        response = requests.get(f"{API_BASE_URL}/torrents", headers=headers, timeout=60)
        response.raise_for_status()
        torrents = response.json()
        active_torrents = [t for t in torrents if t.get('status') in ['downloading', 'queued', 'magnet_conversion']]
        logging.info(f"Active torrents: {json.dumps(active_torrents, indent=2)}")
        return active_torrents
    except Exception as e:
        logging.error(f"An error occurred while fetching active torrents: {e}")
        raise ProviderUnavailableError(f"An error occurred while fetching active torrents: {e}") from e

def cleanup_stale_torrents():
    api_key = get_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    try:
        response = requests.get(f"{API_BASE_URL}/torrents", headers=headers, timeout=60)
        response.raise_for_status()
        torrents = response.json()
        
        # Find torrents that are stuck in downloading/queued state
        stale_torrents = [t for t in torrents if t.get('status') in ['downloading', 'queued', 'magnet_conversion']]
        
        if stale_torrents:
            logging.info(f"Found {len(stale_torrents)} stale torrents")
            for torrent in stale_torrents:
                try:
                    torrent_id = torrent.get('id')
                    if torrent_id:
                        delete_url = f"{API_BASE_URL}/torrents/delete/{torrent_id}"
                        delete_response = requests.delete(delete_url, headers=headers)
                        delete_response.raise_for_status()
                        logging.info(f"Successfully deleted stale torrent {torrent_id}")
                except Exception as e:
                    logging.error(f"Failed to delete torrent {torrent.get('id')}: {e}")
        else:
            logging.info("No stale torrents found")
            
        return True
    except Exception as e:
        logging.error(f"An error occurred while cleaning up stale torrents: {e}")
        raise ProviderUnavailableError(f"An error occurred while cleaning up stale torrents: {e}") from e