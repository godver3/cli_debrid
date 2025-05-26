import logging
from datetime import datetime, timedelta, date
from typing import Dict, Any, List

from utilities.settings import get_setting
from database.manual_blacklist import is_blacklisted
from database.core import get_db_connection
from database.database_reading import get_all_media_items, check_existing_media_item
from database.database_writing import update_media_item_state, update_blacklisted_date, remove_from_media_items

# Define constants for queue size limits
SCRAPING_QUEUE_MAX_SIZE = 500
# New threshold to pause Wanted processing entirely
WANTED_THROTTLE_SCRAPING_SIZE = 100

class WantedQueue:
    def __init__(self):
        logging.info("WantedQueue initialized (no longer stores items in memory).")

    def contains_item_id(self, item_id):
        """Check if the DB contains an item with the given ID in Wanted state"""
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.execute("SELECT 1 FROM media_items WHERE id = ? AND state = 'Wanted' LIMIT 1", (item_id,))
            result = cursor.fetchone()
            return result is not None
        except Exception as e:
            logging.error(f"Error checking DB for wanted item ID {item_id}: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def update(self):
        #logging.debug("WantedQueue.update called - no longer loads items into memory.")
        pass

    def get_contents(self):
        return []

    def add_item(self, item: Dict[str, Any]):
        logging.debug(f"WantedQueue.add_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def remove_item(self, item: Dict[str, Any]):
        logging.debug(f"WantedQueue.remove_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def _reconcile_with_existing_items(self, item: Dict[str, Any]) -> bool:
        """
        Check if an item already exists in Collected or Upgrading state with the same version.
        If found, remove the current item from the wanted state.

        Args:
            item: The item dictionary to check for reconciliation (must contain id, version, type, identifiers)

        Returns:
            bool: True if item was reconciled (found existing and removed from Wanted), False otherwise
        """
        try:
            item_id = item['id']
            # Check if a collected/upgrading version already exists
            if check_existing_media_item(item, item.get('version'), ['Collected', 'Upgrading']):
                logging.info(f"Item ID {item_id} (Version: {item.get('version')}) already exists in Collected/Upgrading state. Removing duplicate from Wanted.")
                # Remove the wanted item from the database directly
                remove_from_media_items(item_id)
                return True
            return False
        except Exception as e:
            logging.error(f"Error during wanted item reconciliation check for ID {item_id}: {str(e)}")
            return False # Assume not reconciled on error

    def _evaluate_item_readiness_and_act(self, item: Dict[str, Any], current_datetime: datetime, queue_manager) -> Dict[str, Any]:
        """
        Evaluates a single item's readiness, performs actions like reconciliation or moving to Unreleased,
        and returns its status. This method encapsulates logic similar to the original item processing
        loop within WantedQueue.process (lines approx 212-380).

        Args:
            item: The media item to evaluate.
            current_datetime: The current datetime.
            queue_manager: The QueueManager instance.

        Returns:
            A dictionary like {'status': 'scrape'|'unreleased'|'reconciled'|'wait'|'error', 
                              'item_data': item, 
                              'message': 'optional message'}
        """
        item_id = item['id']
        item_identifier = queue_manager.generate_identifier(item) # For logging

        try:
            # Check 1: Reconciliation (adapted from original _reconcile_with_existing_items and process loop)
            # This check needs to be robust. Using check_existing_media_item.
            if check_existing_media_item(item, item.get('version'), ['Collected', 'Upgrading']):
                logging.info(f"Item ID {item_id} (Version: {item.get('version')}) already exists in Collected/Upgrading state. Removing duplicate from Wanted.")
                remove_from_media_items(item_id)
                return {'status': 'reconciled', 'item_data': item, 'message': f"Reconciled and removed {item_identifier}"}

            # Handle early release first
            if item.get('early_release', False):
                # No capacity check here, just readiness. Capacity is handled in the main process() loop.
                return {'status': 'scrape', 'item_data': item, 'message': f"Early release {item_identifier} is ready."}

            # Check 2: Release Date & Time Logic (adapted from original process loop)
            release_date_str = item.get('release_date')
            airtime_str = item.get('airtime')
            version = item.get('version')
            is_magnet_assigned = item.get('content_source') == 'Magnet_Assigner'

            scraping_versions = get_setting('Scraping', 'versions', {}) # Consider caching this if called often per cycle
            version_settings = scraping_versions.get(version, {})
            require_physical = version_settings.get('require_physical_release', False)
            physical_release_date_str = item.get('physical_release_date')

            # Move to Unreleased if physical required but missing (and not magnet assigned)
            if not is_magnet_assigned and require_physical and not physical_release_date_str and item.get('type') != 'episode':
                logging.info(f"Item {item_identifier} requires physical release date but none available. Moving to Unreleased.")
                queue_manager.move_to_unreleased(item, "Wanted")
                return {'status': 'unreleased', 'item_data': item, 'message': f"{item_identifier} moved to Unreleased (missing physical date)."}

            release_date = None
            # Determine the effective release date to use (normal or physical)
            effective_release_date_to_parse = release_date_str
            log_release_type = "release"
            if item.get('type') == 'movie' and require_physical and physical_release_date_str:
                effective_release_date_to_parse = physical_release_date_str
                log_release_type = "physical release"
            
            if not is_magnet_assigned and (not effective_release_date_to_parse or str(effective_release_date_to_parse).lower() in ['unknown', 'none']):
                logging.debug(f"Item {item_identifier} has no valid {log_release_type} date. Moving to Unreleased.")
                queue_manager.move_to_unreleased(item, "Wanted")
                return {'status': 'unreleased', 'item_data': item, 'message': f"{item_identifier} moved to Unreleased (no valid {log_release_type} date)."}
            elif effective_release_date_to_parse and str(effective_release_date_to_parse).lower() not in ['unknown', 'none']:
                try:
                    release_date = datetime.strptime(str(effective_release_date_to_parse), '%Y-%m-%d').date()
                except ValueError:
                    if not is_magnet_assigned:
                        logging.warning(f"Invalid {log_release_type} date format for item {item_identifier}: {effective_release_date_to_parse}. Moving to Unreleased.")
                        queue_manager.move_to_unreleased(item, "Wanted")
                        return {'status': 'unreleased', 'item_data': item, 'message': f"{item_identifier} moved to Unreleased (invalid {log_release_type} date)."}
                    else:
                        logging.warning(f"Invalid {log_release_type} date format for Magnet Assigned item {item_identifier}: {effective_release_date_to_parse}. Treating as ready.")
                        release_date = current_datetime.date() # Use today's date if magnet assigned and date is bad

            # If Magnet Assigned and date was bad/missing, release_date is now set to today.
            # If not Magnet Assigned and date was bad/missing, it would have been moved to Unreleased above.

            effective_scrape_time = None
            if release_date: # This implies date is valid or has been set for magnet_assigned
                airtime = None
                if airtime_str:
                    try: airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                    except ValueError:
                        try: airtime = datetime.strptime(airtime_str, '%H:%M').time()
                        except ValueError: airtime = datetime.strptime("00:00", '%H:%M').time()
                else: airtime = datetime.strptime("00:00", '%H:%M').time()
                
                release_datetime = datetime.combine(release_date, airtime)

                offset_hours = 0.0
                offset_setting_key = "movie_airtime_offset" if item['type'] == 'movie' else "episode_airtime_offset"
                default_offset = 19.0 if item['type'] == 'movie' else 0.0
                offset_setting_str = get_setting("Queue", offset_setting_key, str(default_offset))
                try:
                    offset_hours = float(offset_setting_str)
                except (ValueError, TypeError):
                    logging.warning(f"Invalid {offset_setting_key} setting ('{offset_setting_str}'). Using default {default_offset}.")
                    offset_hours = default_offset
                
                effective_scrape_time = release_datetime + timedelta(hours=offset_hours)

            # Check if ready to move
            if is_magnet_assigned: # Always ready if magnet assigned (and not reconciled)
                return {'status': 'scrape', 'item_data': item, 'message': f"Magnet Assigned {item_identifier} is ready."}
            
            if effective_scrape_time:
                if effective_scrape_time <= current_datetime:
                    return {'status': 'scrape', 'item_data': item, 'message': f"{item_identifier} release time met."}
                else: # Not ready yet, check if it should go to Unreleased or just wait
                    time_until_release = effective_scrape_time - current_datetime
                    if time_until_release > timedelta(hours=24):
                        logging.debug(f"Item {item_identifier} is more than 24 hours away from {log_release_type}. Moving to Unreleased.")
                        queue_manager.move_to_unreleased(item, "Wanted")
                        return {'status': 'unreleased', 'item_data': item, 'message': f"{item_identifier} moved to Unreleased (>24h to {log_release_type})."}
                    else:
                        # Within 24 hours, keep in Wanted
                        return {'status': 'wait', 'item_data': item, 'message': f"{item_identifier} waiting (<24h to {log_release_type})."}
            
            # If we reach here, something is off (e.g. no release_date and not magnet_assigned but wasn't caught)
            # This case should ideally be covered by prior checks. Default to wait.
            logging.warning(f"Item {item_identifier} reached end of readiness check with no definitive action. Defaulting to wait.")
            return {'status': 'wait', 'item_data': item, 'message': f"{item_identifier} undecided, defaulting to wait."}

        except Exception as e_eval:
            logging.error(f"Error evaluating readiness for wanted item {item_id}: {str(e_eval)}", exc_info=True)
            return {'status': 'error', 'item_data': item, 'message': f"Error evaluating {item_identifier}: {e_eval}."}

    def _advance_idx_past_show(self, candidate_items_list, current_idx, show_imdb_id):
        """
        Advances the index past all items in candidate_items_list that match the given show_imdb_id,
        starting from current_idx.
        """
        new_idx = current_idx
        while new_idx < len(candidate_items_list) and \
              candidate_items_list[new_idx].get('imdb_id') == show_imdb_id:
            new_idx += 1
        return new_idx

    def process(self, queue_manager):
        processed_candidates_count = 0
        moved_to_scraping_count = 0
        moved_to_unreleased_count = 0 # Will be incremented by _evaluate_item_readiness_and_act

        try:
            # 0. Move manually blacklisted items first
            try:
                blacklisted_count = self.move_blacklisted_items()
                if blacklisted_count > 0:
                    logging.info(f"Processed {blacklisted_count} manually blacklisted items found in Wanted state.")
            except Exception as e_blacklist:
                logging.error(f"Error moving manually blacklisted items: {e_blacklist}", exc_info=True)

            # 1. Check Throttling
            ignore_throttling = get_setting("Debug", "ignore_wanted_queue_throttling", False)
            if ignore_throttling:
                logging.warning("DEBUG SETTING ENABLED: Ignoring Wanted Queue throttling limits.")

            current_scraping_queue_size = 0
            allowed_to_add_count = float('inf')

            if not ignore_throttling:
                try:
                    scraping_queue = queue_manager.queues["Scraping"]
                    current_scraping_queue_size = len(scraping_queue.items) if hasattr(scraping_queue, 'items') else len(scraping_queue.get_contents())
                except KeyError:
                    logging.error("ScrapingQueue not found in queue_manager. Cannot apply throttle.")
                    return False

                if current_scraping_queue_size >= WANTED_THROTTLE_SCRAPING_SIZE:
                    return True

                allowed_to_add_count = max(0, SCRAPING_QUEUE_MAX_SIZE - current_scraping_queue_size)
                if allowed_to_add_count <= 0 and not (len(scraping_queue.items) < SCRAPING_QUEUE_MAX_SIZE and SCRAPING_QUEUE_MAX_SIZE > 0) : # Check if it can actually add anything
                    logging.debug("Scraping queue is full or cannot accept more. Skipping Wanted processing cycle.")
                    return True
            
            # 2. Build Query for Candidate Items (same as before)
            query = "SELECT * FROM media_items WHERE state = 'Wanted'"
            params = []
            order_by_clauses = []
            sort_order_type = get_setting("Queue", "queue_sort_order", "None")
            if sort_order_type == "Movies First":
                order_by_clauses.append("CASE type WHEN 'movie' THEN 0 ELSE 1 END")
            elif sort_order_type == "Episodes First":
                order_by_clauses.append("CASE type WHEN 'episode' THEN 0 ELSE 1 END")
            sort_by_release_date = get_setting("Queue", "sort_by_release_date_desc", False)
            if sort_by_release_date:
                order_by_clauses.append("release_date DESC")
            order_by_clauses.append("imdb_id ASC")
            order_by_clauses.append("CASE WHEN type = 'episode' THEN season_number ELSE NULL END ASC NULLS FIRST")
            order_by_clauses.append("CASE WHEN type = 'episode' THEN episode_number ELSE NULL END ASC NULLS FIRST")
            
            content_source_priority = get_setting("Queue", "content_source_priority", "")
            source_priority_list = [s.strip() for s in content_source_priority.split(',') if s.strip()]

            if order_by_clauses:
                query += " ORDER BY " + ", ".join(order_by_clauses)
            
            # Fetch a bit more to allow for filtering and batching shows
            fetch_limit = int(allowed_to_add_count * 2) + 50 if allowed_to_add_count != float('inf') else 200 
            query += f" LIMIT ?"
            params.append(fetch_limit)

            conn = None
            candidate_items_raw = []
            try:
                conn = get_db_connection()
                cursor = conn.execute(query, params)
                candidate_items_raw = cursor.fetchall()
            except Exception as e_fetch:
                logging.error(f"Error fetching wanted items from DB: {e_fetch}", exc_info=True)
                return False
            finally:
                if conn: conn.close()

            if not candidate_items_raw: return True
            
            candidate_items = [dict(row) for row in candidate_items_raw]

            if source_priority_list: # Python-side sorting by content_source_priority
                def get_source_priority_key(item):
                    source = item.get('content_source', '')
                    priority_index = source_priority_list.index(source) if source in source_priority_list else len(source_priority_list)
                    imdb_id_val = item.get('imdb_id', '') # Default to empty string
                    season_num_val = item.get('season_number') if item.get('type') == 'episode' else float('inf')
                    if season_num_val is None and item.get('type') == 'episode': season_num_val = float('-inf') # Sort None seasons first for episodes
                    episode_num_val = item.get('episode_number') if item.get('type') == 'episode' else float('inf')
                    if episode_num_val is None and item.get('type') == 'episode': episode_num_val = float('-inf')
                    return (priority_index, imdb_id_val, season_num_val, episode_num_val)
                candidate_items.sort(key=get_source_priority_key)

            # 3. Process Candidate Items
            current_datetime = datetime.now()
            shows_batched_or_skipped_this_cycle = set() # Tracks imdb_ids

            idx = 0
            while idx < len(candidate_items):
                # Check if we can START processing a new item or batch
                # This condition allows one show batch to be processed even if allowed_to_add_count is 0 initially (meaning queue is at max but not over throttle)
                # or if a previous show batch exceeded allowed_to_add_count.
                can_start_new_item_or_batch = (moved_to_scraping_count < allowed_to_add_count) or \
                                              (moved_to_scraping_count == 0 and allowed_to_add_count >= 0)


                item = candidate_items[idx]
                item_imdb_id = item.get('imdb_id')
                item_id = item['id'] # For logging

                # Skip if this item is part of a show already handled or decided upon this cycle
                if item_imdb_id and item_imdb_id in shows_batched_or_skipped_this_cycle:
                    idx += 1
                    continue
                
                # If general capacity is full and we've already added something, stop.
                if not can_start_new_item_or_batch and moved_to_scraping_count > 0:
                    logging.debug(f"Scraping queue general capacity effectively reached. Limit: {allowed_to_add_count}, Current: {moved_to_scraping_count}. Halting Wanted processing for this cycle.")
                    break
                
                processed_candidates_count +=1
                
                # Evaluate the current item. This function might move it to Unreleased or Reconcile it.
                evaluation_result = self._evaluate_item_readiness_and_act(item, current_datetime, queue_manager)
                status = evaluation_result['status']
                
                if status == 'reconciled':
                    idx += 1
                    continue 
                if status == 'unreleased':
                    moved_to_unreleased_count += 1 # Count is handled here based on helper's action
                    if item_imdb_id: shows_batched_or_skipped_this_cycle.add(item_imdb_id)
                    idx += 1
                    continue
                if status == 'error' or status == 'wait':
                    if item_imdb_id: shows_batched_or_skipped_this_cycle.add(item_imdb_id) # Mark show as considered
                    # Advance index past other episodes of this show in the current candidate list
                    # as their fate is tied to this first episode's evaluation for this cycle.
                    if item['type'] == 'episode' and item_imdb_id:
                        idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id)
                    else:
                        idx += 1
                    continue

                # If status is 'scrape', the item is ready.
                if status == 'scrape':
                    if item['type'] == 'movie':
                        if can_start_new_item_or_batch: # Check capacity for this movie
                            logging.debug(f"Moving movie {queue_manager.generate_identifier(item)} to Scraping.")
                            queue_manager.move_to_scraping(item, "Wanted")
                            moved_to_scraping_count += 1
                            idx += 1
                        else:
                            logging.debug(f"Movie {queue_manager.generate_identifier(item)} ready, but no capacity. Halting.")
                            break # Stop processing this cycle.
                    
                    elif item['type'] == 'episode':
                        # Full show batching logic
                        if item_imdb_id and can_start_new_item_or_batch: # We can start this show
                            shows_batched_or_skipped_this_cycle.add(item_imdb_id)
                            logging.info(f"Preparing to batch all ready episodes for show {item_imdb_id}, ignoring standard pick limit for this show.")

                            current_show_episodes_from_db = []
                            conn_show = None
                            try:
                                conn_show = get_db_connection()
                                # Fetch ALL wanted episodes for this show, ordered correctly
                                # Ensure correct sorting for episodes within the show
                                show_query_order_clauses = []
                                # Episode type specific ordering (season, episode)
                                show_query_order_clauses.append("CASE WHEN type = 'episode' THEN season_number ELSE NULL END ASC NULLS FIRST")
                                show_query_order_clauses.append("CASE WHEN type = 'episode' THEN episode_number ELSE NULL END ASC NULLS FIRST")
                                # Fallback ordering by ID if not an episode or numbers are null (though less likely for a show batch)
                                show_query_order_clauses.append("id ASC")


                                show_query = f"SELECT * FROM media_items WHERE state = 'Wanted' AND imdb_id = ? ORDER BY {', '.join(show_query_order_clauses)}"
                                cursor_show = conn_show.execute(show_query, (item_imdb_id,))
                                current_show_episodes_from_db_raw = cursor_show.fetchall()
                                current_show_episodes_from_db = [dict(row) for row in current_show_episodes_from_db_raw]
                            except Exception as e_show_fetch:
                                logging.error(f"Error fetching all episodes for show {item_imdb_id} for batching: {e_show_fetch}", exc_info=True)
                                idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id) # Advance main idx
                                continue 
                            finally:
                                if conn_show: conn_show.close()

                            if not current_show_episodes_from_db:
                                logging.warning(f"No episodes found in DB for show {item_imdb_id} during batch attempt, though initial item was ready.")
                                idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id)
                                continue

                            current_show_batch_ready_to_move = []
                            # Track already processed candidates from this specific show fetch to avoid double counting
                            # if an item from candidate_items was also in current_show_episodes_from_db
                            # However, processed_candidates_count is for the outer loop.
                            # Here we are evaluating each item from the specific show query.
                            for show_episode_item in current_show_episodes_from_db:
                                # We increment processed_candidates_count only if it's not the 'item' we already processed.
                                # This count is more about items from the initial candidate_items list.
                                # The important thing is evaluating each show_episode_item.
                                eval_res = self._evaluate_item_readiness_and_act(show_episode_item, current_datetime, queue_manager)
                                if eval_res['status'] == 'scrape':
                                    current_show_batch_ready_to_move.append(show_episode_item)
                                elif eval_res['status'] == 'unreleased':
                                    moved_to_unreleased_count += 1
                                # Reconciled items are handled by _evaluate_item_readiness_and_act.
                                # Wait/error items are just skipped for this batch.

                            if current_show_batch_ready_to_move:
                                logging.info(f"Moving full batch of {len(current_show_batch_ready_to_move)} ready episodes for show {item_imdb_id} to Scraping.")
                                for batch_item_to_move in current_show_batch_ready_to_move:
                                    queue_manager.move_to_scraping(batch_item_to_move, "Wanted")
                                    moved_to_scraping_count += 1
                            else:
                                logging.info(f"No episodes for show {item_imdb_id} were ready to scrape in the full batch check.")
                            
                            # Advance idx in the original candidate_items list past all episodes of this show
                            idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id)
                            
                            # If this large show batch has filled/overfilled what was initially allowed, stop further *new* items this cycle.
                            if moved_to_scraping_count >= allowed_to_add_count and allowed_to_add_count != float('inf') and allowed_to_add_count > 0 :
                                logging.debug(f"Scraping queue add limit ({allowed_to_add_count}) met/exceeded by show batch. Halting for new items this cycle.")
                                break # Exit main while loop for candidate_items

                        elif item_imdb_id and not can_start_new_item_or_batch:
                            # This case means we couldn't even start this show batch due to initial capacity check.
                            shows_batched_or_skipped_this_cycle.add(item_imdb_id) # Mark so we don't retry this show's first item
                            logging.debug(f"Not enough capacity to start show batch for {item_imdb_id} (Allowed: {allowed_to_add_count}, Moved: {moved_to_scraping_count}). Will try next cycle.")
                            idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id) 
                            if moved_to_scraping_count > 0 and allowed_to_add_count > 0 : # If we already moved something and there was a limit
                                break
                            # else continue to check if other smaller items in candidate_items might fit
                        
                        else: # Should not happen if item_imdb_id and can_start_new_item_or_batch was the entry condition
                            idx +=1
                    
                    # After processing an item/batch, check if the overall limit is now hit.
                    # This check is more general now, as the show batch might have exceeded it.
                    if moved_to_scraping_count >= allowed_to_add_count and allowed_to_add_count != float('inf') and allowed_to_add_count > 0 :
                         logging.debug(f"Scraping queue add limit ({allowed_to_add_count}) reached after processing. Stopping.")
                         break # Exit main while loop

                else: # Unknown status from _evaluate_item_readiness_and_act or non-scrape status for first item of a potential show
                    logging.warning(f"Item {item_id} (status: {status}) not moved to scrape. Marking show {item_imdb_id} as considered for this cycle.")
                    if item_imdb_id: # Ensure show is marked as considered
                        shows_batched_or_skipped_this_cycle.add(item_imdb_id)
                    
                    if item['type'] == 'episode' and item_imdb_id: # Advance past all of this show's items in candidate_items
                        idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id)
                    else: # Movie or other, just advance one
                        idx += 1
            
            #logging.info(f"Wanted queue processing complete. Processed Candidates: {processed_candidates_count}/{len(candidate_items)}, Moved to Scraping: {moved_to_scraping_count}, Accounted for Unreleased: {moved_to_unreleased_count}")

        except Exception as e:
            logging.error(f"Fatal error in wanted queue processing: {str(e)}", exc_info=True)
            return False
        return True

    def move_blacklisted_items(self):
        """
        Check Wanted items against the blacklist and move any hits to the Blacklisted state.
        Returns the count of items moved.
        """
        #logging.debug("Checking for manually blacklisted items in Wanted state...")
        items_to_blacklist = []
        conn = None
        try:
            conn = get_db_connection()
            # Fetch potential candidates (state='Wanted')
            # Fetch necessary fields: id, title, imdb_id, tmdb_id, type, season_number
            cursor = conn.execute("""
                SELECT id, title, imdb_id, tmdb_id, type, season_number
                FROM media_items
                WHERE state = 'Wanted'
            """)
            wanted_items = cursor.fetchall()

            blacklisted_count = 0
            if not wanted_items:
                return 0

            #logging.debug(f"Checking {len(wanted_items)} Wanted items against manual blacklist...")
            for item_row in wanted_items:
                item = dict(item_row) # Convert row to dict
                season_number = item.get('season_number') if item.get('type') == 'episode' else None
                # Check blacklist using imported function
                is_item_blacklisted = (
                    is_blacklisted(item.get('imdb_id', ''), season_number) or
                    is_blacklisted(item.get('tmdb_id', ''), season_number)
                )

                if is_item_blacklisted:
                    items_to_blacklist.append(item) # Add the dict

            if items_to_blacklist:
                logging.info(f"Found {len(items_to_blacklist)} Wanted items that are manually blacklisted. Updating state...")
                for item_to_bl in items_to_blacklist:
                     try:
                         # Update state and date directly using imported function
                         update_media_item_state(item_to_bl['id'], 'Blacklisted')
                         update_blacklisted_date(item_to_bl['id'], datetime.now())
                         blacklisted_count += 1
                         logging.info(f"Moved manually blacklisted item to Blacklisted state: {item_to_bl.get('title', 'Unknown')} (ID: {item_to_bl['id']})")
                     except Exception as e_update:
                         logging.error(f"Error updating state for manually blacklisted item {item_to_bl['id']}: {e_update}")

            #logging.debug(f"Finished checking manual blacklist. Moved {blacklisted_count} items.")
            return blacklisted_count

        except Exception as e:
            logging.error(f"Error during manual blacklist check for Wanted items: {e}", exc_info=True)
            return 0 # Return 0 on error
        finally:
            if conn:
                conn.close()