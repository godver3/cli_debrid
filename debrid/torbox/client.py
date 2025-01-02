"""TorBox API client implementation"""

import logging
from typing import Dict, List, Optional, Union, Tuple
from datetime import datetime, timedelta
import tempfile
import os
import time
from urllib.parse import unquote

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

    def _load_api_key(self) -> str:
        """Load API key from settings"""
        try:
            from .api import get_api_key
            return get_api_key()
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to load API key: {str(e)}")

    @property
    def supports_direct_cache_check(self) -> bool:
        """Check if provider supports direct cache checking"""
        return True

    @property
    def supports_bulk_cache_checking(self) -> bool:
        """Check if provider supports checking multiple hashes in a single API call"""
        return True

    @property
    def supports_uncached(self) -> bool:
        """Check if provider supports downloading uncached torrents"""
        return False

    def is_cached(self, magnet_links: Union[str, List[str]]) -> Union[bool, Dict[str, bool]]:
        """
        Check if one or more magnet links are cached on TorBox.
        If a single magnet link is provided, returns a boolean.
        If a list of magnet links is provided, returns a dict mapping hashes to booleans.
        """
        # If single magnet link, convert to list
        if isinstance(magnet_links, str):
            magnet_links = [magnet_links]
            return_single = True
        else:
            return_single = False

        # Initialize results
        results = {}
        hashes = []
        hash_to_magnet = {}
        
        # Process each magnet link
        for magnet_link in magnet_links:
            # For hashes, convert to magnet link format
            if len(magnet_link) == 40 and all(c in '0123456789abcdefABCDEF' for c in magnet_link):
                magnet_link = f"magnet:?xt=urn:btih:{magnet_link}"
            
            # Extract hash at the beginning to ensure it's always available
            hash_value = extract_hash_from_magnet(magnet_link)
            if not hash_value:
                logging.error(f"Could not extract hash from magnet link: {magnet_link}")
                results[magnet_link if return_single else magnet_link] = False
                continue
                
            hashes.append(hash_value)
            hash_to_magnet[hash_value] = magnet_link

        try:
            # Use TorBox's cache check endpoint
            # Convert hashes list to comma-separated query params
            query_params = {
                'hash': ','.join(hashes),
                'format': 'object',  # Use object format for easier processing
                'list_files': 'false'  # We don't need file listings
            }
            logging.debug(f"Checking cache status for hashes: {hashes}")
            response = make_request('GET', '/api/torrents/checkcached', self.api_key, params=query_params)
            
            logging.info(f"Raw TorBox cache check response: {response}")
            
            if response is None or not isinstance(response, dict):
                # Handle case where response is None or invalid
                logging.warning("TorBox cache check returned invalid response")
                for hash_value in hashes:
                    if hash_value not in results:
                        magnet_link = hash_to_magnet[hash_value]
                        results[magnet_link if return_single else hash_value] = False
                return results[magnet_links[0]] if return_single else results

            # Extract the actual cache data from the response
            cache_data = response.get('data', {}) if isinstance(response, dict) else {}
            
            # Process results - response should be a dict in the data field mapping hashes to info
            for hash_value in hashes:
                if hash_value not in results:
                    magnet_link = hash_to_magnet[hash_value]
                    # Check if the hash exists in the cache data
                    is_cached = hash_value in cache_data
                    logging.info(f"Interpreted cache status for {hash_value}: {is_cached}")
                    results[magnet_link if return_single else hash_value] = is_cached

        except TorBoxAPIError as e:
            logging.error(f"Failed to check cache status: {str(e)}")
            # Return False for all unchecked magnets
            for hash_value in hashes:
                if hash_value not in results:
                    magnet_link = hash_to_magnet[hash_value]
                    results[magnet_link if return_single else hash_value] = False

        return results[magnet_links[0]] if return_single else results

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
