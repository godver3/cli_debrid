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
        self.media_matcher = MediaMatcher(relaxed_matching=get_setting('Matching', 'relaxed_matching', False))
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
            
        hash_value = None # Initialize hash_value
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
            
            # Update tracking record with adding error (use hash if available)
            if hash_value:
                try:
                    update_adding_error(hash_value)
                except Exception as e:
                    logging.error(f"Failed to update tracking record for adding error: {str(e)}")
            else:
                 logging.warning(f"Could not update adding error count as hash was not found for torrent {torrent_id}")
            
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
        scraping_items = []
        try:
            if 'Wanted' in queue_manager.queues:
                wanted_items = queue_manager.queues['Wanted'].get_contents()
                logging.debug(f"Fetched {len(wanted_items)} items from Wanted queue for related item check.")
            else:
                logging.warning("Wanted queue not found in QueueManager.")
            if 'Scraping' in queue_manager.queues:
                 scraping_items = queue_manager.queues['Scraping'].get_contents()
                 logging.debug(f"Fetched {len(scraping_items)} items from Scraping queue for related item check.")
            else:
                 logging.warning("Scraping queue not found in QueueManager.")
        except Exception as e:
            logging.error(f"Error getting Wanted/Scraping queue contents: {e}")
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
                accept_uncached_within_hours = int(get_setting('Scraping', 'accept_uncached_within_hours', 0))
                accept_uncached = False
                # Determine if we should accept uncached based on recency
                logging.info(f"Accepting uncached within {accept_uncached_within_hours} hours")
                if accept_uncached_within_hours > 0:
                    release_date_str = item.get('release_date')
                    airtime_str = item.get('airtime')
                    release_datetime = None
                    if release_date_str and release_date_str != 'Unknown':
                        try:
                            release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                            if airtime_str:
                                try:
                                    try:
                                        airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                                    except ValueError:
                                        airtime = datetime.strptime(airtime_str, '%H:%M').time()
                                except ValueError:
                                    airtime = datetime.strptime("00:00", '%H:%M').time()
                            else:
                                airtime = datetime.strptime("00:00", '%H:%M').time()
                            release_datetime = datetime.combine(release_date, airtime)
                            # Apply offset based on type
                            offset_hours = 0.0
                            if item.get('type') == 'movie':
                                offset_setting = get_setting("Queue", "movie_airtime_offset", "19")
                                try:
                                    offset_hours = float(offset_setting)
                                except (ValueError, TypeError):
                                    offset_hours = 19.0
                            elif item.get('type') == 'episode':
                                offset_setting = get_setting("Queue", "episode_airtime_offset", "0")
                                try:
                                    offset_hours = float(offset_setting)
                                except (ValueError, TypeError):
                                    offset_hours = 0.0
                            release_datetime += timedelta(hours=offset_hours)
                            now = datetime.now()
                            hours_since_release = (now - release_datetime).total_seconds() / 3600.0
                            logging.info(f"Hours since release: {hours_since_release}")
                            if 0 <= hours_since_release <= accept_uncached_within_hours:
                                logging.info(f"Accepting uncached release for {item_identifier} because it was released within the last {accept_uncached_within_hours} hours")
                                accept_uncached = True
                        except Exception:
                            pass
                # If not set by recency, use normal uncached_content_handling
                if not accept_uncached:
                    if get_setting('Scraping', 'uncached_content_handling', 'None') == 'None':
                        accept_uncached = False
                    elif get_setting('Scraping', 'uncached_content_handling') == 'Full':
                        accept_uncached = True
                    else: # Hybrid mode is the default if not None or Full
                        accept_uncached = False # Start with cached only for hybrid

                # Now returns torrent_info, magnet, and chosen_result_info
                torrent_info, magnet, chosen_result_info = self._process_results_with_mode(
                    results, item_identifier, accept_uncached, item=item
                )

                if not torrent_info and not magnet and get_setting('Scraping', 'hybrid_mode', True): # Check hybrid setting explicitly
                    logging.info(f"No cached results found, trying uncached results (Hybrid Mode)")
                    accept_uncached = True # Now accept uncached
                    # Call again, getting all three return values
                    torrent_info, magnet, chosen_result_info = self._process_results_with_mode(
                        results, item_identifier, accept_uncached=True, item=item
                    )

                updated_item = get_media_item_by_id(item['id']) # Check state after processing
                if updated_item and updated_item.get('state') == 'Pending Uncached':
                    logging.info(f"Item {item_id} moved to Pending Uncached state. Skipping further processing in Adding queue.")
                    self.remove_item(item) # Remove from memory queue
                    continue # Move to next item

                # Use torrent_info and magnet for the check, chosen_result_info is handled later
                if (not torrent_info or not magnet): # Check again after potential uncached attempt
                    logging.error(f"No valid torrent info or magnet found for {item_identifier} after checking cache/uncached modes.")
                    if torrent_info and torrent_info.get('id'):
                       item['torrent_id'] = torrent_info.get('id')
                    elif chosen_result_info:
                        pass
                    self._handle_failed_item(item, "No valid results found after cache/uncached processing", queue_manager)
                    continue

                # --- Apply filename filters ---
                filename_filter_out_list = get_setting('Debug', 'filename_filter_out_list', '')
                filters = []
                if filename_filter_out_list:
                    filters = [f.strip().lower() for f in filename_filter_out_list.split(',') if f.strip()]

                # 1. Filter torrent's original_filename
                original_torrent_filename = torrent_info.get('original_filename')
                if original_torrent_filename and filters:
                    original_torrent_filename_lower = original_torrent_filename.lower()
                    if any(filter_term in original_torrent_filename_lower for filter_term in filters):
                        logging.warning(f"Torrent's original_filename '{original_torrent_filename}' matches filter list: {filters}. Rejecting entire torrent.")
                        item['torrent_id'] = torrent_info.get('id')
                        self._handle_failed_item(item, f"Torrent's original name '{original_torrent_filename}' matched filter-out list", queue_manager)
                        processed_this_item = True
                        continue

                # 2. Determine and filter the potential torrent title (source for filled_by_title)
                potential_torrent_title_from_info = torrent_info.get('title')
                if not potential_torrent_title_from_info and chosen_result_info:
                    potential_torrent_title_from_info = chosen_result_info.get('title')
                
                if potential_torrent_title_from_info and filters:
                    potential_torrent_title_lower = potential_torrent_title_from_info.lower()
                    if any(filter_term in potential_torrent_title_lower for filter_term in filters):
                        logging.warning(f"Torrent's determined title '{potential_torrent_title_from_info}' matches filter list: {filters}. Rejecting torrent.")
                        item['torrent_id'] = torrent_info.get('id')
                        self._handle_failed_item(item, f"Torrent's title '{potential_torrent_title_from_info}' matched filter-out list", queue_manager)
                        processed_this_item = True
                        continue

                # If we reach here, original_filename and potential_torrent_title are acceptable.
                # The original_torrent_filename variable already holds the vetted name for 'real_debrid_original_title'.
                # The logic later that determines 'torrent_title' for move_to_checking will use the vetted title parts.

                # --- Process Files (Parse Once) ---
                raw_files = torrent_info.get('files', [])
                logging.debug(f"Got {len(raw_files)} raw files from torrent info.")

                # Apply filename filters to individual files (filters list is already prepared from above)
                filtered_raw_files = []
                if filters: # Only filter if filters were defined
                    logging.info(f"Applying filename filters to individual files: {filters}")
                    for file in raw_files:
                        file_path_lower = file['path'].lower()
                        if not any(filter_term in file_path_lower for filter_term in filters):
                            filtered_raw_files.append(file)
                        else:
                             logging.debug(f"Filtering out individual file: {file['path']} due to match with: {filters}")
                    logging.info(f"Filtered {len(raw_files) - len(filtered_raw_files)} files out of {len(raw_files)} total raw files (individual file filtering)")
                else:
                    filtered_raw_files = raw_files # No filter applied, or filters list was empty

                # Log the files before attempting to parse them
                logging.debug(f"Files being considered for parsing (filtered_raw_files) for {item_identifier}: {filtered_raw_files}")

                # Parse the filtered files ONCE
                parsed_torrent_files = []
                for file_dict in filtered_raw_files:
                     parsed_info = self.media_matcher._parse_file_info(file_dict)
                     if parsed_info:
                         parsed_torrent_files.append(parsed_info)
                logging.info(f"Parsed {len(parsed_torrent_files)} valid video files from torrent.")

                if not parsed_torrent_files:
                    logging.error(f"No valid video files found in torrent after parsing/filtering for {item_identifier}")
                    # The log here is redundant if the one above captures the content of filtered_raw_files correctly.
                    # If filtered_raw_files was empty, the log above would show that.
                    # If it had files but parsing yielded nothing, the log above shows what was attempted.
                    item['torrent_id'] = torrent_info.get('id') # Ensure torrent_id is set for removal
                    self._handle_failed_item(item, "No valid video files found in torrent", queue_manager)
                    continue

                # --- Extract Score and Update DB (Use chosen_result_info directly) ---
                current_score = 0
                # chosen_result_info is now directly available from _process_results_with_mode
                # Remove the loop that tries to find it again
                # chosen_hash = torrent_info.get('hash', '').lower() # Keep hash for logging/debugging if needed
                # chosen_magnet = magnet.lower() if magnet and magnet.startswith('magnet:') else magnet or ''
                # --- REMOVED LOOP: for result in results: ... ---

                original_scraped_torrent_title = None
                resolution = None
                xem_mapping = None # Initialize xem_mapping

                if chosen_result_info:
                    # Now directly use the chosen_result_info returned by _process_results_with_mode
                    score_breakdown_debug = chosen_result_info.get('score_breakdown', {})
                    current_score = score_breakdown_debug.get('total_score', 0)
                    original_scraped_torrent_title = chosen_result_info.get('original_scraped_torrent_title') or chosen_result_info.get('original_title')
                    resolution = chosen_result_info.get('resolution')
                    xem_mapping = chosen_result_info.get('xem_scene_mapping') # Extract XEM mapping directly
                    logging.info(f"Using chosen result: Score {current_score:.2f}, XEM mapping {xem_mapping}")
                else:
                    # This fallback should ideally not be needed if _process_results returns None on failure
                    # But keep it as a safeguard, although XEM mapping won't be available here
                    logging.warning(f"Could not obtain chosen_result_info directly. Falling back to first result for score/details (XEM unavailable).")
                    if results:
                         first_result = results[0]
                         score_breakdown_debug = first_result.get('score_breakdown', {})
                         current_score = score_breakdown_debug.get('total_score', 0)
                         original_scraped_torrent_title = first_result.get('original_scraped_torrent_title') or first_result.get('original_title')
                         resolution = first_result.get('resolution')
                         # xem_mapping remains None here
                    else:
                         logging.warning(f"No results available for fallback score/details.")


                update_data = {
                    'current_score': current_score,
                    'original_scraped_torrent_title': original_scraped_torrent_title,
                    'resolution': resolution,
                    'real_debrid_original_title': original_torrent_filename # Use the original_torrent_filename that passed the filter
                }
                logging.info(f"Updating item {item_id} with score and details: {update_data}")
                update_media_item(item['id'], **update_data)
                # --- END Score Update ---

                # --- Determine Torrent Title ---
                # This will now use the parts (torrent_info.get('title'), chosen_result_info.get('title')) that have implicitly passed the filter
                torrent_title = torrent_info.get('title') 
                if not torrent_title and chosen_result_info: 
                    torrent_title = chosen_result_info.get('title')
                if not torrent_title: 
                    torrent_title = "Unknown Torrent Title"


                # --- Apply XEM Mapping (Simplified - needs review for robustness) ---
                # XEM mapping is now available in the `xem_mapping` variable extracted above.
                item_for_matching = item # Use original item details for primary match

                # --- Find Primary Match using Parsed Files ---
                # Pass the extracted xem_mapping (which might be None) to the matcher
                match_result = self.media_matcher.find_best_match_from_parsed(
                    parsed_torrent_files,
                    item_for_matching,
                    xem_mapping=xem_mapping # Pass the extracted mapping
                )

                if not match_result:
                    logging.error(f"No matching files found in parsed files for primary item {item_identifier} (XEM used: {xem_mapping is not None})")
                    item['torrent_id'] = torrent_info.get('id') # Ensure torrent_id is set for removal
                    # Update error message slightly
                    self._handle_failed_item(item, f"No matching files found in torrent (parsed, XEM used: {xem_mapping is not None})", queue_manager)
                    continue

                matched_file_basename = match_result[0] # Now contains basename
                logging.info(f"Best matching file (basename) for {item_identifier}: {matched_file_basename}")


                # --- Move Primary Item to Checking ---
                logging.info(f"Moving {item_identifier} to checking queue")
                queue_manager.move_to_checking(
                    item=item,
                    from_queue="Adding",
                    title=torrent_title, # This title has now been effectively filtered
                    link=magnet,
                    filled_by_file=matched_file_basename, 
                    torrent_id=torrent_info.get('id')
                )
                processed_this_item = True # Mark primary item as processed for delay logic

                logging.info(f"Removing successfully processed item {item_id} from adding queue memory")
                self.remove_item(item) # Remove from memory

                # --- Process Related Items using Parsed Files ---
                if item.get('type') == 'episode':
                    # Pass the pre-parsed files to find_related_items
                    # TODO: How should XEM affect related item matching? Currently uses item's absolute S/E.
                    related_matches = self.media_matcher.find_related_items(
                        parsed_torrent_files,
                        scraping_items, # Fetched before loop
                        wanted_items,   # Fetched before loop
                        item            # Original item for context
                    )

                    if related_matches:
                        logging.info(f"Found {len(related_matches)} related episodes matching parsed files (from Scraping and/or Wanted)")

                        for related_item, related_file_basename in related_matches:
                            related_identifier = f"{related_item.get('title')} S{related_item.get('season_number')}E{related_item.get('episode_number')}"
                            related_item_state = related_item.get('state', 'Unknown')

                            # Update related item score/details (use same score as primary)
                            related_update_data = {
                                'current_score': current_score,
                                'original_scraped_torrent_title': original_scraped_torrent_title,
                                'resolution': resolution
                                # Note: We are NOT applying XEM mapping specifically to related items here
                            }
                            logging.info(f"Updating related item {related_item['id']} with score and details: {related_update_data}")
                            update_media_item(related_item['id'], **related_update_data)

                            logging.info(f"Moving related episode {related_identifier} (from {related_item_state}) to checking")
                            queue_manager.move_to_checking(
                                item=related_item,
                                from_queue=related_item_state,
                                title=torrent_title,
                                link=magnet,
                                filled_by_file=related_file_basename, # Pass the basename
                                torrent_id=torrent_info.get('id')
                            )
                            # move_to_checking handles removal from original queue (Scraping/Wanted)

                success = True # Mark overall success if primary item processed

            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                if 'torrent_info' in locals() and torrent_info and torrent_info.get('id'):
                    item['torrent_id'] = torrent_info.get('id')
                self._handle_failed_item(item, f"Processing error: {str(e)}", queue_manager)

            finally: # Apply delay if an item was processed (successfully or moved by error handling)
                if processed_this_item and delay_seconds > 0:
                    logging.debug(f"Adding Queue: Applying {delay_seconds}s delay after processing item {item_id}.")
                    time.sleep(delay_seconds)

        return success

    def _process_results_with_mode(self, results: List[Dict], item_identifier: str, accept_uncached: bool, item: Dict) -> Tuple[Optional[Dict], Optional[str], Optional[Dict]]:
        """
        Process results with specific caching mode
        
        Args:
            results: List of results to process
            item_identifier: Identifier string for logging
            accept_uncached: Whether to accept uncached results
            item: Media item being processed
            
        Returns:
            Tuple of (torrent_info, magnet_link, chosen_result) if successful, (None, None, None) otherwise
        """
        try:
            # Call process_results which now returns chosen_result as well
            torrent_info, magnet, chosen_result = self.torrent_processor.process_results(
                results,
                accept_uncached=accept_uncached,
                item=item
            )

            return torrent_info, magnet, chosen_result # Return all three
        except Exception as e:
            logging.error(f"Error processing results for {item_identifier}: {str(e)}", exc_info=True) # Added exc_info
            # Cannot call _handle_failed_item reliably without queue_manager instance here
            logging.error(f"Cannot call _handle_failed_item from _process_results_with_mode directly.")
            return None, None, None # Return None for all three on error

    def _handle_failed_item(self, item: Dict, error: str, queue_manager: Any):
        """
        Handle a failed item by moving it back to Wanted queue if media matching failed,
        or to Sleeping/Blacklisted state for other failures, correctly handling upgrades.
        (Keep existing logic, but note the matching error messages might change slightly)
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
                upgrading_queue = UpgradingQueue() # Create instance only if needed

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
                        'torrent_id': item.get('torrent_id'), # Make sure this is set before failure
                        'reason': f'adding_queue_error: {error}'
                    }
                    # Safely attempt to get magnet from scrape results
                    if item.get('scrape_results'):
                        try:
                            results_json = json.loads(item['scrape_results']) if isinstance(item['scrape_results'], str) else item['scrape_results']
                            if results_json and isinstance(results_json, list) and len(results_json) > 0 and isinstance(results_json[0], dict):
                                failed_info['magnet'] = results_json[0].get('magnet')
                        except Exception as json_err:
                            logging.warning(f"Could not extract magnet from scrape_results during failure handling: {json_err}")

                    upgrading_queue.add_failed_upgrade(item['id'], failed_info)
                    logging.info(f"Successfully reverted failed upgrade for {item_identifier}")
                else:
                    logging.error(f"Failed to restore previous state for {item_identifier} after adding queue failure")

                # --- START EDIT: Check if failure was due to filter and remove torrent ---
                if "matched filter-out list" in error:
                    logging.info(f"Upgrade for {item_identifier} failed due to filename filter. Attempting to remove torrent.")
                    if item.get('torrent_id'):
                        self.remove_unwanted_torrent(item['torrent_id'])
                # --- END EDIT ---

                # Remove from Adding queue memory regardless of restore success for upgrades
                self.remove_item(item)
                return # Exit after handling upgrade failure

            # --- Non-upgrade failure handling ---

            # Check for specific matching failure errors (adjust strings if needed)
            if "No matching files found in torrent" in error or \
               "No matching files found in parsed files" in error or \
               "No valid video files found in torrent" in error:
                logging.info(f"Media matching failed for {item_identifier}, moving back to Wanted queue. Error: {error}")
                # Remove torrent if ID is present
                if item.get('torrent_id'):
                    self.remove_unwanted_torrent(item['torrent_id'])
                queue_manager.move_to_wanted(item, "Adding")
                # move_to_wanted handles removing from self.items
                return

            # --- NEW: Handle filename/title filter match ---
            elif "matched filter-out list" in error:
                logging.info(f"Item {item_identifier} matched filename/title filter, moving back to Wanted queue. Error: {error}")
                if item.get('torrent_id'):
                    self.remove_unwanted_torrent(item['torrent_id'])
                queue_manager.move_to_wanted(item, "Adding")
                # move_to_wanted handles removing from self.items
                return

            # --- Fallback to single scraper logic (Keep existing) ---
            current_item_data = get_media_item_by_id(item_id) if item_id else None
            fall_back_to_single_scraper = current_item_data.get('fall_back_to_single_scraper') if current_item_data else False

            if not fall_back_to_single_scraper and get_setting('Scraping', 'fallback_to_single_enabled', default=True):
                logging.info(f"Falling back to single scraper for {item_identifier} due to error: {error}")
                if item_id: update_media_item(item_id, fall_back_to_single_scraper=True)

                # Update related items (keep existing logic)
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
                                (i.get('episode') or i.get('episode_number', -1)) > current_episode and
                                i.get('version') == version and
                                not i.get('fall_back_to_single_scraper')) # Only update those not already set
                        ]

                        for match in matching_items:
                            match_id = match.get('id')
                            if match_id: # Check if ID exists
                                update_media_item(match_id, fall_back_to_single_scraper=True)
                                logging.debug(f"Enabled single scraper fallback for related item ID: {match_id} ({match.get('title')})")

                queue_manager.move_to_scraping(item, "Adding")
                # move_to_scraping handles removal from self.items
                return

            # --- Blacklisting logic for old items (Keep existing) ---
            release_date_str = item.get('release_date')
            if release_date_str and release_date_str != 'Unknown':
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
                    if release_date < datetime.now() - timedelta(days=get_setting('Queue', 'adding_failure_blacklist_days', 30)):
                        logging.warning(f"Blacklisting old item {item_identifier} due to adding failure: {error}")
                        queue_manager.move_to_blacklisted(item, "Adding")

                        # Blacklist related items (keep existing logic)
                        if item_id and item.get('type') == 'episode':
                            series_title = item.get('series_title', '') or item.get('title', '')
                            season = item.get('season') or item.get('season_number')
                            version = item.get('version')

                            if series_title and season is not None:
                                from database import get_all_media_items # Local import
                                all_items_for_blacklist = [dict(row) for row in get_all_media_items(state=None)]
                                if all_items_for_blacklist:
                                    matching_items_for_blacklist = [
                                        i for i in all_items_for_blacklist
                                        if (i.get('id') != item_id and
                                            (i.get('series_title', '') or i.get('title', '')) == series_title and
                                            (i.get('season') or i.get('season_number')) == season and
                                            i.get('version') == version and
                                            i.get('state') not in ('Blacklisted', 'Collected') )
                                    ]

                                    for match in matching_items_for_blacklist:
                                        related_item_state = match.get('state', 'Unknown')
                                        logging.info(f"Blacklisting related episode ID: {match.get('id')} ({match.get('title')}) from state {related_item_state} due to primary item failure.")
                                        queue_manager.move_to_blacklisted(match, related_item_state)

                        # move_to_blacklisted handles removal from self.items
                        return
                except ValueError:
                    logging.error(f"Invalid release date format '{release_date_str}' for {item_identifier} during failure handling")
                except Exception as e:
                    logging.error(f"Error during blacklist check for {item_identifier}: {e}", exc_info=True)

            # --- Default: Move to Sleeping ---
            logging.warning(f"Moving item to Sleeping state due to adding failure: {item_identifier} - {error}")
            queue_manager.move_to_sleeping(item, "Adding")
            # move_to_sleeping handles removal from self.items

        except Exception as e:
            logging.error(f"Critical error in _handle_failed_item for {item.get('id', 'Unknown ID')}: {str(e)}", exc_info=True)
            # Ensure item is removed from memory queue even on critical failure
            if item_id:
                 self.remove_item(item) # Use the method to ensure logging consistency

    def get_new_item_values(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """ Get updated values after an item might have been modified """
        from database import get_media_item_by_id
        # Ensure item has an ID before attempting lookup
        item_id = item.get('id')
        if not item_id:
            logging.warning("Attempted get_new_item_values for item without ID.")
            return {}

        updated_item = get_media_item_by_id(item_id)

        if updated_item:
            # Make sure to handle None values gracefully if needed downstream
            new_values = {
                'filled_by_title': updated_item.get('filled_by_title'),
                'filled_by_magnet': updated_item.get('filled_by_magnet'),
                'filled_by_file': updated_item.get('filled_by_file'),
                'filled_by_torrent_id': updated_item.get('filled_by_torrent_id'),
                'version': updated_item.get('version'),
                # Add other fields if necessary
            }
            return new_values
        else:
            logging.warning(f"Could not retrieve updated item details for ID {item_id} in get_new_item_values")
            return {}

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i.get('id') == item_id for i in self.items) # Safer access with .get()