"""TorBox API client implementation"""

import logging
from typing import Dict, List, Optional, Union, Tuple
from datetime import datetime, timedelta
import tempfile
import os
import time
from urllib.parse import unquote
import hashlib
import bencodepy

from ..base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError
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
from .exceptions import TorBoxAPIError, TorBoxAuthError, TorBoxPlanError, TorBoxLimitError

class TorBoxProvider(DebridProvider):
    """TorBox implementation of the DebridProvider interface"""
    
    API_BASE_URL = "https://api.torbox.app/v1"
    MAX_DOWNLOADS = 25  # This may vary based on plan
    
    def __init__(self):
        super().__init__()
        
    def _load_api_key(self) -> str:
        """Load API key from settings"""
        try:
            from .api import get_api_key
            return get_api_key()
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to load API key: {str(e)}")
            
    def is_cached(self, magnet_links: Union[str, List[str]], temp_file_path: Optional[str] = None) -> Union[bool, Dict[str, Optional[bool]]]:
        """
        Check if one or more magnet links or torrent files are cached on TorBox.
        If a single input is provided, returns a boolean or None (for error).
        If a list of inputs is provided, returns a dict mapping hashes to booleans or None (for error).
        
        Returns:
            - True: Torrent is cached
            - False: Torrent is not cached
            - None: Error occurred during check (invalid magnet, no video files, etc)
        """
        logging.debug(f"Starting cache check for {len([magnet_links] if isinstance(magnet_links, str) else magnet_links)} magnet(s)")
        
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
            logging.debug(f"Processing magnet/URL: {magnet_link[:60]}...")
            
            # Extract hash
            hash_value = None
            if magnet_link.startswith('magnet:'):
                hash_value = extract_hash_from_magnet(magnet_link)
            elif temp_file_path:
                try:
                    with open(temp_file_path, 'rb') as f:
                        torrent_data = bencodepy.decode(f.read())
                        info = torrent_data[b'info']
                        hash_value = hashlib.sha1(bencodepy.encode(info)).hexdigest()
                except Exception as e:
                    logging.error(f"Could not extract hash from torrent file: {str(e)}")
                    results[magnet_link] = None
                    continue
            
            if not hash_value:
                logging.error(f"Could not extract hash from input: {magnet_link}")
                results[magnet_link] = None
                continue
                
            logging.debug(f"Extracted hash: {hash_value}")
            
            try:
                # Check cache status
                response = make_request('GET', f'/torrents/{hash_value}/status', self.api_key)
                if not response:
                    logging.error("No response from TorBox API")
                    results[hash_value] = None
                    continue
                    
                status = response.get('status', '')
                is_cached = status == 'completed'
                
                # Store torrent ID if cached
                if is_cached:
                    self._cached_torrent_ids[hash_value] = hash_value  # TorBox uses hash as ID
                    logging.debug(f"Stored cached torrent hash {hash_value}")
                
                results[hash_value] = is_cached
                
            except Exception as e:
                logging.error(f"Error checking cache: {str(e)}")
                results[hash_value] = None
                
        logging.debug(f"Cache check complete. Results: {results}")
        # Return single result if input was single magnet, otherwise return dict
        return results[list(results.keys())[0]] if return_single else results

    def add_torrent(self, magnet_link: str) -> Optional[str]:
        """
        Add a torrent to TorBox
        
        Args:
            magnet_link: Magnet link or hash
            
        Returns:
            Torrent ID if successful, None if failed
            For already queued torrents, returns the hash as the ID
        """
        try:
            # Send as URL-encoded form data
            form_data = {
                'magnet': magnet_link,
                'seed': '1',  # Auto seed
                'as_queued': 'true'  # Queue immediately if possible
            }
            
            # Set the content type header
            kwargs = {'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}
            
            response = make_request('POST', '/api/torrents/createtorrent', self.api_key, data=form_data, **kwargs)
            if response is None:
                logging.error("Failed to add torrent: API request failed after retries")
                return None
                
            # Log the full response for debugging
            logging.debug(f"Add torrent response: {response}")
            
            # Check for DIFF_ISSUE error (torrent already queued)
            if isinstance(response, dict) and response.get('error') == 'DIFF_ISSUE':
                # Extract hash from magnet link to use as ID
                hash_value = extract_hash_from_magnet(magnet_link)
                if hash_value:
                    logging.info(f"Torrent already queued, using hash as ID: {hash_value}")
                    return hash_value
                return None
            
            # The response is the data object directly
            queued_id = response.get('queued_id')
            if queued_id:
                logging.info(f"Successfully queued torrent with ID: {queued_id}")
                return str(queued_id)
            else:
                logging.error("Response missing queued_id")
            
            return None
        except TorBoxAPIError as e:
            if 'DUPLICATE_ITEM' in str(e):
                # Extract hash from magnet link to use as ID
                hash_value = extract_hash_from_magnet(magnet_link)
                if hash_value:
                    logging.info(f"Duplicate torrent, using hash as ID: {hash_value}")
                    return hash_value
            logging.error(f"Failed to add torrent: {str(e)}")
            return None

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict]:
        """
        Get information about a torrent
        
        Args:
            torrent_id: TorBox torrent ID
            
        Returns:
            Torrent information dict or None if not found
        """
        try:
            response = make_request('GET', f'/api/torrents/createtorrent/{torrent_id}', self.api_key)
            if response is not None:
                logging.debug(f"Got torrent info response: {response}")
                return response  # Return the response directly as it contains the torrent info
            return None
        except TorBoxAPIError:
            logging.error(f"Failed to get info for torrent {torrent_id}")
            return None

    def get_download_link(self, torrent_id: str, file_id: Optional[str] = None) -> Optional[str]:
        """
        Get a download link for a torrent file
        
        Args:
            torrent_id: TorBox torrent ID
            file_id: Optional specific file ID to download
            
        Returns:
            Download URL if successful, None otherwise
        """
        try:
            response = make_request('GET', f'/torrents/requestdl?id={torrent_id}', self.api_key)
            return response.get('download_url')
        except TorBoxAPIError:
            return None

    def delete_torrent(self, torrent_id: str) -> bool:
        """
        Delete a torrent from TorBox
        
        Args:
            torrent_id: TorBox torrent ID
            
        Returns:
            True if successful, False otherwise
        """
        try:
            make_request('POST', '/torrents/controltorrent', self.api_key, data={
                'id': torrent_id,
                'action': 'delete'
            })
            return True
        except TorBoxAPIError:
            return False

    def list_torrents(self) -> List[Dict]:
        """
        List all torrents in TorBox account
        
        Returns:
            List of torrent information dictionaries
        """
        try:
            return make_request('GET', '/torrents/mylist', self.api_key) or []
        except TorBoxAPIError:
            return []

    def get_active_downloads(self) -> Tuple[int, int]:
        """Get number of active downloads and concurrent download limit"""
        try:
            torrents = self.list_torrents()
            active = len([t for t in torrents if t.get('status') == 'downloading'])
            return active, self.MAX_DOWNLOADS
        except TorBoxAPIError:
            return 0, self.MAX_DOWNLOADS

    def get_user_traffic(self) -> Dict:
        """Get user traffic/usage information"""
        try:
            response = make_request('GET', '/user/traffic', self.api_key)
            return {
                'used': response.get('used', 0),
                'limit': response.get('limit', 0),
                'reset_time': response.get('reset_time')
            }
        except TorBoxAPIError:
            return {'used': 0, 'limit': 0, 'reset_time': None}

    def remove_torrent(self, torrent_id: str) -> None:
        """Remove a torrent from TorBox"""
        try:
            make_request('POST', '/torrents/controltorrent', self.api_key, data={
                'id': torrent_id,
                'action': 'delete'
            })
        except TorBoxAPIError as e:
            logging.error(f"Failed to remove torrent {torrent_id}: {str(e)}")

    def _map_status(self, torbox_status: str) -> TorrentStatus:
        """Map TorBox status to internal status enum"""
        status_map = {
            'downloading': TorrentStatus.DOWNLOADING,
            'downloaded': TorrentStatus.FINISHED,
            'error': TorrentStatus.ERROR,
            'magnet_error': TorrentStatus.ERROR,
            'magnet_conversion': TorrentStatus.INITIALIZING,
            'waiting_files_selection': TorrentStatus.INITIALIZING,
            'queued': TorrentStatus.QUEUED,
            'virus': TorrentStatus.ERROR,
        }
        return status_map.get(torbox_status, TorrentStatus.UNKNOWN)
