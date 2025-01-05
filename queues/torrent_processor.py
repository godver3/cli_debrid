"""
Handles processing of torrent files and magnet links, including cache checking
and addition to debrid service accounts.
"""

import logging
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from debrid.base import DebridProvider, TooManyDownloadsError
from debrid.common import (
    download_and_convert_to_magnet,
    extract_hash_from_magnet,
    torrent_to_magnet,
    is_video_file,
    is_unwanted_file,
    download_and_extract_hash
)
from debrid.status import TorrentStatus

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
        
    def process_magnet(self, magnet_or_url: str) -> Optional[str]:
        """
        Process a magnet link or torrent URL into a magnet link
        
        Args:
            magnet_or_url: Either a magnet link or URL to a torrent file
            
        Returns:
            Magnet link if successful, None otherwise
        """
        try:
            logging.debug(f"Processing magnet/URL: {magnet_or_url[:100]}...")  # Log first 100 chars
            
            # If it's already a magnet link, return it
            if magnet_or_url.startswith('magnet:'):
                logging.debug("Input is already a magnet link")
                return magnet_or_url
                
            # Check if it's a URL
            parsed = urlparse(magnet_or_url)
            if not parsed.scheme or not parsed.netloc:
                logging.error(f"Invalid URL or magnet link: {magnet_or_url}")
                return None
                
            logging.debug("Input is a URL, attempting to download and convert to magnet")
            # Download and convert torrent to magnet
            magnet = download_and_convert_to_magnet(magnet_or_url)
            if not magnet:
                logging.error(f"Failed to convert torrent to magnet: {magnet_or_url}")
                return None
                
            logging.debug("Successfully converted torrent to magnet")
            return magnet
            
        except Exception as e:
            logging.error(f"Error processing magnet/URL {magnet_or_url}: {str(e)}", exc_info=True)
            return None
            
    def check_cache(self, magnet: str) -> Optional[bool]:
        """
        Check if a magnet link is cached
        
        Args:
            magnet: Magnet link to check
            
        Returns:
            - True: Torrent is cached
            - False: Torrent is not cached
            - None: Error occurred (no video files, invalid magnet, etc)
        """
        try:
            logging.debug("Checking cache status")
            is_cached = self.debrid_provider.is_cached(magnet)
            
            # Handle the three possible states
            if is_cached is None:
                logging.debug("Cache check returned error state (None)")
                return None
            
            logging.debug(f"Cache check result: {'Cached' if is_cached else 'Not cached'}")
            return is_cached
            
        except Exception as e:
            logging.error(f"Error checking cache for magnet: {str(e)}", exc_info=True)
            return None
            
    def add_to_account(self, magnet: str) -> Optional[Dict]:
        """
        Add a magnet to the debrid account
        
        Args:
            magnet: Magnet link to add
            
        Returns:
            Torrent info if successful, None otherwise
        """
        torrent_id = None
        info = None
        max_retries = 3
        retry_delay = 2  # seconds
        
        try:
            logging.debug("Adding torrent to debrid account")
            # Add the torrent
            add_response = self.debrid_provider.add_torrent(magnet)
            logging.debug(f"Full add_torrent response: {add_response}")
            
            torrent_id = add_response
            if not torrent_id:
                logging.error("Failed to add torrent - no torrent ID returned")
                return None
                
            logging.debug(f"Successfully added torrent (ID: {torrent_id})")
            
            # Get the torrent info with retries
            for attempt in range(max_retries):
                logging.debug(f"Fetching info for torrent {torrent_id} (attempt {attempt + 1}/{max_retries})")
                info = self.debrid_provider.get_torrent_info(torrent_id)
                logging.debug(f"Full get_torrent_info response: {info}")
                
                if not info:
                    logging.debug(f"No info returned on attempt {attempt + 1}")
                    time.sleep(retry_delay)
                    continue
                
                if len(info.get('files', [])) == 0:
                    logging.debug(f"No files found on attempt {attempt + 1}, waiting {retry_delay}s before retry")
                    time.sleep(retry_delay)
                    continue
                
                # If we have files, break out of retry loop
                logging.debug(f"Successfully retrieved torrent info - Size: {info.get('bytes', 0)} bytes, Files: {len(info.get('files', []))} files")
                return info
            
            # If we got here, we exhausted our retries
            if not info:
                logging.error(f"Failed to get info for torrent {torrent_id} after {max_retries} attempts")
            else:
                logging.error(f"No files found in torrent {torrent_id} after {max_retries} attempts")
            return info
            
        except Exception as e:
            logging.error(f"Error adding magnet: {str(e)}", exc_info=True)
            return None
            
        finally:
            # If we failed after adding the torrent, try to clean it up
            if torrent_id and (not info or len(info.get('files', [])) == 0):
                try:
                    logging.debug(f"Cleaning up failed torrent {torrent_id}")
                    self.debrid_provider.remove_torrent(torrent_id)
                except Exception as e:
                    logging.error(f"Error cleaning up torrent {torrent_id}: {str(e)}")
                    
    def process_results(
        self,
        results: list[Dict],
        accept_uncached: bool = False,
        item: Optional[Dict] = None
    ) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Process a list of results to find the best match
        
        Args:
            results: List of results to process
            accept_uncached: Whether to accept uncached results
            item: Optional media item for tracking successful results
            
        Returns:
            Tuple of (torrent_info, magnet_link) if successful, (None, None) otherwise
        """
        logging.debug(f"Processing {len(results)} results (accept_uncached={accept_uncached})")
        
        for idx, result in enumerate(results, 1):
            try:
                # Get magnet link - try both 'link' and 'magnet' keys
                original_link = result.get('magnet') or result.get('link')
                logging.debug(f"Result {idx}: Raw result data: {result}")
                if not original_link:
                    logging.debug(f"Result {idx}: No magnet link found in result")
                    continue
                    
                logging.debug(f"Result {idx}: Found magnet link: {original_link}")
                magnet = self.process_magnet(original_link)
                if not magnet:
                    logging.debug(f"Result {idx}: Failed to process magnet link")
                    continue
                    
                logging.debug(f"Result {idx}: Processed magnet link: {magnet}")
                
                # Check cache status
                logging.debug(f"Result {idx}: Checking cache status")
                is_cached = self.check_cache(magnet)
                
                # Skip if there was an error checking cache
                if is_cached is None:
                    logging.debug(f"Result {idx}: Error checking cache status, skipping result")
                    continue
                    
                # Skip if we need cached and this isn't
                if not accept_uncached and not is_cached:
                    logging.debug(f"Result {idx}: Skipping uncached result (accept_uncached={accept_uncached})")
                    continue
                
                # If uncached, check download limits before proceeding
                if not is_cached:
                    try:
                        active_downloads, download_limit = self.debrid_provider.get_active_downloads()
                        if active_downloads >= download_limit:
                            logging.info(f"Download limit reached ({active_downloads}/{download_limit}). Moving to pending uncached queue.")
                            logging.debug(f"Original link: {original_link}")
                            if item:
                                from database import update_media_item_state
                                update_media_item_state(item['id'], "Pending Uncached", 
                                    filled_by_magnet=original_link,
                                    filled_by_title=result.get('title', ''))
                                item['filled_by_magnet'] = original_link  # Store original URL/magnet
                                item['filled_by_title'] = result.get('title', '')
                            return None, original_link  # Return original URL/magnet
                    except TooManyDownloadsError:
                        logging.info("Download limit reached. Moving to pending uncached queue.")
                        logging.debug(f"Original link: {original_link}")

                        if item:
                            from database import update_media_item_state
                            update_media_item_state(item['id'], "Pending Uncached",
                                filled_by_magnet=original_link,
                                filled_by_title=result.get('title', ''))
                            item['filled_by_magnet'] = original_link  # Store original URL/magnet
                            item['filled_by_title'] = result.get('title', '')
                        return None, original_link  # Return original URL/magnet
                    except Exception as e:
                        logging.error(f"Error checking download limits: {str(e)}")
                        continue
                    
                # Try to add it
                logging.debug(f"Result {idx}: Attempting to add torrent to debrid service")
                info = self.add_to_account(magnet)
                if info:
                    logging.debug(f"Result {idx}: Successfully added torrent (ID: {info.get('id')})")
                    logging.debug(f"Result {idx}: Torrent info - Size: {info.get('bytes', 0)} bytes, Files: {len(info.get('files', []))} files")
                    
                    # Only proceed if the torrent has files
                    if len(info.get('files', [])) > 0:
                        # Mark the successful magnet/URL as not wanted to prevent reuse
                        if item:
                            if 'magnet' in result:
                                from not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
                                result_magnet = result['magnet']
                                logging.debug(f"Result {idx}: Attempting to add magnet to not wanted: {result_magnet}")
                                try:
                                    # Check if magnet is actually an HTTP link
                                    if result_magnet.startswith('http'):
                                        logging.debug(f"Result {idx}: Magnet is HTTP link, downloading torrent first")
                                        hash_value = download_and_extract_hash(result_magnet)
                                        add_to_not_wanted(hash_value)
                                        add_to_not_wanted_urls(result_magnet)
                                        logging.info(f"Added successful magnet hash {hash_value} and URL to not wanted lists")
                                    else:
                                        hash_value = extract_hash_from_magnet(result_magnet)
                                        add_to_not_wanted(hash_value)
                                        logging.info(f"Added successful magnet hash {hash_value} to not wanted list")
                                except Exception as e:
                                    logging.error(f"Result {idx}: Failed to process magnet for not wanted: {str(e)}")
                            # elif 'url' in result:
                            #     # Dead code - all scrapers use 'magnet' key
                            #     pass
                        return info, magnet
                    else:
                        logging.debug(f"Result {idx}: Torrent has no files, continuing to next result")
                        # Clean up the empty torrent
                        try:
                            if info.get('id'):
                                logging.debug(f"Result {idx}: Removing empty torrent {info['id']}")
                                self.debrid_provider.remove_torrent(info['id'])
                        except Exception as e:
                            logging.error(f"Result {idx}: Error removing empty torrent {info.get('id')}: {str(e)}")
                else:
                    logging.debug(f"Result {idx}: Failed to add torrent")
                    
            except Exception as e:
                logging.error(f"Result {idx}: Error processing result: {str(e)}", exc_info=True)
                continue
                
        logging.debug("No suitable results found after processing all options")
        return None, None
