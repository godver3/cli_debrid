"""
Queue management for handling the addition of media items to a debrid service.
Orchestrates the process of adding content and managing queue state.
"""

import logging
import json
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
import time # Add time import

from debrid import get_debrid_provider
from utilities.settings import get_setting
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
        from database import get_all_media_items
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
                logging.debug(f"Attempted to remove item {item_id} but it was not in queue")
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
                        from database.not_wanted_magnets import add_to_not_wanted
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
                
    def process(self, queue_manager: Any, ignore_upgrade_lock: bool = False) -> bool:
        """
        Process items in the queue
        
        Args:
            queue_manager: Global queue manager instance
            ignore_upgrade_lock: If True, bypasses the check for upgrade locks.
            
        Returns:
            True if any items were processed successfully
        """
        if not self.items:
            return False
        
        from database import update_media_item, get_all_media_items, get_media_item_by_id

        success = False
        items_to_process = []
        for item in self.items[:]: # Iterate over a copy
             item_id = item.get('id')
             is_locked = False
             if not ignore_upgrade_lock:
                 if item_id and queue_manager and hasattr(queue_manager, 'upgrade_process_locks'):
                     try:
                         if item_id in queue_manager.upgrade_process_locks:
                             is_locked = True
                     except Exception as lock_check_err:
                         logging.error(f"Error checking upgrade lock set for item {item_id}: {lock_check_err}")

             if is_locked:
                 item_identifier_log = f"{item.get('title', 'N/A')} ({item.get('type', 'N/A')})"
                 logging.debug(f"[{item_identifier_log}] Skipping processing for item {item_id} - locked by upgrade process.")
                 continue

             items_to_process.append(item)

        if not items_to_process:
             logging.debug("Adding Queue: No items available to process this cycle (either empty or locked).")
             return False

        logging.info(f"Adding Queue - Starting processing of {len(items_to_process)} available items")
            
        # --- START EDIT: Get Wanted Queue items ONCE before the loop ---
        wanted_items = []
        try:
            if 'Wanted' in queue_manager.queues:
                wanted_items = queue_manager.queues['Wanted'].get_contents()
                logging.debug(f"Fetched {len(wanted_items)} items from Wanted queue for related item check.")
            else:
                logging.warning("Wanted queue not found in QueueManager.")
        except Exception as e:
            logging.error(f"Error getting Wanted queue contents: {e}")
        # --- END EDIT ---

        # --- START EDIT: Fetch setting from Queue section ---
        try:
            delay_seconds = float(get_setting('Queue', 'item_process_delay_seconds', 0.0))
        except (ValueError, TypeError):
            delay_seconds = 0.0
        # --- END EDIT ---

        for item in items_to_process:
            item_identifier = f"{item.get('title')} ({item.get('type')})"
            item_id = item.get('id')
            if not item_id:
                logging.warning(f"Skipping item without ID in AddingQueue: {item.get('title')}")
                continue
            logging.info(f"Processing item {item_id}: {item_identifier}")
            processed_this_item = False # Flag for applying delay

            try:
                # --- Load scrape_results ---
                results = item.get('scrape_results', [])
                if isinstance(results, str):
                    try:
                        results = json.loads(results)
                    except json.JSONDecodeError:
                        logging.error(f"Failed to parse scrape results JSON for {item_identifier}")
                        self._handle_failed_item(item, "Invalid scrape results format", queue_manager)
                        continue
                
                if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
                    logging.error(f"Scrape results are not in the expected format (list of dicts) for {item_identifier}")
                    self._handle_failed_item(item, "Invalid scrape results structure", queue_manager)
                    continue

                if not results:
                    logging.info(f"No scrape results found for {item_identifier}")
                    self._handle_failed_item(item, "No results found", queue_manager)
                    continue

                logging.info(f"Found {len(results)} scrape results for {item_identifier}")

                # Add original_scraped_torrent_title to results if missing (important for later matching)
                for result in results:
                     if 'original_scraped_torrent_title' not in result:
                         result['original_scraped_torrent_title'] = result.get('original_title')

                # --- Select Torrent (cached/uncached logic) ---
                if get_setting('Scraping', 'uncached_content_handling', 'None') == 'None':
                    accept_uncached = False
                elif get_setting('Scraping', 'uncached_content_handling') == 'Full':
                    accept_uncached = True
                else: # Hybrid mode is the default if not None or Full
                    accept_uncached = False # Start with cached only for hybrid

                torrent_info, magnet = self._process_results_with_mode(results, item_identifier, accept_uncached, item=item)

                if not torrent_info and not magnet and get_setting('Scraping', 'hybrid_mode', True): # Check hybrid setting explicitly
                    logging.info(f"No cached results found, trying uncached results (Hybrid Mode)")
                    accept_uncached = True # Now accept uncached
                    torrent_info, magnet = self._process_results_with_mode(results, item_identifier, accept_uncached=True, item=item)

                updated_item = get_media_item_by_id(item['id']) # Check state after processing
                if updated_item and updated_item.get('state') == 'Pending Uncached':
                    logging.info(f"Item {item_id} moved to Pending Uncached state. Skipping further processing in Adding queue.")
                    self.remove_item(item) # Remove from memory queue
                    continue # Move to next item

                if (not torrent_info or not magnet): # Check again after potential uncached attempt
                    logging.error(f"No valid torrent info or magnet found for {item_identifier} after checking cache/uncached modes.")
                    # Try to get torrent_id for potential removal in handle_failed_item
                    if torrent_info and torrent_info.get('id'):
                       item['torrent_id'] = torrent_info.get('id')
                    self._handle_failed_item(item, "No valid results found after cache/uncached processing", queue_manager)
                    continue

                # --- Process Files and Match ---
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

                # --- Apply XEM Mapping for Matching ---
                target_season = item.get('season_number')
                target_episode = item.get('episode_number')
                best_result_for_mapping = results[0] if results else None # Best result is used for mapping hint
                scene_mapping_applied = False
                primary_absolute_season = item.get('season_number')
                primary_absolute_episode = item.get('episode_number')
                primary_scene_season = None
                primary_scene_episode = None

                if item.get('type') == 'episode' and best_result_for_mapping:
                    scene_mapping = best_result_for_mapping.get('xem_scene_mapping')
                    if scene_mapping:
                        mapped_season = scene_mapping.get('season')
                        mapped_episode = scene_mapping.get('episode')
                        if mapped_season is not None and mapped_episode is not None:
                            logging.info(f"Using XEM mapping S{mapped_season}E{mapped_episode} for primary media matching (original S{primary_absolute_season}E{primary_absolute_episode}).")
                            target_season = mapped_season
                            target_episode = mapped_episode
                            scene_mapping_applied = True
                            primary_scene_season = mapped_season
                            primary_scene_episode = mapped_episode
                        else:
                            logging.warning("Found xem_scene_mapping in result, but season or episode was missing.")
                
                item_for_matching = item.copy()
                if item.get('type') == 'episode':
                    item_for_matching['season_number'] = target_season
                    item_for_matching['episode_number'] = target_episode
                    
                torrent_title = self.debrid_provider.get_cached_torrent_title(torrent_info.get('hash'))
                matches = self.media_matcher.match_content(files, item_for_matching)
                
                if not matches:
                    logging.error(f"No matching files found in torrent for {item_identifier}")
                    item['torrent_id'] = torrent_info.get('id') # Ensure torrent_id is set for removal
                    self._handle_failed_item(item, "No matching files found in torrent", queue_manager)
                    continue
                    
                matched_file = matches[0][0]
                logging.info(f"Best matching file for {item_identifier}: {matched_file}")
                
                # --- START: Extract Score and Update DB ---
                current_score = 0 # Default score
                chosen_result_info = None

                # Find the result corresponding to the chosen torrent_info (match by magnet or hash)
                chosen_hash = torrent_info.get('hash', '').lower()
                if magnet and magnet.startswith('magnet:'):
                    chosen_magnet = magnet.lower()
                else:
                    chosen_magnet = magnet or ''

                for result in results:
                    result_magnet_raw = result.get('magnet', '')
                    if result_magnet_raw.startswith('magnet:'):
                        result_magnet = result_magnet_raw.lower()
                    else:
                        result_magnet = result_magnet_raw
                    # Attempt to extract hash from result magnet for comparison
                    from debrid.common import extract_hash_from_magnet
                    result_hash = extract_hash_from_magnet(result_magnet) if result_magnet else None

                    # Match primarily by hash if available, fallback to full magnet URI
                    if chosen_hash and result_hash and chosen_hash == result_hash:
                        chosen_result_info = result
                        break
                    elif chosen_magnet and result_magnet and chosen_magnet == result_magnet:
                        chosen_result_info = result
                        break

                score_breakdown_debug = None # Variable for logging
                if chosen_result_info:
                    score_breakdown_debug = chosen_result_info.get('score_breakdown', {})
                    current_score = score_breakdown_debug.get('total_score', 0) # Use the debug var here too
                    logging.info(f"Extracted score {current_score:.2f} for the chosen torrent.")
                    # Extract other info from the chosen result
                    original_scraped_torrent_title = chosen_result_info.get('original_scraped_torrent_title') or chosen_result_info.get('original_title')
                    resolution = chosen_result_info.get('resolution')
                else:
                    # Fallback: Try to get info from the first result if no match found (less accurate)
                    if results:
                         first_result = results[0]
                         score_breakdown_debug = first_result.get('score_breakdown', {})
                         current_score = score_breakdown_debug.get('total_score', 0) # Use the debug var here too
                         original_scraped_torrent_title = first_result.get('original_scraped_torrent_title') or first_result.get('original_title')
                         resolution = first_result.get('resolution')
                         logging.warning(f"Could not definitively match chosen torrent to scrape results. Using score {current_score:.2f} from the first result as fallback.")
                    else:
                         original_scraped_torrent_title = None
                         resolution = None
                         logging.warning(f"Could not find any result to extract score from for {item_identifier}. Defaulting score to 0.")


                # Update the item in the database with score and other details BEFORE moving state
                update_data = {
                    'current_score': current_score,
                    'original_scraped_torrent_title': original_scraped_torrent_title,
                    'resolution': resolution
                }
                # --- DEBUG LOGGING START ---
                logging.info(f"DEBUG: Preparing to update item {item_id}. Current Score Type: {type(current_score)}, Value: {current_score}")
                if score_breakdown_debug is not None:
                    logging.info(f"DEBUG: Score breakdown type: {type(score_breakdown_debug)}")
                    logging.info(f"DEBUG: Score breakdown content keys: {list(score_breakdown_debug.keys())}")
                    logging.info(f"DEBUG: Score breakdown total_score value: {score_breakdown_debug.get('total_score')}")
                else:
                    logging.warning(f"DEBUG: Score breakdown was None before update for item {item_id}")
                # --- DEBUG LOGGING END ---
                logging.info(f"Updating item {item_id} with score and details: {update_data}")
                update_media_item(item['id'], **update_data)
                # --- END: Extract Score and Update DB ---

                logging.info(f"Moving {item_identifier} to checking queue")
                queue_manager.move_to_checking(
                    item=item,
                    from_queue="Adding",
                    title=torrent_title,
                    link=magnet,
                    filled_by_file=matched_file,
                    torrent_id=torrent_info.get('id')
                )
                
                logging.info(f"Removing successfully processed item {item_id} from adding queue")
                self.remove_item(item) # Remove from memory
                
                # --- Process Related Items ---
                if item.get('type') == 'episode':
                    # Get Scraping items
                    scraping_items = []
                    try:
                         if 'Scraping' in queue_manager.queues:
                             scraping_items = queue_manager.queues['Scraping'].get_contents()
                         else:
                              logging.warning("Scraping queue not found in QueueManager for related item check.")
                    except Exception as e:
                         logging.error(f"Error getting Scraping queue contents for related item check: {e}")

                    # --- START EDIT: Pass Wanted items to find_related_items ---
                    related_items = self.media_matcher.find_related_items(
                        files,
                        scraping_items, # Still pass scraping items
                        wanted_items, # Pass the wanted_items list fetched earlier
                        item
                    )
                    # --- END EDIT ---

                    if related_items:
                        logging.info(f"Found {len(related_items)} related episodes (from Scraping and/or Wanted)")
                    
                    for related in related_items:
                        related_identifier = f"{related.get('title')} S{related.get('season_number')}E{related.get('episode_number')}"
                        related_item_state = related.get('state', 'Unknown')

                        # --- Apply XEM mapping to related items ---
                        related_item_for_matching = related.copy()
                        
                        if scene_mapping_applied and primary_scene_season is not None and primary_scene_episode is not None:
                            related_absolute_season = related.get('season_number')
                            related_absolute_episode = related.get('episode_number')
                            
                            if related_absolute_season == primary_absolute_season and related_absolute_episode is not None and primary_absolute_episode is not None:
                                episode_offset = related_absolute_episode - primary_absolute_episode
                                inferred_related_scene_episode = primary_scene_episode + episode_offset
                                inferred_related_scene_season = primary_scene_season
                                
                                logging.debug(f"Inferred scene mapping S{inferred_related_scene_season}E{inferred_related_scene_episode} for related item {related_identifier} (original S{related_absolute_season}E{related_absolute_episode})")
                                related_item_for_matching['season_number'] = inferred_related_scene_season
                                related_item_for_matching['episode_number'] = inferred_related_scene_episode
                            else:
                                logging.warning(f"Cannot infer scene mapping for related item {related_identifier} (season mismatch or missing episode). Using absolute S{related_absolute_season}E{related_absolute_episode}.")
                        related_matches = self.media_matcher.match_content(files, related_item_for_matching)
                        if related_matches:
                            # --- START: Update related item score/details ---
                            # Use the same score/details derived from the chosen torrent for the primary item
                            related_update_data = {
                                'current_score': current_score,
                                'original_scraped_torrent_title': original_scraped_torrent_title,
                                'resolution': resolution
                            }
                            logging.info(f"Updating related item {related['id']} with score and details: {related_update_data}")
                            update_media_item(related['id'], **related_update_data)
                            # --- END: Update related item score/details ---

                            related_file = related_matches[0][0]
                            logging.info(f"Moving related episode {related_identifier} (from {related_item_state}) to checking")
                            queue_manager.move_to_checking(
                                item=related,
                                from_queue=related_item_state,
                                title=torrent_title,
                                link=magnet,
                                filled_by_file=related_file,
                                torrent_id=torrent_info.get('id')
                            )
                            # Note: move_to_checking handles removing the related item from its original queue (Scraping or Wanted)

                success = True
                
            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                # Try to get torrent_id for potential removal in handle_failed_item
                if 'torrent_info' in locals() and torrent_info and torrent_info.get('id'):
                    item['torrent_id'] = torrent_info.get('id')
                self._handle_failed_item(item, f"Processing error: {str(e)}", queue_manager)
                
            # --- START EDIT: Add delay within the loop ---
            finally: # Use finally to ensure delay happens even after errors if item was processed
                if processed_this_item and delay_seconds > 0:
                    logging.debug(f"Adding Queue: Applying {delay_seconds}s delay after processing item {item_id}.")
                    time.sleep(delay_seconds)
            # --- END EDIT ---

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
            try:
                # Avoid circular import if QueueManager is needed here
                # Assuming QueueManager instance is passed if needed, or accessed globally
                # For now, just log the error and return None
                # Example: Access via singleton if available: from queues.queue_manager import QueueManager; queue_manager = QueueManager()
                # self._handle_failed_item(item, f"Error checking cache status: {str(e)}", queue_manager) # Need queue_manager instance here
                logging.error(f"Cannot call _handle_failed_item from _process_results_with_mode without QueueManager instance.")
            except Exception as qm_err:
                logging.error(f"Failed to get QueueManager instance for error handling: {qm_err}")
            return None, None

    def _handle_failed_item(self, item: Dict, error: str, queue_manager: Any):
        """
        Handle a failed item by moving it back to Wanted queue if media matching failed,
        or to Sleeping/Blacklisted state for other failures, correctly handling upgrades.
        
        Args:
            item: The media item that failed
            error: Error message describing why it failed
            queue_manager: Global queue manager instance
        """
        from database import get_media_item_by_id, update_media_item
        from queues.upgrading_queue import UpgradingQueue
        from routes.notifications import send_upgrade_failed_notification

        item_identifier = queue_manager.generate_identifier(item)
        is_upgrade = item.get('upgrading') or item.get('upgrading_from') is not None
        upgrading_queue = None
        item_id = item.get('id') # Get item ID early

        try:
            if is_upgrade:
                logging.warning(f"Handling failed upgrade for {item_identifier}: {error}")
                # Create instance only if needed
                upgrading_queue = UpgradingQueue()

                notification_data = {
                    'title': item.get('title', 'Unknown Title'),
                    'year': item.get('year', ''),
                    'reason': f'Adding Queue Failure: {error}'
                }
                send_upgrade_failed_notification(notification_data)

                upgrading_queue.log_failed_upgrade(
                    item,
                    item.get('filled_by_title', 'Unknown'),
                    f'Adding Queue Failure: {error}'
                )

                if upgrading_queue.restore_item_state(item):
                    failed_info = {
                        'title': item.get('filled_by_title'),
                        'magnet': item.get('filled_by_magnet'),
                        'torrent_id': item.get('torrent_id'),
                        'reason': f'adding_queue_error: {error}'
                    }
                    # Safely attempt to get magnet from scrape results
                    if item.get('scrape_results'):
                        try:
                            results = json.loads(item['scrape_results']) if isinstance(item['scrape_results'], str) else item['scrape_results']
                            if results and isinstance(results, list) and len(results) > 0 and isinstance(results[0], dict):
                                failed_info['magnet'] = results[0].get('magnet')
                        except Exception as json_err:
                            logging.warning(f"Could not extract magnet from scrape_results during failure handling: {json_err}")

                    upgrading_queue.add_failed_upgrade(item['id'], failed_info)
                    logging.info(f"Successfully reverted failed upgrade for {item_identifier}")
                else:
                    logging.error(f"Failed to restore previous state for {item_identifier} after adding queue failure")
                    # Decide if item should be removed or left in Adding state on restoration failure
                    self.remove_item(item) # Remove from Adding queue memory regardless
                return # Exit after handling upgrade failure

            # --- Non-upgrade failure handling ---

            if "No matching files found in torrent" in error:
                logging.info(f"Media matching failed for {item_identifier}, moving back to Wanted queue")
                # Remove torrent if ID is present
                if item.get('torrent_id'):
                    self.remove_unwanted_torrent(item['torrent_id'])
                queue_manager.move_to_wanted(item, "Adding")
                # No need to call self.remove_item here, move_to_wanted handles it
                return

            # --- Fallback to single scraper logic ---
            current_item_data = get_media_item_by_id(item_id) if item_id else None
            fall_back_to_single_scraper = current_item_data.get('fall_back_to_single_scraper') if current_item_data else False

            if not fall_back_to_single_scraper and get_setting('Scraping', 'fallback_to_single_enabled', default=True):
                logging.info(f"Falling back to single scraper for {item_identifier}")
                if item_id: update_media_item(item_id, fall_back_to_single_scraper=True)

                # Update related items (only if item ID exists)
                if item_id and item.get('type') == 'episode':
                    series_title = item.get('series_title', '') or item.get('title', '')
                    season = item.get('season') or item.get('season_number')
                    current_episode = item.get('episode') or item.get('episode_number')
                    version = item.get('version')

                    if series_title and season is not None and current_episode is not None:
                        from database import get_all_media_items # Local import
                        all_items = [dict(row) for row in get_all_media_items(state=None)] # Fetch all items
                        matching_items = [
                            i for i in all_items
                            if ((i.get('series_title', '') or i.get('title', '')) == series_title and
                                (i.get('season') or i.get('season_number')) == season and
                                (i.get('episode') or i.get('episode_number', -1)) > current_episode and # Use -1 default?
                                i.get('version') == version)
                        ]

                        for match in matching_items:
                            match_id = match.get('id')
                            if match_id and not match.get('fall_back_to_single_scraper'):
                                update_media_item(match_id, fall_back_to_single_scraper=True)
                                logging.debug(f"Enabled single scraper fallback for related item ID: {match_id} ({match.get('title')})")


                queue_manager.move_to_scraping(item, "Adding")
                # No need to call self.remove_item here, move_to_scraping handles it
                return

            # --- Blacklisting logic for old items ---
            release_date_str = item.get('release_date')
            if release_date_str and release_date_str != 'Unknown':
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
                    if release_date < datetime.now() - timedelta(days=get_setting('Queue', 'adding_failure_blacklist_days', 30)):
                        blacklist_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        logging.warning(f"Blacklisting old item {item_identifier} due to adding failure: {error}")
                        # Call move_to_blacklisted instead of direct DB update
                        queue_manager.move_to_blacklisted(item, "Adding")

                        # Blacklist related items if applicable
                        if item_id and item.get('type') == 'episode':
                            series_title = item.get('series_title', '') or item.get('title', '')
                            season = item.get('season') or item.get('season_number')
                            version = item.get('version')

                            if series_title and season is not None: # Version match handled implicitly by original item context?
                                from database import get_all_media_items # Local import
                                all_items = [dict(row) for row in get_all_media_items(state=None)]
                                if all_items:
                                    matching_items = [
                                        i for i in all_items
                                        if (i.get('id') != item_id and # Don't re-blacklist original
                                            (i.get('series_title', '') or i.get('title', '')) == series_title and
                                            (i.get('season') or i.get('season_number')) == season and
                                            i.get('version') == version and
                                            i.get('state') not in ('Blacklisted', 'Collected') ) # Avoid already blacklisted/collected
                                    ]

                                    for match in matching_items:
                                        related_item_state = match.get('state', 'Unknown')
                                        logging.info(f"Blacklisting related episode ID: {match.get('id')} ({match.get('title')}) from state {related_item_state}")
                                        queue_manager.move_to_blacklisted(match, related_item_state)

                        # Item is removed from Adding queue by move_to_blacklisted
                        return
                except ValueError:
                    logging.error(f"Invalid release date format '{release_date_str}' for {item_identifier} during failure handling")
                except Exception as e:
                    logging.error(f"Error during blacklist check for {item_identifier}: {e}", exc_info=True)

            # --- Default: Move to Sleeping ---
            logging.warning(f"Moving item to Sleeping state due to adding failure: {item_identifier} - {error}")
            queue_manager.move_to_sleeping(item, "Adding")
            # Item is removed from Adding queue by move_to_sleeping

        except Exception as e:
            logging.error(f"Critical error in _handle_failed_item for {item.get('id', 'Unknown ID')}: {str(e)}", exc_info=True)
            # Ensure item is removed from memory queue even on critical failure
            if item_id:
                 self.items = [i for i in self.items if i['id'] != item_id]

    def get_new_item_values(self, item: Dict[str, Any]) -> Dict[str, Any]:
        from database import get_media_item_by_id
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

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)