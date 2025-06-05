import logging
from datetime import datetime, timedelta, date, time as dt_time
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
        logging.info("WantedQueue initialized.")

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

    def _is_within_alternate_scrape_window(self, item, now=None):
        if now is None:
            now = datetime.now()
        use_alt = get_setting('Debug', 'use_alternate_scrape_time_strategy', False)
        if not use_alt:
            return False
        anchor_str = get_setting('Debug', 'alternate_scrape_time_24h', '00:00')
        try:
            anchor_time = datetime.strptime(anchor_str, '%H:%M').time()
        except Exception:
            anchor_time = dt_time(0, 0)
        today_anchor = now.replace(hour=anchor_time.hour, minute=anchor_time.minute, second=0, microsecond=0)
        if now < today_anchor:
            anchor_dt = today_anchor
        else:
            anchor_dt = today_anchor
        window_start = anchor_dt - timedelta(hours=24)
        # Get item datetime
        release_date_str = item.get('release_date')
        airtime_str = item.get('airtime')
        if not release_date_str or release_date_str.lower() in ['unknown', 'none']:
            return False
        try:
            release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
            if airtime_str:
                try:
                    airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                except ValueError:
                    try:
                        airtime = datetime.strptime(airtime_str, '%H:%M').time()
                    except ValueError:
                        airtime = dt_time(0, 0)
            else:
                airtime = dt_time(0, 0)
            item_dt = datetime.combine(release_date, airtime)
        except Exception:
            return False
        # If the item is older than the window, always allow
        if item_dt < window_start:
            return True
        return window_start <= item_dt <= anchor_dt

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
            is_early_release = item.get('early_release', False)
            if is_early_release:
                # No capacity check here, just readiness. Capacity is handled in the main process() loop.
                logging.info(f"[{item_identifier}] Early release flag is True. Marking as 'scrape'.")
                return {'status': 'scrape', 'item_data': item, 'message': f"Early release {item_identifier} is ready."}

            # --- Alternate scrape time strategy ---
            use_alt_strategy = get_setting('Debug', 'use_alternate_scrape_time_strategy', False)
            if use_alt_strategy:
                is_in_alt_window = self._is_within_alternate_scrape_window(item, current_datetime)
                logging.debug(f"[{item_identifier}] Alternate scrape window check result: {is_in_alt_window}")
                if is_in_alt_window:
                    logging.info(f"[{item_identifier}] Eligible by alternate scrape time strategy. Marking as 'scrape'.")
                    return {'status': 'scrape', 'item_data': item, 'message': f"{item_identifier} eligible by alternate scrape time strategy."}
                else:
                    # If alt strategy is on but item is not in window, it doesn't mean 'wait' yet, normal logic follows.
                    logging.debug(f"[{item_identifier}] Not in alternate scrape window. Proceeding with normal date logic.")
            
            # If alternate strategy is ON but item wasn't in window, it should fall through to normal date logic.
            # If alternate strategy is OFF, it also falls through to normal date logic.
            # Only if alt strategy is ON AND item IS in window does it return 'scrape' or 'wait' from that block.
            # The original code for alternate strategy only returned 'scrape' or 'wait' if it was in the window.
            # My adjustment: if alt strategy is on and item is NOT in window, then normal processing continues
            # If the item IS in the window, it gets 'scrape'.
            # The 'else' for alt_strategy returning 'wait' is only if the function _is_within_alternate_scrape_window itself decided it should wait.
            # The original:
            # if get_setting('Debug', 'use_alternate_scrape_time_strategy', False):
            #     if self._is_within_alternate_scrape_window(item, current_datetime):
            #         return {'status': 'scrape', 'item_data': item, 'message': f"{item_identifier} eligible by alternate scrape time strategy."}
            #     else: # This else was problematic, it would make items wait indefinitely if not in window
            #         return {'status': 'wait', 'item_data': item, 'message': f"{item_identifier} not in alternate scrape window."}
            # Corrected flow: if use_alt_strategy and is_in_alt_window -> 'scrape'. Otherwise, continue.
            # The _is_within_alternate_scrape_window does not have a 'wait' state, it's boolean.

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


            effective_scrape_time = None
            if release_date: # This implies date is valid or has been set for magnet_assigned
                airtime = None
                if airtime_str:
                    try: airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                    except ValueError:
                        try: airtime = datetime.strptime(airtime_str, '%H:%M').time()
                        except ValueError: 
                            airtime = datetime.strptime("00:00", '%H:%M').time()
                            logging.warning(f"[{item_identifier}] Invalid airtime format '{airtime_str}', defaulting to 00:00.")
                else: 
                    airtime = datetime.strptime("00:00", '%H:%M').time()
                
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
            if is_magnet_assigned: # Always ready if magnet assigned (and not reconciled or early_release)
                logging.info(f"[{item_identifier}] Magnet Assigned. Marking as 'scrape'.")
                return {'status': 'scrape', 'item_data': item, 'message': f"Magnet Assigned {item_identifier} is ready."}
            
            if effective_scrape_time:
                if effective_scrape_time <= current_datetime:
                    logging.info(f"[{item_identifier}] Effective scrape time is in the past or now. Marking as 'scrape'.")
                    return {'status': 'scrape', 'item_data': item, 'message': f"{item_identifier} release time met."}
                else: # Not ready yet, check if it should go to Unreleased or just wait
                    time_until_release = effective_scrape_time - current_datetime
                    if time_until_release > timedelta(hours=24):
                        queue_manager.move_to_unreleased(item, "Wanted")
                        return {'status': 'unreleased', 'item_data': item, 'message': f"{item_identifier} moved to Unreleased (>24h to {log_release_type})."}
                    else:
                        return {'status': 'wait', 'item_data': item, 'message': f"{item_identifier} waiting (<24h to {log_release_type})."}
            
            logging.warning(f"[{item_identifier}] Reached end of readiness check with no definitive action (e.g. no release_date and not magnet_assigned but wasn't caught). Defaulting to wait.")
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
        # logging.debug(f"Advanced index from {current_idx} to {new_idx} for show {show_imdb_id}") # Optional: if too verbose
        return new_idx

    def process(self, queue_manager):
        processed_candidates_count = 0
        moved_to_scraping_count = 0
        moved_to_unreleased_count = 0 
        forced_items_moved_count = 0 # New counter for forced items

        try:
            # 0. Move manually blacklisted items first
            try:
                blacklisted_count = self.move_blacklisted_items()
                # Optional: logging.info(f"Processed manual blacklist, {blacklisted_count} items moved.")
            except Exception as e_blacklist:
                logging.error(f"Error moving manually blacklisted items: {e_blacklist}", exc_info=True)

            # 1. Process Force Priority Items (NEW SECTION)
            logging.info("Starting processing of force-prioritized items.")
            conn_force = None
            try:
                conn_force = get_db_connection()
                # Assuming force_priority is an INTEGER field (1 for true, 0 or NULL for false)
                # The specific column name and true value (e.g., `force_priority = TRUE`) might need adjustment
                # based on your actual database schema.
                cursor_force = conn_force.execute("SELECT * FROM media_items WHERE state = 'Wanted' AND force_priority = 1")
                forced_items_raw = cursor_force.fetchall()
                
                if forced_items_raw:
                    forced_items = [dict(row) for row in forced_items_raw]
                    logging.info(f"Found {len(forced_items)} force-prioritized items in Wanted state.")
                    for item in forced_items:
                        item_identifier_log = queue_manager.generate_identifier(item)
                        try:
                            logging.info(f"Force-prioritizing item {item_identifier_log} to Scraping queue.")
                            queue_manager.move_to_scraping(item, "Wanted") # State changes here
                            forced_items_moved_count += 1
                            moved_to_scraping_count += 1 # Increment general counter as well
                        except Exception as e_move_forced:
                            logging.error(f"Error moving force-prioritized item {item_identifier_log} to scraping: {e_move_forced}", exc_info=True)
                else:
                    logging.info("No force-prioritized items found in Wanted state.")

            except Exception as e_force:
                logging.error(f"Error fetching or processing force-prioritized items: {e_force}", exc_info=True)
            finally:
                if conn_force:
                    conn_force.close()
            logging.info(f"Finished processing force-prioritized items. Moved {forced_items_moved_count} items directly to scraping.")

            # 2. Check Throttling (for REGULAR items)
            ignore_throttling = get_setting("Debug", "ignore_wanted_queue_throttling", False)
            if ignore_throttling:
                logging.warning("DEBUG SETTING ENABLED: Ignoring Wanted Queue throttling limits.")

            current_scraping_queue_size = 0
            allowed_to_add_count = float('inf') 

            if not ignore_throttling:
                try:
                    scraping_queue = queue_manager.queues.get("Scraping") 
                    if scraping_queue:
                        current_scraping_queue_size = len(scraping_queue.get_contents())
                    else:
                        logging.error("ScrapingQueue not found in queue_manager. Cannot apply throttle.")
                except Exception as e_sq_check:
                    logging.error(f"Error checking ScrapingQueue size: {e_sq_check}", exc_info=True)

                # If current scraping queue is already at or above the hard throttle limit for Wanted, stop.
                # This check now correctly reflects any forced items that were just added.
                if current_scraping_queue_size >= WANTED_THROTTLE_SCRAPING_SIZE:
                    logging.info(f"Scraping queue size ({current_scraping_queue_size}) meets or exceeds WANTED_THROTTLE_SCRAPING_SIZE ({WANTED_THROTTLE_SCRAPING_SIZE}). Pausing Wanted processing.")
                    return True # Stop processing more items from Wanted

                # Calculate how many more items we are allowed to add to scraping from the regular wanted pool
                allowed_to_add_this_cycle = max(0, SCRAPING_QUEUE_MAX_SIZE - current_scraping_queue_size)
                
                # Note: `moved_to_scraping_count` already includes forced items.
                # We need to calculate how many *more* regular items can be added.
                # The `allowed_to_add_count` for the loop below should be the remaining capacity.
                allowed_to_add_count = allowed_to_add_this_cycle

                if allowed_to_add_count <= 0 and SCRAPING_QUEUE_MAX_SIZE > 0 : # Check SCRAPING_QUEUE_MAX_SIZE > 0 to avoid issues if it's 0 (unlimited)
                    logging.info(f"Scraping queue size ({current_scraping_queue_size}) means no more regular items can be added (max: {SCRAPING_QUEUE_MAX_SIZE}).")
                    return True # Stop processing more items from Wanted if no capacity for regular items

            # 3. Build Query for Candidate Items (REGULAR items)
            query = "SELECT * FROM media_items WHERE state = 'Wanted'" # This will not pick up already moved forced items
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
            
            fetch_limit = int(allowed_to_add_count * 1.5) + 50 if allowed_to_add_count != float('inf') else 200 
            if allowed_to_add_count != float('inf') and allowed_to_add_count <= 10: 
                 fetch_limit = max(fetch_limit, 50) 
            elif allowed_to_add_count == float('inf'): 
                 fetch_limit = 200 
            else: 
                 fetch_limit = int(allowed_to_add_count * 1.5) + 50

            query_with_limit = query + f" LIMIT ?" 
            params_with_limit = params + [fetch_limit] 

            conn = None
            candidate_items_raw = []
            try:
                conn = get_db_connection()
                cursor = conn.execute(query_with_limit, params_with_limit)
                candidate_items_raw = cursor.fetchall()
            except Exception as e_fetch:
                logging.error(f"Error fetching wanted items from DB: {e_fetch}", exc_info=True)
                return False
            finally:
                if conn: conn.close()

            if not candidate_items_raw:
                return True 
            
            candidate_items = [dict(row) for row in candidate_items_raw]

            if source_priority_list: 
                def get_source_priority_key(item):
                    source = item.get('content_source', '')
                    priority_index = source_priority_list.index(source) if source in source_priority_list else len(source_priority_list)
                    imdb_id_val = item.get('imdb_id') or '' 
                    season_num_val = item.get('season_number') if item.get('type') == 'episode' else float('inf')
                    if season_num_val is None and item.get('type') == 'episode': season_num_val = float('-inf') 
                    episode_num_val = item.get('episode_number') if item.get('type') == 'episode' else float('inf')
                    if episode_num_val is None and item.get('type') == 'episode': episode_num_val = float('-inf')
                    release_date_val = item.get('release_date') or '' 
                    type_priority = 0 
                    if sort_order_type == "Movies First":
                        type_priority = 0 if item.get('type') == 'movie' else 1
                    elif sort_order_type == "Episodes First":
                        type_priority = 0 if item.get('type') == 'episode' else 1
                    return (priority_index, type_priority, release_date_val if sort_by_release_date else '', imdb_id_val, season_num_val, episode_num_val)
                candidate_items.sort(key=get_source_priority_key)

            # 4. Process Candidate Items
            current_datetime = datetime.now()
            # Tracks imdb_ids of shows for which a scrape batch was done, OR a lead item errored causing candidate skip for that show.
            shows_fully_processed_this_cycle = set() 

            idx = 0
            while idx < len(candidate_items):
                
                can_start_new_item_or_batch = (moved_to_scraping_count < allowed_to_add_count) or \
                                              (moved_to_scraping_count == 0 and allowed_to_add_count >= 0)
                if allowed_to_add_count == float('inf'): 
                    can_start_new_item_or_batch = True

                item = candidate_items[idx]
                item_imdb_id = item.get('imdb_id')
                item_id = item['id'] 
                item_identifier_log = queue_manager.generate_identifier(item)

                # If this show's candidate items were already processed (due to full batch OR leading error skip), advance.
                if item_imdb_id and item_imdb_id in shows_fully_processed_this_cycle:
                    idx += 1
                    continue
                
                if not can_start_new_item_or_batch and moved_to_scraping_count > 0:
                    break
                
                processed_candidates_count +=1
                
                evaluation_result = self._evaluate_item_readiness_and_act(item, current_datetime, queue_manager)
                status = evaluation_result['status']
                
                if status == 'reconciled':
                    idx += 1
                    continue 
                
                if status == 'unreleased':
                    moved_to_unreleased_count += 1
                    idx += 1 # Process next candidate item
                    continue
                
                if status == 'error':
                    if item_imdb_id:
                        shows_fully_processed_this_cycle.add(item_imdb_id) # Mark show due to error
                        if item['type'] == 'episode': 
                            prev_idx = idx
                            idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id)
                            continue 
                    idx += 1 # Movie error or post-advance
                    continue

                if status == 'wait':
                    # Item is waiting. Process next candidate item individually.
                    # Do NOT advance past other episodes of this show in candidate_items.
                    idx += 1
                    continue

                if status == 'scrape':
                    if item['type'] == 'movie':
                        if can_start_new_item_or_batch:
                            logging.info(f"Moving movie {item_identifier_log} to Scraping.")
                            queue_manager.move_to_scraping(item, "Wanted")
                            moved_to_scraping_count += 1
                        else:
                            break 
                        idx += 1
                    
                    elif item['type'] == 'episode':
                        if item_imdb_id and can_start_new_item_or_batch:
                            # This episode is scrape-ready. Initiate full show batch.
                            # Add to set to indicate this show's scrape batch processing has been triggered from candidate list.
                            shows_fully_processed_this_cycle.add(item_imdb_id)

                            current_show_episodes_from_db = []
                            conn_show = None
                            try:
                                conn_show = get_db_connection()
                                show_query_order_clauses = [
                                    "CASE WHEN type = 'episode' THEN season_number ELSE NULL END ASC NULLS FIRST",
                                    "CASE WHEN type = 'episode' THEN episode_number ELSE NULL END ASC NULLS FIRST",
                                    "id ASC"
                                ]
                                show_query = f"SELECT * FROM media_items WHERE state = 'Wanted' AND imdb_id = ? ORDER BY {', '.join(show_query_order_clauses)}"
                                cursor_show = conn_show.execute(show_query, (item_imdb_id,))
                                current_show_episodes_from_db_raw = cursor_show.fetchall()
                                current_show_episodes_from_db = [dict(row) for row in current_show_episodes_from_db_raw]
                            except Exception as e_show_fetch:
                                logging.error(f"Error fetching all episodes for show {item_imdb_id} for batching: {e_show_fetch}", exc_info=True)
                                prev_idx = idx
                                idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id) 
                                continue 
                            finally:
                                if conn_show: conn_show.close()

                            if not current_show_episodes_from_db:
                                logging.warning(f"No episodes found in DB for show {item_imdb_id} during batch attempt, though initial item was ready.")
                                prev_idx = idx
                                idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id)
                                continue

                            current_show_batch_ready_to_move = []
                            for i_s_e, show_episode_item in enumerate(current_show_episodes_from_db):
                                show_ep_identifier = queue_manager.generate_identifier(show_episode_item)
                                eval_res = self._evaluate_item_readiness_and_act(show_episode_item, current_datetime, queue_manager)
                                if eval_res['status'] == 'scrape':
                                    current_show_batch_ready_to_move.append(show_episode_item)
                                elif eval_res['status'] == 'unreleased':
                                    moved_to_unreleased_count += 1
                                
                            if current_show_batch_ready_to_move:
                                logging.info(f"Moving full batch of {len(current_show_batch_ready_to_move)} ready episodes for show {item_imdb_id} to Scraping.")
                                for batch_item_to_move in current_show_batch_ready_to_move:
                                    queue_manager.move_to_scraping(batch_item_to_move, "Wanted")
                                    moved_to_scraping_count += 1
                            
                            prev_idx = idx
                            idx = self._advance_idx_past_show(candidate_items, idx, item_imdb_id) # Advance past this show in candidate_items
                            
                        elif item_imdb_id and not can_start_new_item_or_batch: # Not enough capacity to start a batch
                            if moved_to_scraping_count < allowed_to_add_count or allowed_to_add_count == float('inf'):
                                queue_manager.move_to_scraping(item, "Wanted")
                                moved_to_scraping_count += 1
                            else:
                                break 
                            idx += 1 
                        
                        else: # Episode 'scrape' but no imdb_id, or other edge case
                            logging.warning(f"Episode {item_identifier_log} is 'scrape' but not handled by batch logic (no imdb_id or other). Moving as single item if capacity allows.")
                            if moved_to_scraping_count < allowed_to_add_count or allowed_to_add_count == float('inf'):
                                queue_manager.move_to_scraping(item, "Wanted")
                                moved_to_scraping_count += 1
                            else:
                                break
                            idx += 1
                    
                    else: # Unknown type marked as 'scrape'
                        logging.warning(f"Item {item_identifier_log} is 'scrape' but unknown type: {item.get('type')}")
                        idx += 1

                    if moved_to_scraping_count >= allowed_to_add_count and allowed_to_add_count != float('inf') and allowed_to_add_count > 0 :
                         break 
                    continue

                logging.error(f"Item {item_identifier_log} (idx {idx}) had unexpected status '{status}' from evaluation. Advancing index.")
                idx += 1
            
        except Exception as e:
            logging.error(f"Fatal error in wanted queue processing: {str(e)}", exc_info=True)
            return False
        finally:
            # logging.info("WantedQueue process cycle ended.")
            pass
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