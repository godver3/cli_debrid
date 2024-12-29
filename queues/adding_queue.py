"""
Queue management for handling the addition of media items to a debrid service.
Separates queue management from content processing logic.
"""

import logging
import json
import re
from typing import Dict, Any, List, Optional, Tuple
from database import get_all_media_items, get_media_item_by_id, update_media_item_state
from debrid import get_debrid_provider
from debrid.status import TorrentStatus
from .media_matcher import MediaMatcher
from settings import get_setting

class ContentProcessor:
    """Handles the processing of media content after it's been added to the debrid service"""
    
    def __init__(self, debrid_provider):
        self.debrid_provider = debrid_provider
        self.media_matcher = MediaMatcher()

    def process_content(self, item: Dict[str, Any], torrent_info: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Process content after it's been added to the debrid service
        
        Args:
            item: Media item to process
            torrent_info: Information about the added torrent
            
        Returns:
            Tuple of (success, message)
        """
        try:
            files = torrent_info.get('files', [])
            if not files:
                return False, "No files found in torrent"

            matches = self.media_matcher.match_content(files, item)
            if not matches:
                return False, "No matching files found"

            if len(matches) > 1 and item.get('type') == 'movie':
                return False, "Multiple matches found for movie"

            return True, "Content processed successfully"
            
        except Exception as e:
            logging.error(f"Error processing content: {str(e)}")
            return False, f"Error processing content: {str(e)}"

class AddingQueue:
    """Manages the queue of items being added to the debrid service"""
    
    def __init__(self):
        self.items: List[Dict[str, Any]] = []
        self.debrid_provider = get_debrid_provider()
        self.content_processor = ContentProcessor(self.debrid_provider)

    def update(self):
        """Update the queue with current items in 'Adding' state"""
        self.items = [dict(row) for row in get_all_media_items(state="Adding")]

    def get_contents(self) -> List[Dict[str, Any]]:
        """Get current queue contents"""
        return self.items

    def add_item(self, item: Dict[str, Any]):
        """Add an item to the queue"""
        self.items.append(item)

    def process(self, queue_manager: Any):
        """Process the next item in the queue"""
        if not self.items:
            return

        item = self.items[0]
        results = self._get_scrape_results(item)
        if not results:
            logging.error("No valid results found in item")
            self._handle_failed_item(queue_manager, item)
            return

        try:
            # Try each result in order until one succeeds
            for idx, result in enumerate(results):
                logging.debug(f"Processing result {idx + 1}/{len(results)}: {result}")
                
                # Try to get hash directly or extract from magnet
                hash_value = result.get('hash')
                if not hash_value and 'magnet' in result:
                    hash_value = self._extract_hash_from_magnet(result['magnet'])
                    if hash_value:
                        logging.debug(f"Extracted hash from magnet: {hash_value}")
                
                if not hash_value:
                    logging.warning(f"No hash found in result {idx + 1} and couldn't extract from magnet")
                    continue

                cache_status = self.debrid_provider.is_cached([hash_value])
                is_cached = cache_status.get(hash_value, False)

                try:
                    if is_cached:
                        success = self._process_cached_result(queue_manager, item, result)
                    else:
                        success = self._process_uncached_result(queue_manager, item, result)

                    if success:
                        self.items.pop(0)
                        return
                except Exception as e:
                    logging.error(f"Error processing result: {str(e)}")
                    continue

            # If we get here, all results failed
            logging.error("All results failed to process")
            self._handle_failed_item(queue_manager, item)

        except Exception as e:
            logging.error(f"Error processing item: {str(e)}")
            self._handle_failed_item(queue_manager, item)

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
            except json.JSONDecodeError:
                logging.error("Failed to decode scrape results JSON")
                return []

        # Ensure we have a list
        if not isinstance(scrape_results, list):
            logging.error(f"Scrape results is not a list, got type: {type(scrape_results)}")
            return []

        # Log the first result for debugging
        if scrape_results:
            logging.debug(f"First result structure: {scrape_results[0]}")

        return scrape_results

    def _extract_hash_from_magnet(self, magnet: str) -> Optional[str]:
        """Extract hash from magnet link"""
        try:
            # Look for btih hash in magnet link
            btih_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
            if btih_match:
                return btih_match.group(1).lower()
            return None
        except Exception as e:
            logging.error(f"Error extracting hash from magnet: {str(e)}")
            return None

    def _process_cached_result(self, queue_manager: Any, item: Dict[str, Any], 
                             result: Dict[str, Any]) -> bool:
        """Process a cached result"""
        try:
            add_result = self.debrid_provider.add_torrent(result['magnet'])
            if not add_result or not isinstance(add_result, dict):
                logging.error("Invalid response from debrid provider")
                return False

            torrent_id = add_result.get('id')
            if not torrent_id:
                logging.error("No torrent ID in response")
                return False

            torrent_info = self.debrid_provider.get_torrent_info(torrent_id)
            if not torrent_info:
                logging.error(f"Could not get torrent info for ID: {torrent_id}")
                return False

            if torrent_info['is_error']:
                logging.error(f"Torrent in error state: {torrent_info['status']}")
                self.debrid_provider.remove_torrent(torrent_id)
                return False

            success, message = self.content_processor.process_content(item, torrent_info)
            if success:
                update_media_item_state(item['id'], 'Checking')
                return True

            logging.error(f"Content processing failed: {message}")
            self.debrid_provider.remove_torrent(torrent_id)
            return False

        except Exception as e:
            logging.error(f"Error processing cached result: {str(e)}")
            return False

    def _process_uncached_result(self, queue_manager: Any, item: Dict[str, Any], 
                               result: Dict[str, Any]) -> bool:
        """Process an uncached result"""
        if not self._should_process_uncached():
            logging.info("Skipping uncached content based on settings")
            return False

        try:
            add_result = self.debrid_provider.add_torrent(result['magnet'])
            if not add_result or not isinstance(add_result, dict):
                logging.error("Invalid response from debrid provider")
                return False

            torrent_id = add_result.get('id')
            if not torrent_id:
                logging.error("No torrent ID in response")
                return False

            torrent_info = self.debrid_provider.get_torrent_info(torrent_id)
            if not torrent_info:
                logging.error(f"Could not get torrent info for ID: {torrent_id}")
                return False

            if torrent_info['is_error']:
                logging.error(f"Torrent in error state: {torrent_info['status']}")
                self.debrid_provider.remove_torrent(torrent_id)
                return False

            success, message = self.content_processor.process_content(item, torrent_info)
            if success:
                update_media_item_state(item['id'], 'Downloading')
                return True

            logging.error(f"Content processing failed: {message}")
            self.debrid_provider.remove_torrent(torrent_id)
            return False

        except Exception as e:
            logging.error(f"Error processing uncached result: {str(e)}")
            return False

    def _should_process_uncached(self) -> bool:
        """Determine if we should process uncached content based on settings"""
        mode = get_setting("Scraping", "uncached_content_handling", "none").lower()
        return mode in ["none", "full"]

    def _handle_failed_item(self, queue_manager: Any, item: Dict[str, Any]):
        """Handle a failed item by moving it to the appropriate queue"""
        # Move to error state instead of failed
        update_media_item_state(item['id'], 'Error')
        self.items.pop(0)
