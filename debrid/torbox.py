import logging
from typing import Dict, List, Optional, Tuple, Union
from .base import DebridProvider, ProviderUnavailableError, TooManyDownloadsError
from settings import get_setting
from api_tracker import api
import os
import tempfile
import bencodepy
import hashlib
import time
from functools import lru_cache

class TorboxUnavailableError(ProviderUnavailableError):
    pass

class TorboxTooManyDownloadsError(TooManyDownloadsError):
    pass

class TorboxProvider(DebridProvider):
    """Torbox implementation of the DebridProvider interface"""
    
    API_BASE_URL = "https://api.torbox.app/v1"  # Base URL without specific endpoints
    MAX_DOWNLOADS = 5
    
    def __init__(self):
        self.api_key = self.get_api_key()

    def get_api_key(self):
        return get_setting('Debrid Provider', 'api_key')

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
            
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Error making request to Torbox API: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logging.error(f"Response status: {e.response.status_code}")
                logging.error(f"Response content: {e.response.text}")
            raise TorboxUnavailableError(f"Failed to access Torbox API: {str(e)}")

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

    def get_cached_files(self, hash_: str) -> List[Dict]:
        """Get available cached files for a hash"""
        try:
            params = {'hash': hash_}
            response = self._make_request('GET', 'api/torrents/requestdl', params=params)
            return response.get('files', [])
        except Exception as e:
            logging.error(f"Error getting cached files from Torbox: {str(e)}")
            return []

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

    def get_user_traffic(self) -> Dict:
        """Get user traffic/usage information"""
        try:
            response = self._make_request('GET', 'api/user')
            return {
                'downloaded': response.get('downloaded', 0),
                'uploaded': response.get('uploaded', 0)
            }
        except Exception as e:
            logging.error(f"Error getting user traffic from Torbox: {str(e)}")
            return {'downloaded': 0, 'uploaded': 0}

    def get_torrent_info(self, hash_value: str) -> Optional[Dict]:
        """Get information about a specific torrent by its hash"""
        try:
            response = self._make_request('GET', 'api/torrents/mylist')
            for torrent in response.get('torrents', []):
                if torrent.get('hash') == hash_value:
                    return torrent
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
