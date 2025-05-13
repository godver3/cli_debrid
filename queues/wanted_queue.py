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

    def process(self, queue_manager):
        #logging.debug("Processing Wanted queue using direct DB query.")
        processed_item_count = 0
        moved_to_scraping_count = 0
        moved_to_unreleased_count = 0

        try:
            # 0. Check for and move manually blacklisted items first
            try:
                blacklisted_count = self.move_blacklisted_items()
                if blacklisted_count > 0:
                    logging.info(f"Processed {blacklisted_count} manually blacklisted items found in Wanted state.")
            except Exception as e_blacklist:
                logging.error(f"Error moving manually blacklisted items: {e_blacklist}", exc_info=True)
                # Continue processing even if blacklist check fails

            # 1. Check Throttling
            ignore_throttling = get_setting("Debug", "ignore_wanted_queue_throttling", False)
            if ignore_throttling:
                logging.warning("DEBUG SETTING ENABLED: Ignoring Wanted Queue throttling limits.")

            current_scraping_queue_size = 0
            allowed_to_add_count = float('inf')

            if not ignore_throttling:
                try:
                    scraping_queue = queue_manager.queues["Scraping"]
                    # Use get_contents() size if queue isn't in-memory
                    current_scraping_queue_size = len(scraping_queue.items) if hasattr(scraping_queue, 'items') else len(scraping_queue.get_contents())
                except KeyError:
                    logging.error("ScrapingQueue not found in queue_manager. Cannot apply throttle.")
                    return False # Cannot proceed safely

                if current_scraping_queue_size >= WANTED_THROTTLE_SCRAPING_SIZE:
                    #logging.debug(f"Scraping queue size ({current_scraping_queue_size}) is >= throttle limit ({WANTED_THROTTLE_SCRAPING_SIZE}). Skipping Wanted processing cycle.")
                    return True

                allowed_to_add_count = max(0, SCRAPING_QUEUE_MAX_SIZE - current_scraping_queue_size)
                if allowed_to_add_count <= 0:
                    logging.debug("Scraping queue is full. Skipping Wanted processing cycle.")
                    return True

            # 2. Build Query for Candidate Items
            query = "SELECT * FROM media_items WHERE state = 'Wanted'"
            params = []
            order_by_clauses = []

            # Sorting Logic (adapted for SQL)
            sort_order_type = get_setting("Queue", "queue_sort_order", "None")
            if sort_order_type == "Movies First":
                order_by_clauses.append("CASE type WHEN 'movie' THEN 0 ELSE 1 END")
            elif sort_order_type == "Episodes First":
                order_by_clauses.append("CASE type WHEN 'episode' THEN 0 ELSE 1 END")

            sort_by_release_date = get_setting("Queue", "sort_by_release_date_desc", False)
            if sort_by_release_date:
                 # Sort newest first, NULLs/Unknown last
                 # SQLite treats NULL as smallest, so DESC puts them last
                order_by_clauses.append("release_date DESC")

            # Add sorting by show, season, and episode for deterministic episode processing order
            order_by_clauses.append("imdb_id ASC") # Group by show
            order_by_clauses.append("CASE WHEN type = 'episode' THEN season_number ELSE NULL END ASC") # Sort episodes by season (NULLS FIRST/LAST depends on DB)
            order_by_clauses.append("CASE WHEN type = 'episode' THEN episode_number ELSE NULL END ASC") # Then by episode

            # Content Source Priority (harder in pure SQL, apply *after* fetching batch)
            content_source_priority = get_setting("Queue", "content_source_priority", "")
            source_priority_list = [s.strip() for s in content_source_priority.split(',') if s.strip()]

            if order_by_clauses:
                query += " ORDER BY " + ", ".join(order_by_clauses)

            # Fetch a limited batch of candidates (e.g., double the allowed count to allow for filtering/unreleased)
            # Fetch more than needed to allow filtering/sorting before processing limit
            fetch_limit = int(allowed_to_add_count * 1.5) + 20 # Fetch a bit more than strictly needed
            query += f" LIMIT ?"
            params.append(fetch_limit)

            conn = None
            candidate_items = []
            try:
                conn = get_db_connection()
                cursor = conn.execute(query, params)
                candidate_items_raw = cursor.fetchall()
                candidate_items = [dict(row) for row in candidate_items_raw]
            except Exception as e_fetch:
                logging.error(f"Error fetching wanted items from DB: {e_fetch}", exc_info=True)
                return False # Stop processing if DB fetch fails
            finally:
                if conn:
                    conn.close()

            if not candidate_items:
                #logging.debug("No items found in Wanted state to process.")
                return True

            # Apply content source sorting in Python if needed
            if source_priority_list:
                def get_source_priority(item):
                    source = item.get('content_source', '')
                    # Determine priority_index based on source
                    if source in source_priority_list:
                        priority_index = source_priority_list.index(source)
                    else:
                        priority_index = len(source_priority_list)

                    # Safely get imdb_id, defaulting None to an empty string for consistent sorting
                    imdb_id_val = item.get('imdb_id')
                    safe_imdb_id = imdb_id_val if imdb_id_val is not None else ''

                    # Safely get season_number
                    # For episodes, if season_number is None, use float('-inf') to sort them first.
                    # For non-episodes, use float('inf') to sort them after episodes on this key part.
                    if item['type'] == 'episode':
                        season_num_val = item.get('season_number')
                        safe_season_num = season_num_val if season_num_val is not None else float('-inf')
                    else:
                        safe_season_num = float('inf')

                    # Safely get episode_number
                    # Similar logic as season_number.
                    if item['type'] == 'episode':
                        episode_num_val = item.get('episode_number')
                        safe_episode_num = episode_num_val if episode_num_val is not None else float('-inf')
                    else:
                        safe_episode_num = float('inf')

                    return (
                        priority_index,
                        safe_imdb_id,
                        safe_season_num,
                        safe_episode_num
                    )
                candidate_items.sort(key=get_source_priority)

            # 3. Process Candidate Items
            current_datetime = datetime.now()
            # items_to_move_scraping = [] # Defined implicitly by checking count < allowed_to_add_count
            # items_to_move_unreleased = [] # Defined implicitly by moving directly

            for item in candidate_items:
                processed_item_count += 1
                item_id = item['id']
                item_identifier = queue_manager.generate_identifier(item)

                try:
                    # Check 1: Reconciliation (Skip if already exists in Collected/Upgrading)
                    # Use the DB check function directly
                    if check_existing_media_item(item, item.get('version'), ['Collected', 'Upgrading']):
                         logging.info(f"Item ID {item_id} (Version: {item.get('version')}) already exists in Collected/Upgrading state. Removing duplicate from Wanted.")
                         remove_from_media_items(item_id) # Remove directly
                         continue # Skip processing this item

                    # Handle early release first - bypass other checks
                    if item.get('early_release', False):
                        if moved_to_scraping_count < allowed_to_add_count:
                            logging.info(f"Early release item {item_identifier} - moving to Scraping.")
                            queue_manager.move_to_scraping(item, "Wanted")
                            moved_to_scraping_count += 1
                        else:
                             logging.debug(f"Skipping early release item {item_identifier} due to scraping queue throttle.")
                        continue # Process next item

                    # Check 2: Release Date & Time Logic (Now happens *after* early release check)
                    release_date_str = item.get('release_date')
                    airtime_str = item.get('airtime')
                    version = item.get('version')
                    is_magnet_assigned = item.get('content_source') == 'Magnet_Assigner'

                    # Check if version requires physical release
                    scraping_versions = get_setting('Scraping', 'versions', {})
                    version_settings = scraping_versions.get(version, {})
                    require_physical = version_settings.get('require_physical_release', False)
                    physical_release_date = item.get('physical_release_date')

                    # Move to Unreleased if physical required but missing (and not magnet assigned)
                    # This check is now safe because early_release items already continued
                    if not is_magnet_assigned and require_physical and not physical_release_date:
                        logging.info(f"Item {item_identifier} requires physical release date but none available. Moving to Unreleased.")
                        queue_manager.move_to_unreleased(item, "Wanted")
                        moved_to_unreleased_count += 1
                        continue

                    # Move to Unreleased if release date unknown/invalid (and not magnet assigned)
                    # This check is now safe because early_release items already continued
                    release_date = None
                    if not is_magnet_assigned and (not release_date_str or str(release_date_str).lower() in ['unknown', 'none']):
                         logging.debug(f"Item {item_identifier} has no valid release date. Moving to Unreleased.")
                         queue_manager.move_to_unreleased(item, "Wanted")
                         moved_to_unreleased_count += 1
                         continue
                    elif release_date_str and str(release_date_str).lower() not in ['unknown', 'none']:
                         try:
                             if require_physical and physical_release_date:
                                 release_date = datetime.strptime(physical_release_date, '%Y-%m-%d').date()
                             else:
                                 release_date = datetime.strptime(str(release_date_str), '%Y-%m-%d').date()
                         except ValueError:
                              if not is_magnet_assigned:
                                  logging.warning(f"Invalid release date format for item {item_identifier}: {release_date_str}. Moving to Unreleased.")
                                  queue_manager.move_to_unreleased(item, "Wanted")
                                  moved_to_unreleased_count += 1
                              else:
                                  # If magnet assigned, proceed even with bad date
                                  logging.warning(f"Invalid release date format for Magnet Assigned item {item_identifier}: {release_date_str}. Treating as ready.")
                                  release_date = current_datetime.date() # Use today's date
                              continue # Skip rest of date logic if moved or handled

                    # If Magnet Assigned and date was bad/missing, release_date is now set to today
                    # If not Magnet Assigned and date was bad/missing, we continued above

                    # Calculate effective scrape time (only if release_date is valid)
                    effective_scrape_time = None
                    if release_date:
                        # Parse airtime (defaulting as before)
                        airtime = None
                        if airtime_str:
                            try: airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                            except ValueError:
                                try: airtime = datetime.strptime(airtime_str, '%H:%M').time()
                                except ValueError: airtime = datetime.strptime("00:00", '%H:%M').time()
                        else: airtime = datetime.strptime("00:00", '%H:%M').time()

                        # Calculate base release datetime
                        release_datetime = datetime.combine(release_date, airtime)

                        # Apply offset
                        offset_hours = 0.0
                        if item['type'] == 'movie':
                            movie_offset_setting = get_setting("Queue", "movie_airtime_offset", "19") # Get as string initially
                            try:
                                offset_hours = float(movie_offset_setting)
                            except (ValueError, TypeError):
                                logging.warning(f"Invalid movie_airtime_offset setting ('{movie_offset_setting}'). Using default 19.")
                                offset_hours = 19.0 # Default float value
                        elif item['type'] == 'episode':
                            episode_offset_setting = get_setting("Queue", "episode_airtime_offset", "0") # Get as string initially
                            try:
                                 offset_hours = float(episode_offset_setting)
                            except (ValueError, TypeError):
                                 logging.warning(f"Invalid episode_airtime_offset setting ('{episode_offset_setting}'). Using default 0.")
                                 offset_hours = 0.0 # Default float value

                        effective_scrape_time = release_datetime + timedelta(hours=offset_hours)

                    # Check if ready to move
                    item_is_ready = False
                    if is_magnet_assigned:
                        item_is_ready = True # Always ready if magnet assigned
                    elif effective_scrape_time and effective_scrape_time <= current_datetime:
                        item_is_ready = True # Ready if scrape time is past

                    if item_is_ready:
                        if moved_to_scraping_count < allowed_to_add_count:
                            reason = "Magnet Assigned" if is_magnet_assigned else "Release time met"
                            logging.debug(f"Item {item_identifier} ready ({reason}). Moving to Scraping.")
                            queue_manager.move_to_scraping(item, "Wanted")
                            moved_to_scraping_count += 1
                        else:
                             logging.debug(f"Item {item_identifier} is ready but scraping queue hit limit. Keeping in Wanted.")
                             # Since we fetched a limited batch, we might stop early anyway
                             break # Stop processing further candidates if throttle limit reached
                    elif effective_scrape_time: # Item not ready, check if it should go to Unreleased
                        time_until_release = effective_scrape_time - current_datetime
                        if time_until_release > timedelta(hours=24):
                             release_type_log = "physical release" if require_physical and physical_release_date else "release"
                             logging.debug(f"Item {item_identifier} is more than 24 hours away from {release_type_log}. Moving to Unreleased.")
                             queue_manager.move_to_unreleased(item, "Wanted")
                             moved_to_unreleased_count += 1
                        # else: Within 24 hours, keep in Wanted

                    # Check throttle limit again after processing item
                    if moved_to_scraping_count >= allowed_to_add_count:
                         logging.debug("Reached scraping queue add limit during processing. Stopping.")
                         break # Exit loop

                except Exception as e_item:
                    logging.error(f"Error processing wanted item {item.get('id', 'Unknown')}: {str(e_item)}", exc_info=True)
                    continue # Skip to next item on error

            # Log summary
            #logging.info(f"Wanted queue processing complete. Processed Candidates: {processed_item_count}/{len(candidate_items)}, Moved to Scraping: {moved_to_scraping_count}, Moved to Unreleased: {moved_to_unreleased_count}")

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