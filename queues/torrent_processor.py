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

from debrid.base import DebridProvider, TooManyDownloadsError
from debrid.common import (
    extract_hash_from_magnet,
    extract_hash_from_file,
    is_video_file,
    is_unwanted_file,
    download_and_extract_hash
)
from debrid.status import TorrentStatus
from not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls

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
        
    def process_torrent(self, magnet_or_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Process a magnet link or torrent URL
        
        Args:
            magnet_or_url: Either a magnet link or URL to a torrent file
            
        Returns:
            Tuple of (magnet_link, temp_file_path) where:
                - For magnet links: (magnet_link, None)
                - For torrent files: (None, temp_file_path)
                - Returns (None, None) on error
        """
        try:
            if magnet_or_url.startswith('magnet:'):
                return magnet_or_url, None
                
            parsed = urlparse(magnet_or_url)
            if not parsed.scheme or not parsed.netloc:
                logging.error(f"Invalid URL or magnet link: {magnet_or_url}")
                return None, None
                
            with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as tmp:
                try:
                    response = requests.get(magnet_or_url, timeout=30)
                    response.raise_for_status()
                    tmp.write(response.content)
                    tmp.flush()
                    return None, tmp.name
                except Exception as e:
                    logging.error(f"Failed to download torrent file: {str(e)}")
                    try:
                        os.unlink(tmp.name)
                    except:
                        pass
                    return None, None
            
        except Exception as e:
            logging.error(f"Error processing magnet/URL: {str(e)}", exc_info=True)
            return None, None
            
    def check_cache(self, magnet_or_url: str, temp_file: Optional[str] = None) -> Optional[bool]:
        """
        Check if a magnet link or torrent file is cached
        
        Args:
            magnet_or_url: Magnet link or URL
            temp_file: Optional path to torrent file
            
        Returns:
            - True: Torrent is cached
            - False: Torrent is not cached
            - None: Error occurred (no video files, invalid magnet, etc)
        """
        try:
            is_cached = self.debrid_provider.is_cached(magnet_or_url, temp_file)
            if is_cached is None:
                return None
            return is_cached
            
        except Exception as e:
            logging.error(f"Error checking cache: {str(e)}", exc_info=True)
            return None
            
    def add_to_account(self, magnet_or_url: str) -> Optional[Dict]:
        """
        Add a magnet or torrent to the debrid account
        
        Args:
            magnet_or_url: Magnet link or torrent URL to add
            
        Returns:
            Torrent info if successful, None otherwise
        """
        torrent_id = None
        info = None
        max_retries = 3
        retry_delay = 2  # seconds
        temp_file = None

        # Get caller information
        caller_frame = inspect.currentframe().f_back
        caller_info = f"{caller_frame.f_code.co_filename}:{caller_frame.f_code.co_name}:{caller_frame.f_lineno}"
        logging.info(f"TorrentProcessor.add_to_account called from {caller_info}")
        
        try:
            magnet, temp_file = self.process_torrent(magnet_or_url)
            add_response = self.debrid_provider.add_torrent(magnet if magnet else None, temp_file)
            
            torrent_id = add_response
            if not torrent_id:
                logging.error("Failed to add torrent - no torrent ID returned")
                return None
                
            logging.info(f"Successfully added torrent (ID: {torrent_id})")
            
            for attempt in range(max_retries):
                info = self.debrid_provider.get_torrent_info(torrent_id)
                
                if not info or len(info.get('files', [])) == 0:
                    time.sleep(retry_delay)
                    continue
                
                return info
            
            if not info:
                logging.error(f"Failed to get info for torrent {torrent_id} after {max_retries} attempts")
            else:
                logging.error(f"No files found in torrent {torrent_id} after {max_retries} attempts")
            return info
            
        except Exception as e:
            logging.error(f"Error adding magnet: {str(e)}", exc_info=True)
            return None
            
        finally:
            if temp_file:
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    logging.error(f"Error cleaning up temp file: {str(e)}")
            
            if torrent_id and (not info or len(info.get('files', [])) == 0):
                try:
                    logging.info(f"Attempting to remove empty/failed torrent {torrent_id}")
                    self.debrid_provider.remove_torrent(
                        torrent_id,
                        removal_reason="Empty or failed torrent during processing"
                    )
                    logging.info(f"Successfully removed empty/failed torrent {torrent_id}")
                except Exception as e:
                    logging.error(f"Error cleaning up empty/failed torrent {torrent_id}: {str(e)}", exc_info=True)
                    
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
        item_identifier = item.get('title', 'Unknown') if item else 'Unknown'
        logging.info(f"[{item_identifier}] Starting to process {len(results)} results (accept_uncached={accept_uncached})")
        
        for idx, result in enumerate(results, 1):
            try:
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
                is_cached = self.debrid_provider.is_cached(
                    magnet if not temp_file else "",
                    temp_file,
                    result_title=result_title,
                    result_index=f"{idx}/{len(results)}"
                )
                    
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
                            return None, original_link
                    except TooManyDownloadsError:
                        logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Download limit reached. Moving to pending uncached queue.")
                        if item:
                            from database import update_media_item_state
                            update_media_item_state(item['id'], "Pending Uncached",
                                filled_by_magnet=original_link,
                                filled_by_title=result.get('title', ''))
                            item['filled_by_magnet'] = original_link
                            item['filled_by_title'] = result.get('title', '')
                        return None, original_link
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
                    magnet, temp_file = self.process_torrent(original_link)
                    try:
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
                    if len(info.get('files', [])) > 0:
                        if item and 'magnet' in result:
                            result_magnet = result['magnet']
                            try:
                                if result_magnet.startswith('http'):
                                    hash_value = download_and_extract_hash(result_magnet)
                                    add_to_not_wanted(hash_value)
                                    add_to_not_wanted_urls(result_magnet)
                                else:
                                    hash_value = extract_hash_from_magnet(result_magnet)
                                    add_to_not_wanted(hash_value)

                                # Record torrent tracking after confirming valid result
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
                            except Exception as e:
                                logging.error(f"[{item_identifier}] [Result {idx}/{len(results)}] Failed to process magnet for not wanted: {str(e)}")

                            logging.info(f"[{item_identifier}] [Result {idx}/{len(results)}] Successfully processed and added")
                            return info, original_link
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
        return None, None
