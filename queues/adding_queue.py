"""
Queue management for handling the addition of media items to a debrid service.
Orchestrates the process of adding content and managing queue state.
"""

import logging
import json
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta

from debrid import get_debrid_provider
from database import update_media_item, get_all_media_items, get_media_item_by_id
from settings import get_setting
from .torrent_processor import TorrentProcessor
from .media_matcher import MediaMatcher

class AddingQueue:
    """Manages the queue of items being added to the debrid service"""
    
    def __init__(self):
        """Initialize the queue manager"""
        self.debrid_provider = get_debrid_provider()
        self.torrent_processor = TorrentProcessor(self.debrid_provider)
        self.media_matcher = MediaMatcher()
        self.items: List[Dict] = []
        
    def reinitialize_provider(self):
        """Reinitialize the debrid provider and processors"""
        self.debrid_provider = get_debrid_provider()
        self.torrent_processor = TorrentProcessor(self.debrid_provider)
        
    def update(self):
        """Update the queue with current items in 'Adding' state"""
        self.items = [dict(row) for row in get_all_media_items(state="Adding")]
        
    def get_contents(self) -> List[Dict]:
        """Get current queue contents"""
        return self.items
        
    def add_item(self, item: Dict):
        """Add an item to the queue"""
        self.items.append(item)
        
    def remove_item(self, item: Dict):
        """Remove an item from the queue"""
        self.items = [i for i in self.items if i['id'] != item['id']]
        
    def remove_unwanted_torrent(self, torrent_id: str):
        """Remove an unwanted torrent from the debrid service
        
        Args:
            torrent_id: ID of the torrent to remove
        """
        if torrent_id:
            try:
                self.debrid_provider.remove_torrent(torrent_id)
                logging.debug(f"Successfully removed unwanted torrent {torrent_id} from debrid service")
            except Exception as e:
                logging.error(f"Failed to remove unwanted torrent {torrent_id}: {e}")
                
    def process(self, queue_manager: Any) -> bool:
        """
        Process items in the queue
        
        Args:
            queue_manager: Global queue manager instance
            
        Returns:
            True if any items were processed successfully
        """
        if not self.items:
            #logging.debug("Adding queue is empty, skipping processing")
            return False
            
        success = False
        for item in self.items[:]:  # Copy list as we'll modify it
            item_identifier = f"{item.get('title')} ({item.get('type')})"
            logging.debug(f"Processing item: {item_identifier}")
            
            try:
                # Get scraping results from the item
                results = item.get('scrape_results', [])
                if isinstance(results, str):
                    try:
                        results = json.loads(results)
                        logging.debug(f"Successfully parsed scrape results JSON for {item_identifier}")
                    except json.JSONDecodeError:
                        logging.error(f"Failed to parse scrape results JSON for {item_identifier}: {results}")
                        self._handle_failed_item(item, "Invalid scrape results format", queue_manager)
                        continue
                
                if not results:
                    logging.debug(f"No scrape results found for {item_identifier}")
                    self._handle_failed_item(item, "No results found", queue_manager)
                    continue
                    
                # Log number of results found
                logging.debug(f"Found {len(results)} scrape results for {item_identifier}")
                
                logging.debug(f"Uncached content management: {get_setting('Scraping', 'uncached_content_handling')}")

                if get_setting('Scraping', 'uncached_content_handling', 'None') == 'None':
                    logging.debug(f"First pass: Looking for cached results only, accept_uncached=False")
                    accept_uncached = False
                elif get_setting('Scraping', 'uncached_content_handling') == 'Full':
                    logging.debug(f"First pass: Looking for cached results only, accept_uncached=True")
                    accept_uncached = True

                # First try with cached results only
                torrent_info, magnet = self._process_results_with_mode(results, item_identifier, accept_uncached, item=item)

                logging.debug(f"torrent_info: {torrent_info}")
                logging.debug(f"magnet: {magnet}")
                
                # If no cached results and hybrid mode is enabled, try again with uncached
                if not torrent_info and not magnet and get_setting('Scraping', 'hybrid_mode'):
                    logging.debug(f"No cached results found and hybrid mode enabled, trying uncached results")
                    torrent_info, magnet = self._process_results_with_mode(results, item_identifier, accept_uncached=True, item=item)
                
                # Refresh item state from database since it may have been updated
                updated_item = get_media_item_by_id(item['id'])
                if updated_item and updated_item.get('state') == 'Pending Uncached':
                    logging.debug(f"Item moved to pending uncached queue for {item_identifier}")
                    continue
                
                # Only handle as failure if we didn't move to pending uncached
                if (not torrent_info or not magnet) and (not updated_item or updated_item.get('state') != 'Pending Uncached'):
                    logging.debug(f"No valid torrent info or magnet found for {item_identifier}")
                    self._handle_failed_item(item, "No valid results found", queue_manager)
                    continue
                    
                logging.debug(f"Successfully found torrent info and magnet for {item_identifier}")
                
                # Match content
                files = torrent_info.get('files', [])
                logging.debug(f"Found {len(files)} files in torrent for {item_identifier}")
                matches = self.media_matcher.match_content(files, item)
                
                if not matches:
                    logging.debug(f"No matching files found in torrent for {item_identifier}")
                    logging.debug(f"Current torrent_id: {torrent_info.get('id')}")
                    item['torrent_id'] = torrent_info.get('id')
                    logging.debug(f"Moving {item_identifier} back to handle_failed_item with [{item['torrent_id']}]")
                    self._handle_failed_item(item, "No matching files found in torrent", queue_manager)
                    continue
                    
                # Get the best matching file
                matched_file = matches[0][0]  # First match's path
                logging.debug(f"Best matching file for {item_identifier}: {matched_file}")
                
                # Move item to checking
                logging.debug(f"Moving {item_identifier} to checking queue")
                queue_manager.move_to_checking(
                    item=item,
                    from_queue="Adding",
                    title=results[0].get('title', ''),  # Use first result's title
                    link=magnet,
                    filled_by_file=matched_file,
                    torrent_id=torrent_info.get('id')
                )
                
                # Check for related items if it's an episode
                if item.get('type') == 'episode':
                    logging.debug(f"Checking for related episodes for {item_identifier}")
                    scraping_items = queue_manager.get_scraping_items()
                    related_items = self.media_matcher.find_related_items(files, scraping_items, item)
                    
                    if related_items:
                        logging.debug(f"Found {len(related_items)} related items for {item_identifier}")
                    
                    # Move related items to checking
                    for related in related_items:
                        related_identifier = f"{related.get('title')} S{related.get('season_number')}E{related.get('episode_number')}"
                        logging.debug(f"Processing related item: {related_identifier}")
                        related_matches = self.media_matcher.match_content(files, related)
                        if related_matches:
                            related_file = related_matches[0][0]  # First match's path
                            logging.debug(f"Moving related item {related_identifier} to checking with file: {related_file}")
                            queue_manager.move_to_checking(
                                item=related,
                                from_queue="Scraping",
                                title=results[0].get('title', ''),
                                link=magnet,
                                filled_by_file=related_file,
                                torrent_id=torrent_info.get('id')
                            )
                        else:
                            logging.debug(f"No matching files found for related item: {related_identifier}")
                            
                success = True
                
            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                self._handle_failed_item(item, f"Processing error: {str(e)}", queue_manager)
                
        return success

    def _process_results_with_mode(self, results: List[Dict], item_identifier: str, accept_uncached: bool, item: Dict) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Process results with specific caching mode
        
        Args:
            results: List of results to process
            item_identifier: Identifier string for logging
            accept_uncached: Whether to accept uncached results
            item: Media item being processed
            
        Returns:
            Tuple of (torrent_info, magnet_link) if successful, (None, None) otherwise
        """
        logging.debug(f"Processing results with accept_uncached={accept_uncached}")
        
        try:
            # Process results to get best match
            torrent_info, magnet = self.torrent_processor.process_results(
                results,
                accept_uncached=accept_uncached,
                item=item
            )
            
            if torrent_info and magnet:
                cache_status = "uncached" if accept_uncached else "cached"
                logging.debug(f"Found valid {cache_status} result for {item_identifier}")
            
            return torrent_info, magnet
        except TorrentAdditionError as e:
            logging.error(f"Error processing results for {item_identifier}: {str(e)}")
            self._handle_failed_item(item, f"Error checking cache status: {str(e)}", queue_manager)
            return None, None

    def _handle_failed_item(self, item: Dict, error: str, queue_manager: Any):
        """
        Handle a failed item by moving it back to Wanted queue if media matching failed,
        or to Sleeping/Blacklisted state for other failures
        
        Args:
            item: The media item that failed
            error: Error message describing why it failed
            queue_manager: Global queue manager instance
        """
        try:
            # If failure was due to media matching after torrent addition
            if "No matching files found in torrent" in error:
                logging.info(f"Media matching failed for {item.get('title')}, moving back to Wanted queue")
                logging.info(f"Torrent ID: {item.get('torrent_id')}")
                # Remove the torrent from the debrid service
                if 'torrent_id' in item:
                    logging.debug(f"Removing unwanted torrent {item['torrent_id']} from debrid service")
                    self.remove_unwanted_torrent(item['torrent_id'])
                else:
                    logging.warning(f"No torrent ID found for {item.get('title')}, skipping removal")
                queue_manager.move_to_wanted(item, "Adding")
                return

            # For other failures, check if item is old (>7 days from release date)
            release_date_str = item.get('release_date')
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
                    if release_date < datetime.now() - timedelta(days=7):
                        # Item is old, blacklist it
                        blacklist_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        update_media_item(
                            item['id'],
                            state='Blacklisted',
                            blacklisted_date=blacklist_date
                        )
                        
                        # If this is a TV show, blacklist other episodes from same season in scraping queue
                        if item.get('type') == 'episode':
                            series_title = item.get('series_title', '') or item.get('title', '')
                            season = item.get('season') or item.get('season_number')
                            version = item.get('version')
                            
                            if series_title and season is not None and version:
                                scraping_items = [dict(row) for row in get_all_media_items(state="Scraping")]
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
                                        update_media_item(
                                            match['id'],
                                            state='Blacklisted',
                                            blacklisted_date=blacklist_date
                                        )
                                        logging.info(f"Blacklisted related episode: {match.get('title')}")
                        return
                except Exception as e:
                    logging.error(f"Error parsing release date: {e}")
            
            # If not blacklisted, move to sleeping state
            logging.debug(f"Moving item to Sleeping state: {item.get('title')} ({item.get('type')})")
            update_media_item(
                item['id'],
                state='Sleeping'
            )
            self.remove_item(item)
            
        except Exception as e:
            logging.error(f"Error handling failed item: {str(e)}", exc_info=True)