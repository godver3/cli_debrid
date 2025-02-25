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
from database.torrent_tracking import update_adding_error

class AddingQueue:
    """Manages the queue of items being added to the debrid service"""
    
    def __init__(self):
        """Initialize the queue manager"""
        self.debrid_provider = get_debrid_provider()
        self.torrent_processor = TorrentProcessor(self.debrid_provider)
        self.media_matcher = MediaMatcher()
        self.items: List[Dict] = []
        self.last_process_time = {}
        logging.info("Initialized AddingQueue")
        
    def reinitialize_provider(self):
        """Reinitialize the debrid provider and processors"""
        self.debrid_provider = get_debrid_provider()
        self.torrent_processor = TorrentProcessor(self.debrid_provider)
        
    def update(self):
        """Update the queue with current items in 'Adding' state"""
        old_items = {item['id']: item for item in self.items}
        self.items = [dict(row) for row in get_all_media_items(state="Adding")]
        new_items = {item['id']: item for item in self.items}
        
        # Log changes
        added = set(new_items.keys()) - set(old_items.keys())
        removed = set(old_items.keys()) - set(new_items.keys())
        if added:
            logging.info(f"Added items to queue during update: {added}")
        if removed:
            logging.info(f"Removed items from queue during update: {removed}")
        if len(self.items) > 0:
            logging.debug(f"Queue now contains {len(self.items)} items")
        
    def get_contents(self) -> List[Dict]:
        """Get current queue contents"""
        return self.items
        
    def add_item(self, item: Dict):
        """Add an item to the queue"""
        item_id = item.get('id')
        if item_id:
            existing = any(i['id'] == item_id for i in self.items)
            if existing:
                logging.warning(f"Item {item_id} already exists in queue - duplicate add attempt")
            else:
                logging.info(f"Adding item {item_id} to queue")
                self.items.append(item)
        else:
            logging.error("Attempted to add item without ID to queue")
        
    def remove_item(self, item: Dict):
        """Remove an item from the queue"""
        item_id = item.get('id')
        if item_id:
            old_len = len(self.items)
            self.items = [i for i in self.items if i['id'] != item_id]
            if len(self.items) < old_len:
                logging.info(f"Removed item {item_id} from queue")
            else:
                logging.warning(f"Attempted to remove item {item_id} but it was not in queue")
        else:
            logging.error("Attempted to remove item without ID from queue")
        
    def remove_unwanted_torrent(self, torrent_id: str):
        """
        Remove an unwanted torrent from the debrid service and track the removal
        
        Args:
            torrent_id: ID of the torrent to remove
        """
        if not torrent_id:
            logging.warning("Attempted to remove torrent with empty ID")
            return
            
        try:
            # Get torrent info before removal to record hash
            try:
                info = self.debrid_provider.get_torrent_info(torrent_id)
                if info:
                    hash_value = info.get('hash', '').lower()
                    if hash_value:
                        from not_wanted_magnets import add_to_not_wanted
                        try:
                            add_to_not_wanted(hash_value)
                            logging.info(f"Added hash {hash_value} to not wanted list")
                        except Exception as e:
                            logging.error(f"Failed to add to not wanted list: {str(e)}")
            except Exception as e:
                logging.warning(f"Could not get torrent info before removal: {str(e)}")
                
            # Remove the torrent with a descriptive reason
            self.debrid_provider.remove_torrent(
                torrent_id,
                removal_reason="Removed due to no matching files for media item"
            )
            logging.info(f"Successfully removed unwanted torrent {torrent_id}")
            
            # Update tracking record with adding error
            try:
                update_adding_error(hash_value)
            except Exception as e:
                logging.error(f"Failed to update tracking record for adding error: {str(e)}")
            
        except Exception as e:
            logging.error(f"Failed to remove unwanted torrent {torrent_id}: {str(e)}", exc_info=True)
                
    def process(self, queue_manager: Any) -> bool:
        """
        Process items in the queue
        
        Args:
            queue_manager: Global queue manager instance
            
        Returns:
            True if any items were processed successfully
        """
        if not self.items:
            return False
            
        success = False
        if self.items:
            item = self.items[0]  # Peek at first item
            item_identifier = queue_manager.generate_identifier(item)
            logging.info(f"Adding Queue - Starting processing of {len(self.items)} items")
            logging.debug(f"Adding Queue - Processing item with resolution: {item.get('resolution', 'Not found')} for {item_identifier}")
            
        for item in self.items[:]:  # Copy list as we'll modify it
            item_identifier = f"{item.get('title')} ({item.get('type')})"
            item_id = item.get('id')
            logging.info(f"Processing item {item_id}: {item_identifier}")
            
            try:
                results = item.get('scrape_results', [])
                if isinstance(results, str):
                    try:
                        results = json.loads(results)
                    except json.JSONDecodeError:
                        logging.error(f"Failed to parse scrape results JSON for {item_identifier}")
                        self._handle_failed_item(item, "Invalid scrape results format", queue_manager)
                        continue
                
                if not results:
                    logging.info(f"No scrape results found for {item_identifier}")
                    self._handle_failed_item(item, "No results found", queue_manager)
                    continue
                    
                logging.info(f"Found {len(results)} scrape results for {item_identifier}")

                for result in results:
                    result['original_scraped_torrent_title'] = result.get('original_title')
                
                if get_setting('Scraping', 'uncached_content_handling', 'None') == 'None':
                    accept_uncached = False
                elif get_setting('Scraping', 'uncached_content_handling') == 'Full':
                    accept_uncached = True

                torrent_info, magnet = self._process_results_with_mode(results, item_identifier, accept_uncached, item=item)
                
                if not torrent_info and not magnet and get_setting('Scraping', 'hybrid_mode'):
                    logging.info(f"No cached results found, trying uncached results")
                    torrent_info, magnet = self._process_results_with_mode(results, item_identifier, accept_uncached=True, item=item)
                
                updated_item = get_media_item_by_id(item['id'])
                if updated_item and updated_item.get('state') == 'Pending Uncached':
                    continue
                
                if (not torrent_info or not magnet) and (not updated_item or updated_item.get('state') != 'Pending Uncached'):
                    logging.error(f"No valid torrent info or magnet found for {item_identifier}")
                    # Only try to get torrent ID if torrent_info exists
                    item['torrent_id'] = torrent_info.get('id') if torrent_info else None
                    self._handle_failed_item(item, "No valid results found", queue_manager)
                    continue
                    
                files = torrent_info.get('files', [])

                filename_filter_out_list = get_setting('Debug', 'filename_filter_out_list', '')
                if filename_filter_out_list:
                    filters = [f.strip().lower() for f in filename_filter_out_list.split(',') if f.strip()]
                    logging.info(f"Applying filename filters: {filters}")
                    filtered_files = []
                    for file in files:
                        file_path = file['path'].lower()
                        should_keep = True
                        
                        for filter_term in filters:
                            if filter_term in file_path:
                                should_keep = False
                                logging.debug(f"Filtering out file: {file['path']} (matched filter: {filter_term})")
                                break
                                
                        if should_keep:
                            filtered_files.append(file)
                            
                    logging.info(f"Filtered {len(files) - len(filtered_files)} files out of {len(files)} total files")
                    files = filtered_files

                torrent_title = self.debrid_provider.get_cached_torrent_title(torrent_info.get('hash'))
                matches = self.media_matcher.match_content(files, item)
                
                if not matches:
                    logging.error(f"No matching files found in torrent for {item_identifier}")
                    item['torrent_id'] = torrent_info.get('id')
                    self._handle_failed_item(item, "No matching files found in torrent", queue_manager)
                    continue
                    
                matched_file = matches[0][0]  # First match's path
                logging.info(f"Best matching file for {item_identifier}: {matched_file}")
                
                original_scraped_torrent_title = torrent_info.get('original_scraped_torrent_title')
                resolution = None
                # Extract resolution from torrent info or scrape results
                if results and isinstance(results[0], dict):
                    best_result = results[0]
                    resolution = best_result.get('resolution')
                    logging.debug(f"Extracted resolution {resolution} from best result for {item_identifier}")

                update_media_item(item['id'], original_scraped_torrent_title=original_scraped_torrent_title, resolution=resolution)

                logging.info(f"Moving {item_identifier} to checking queue")
                queue_manager.move_to_checking(
                    item=item,
                    from_queue="Adding",
                    title=torrent_title,
                    link=magnet,
                    filled_by_file=matched_file,
                    torrent_id=torrent_info.get('id')
                )
                
                # Remove item from queue after successful processing
                logging.info(f"Removing successfully processed item {item_id} from adding queue")
                self.remove_item(item)
                
                if item.get('type') == 'episode':
                    scraping_items = queue_manager.get_scraping_items()
                    related_items = self.media_matcher.find_related_items(files, scraping_items, item)
                    
                    if related_items:
                        logging.info(f"Found {len(related_items)} related episodes")
                    
                    for related in related_items:
                        related_identifier = f"{related.get('title')} S{related.get('season_number')}E{related.get('episode_number')}"
                        related_matches = self.media_matcher.match_content(files, related)
                        if related_matches:
                            # Pass resolution to related episodes
                            resolution = None
                            if results and isinstance(results[0], dict):
                                best_result = results[0]
                                resolution = best_result.get('resolution')
                                logging.debug(f"Passing resolution {resolution} to related episode {related_identifier}")

                            update_media_item(related['id'], original_scraped_torrent_title=original_scraped_torrent_title, resolution=resolution)
                            related_file = related_matches[0][0]
                            logging.info(f"Moving related episode {related_identifier} to checking")
                            queue_manager.move_to_checking(
                                item=related,
                                from_queue="Scraping",
                                title=torrent_title,
                                link=magnet,
                                filled_by_file=related_file,
                                torrent_id=torrent_info.get('id')
                            )
                            
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
        try:
            torrent_info, magnet = self.torrent_processor.process_results(
                results,
                accept_uncached=accept_uncached,
                item=item
            )
            
            return torrent_info, magnet
        except Exception as e:
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
            if "No matching files found in torrent" in error:
                logging.info(f"Media matching failed for {item.get('title')}, moving back to Wanted queue")
                if 'torrent_id' in item:
                    self.remove_unwanted_torrent(item['torrent_id'])
                queue_manager.move_to_wanted(item, "Adding")
                return

            fall_back_to_single_scraper = get_media_item_by_id(item['id']).get('fall_back_to_single_scraper')
            if not fall_back_to_single_scraper:
                logging.info(f"Falling back to single scraper for {item.get('title')}")
                update_media_item(item['id'], fall_back_to_single_scraper=True)
                
                if item.get('type') == 'episode':
                    series_title = item.get('series_title', '') or item.get('title', '')
                    season = item.get('season') or item.get('season_number')
                    current_episode = item.get('episode') or item.get('episode_number')
                    version = item.get('version')
                    
                    if series_title and season is not None and current_episode is not None:
                        scraping_items = [dict(row) for row in get_all_media_items(state="Scraping")]
                        matching_items = [
                            i for i in scraping_items
                            if ((i.get('series_title', '') or i.get('title', '')) == series_title and
                                (i.get('season') or i.get('season_number')) == season and
                                (i.get('episode') or i.get('episode_number', 0)) > current_episode and
                                i.get('version') == version)
                        ]
                        
                        for match in matching_items:
                            update_media_item(match['id'], fall_back_to_single_scraper=True)
                
                queue_manager.move_to_scraping(item, "Adding")
                return

            release_date_str = item.get('release_date')
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
                    if release_date < datetime.now() - timedelta(days=7):
                        blacklist_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        update_media_item(
                            item['id'],
                            state='Blacklisted',
                            blacklisted_date=blacklist_date
                        )
                        
                        if item.get('type') == 'episode':
                            series_title = item.get('series_title', '') or item.get('title', '')
                            season = item.get('season') or item.get('season_number')
                            version = item.get('version')
                            
                            if series_title and season is not None and version:
                                scraping_items = [dict(row) for row in get_all_media_items(state="Scraping")]
                                if scraping_items:
                                    matching_items = [
                                        i for i in scraping_items
                                        if ((i.get('series_title', '') or i.get('title', '')) == series_title and
                                            (i.get('season') or i.get('season_number')) == season and
                                            i.get('version') == version)
                                    ]
                                    
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
            
            logging.info(f"Moving item to Sleeping state: {item.get('title')} ({item.get('type')})")
            update_media_item(
                item['id'],
                state='Sleeping'
            )
            self.remove_item(item)
            
        except Exception as e:
            logging.error(f"Error handling failed item: {str(e)}", exc_info=True)
    
    def get_new_item_values(self, item: Dict[str, Any]) -> Dict[str, Any]:
        updated_item = get_media_item_by_id(item['id'])

        if updated_item:
            new_values = {
                'filled_by_title': updated_item.get('filled_by_title'),
                'filled_by_magnet': updated_item.get('filled_by_magnet'),
                'filled_by_file': updated_item.get('filled_by_file'),
                'filled_by_torrent_id': updated_item.get('filled_by_torrent_id'),
                'version': updated_item.get('version'),
            }
            return new_values
        else:
            logging.warning(f"Could not retrieve updated item for ID {item['id']}")
            return {}