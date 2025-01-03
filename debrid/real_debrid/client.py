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
from not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls

class RealDebridProvider(DebridProvider):
    """Real-Debrid implementation of the DebridProvider interface"""
    
    API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
    MAX_DOWNLOADS = 25

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
        return False

    @property
    def supports_bulk_cache_checking(self) -> bool:
        """Check if provider supports checking multiple hashes in a single API call"""
        return False

    @property
    def supports_uncached(self) -> bool:
        """Check if provider supports downloading uncached torrents"""
        return True

    def is_cached(self, magnet_links: Union[str, List[str]]) -> Union[bool, Dict[str, bool]]:
        """
        Check if one or more magnet links are cached on Real-Debrid.
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
        
        # Process each magnet link
        for magnet_link in magnet_links:
            # For hashes, convert to magnet link format
            if len(magnet_link) == 40 and all(c in '0123456789abcdefABCDEF' for c in magnet_link):
                magnet_link = f"magnet:?xt=urn:btih:{magnet_link}"
            
            # Extract hash at the beginning to ensure it's always available
            hash_value = extract_hash_from_magnet(magnet_link)
            if not hash_value:
                logging.error(f"Could not extract hash from magnet link: {magnet_link}")
                results[magnet_link] = False
                continue
                
            torrent_id = None
            try:
                # Add the magnet to RD
                torrent_id = self.add_torrent(magnet_link)
                if not torrent_id:
                    # If add_torrent returns None, the torrent might already be added
                    # Try to get the hash and look up existing torrent
                    if hash_value:
                        # Search for existing torrent with this hash
                        torrents = make_request('GET', '/torrents', self.api_key) or []
                        for torrent in torrents:
                            if torrent.get('hash', '').lower() == hash_value.lower():
                                torrent_id = torrent['id']
                                break
                    
                    if not torrent_id:
                        results[hash_value] = False
                        continue
                    
                # Get torrent info
                info = self.get_torrent_info(torrent_id)
                if not info:
                    results[hash_value] = False
                    continue
                    
                # Check if it's already cached
                status = info.get('status', '')
                
                # If there are no video files, return None to indicate error
                if not any(is_video_file(f.get('path', '') or f.get('name', '')) for f in info.get('files', [])):
                    logging.error("No video files found in torrent")
                    self.update_status(torrent_id, TorrentStatus.ERROR)
                    
                    # Add to not wanted list - we only get magnet links or hashes here
                    try:
                        hash_value = extract_hash_from_magnet(magnet_link)
                        add_to_not_wanted(hash_value)
                        logging.info(f"Added magnet hash {hash_value} to not wanted list")
                    except Exception as e:
                        logging.error(f"Failed to add to not wanted list: {str(e)}")
                    
                    results[hash_value] = None
                    continue
                
                is_cached = status == 'downloaded'
                
                # Update status tracking
                self.update_status(
                    torrent_id,
                    TorrentStatus.CACHED if is_cached else TorrentStatus.NOT_CACHED
                )
                
                results[hash_value] = is_cached
                
            except Exception as e:
                logging.error(f"Error checking cache for magnet {magnet_link}: {str(e)}")
                if torrent_id:
                    self.update_status(torrent_id, TorrentStatus.ERROR)
                results[hash_value] = False
                
            finally:
                # Always clean up the torrent if we added it
                if torrent_id:
                    try:
                        self.remove_torrent(torrent_id)
                        logging.info(f"Successfully removed torrent {torrent_id} after cache check")
                    except Exception as e:
                        logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
                        self.update_status(torrent_id, TorrentStatus.CLEANUP_NEEDED)

        # Return single boolean if input was single magnet, otherwise return dict
        return results[list(results.keys())[0]] if return_single else results

    def add_torrent(self, magnet_link: str, temp_file_path: Optional[str] = None) -> Optional[str]:
        """Add a torrent to Real-Debrid"""
        try:
            # URL decode the magnet link if needed
            if '%' in magnet_link:
                magnet_link = unquote(magnet_link)

            # Check if torrent already exists
            hash_value = extract_hash_from_magnet(magnet_link)
            if hash_value:
                torrents = make_request('GET', '/torrents', self.api_key) or []
                for torrent in torrents:
                    if torrent.get('hash', '').lower() == hash_value.lower():
                        logging.info(f"Torrent already exists with ID {torrent['id']}")
                        return torrent['id']

            # Get available hosts
            hosts = self.get_available_hosts()
            host = hosts[0] if hosts else None
            
            # Handle both magnet links and torrent files
            if magnet_link.startswith('magnet:'):
                data = {'magnet': magnet_link}
                if host:
                    data['host'] = host
                result = make_request('POST', '/torrents/addMagnet', self.api_key, data=data)
            else:
                # If we have a torrent file path, use it directly
                if temp_file_path:
                    file_path = temp_file_path
                else:
                    # Convert torrent URL to magnet
                    magnet = torrent_to_magnet(magnet_link)
                    if not magnet:
                        return None
                    data = {'magnet': magnet}
                    if host:
                        data['host'] = host
                    result = make_request('POST', '/torrents/addMagnet', self.api_key, data=data)
                    
            if not result or 'id' not in result:
                logging.error(f"Failed to add torrent - response: {result}")
                return None
                
            torrent_id = result['id']
            logging.debug(f"Initial add response: {result}")
            
            # Wait for files to be available
            for attempt in range(10):  # Try for up to 10 seconds
                info = self.get_torrent_info(torrent_id)
                if not info:
                    logging.error("Failed to get torrent info")
                    return None
                    
                logging.debug(f"Torrent info (attempt {attempt + 1}): {info}")
                status = info.get('status', '')
                
                # Early exit for invalid magnets
                if status == 'magnet_error' and info.get('filename') == 'Invalid Magnet':
                    logging.error("Invalid magnet link detected")
                    self.remove_torrent(torrent_id)
                    return None
                
                if status == 'magnet_conversion':
                    logging.debug("Waiting for magnet conversion...")
                    time.sleep(1)
                    continue
                    
                if status == 'waiting_files_selection':
                    # Select only video files
                    files = info.get('files', [])
                    if files:
                        # Get list of file IDs for video files
                        video_file_ids = []
                        for i, file_info in enumerate(files, start=1):
                            filename = file_info.get('path', '') or file_info.get('name', '')
                            if filename and is_video_file(filename) and not is_unwanted_file(filename):
                                video_file_ids.append(str(i))
                                logging.debug(f"Found video file: {filename}")
                                
                        if video_file_ids:
                            data = {'files': ','.join(video_file_ids)}
                            make_request('POST', f'/torrents/selectFiles/{torrent_id}', self.api_key, data=data)
                            logging.info(f"Selected video files: {video_file_ids}")
                            break
                        else:
                            logging.error("No video files found in torrent")
                            self.remove_torrent(torrent_id)
                            return None
                    else:
                        logging.error("No files available in torrent info")
                        time.sleep(1)
                        continue
                        
                elif status in ['downloaded', 'downloading']:
                    logging.debug(f"Torrent is in {status} state")
                    break
                else:
                    logging.debug(f"Unknown status: {status}, waiting...")
                    time.sleep(1)
                    
            self.update_status(torrent_id, TorrentStatus.ADDED)
            return torrent_id
            
        except Exception as e:
            logging.error(f"Error adding torrent: {str(e)}")
            return None

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
            active_data = make_request('GET', '/torrents/activeCount', self.api_key)
            #logging.info(f"Active torrents data: {active_data}")
            active_count = active_data.get('nb', 0)
            max_downloads = active_data.get('limit', self.MAX_DOWNLOADS)

            max_downloads = round(max_downloads * 0.75)
            
            if active_count >= max_downloads:
                raise TooManyDownloadsError(
                    f"Too many active downloads ({active_count}/{max_downloads})"
                )
                
            return active_count, max_downloads
            
        except TooManyDownloadsError:
            raise
        except Exception as e:
            logging.error(f"Error getting active downloads: {str(e)}")
            raise ProviderUnavailableError(f"Failed to get active downloads: {str(e)}")

    def get_user_traffic(self) -> Dict:
        """Get user traffic information"""
        try:
            traffic_info = make_request('GET', '/traffic/details', self.api_key)
            overall_traffic = make_request('GET', '/traffic', self.api_key)
            #logging.info(f"Overall traffic: {overall_traffic}")
            #logging.info(f"Raw traffic info received: {traffic_info}")
            
            if not traffic_info:
                logging.error("Failed to get traffic information")
                return {'downloaded': 0, 'limit': None}

            try:
                # Get today in UTC since Real-Debrid uses UTC dates
                today_utc = datetime.utcnow().strftime("%Y-%m-%d")
                #logging.info(f"Looking for traffic data for UTC date: {today_utc}")
                
                # Get today's traffic
                daily_traffic = traffic_info.get(today_utc, {})
                #logging.info(f"Daily traffic data for {today_utc}: {daily_traffic}")
                
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
                if status == 'downloaded':
                    self.update_status(torrent_id, TorrentStatus.CACHED)
                elif status == 'downloading':
                    self.update_status(torrent_id, TorrentStatus.DOWNLOADING)
                elif status == 'waiting_files_selection':
                    self.update_status(torrent_id, TorrentStatus.SELECTING)
                elif status == 'magnet_error':
                    self.update_status(torrent_id, TorrentStatus.ERROR)
                    
            return info
            
        except Exception as e:
            logging.error(f"Error getting torrent info: {str(e)}")
            self.update_status(torrent_id, TorrentStatus.ERROR)
            return None

    def remove_torrent(self, torrent_id: str) -> None:
        """Remove a torrent from Real-Debrid"""
        try:
            make_request('DELETE', f'/torrents/delete/{torrent_id}', self.api_key)
            self.update_status(torrent_id, TorrentStatus.REMOVED)
        except Exception as e:
            logging.error(f"Error removing torrent: {str(e)}")
            raise

    def cleanup(self) -> None:
        """Clean up stale torrents and status tracking"""
        try:
            # Get list of active torrents
            torrents = make_request('GET', '/torrents', self.api_key)
            if not torrents:
                return
                
            # Find and remove stale torrents (older than 24 hours)
            cutoff = datetime.utcnow() - timedelta(hours=24)
            for torrent in torrents:
                try:
                    added = datetime.strptime(torrent['added'], "%Y-%m-%d %H:%M:%S")
                    if added < cutoff:
                        self.remove_torrent(torrent['id'])
                except (KeyError, ValueError) as e:
                    logging.warning(f"Error parsing torrent data: {str(e)}")
                    
        except Exception as e:
            logging.error(f"Error cleaning up torrents: {str(e)}")
        finally:
            # Always clean up status tracking
            super().cleanup()
