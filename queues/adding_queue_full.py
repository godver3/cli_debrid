"""
Handles the addition of media items to debrid service when uncached content handling mode is 'full'.
"""

import logging
import json
import re
import os
import tempfile
import requests
import hashlib
import bencodepy
from typing import Dict, Any, List, Optional, Tuple
from database import update_media_item_state, update_media_item, get_all_media_items
from debrid.status import TorrentStatus
from .media_matcher import MediaMatcher
from not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
from datetime import datetime, timedelta

class FullModeProcessor:
    """Handles the processing of media content in full mode"""
    
    def __init__(self, content_processor):
        self.content_processor = content_processor
        self.debrid_provider = content_processor.debrid_provider
        self.media_matcher = MediaMatcher()

    def process_full_mode(self, item: Dict[str, Any], queue_manager: Any) -> None:
        """
        Process an item when uncached content handling mode is set to 'full'.
        Attempts to add content starting from top result, regardless of cache status.
        
        Args:
            item: The media item to process
            queue_manager: The queue manager instance
        """
        try:
            scrape_results = self._get_scrape_results(item)
            if not scrape_results:
                self._handle_failed_item(queue_manager, item, "No scrape results found")
                return

            for result in scrape_results:
                if not isinstance(result, dict):
                    logging.warning(f"Invalid result format: {result}")
                    continue

                hash_value = result.get('hash')
                temp_file = None
                if not hash_value and ('magnet' in result or 'url' in result):
                    magnet = result.get('magnet') or result.get('url')
                    try:
                        hash_value, temp_file = self._extract_hash_from_magnet(magnet)
                        if not hash_value:
                            logging.warning(f"Could not extract hash from magnet/url: {magnet}")
                            continue
                    except Exception as e:
                        logging.error(f"Error extracting hash: {str(e)}")
                        continue

                logging.info(f"Attempting to add result with hash {hash_value}")
                
                try:
                    # Try to add the torrent regardless of cache status
                    magnet_or_url = None if temp_file else (result.get('magnet') or result.get('url'))
                    add_result = self.debrid_provider.add_torrent(magnet_or_url, temp_file)
                    logging.debug(f"Add result from debrid provider: {add_result}")
                    
                    if not add_result or not isinstance(add_result, dict):
                        logging.warning("Invalid add result from provider")
                        continue

                    # Get files from add_result
                    files = add_result.get('files', [])
                    if not files:
                        files = add_result.get('data', {}).get('files', [])
                    
                    if not files:
                        logging.warning("No files found in add_result")
                        self._cleanup_failed_torrent(add_result)
                        continue

                    # Process the content
                    success, message = self.content_processor.process_content(item, {'files': files})
                    logging.debug(f"Content processing result - success: {success}, message: {message}")
                    
                    if success:
                        # For TV shows, check if this is a multi-pack that could fill other episodes
                        if item.get('type') == 'episode':
                            # Get our series title
                            series_title = item.get('series_title', '') or item.get('title', '')
                            
                            # Get all scraping queue items for the same show
                            scraping_items = self._get_scraping_queue_items(queue_manager)
                            if scraping_items:
                                # Filter to only items from the same show and version
                                matching_items = [
                                    i for i in scraping_items 
                                    if ((i.get('series_title', '') or i.get('title', '')) == series_title and
                                        i.get('version') == item.get('version'))
                                ]
                                
                                if matching_items:
                                    logging.info(f"Found {len(matching_items)} other episodes of '{series_title}' to check")
                                    
                                    # Try to match each file against each item
                                    for scraping_item in matching_items:
                                        matches = self.content_processor.media_matcher.match_content(files, scraping_item)
                                        if matches:
                                            # Take the first matching file - matches are tuples of (path_str, score)
                                            matched_file = matches[0][0]  # Get the file path from the first match tuple
                                            file_name = os.path.basename(matched_file)
                                            
                                            # Update the item state
                                            queue_manager.move_to_checking(
                                                scraping_item, 
                                                "Adding", 
                                                result.get('title'), 
                                                result.get('magnet') or result.get('url'), 
                                                file_name, 
                                                add_result.get('torrent_id')
                                            )
                                            logging.info(f"Updated matching item with filled_by_file: {file_name}")

                        # Mark magnet/URL as not wanted
                        if 'magnet' in result:
                            hash_value = self._extract_hash_from_magnet(result['magnet'])[0]
                            if hash_value:
                                add_to_not_wanted(hash_value, str(item.get('id')), item)
                                logging.info(f"Added successful magnet hash {hash_value} to not wanted list")
                        elif 'url' in result:
                            add_to_not_wanted_urls(result['url'], str(item.get('id')), item)
                            logging.info(f"Added successful URL {result['url']} to not wanted list")
                        
                        # Move item to checking queue using queue manager
                        queue_manager.move_to_checking(
                            item,
                            "Adding",
                            result.get('title'),
                            result.get('magnet') or result.get('url'),
                            os.path.basename(files[0].get('path', '')),
                            add_result.get('torrent_id')
                        )
                        logging.info(f"Moved item to checking queue")
                        return
                    else:
                        logging.warning(f"Content processing failed: {message}")
                        self._cleanup_failed_torrent(add_result)
                        continue

                except Exception as e:
                    if isinstance(e, requests.exceptions.RetryError):
                        logging.error(f"Retry error processing result: {str(e)}")
                    else:
                        logging.error(f"Error processing result: {str(e)}")
                    continue
                finally:
                    # Clean up temp file if it exists
                    if temp_file:
                        try:
                            os.unlink(temp_file)
                        except Exception as e:
                            logging.warning(f"Error deleting temporary file: {e}")

            # If we get here, no results worked
            self._handle_failed_item(queue_manager, item, "No valid results found")

        except Exception as e:
            logging.error(f"Error in full mode processing: {str(e)}")
            self._handle_failed_item(queue_manager, item, f"Error in full mode: {str(e)}")

    def _get_scrape_results(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get and validate scrape results for an item"""
        scrape_results = item.get('scrape_results', [])
        logging.debug(f"Raw scrape results: {scrape_results}")
        
        if not scrape_results:
            logging.error("No scrape results found")
            return []

        # Handle string (JSON) format
        if isinstance(scrape_results, str):
            try:
                scrape_results = json.loads(scrape_results)
                logging.debug(f"Parsed JSON scrape results: {scrape_results}")
            except json.JSONDecodeError as e:
                logging.error(f"Error parsing scrape results JSON: {e}")
                return []

        # Ensure we have a list
        if not isinstance(scrape_results, list):
            logging.error(f"Scrape results is not a list, got type: {type(scrape_results)}")
            return []

        return scrape_results

    def _extract_hash_from_magnet(self, magnet: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract hash from magnet link or download and parse torrent file
        
        Returns:
            Tuple of (hash, temp_file_path). temp_file_path will be None for magnet links
        """
        try:
            # Check if this is a magnet link
            if magnet.startswith('magnet:'):
                btih_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
                if btih_match:
                    return btih_match.group(1).lower(), None
            # Check if this is a Jackett URL
            elif 'jackett' in magnet.lower():
                logging.debug(f"Downloading torrent from Jackett URL: {magnet}")
                try:
                    # Download the torrent file to a temporary location
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as tmp_file:
                        response = requests.get(magnet, timeout=10)
                        if response.status_code != 200:
                            logging.error(f"Failed to download torrent file: {response.status_code}")
                            return None, None
                            
                        # Check content type and headers
                        logging.debug(f"Response headers: {dict(response.headers)}")
                        content_type = response.headers.get('content-type', '')
                        logging.debug(f"Content type: {content_type}")
                        
                        # Log first few bytes of content
                        content = response.content
                        logging.debug(f"Content first 100 bytes: {content[:100]}")
                        
                        if not content:
                            logging.error("Empty torrent file content")
                            return None, None
                            
                        # Try to decode as bencode to validate
                        try:
                            torrent_data = bencodepy.decode(content)
                            logging.debug(f"Decoded torrent data keys: {[k.decode() if isinstance(k, bytes) else k for k in torrent_data.keys()]}")
                            
                            if not isinstance(torrent_data, dict) or b'info' not in torrent_data:
                                logging.error("Invalid torrent file structure")
                                return None, None
                                
                            # Log info dict keys
                            info = torrent_data[b'info']
                            logging.debug(f"Info dict keys: {[k.decode() if isinstance(k, bytes) else k for k in info.keys()]}")
                            
                        except Exception as e:
                            logging.error(f"Failed to decode torrent file: {e}")
                            return None, None
                            
                        # Write validated content
                        tmp_file.write(content)
                        tmp_file.flush()
                        
                        # Calculate info hash
                        info = torrent_data.get(b'info', {})
                        if info:
                            info_encoded = bencodepy.encode(info)
                            hash_value = hashlib.sha1(info_encoded).hexdigest().lower()
                            logging.debug(f"Calculated hash: {hash_value}")
                            return hash_value, tmp_file.name
                            
                except requests.exceptions.RequestException as e:
                    logging.error(f"Error downloading torrent file: {e}")
                except Exception as e:
                    logging.error(f"Error processing torrent file: {e}")
                    if 'tmp_file' in locals() and os.path.exists(tmp_file.name):
                        try:
                            os.unlink(tmp_file.name)
                        except Exception as e:
                            logging.warning(f"Error deleting temporary file: {e}")
            
            logging.warning(f"Could not extract hash from: {magnet}")
            return None, None
            
        except Exception as e:
            logging.error(f"Error extracting hash: {str(e)}")
            return None, None

    def _cleanup_failed_torrent(self, add_result: Dict[str, Any]) -> None:
        """Remove a failed torrent from the debrid service"""
        torrent_id = add_result.get('torrent_id')
        if torrent_id:
            try:
                self.debrid_provider.remove_torrent(torrent_id)
                logging.info(f"Removed failed torrent {torrent_id}")
            except Exception as e:
                logging.error(f"Error removing torrent {torrent_id}: {str(e)}")

    def _handle_failed_item(self, queue_manager: Any, item: Dict[str, Any], message: str) -> None:
        """Handle a failed item"""
        try:
            # Check if item is old (>7 days from release date)
            release_date_str = item.get('release_date')
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
                    days_old = (datetime.now() - release_date).days
                    
                    if days_old > 7:
                        logging.info(f"Item is {days_old} days old, blacklisting: {item.get('title')}")
                        blacklist_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        update_media_item_state(item['id'], 'Blacklisted', blacklisted_date=blacklist_date)
                        
                        # If this is a TV show, blacklist other episodes from same season in scraping queue
                        if item.get('type') == 'episode':
                            series_title = item.get('series_title', '') or item.get('title', '')
                            season = item.get('season') or item.get('season_number')
                            version = item.get('version')
                            
                            if series_title and season is not None and version:
                                scraping_items = queue_manager.get_queue_items('Scraping')
                                if scraping_items:
                                    # Find matching episodes from same show, season and version
                                    matching_items = [
                                        i for i in scraping_items
                                        if ((i.get('series_title', '') or i.get('title', '')) == series_title and
                                            (i.get('season') or i.get('season_number')) == season and
                                            i.get('version') == version)
                                    ]
                                    
                                    # Blacklist matching items
                                    for match in matching_items:
                                        update_media_item_state(match['id'], 'Blacklisted', blacklisted_date=blacklist_date)
                                        logging.info(f"Blacklisted related episode: {match.get('title')}")
                        return
                except Exception as e:
                    logging.error(f"Error parsing release date: {e}")
            
            # If not blacklisted, mark as failed
            update_media_item_state(item['id'], "Failed")
            queue_manager.remove_item(item)
            logging.info(f"Marked item {item.get('id')} as failed: {message}")
        except Exception as e:
            logging.error(f"Error handling failed item: {str(e)}")

    def _get_scraping_queue_items(self, queue_manager: Any) -> List[Dict[str, Any]]:
        """Get all items currently in the Scraping queue"""
        try:
            return [dict(row) for row in get_all_media_items(state="Scraping")]
        except Exception as e:
            logging.error(f"Error getting scraping queue items: {str(e)}")
            return []
