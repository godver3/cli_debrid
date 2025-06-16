import logging
from typing import Dict, Any, List
from datetime import datetime, date, timedelta, time as dt_time
import json
import time
import os
import re

from utilities.settings import get_setting
from scraper.scraper import scrape
from database.not_wanted_magnets import is_magnet_not_wanted, is_url_not_wanted
from cli_battery.app.direct_api import DirectAPI
from routes.notifications import send_upgrade_failed_notification


class ScrapingQueue:
    def __init__(self):
        self.items = []
        # Use a set for efficient ID lookup of in-memory items
        self._item_ids = set()

    def update(self):
        """Synchronize the in-memory queue with the database state."""
        from database import get_all_media_items, get_media_item_by_id
        
        # Specific ID for debugging
        DEBUG_ITEM_ID_UPDATE = '177245'
        # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] ScrapingQueue.update() called.")
        
        # Fetch current items with 'Scraping' state from DB
        db_items_raw = get_all_media_items(state="Scraping")
        db_items_dict = {item['id']: dict(item) for item in db_items_raw}
        db_item_ids_from_db_query = set(db_items_dict.keys())
        
        if DEBUG_ITEM_ID_UPDATE in db_item_ids_from_db_query:
            debug_item_from_db = db_items_dict.get(DEBUG_ITEM_ID_UPDATE)
            if debug_item_from_db:
                # l ogging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] Found in DB fetch. DB State: {debug_item_from_db.get('state')}, DB force_priority: {debug_item_from_db.get('force_priority')}")
                pass
        else:
            # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] NOT found in DB fetch with state='Scraping'.")
            pass
        
        # --- Synchronization Logic --- 
        new_items_list = []
        updated_item_ids_this_cycle = set()
        removed_item_ids_this_cycle = set()

        # 1. Iterate through current in-memory items (self.items)
        for index, mem_item in enumerate(self.items):            
            mem_item_id = mem_item.get('id')
            if mem_item_id in db_items_dict: 
                # Item is in memory AND still in DB as 'Scraping'. Refresh its data.
                fresh_db_data = db_items_dict[mem_item_id]
                new_items_list.append(fresh_db_data) # Use the fresh data
                updated_item_ids_this_cycle.add(mem_item_id)
                if str(mem_item_id) == DEBUG_ITEM_ID_UPDATE:
                    # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] REFRESHED in-memory item with DB data. New force_priority for sorting: {fresh_db_data.get('force_priority')}")
                    pass
            else:
                # Item was in memory but is NO LONGER in DB as 'Scraping'. It will be dropped.
                removed_item_ids_this_cycle.add(mem_item_id)
                if str(mem_item_id) == DEBUG_ITEM_ID_UPDATE:
                    # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] To be REMOVED from memory (no longer in DB as Scraping).")
                    pass

        # 2. Add items that are in DB as 'Scraping' but were not previously in memory.
        for db_item_id, db_item_data in db_items_dict.items():
            if db_item_id not in updated_item_ids_this_cycle and db_item_id not in removed_item_ids_this_cycle:
                # This means it was not in self.items before, so it's a truly new add to memory
                new_items_list.append(db_item_data)
                if str(db_item_id) == DEBUG_ITEM_ID_UPDATE:
                    # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] ADDED to memory (was in DB as Scraping but not in memory). Force_priority for sorting: {db_item_data.get('force_priority')}")
                    pass
        
        self.items = new_items_list
        self._item_ids = {item.get('id') for item in self.items} # Rebuild the ID set

        if removed_item_ids_this_cycle:
            # logging.debug(f"Removed {len(removed_item_ids_this_cycle)} items from ScrapingQueue memory (state changed in DB or item deleted).")
            pass
        added_count = len(self._item_ids) - (len(updated_item_ids_this_cycle) + len(removed_item_ids_this_cycle) - len(removed_item_ids_this_cycle.intersection(updated_item_ids_this_cycle)) ) # complex way to say newly added
        truly_added_ids = set(db_items_dict.keys()) - (updated_item_ids_this_cycle | removed_item_ids_this_cycle)

        if truly_added_ids:
            # logging.debug(f"Added {len(truly_added_ids)} new items to ScrapingQueue memory (found in DB). Example ID: {next(iter(truly_added_ids)) if truly_added_ids else 'N/A'}")
            pass
        if updated_item_ids_this_cycle:
            # logging.debug(f"Refreshed {len(updated_item_ids_this_cycle)} existing items in ScrapingQueue memory with latest DB data.")
            pass

        # --- Sorting Logic (applied after synchronization) ---
        # Get the queue sort order setting
        sort_order = get_setting("Queue", "queue_sort_order", "None")
        
        # Get content source priority setting
        content_source_priority = get_setting("Queue", "content_source_priority", "")
        source_priority_list = [s.strip() for s in content_source_priority.split(',') if s.strip()]
        
        # First sort by content source priority
        if source_priority_list:
            def get_source_priority(item):
                source = item.get('content_source', '')
                try:
                    # Sources in the priority list are ordered by their position
                    # Sources not in the list go last (hence the len(source_priority_list))
                    return source_priority_list.index(source) if source in source_priority_list else len(source_priority_list)
                except ValueError:
                    return len(source_priority_list)
            
            self.items.sort(key=get_source_priority)
        
        # Then apply type-based sorting if specified
        if sort_order == "Movies First":
            self.items.sort(key=lambda x: 0 if x['type'] == 'movie' else 1)
        elif sort_order == "Episodes First":
            self.items.sort(key=lambda x: 0 if x['type'] == 'episode' else 1)
        # For "None", we keep the default order

        # --- Secondary Sorting by Release Date ---
        sort_by_release_date = get_setting("Queue", "sort_by_release_date_desc", False)
        if sort_by_release_date:
            def get_release_date_key(item):
                release_date_str = item.get('release_date')
                if release_date_str and release_date_str != 'Unknown':
                    try:
                        # Parse date and return it for sorting (newest first means reverse=True later)
                        return datetime.strptime(release_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        pass # Invalid date format, treat as unknown
                # Assign a very old date to unknown/invalid dates to sort them last
                return date.min

            # Sort by release date, newest first. Unknown dates are handled by date.min
            self.items.sort(key=get_release_date_key, reverse=True)

        # Sort: Ensure episodes are processed in order (S/E ascending) within their priority groups
        self.items.sort(key=lambda item: (
            # Preserve primary sort orders (source priority, type, release date implicitly handled by sorting earlier)
            # Then sort episodes explicitly by show/season/episode
            item.get('imdb_id') or '', # Group by show, ensuring None becomes ''
            item.get('season_number') if item.get('type') == 'episode' and item.get('season_number') is not None else float('inf'), # Sort episodes by season (handle None)
            item.get('episode_number') if item.get('type') == 'episode' and item.get('episode_number') is not None else float('inf') # Then by episode number (handle None)
        ))

        # --- Force Priority Sorting (NEW) ---
        # This sort should be last to ensure forced items are at the very top,
        # overriding other sorting criteria for their position at the front.
        # Assumes force_priority is a boolean or integer (0/1).
        # `not item.get('force_priority', False)` makes True values (forced) sort before False values.
        self.items.sort(key=lambda item: not item.get('force_priority', False))

        # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] After all sorting in ScrapingQueue.update():")
        if self.items:
            # Correctly create a list of IDs for the f-string
            first_three_ids = [it.get('id') for it in self.items[:3]]
            # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] First 3 items in self.items: {first_three_ids}")
            found_debug_item_in_sorted_list = False
            for i, s_item in enumerate(self.items):
                if str(s_item.get('id')) == DEBUG_ITEM_ID_UPDATE:
                    # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] Found in self.items at index {i}. Its force_priority: {s_item.get('force_priority')}")
                    found_debug_item_in_sorted_list = True
                    break
            if not found_debug_item_in_sorted_list:
                # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] NOT found in self.items after sorting.")
                pass
        else:
            # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID_UPDATE}] self.items is EMPTY after sorting.")
            pass

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        """Add an item to the in-memory queue if not already present."""
        item_id = item.get('id')
        if item_id and item_id not in self._item_ids:
            self.items.append(item)
            self._item_ids.add(item_id)
        elif not item_id:
             logging.warning("Attempted to add item without ID to ScrapingQueue.")
        # else: item already exists in memory

    def remove_item(self, item: Dict[str, Any]):
        """Remove an item from the in-memory queue."""
        item_id = item.get('id')
        if item_id and item_id in self._item_ids:
            self.items = [i for i in self.items if i['id'] != item_id]
            self._item_ids.remove(item_id)
        elif not item_id:
            logging.warning("Attempted to remove item without ID from ScrapingQueue.")
        # else: item not found in memory

    def reset_not_wanted_check(self, item_id):
        """Reset the disable_not_wanted_check flag after scraping is complete"""
        from database import get_db_connection
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE media_items 
                SET disable_not_wanted_check = FALSE
                WHERE id = ?
            """, (item_id,))
            conn.commit()
        except Exception as e:
            logging.error(f"Error resetting disable_not_wanted_check flag: {str(e)}")
        finally:
            conn.close()

    def process(self, queue_manager):
        from database import get_all_media_items, get_media_item_by_id, get_db_connection, get_wake_count
        processed_count = 0
        had_error = False
        today = date.today()
        processed_an_item_this_cycle = False # Flag to indicate if we attempted processing

        # Specific ID for debugging
        DEBUG_ITEM_ID = '177245'

        if self.items:
            # Peek at the first item instead of popping immediately
            item_to_process = self.items[0]
            item_id_being_processed = item_to_process['id'] # Store ID for later check
            item_identifier = queue_manager.generate_identifier(item_to_process)
            processed_successfully_or_moved = False # Flag to track if item was moved/handled

            if str(item_id_being_processed) == DEBUG_ITEM_ID:
                # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Now being processed at the start of ScrapingQueue.process(). Force_priority: {item_to_process.get('force_priority', False)}")
                pass

            # --- Alternate scrape time strategy ---
            if str(item_id_being_processed) == DEBUG_ITEM_ID:
                # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Checking alternate scrape window. Item date: {item_to_process.get('release_date')}, Airtime: {item_to_process.get('airtime')}")
                pass
            
            # NEW: Check physical release requirement before alternate scrape window logic
            # This prevents items with future physical release dates from bouncing between queues
            scraping_versions = get_setting('Scraping', 'versions', {})
            version_settings = scraping_versions.get(item_to_process.get('version', ''), {})
            require_physical = version_settings.get('require_physical_release', False)
            is_magnet_assigned = item_to_process.get('content_source') == 'Magnet_Assigner'
            
            if not is_magnet_assigned and item_to_process.get('type') == 'movie' and require_physical:
                physical_release_date = item_to_process.get('physical_release_date')
                if physical_release_date:
                    try:
                        physical_date = datetime.strptime(physical_release_date, '%Y-%m-%d').date()
                        if physical_date > today:
                            if str(item_id_being_processed) == DEBUG_ITEM_ID:
                                # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Has future physical release date ({physical_date}). Moving to Wanted regardless of alternate scrape window.")
                                pass
                            logging.info(f"Movie {item_identifier} has a future physical release date ({physical_date}). Moving back to Wanted queue.")
                            queue_manager.move_to_wanted(item_to_process, "Scraping")
                            self.remove_item(item_to_process)
                            return True
                    except ValueError:
                        logging.warning(f"Invalid physical release date format for movie {item_identifier}: {physical_release_date}")
                        # Continue with normal processing if date format is invalid
                        pass
                # If no physical date or physical date has passed, continue with alternate scrape window check
            
            # Now check alternate scrape window (only if physical release check passed)
            alt_scrape_check_result = self._is_within_alternate_scrape_window(item_to_process, datetime.now())
            if not alt_scrape_check_result:
                if str(item_id_being_processed) == DEBUG_ITEM_ID:
                    # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] FAILED alternate scrape window check. Moving to Wanted.")
                    pass
                # logging.info(f"{item_identifier} is not within the alternate scrape window. Moving back to Wanted queue.")
                queue_manager.move_to_wanted(item_to_process, "Scraping")
                self.remove_item(item_to_process)
                return True
            elif str(item_id_being_processed) == DEBUG_ITEM_ID:
                # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] PASSED alternate scrape window check.")
                pass

            # --- START: Check if related item is in Adding Queue --- 
            item_imdb_id = item_to_process.get('imdb_id')
            if item_imdb_id: # Only check if IMDb ID exists
                if str(item_id_being_processed) == DEBUG_ITEM_ID:
                    # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Checking if related item (IMDb: {item_imdb_id}) is in Adding Queue.")
                    pass
                try:
                    adding_queue = queue_manager.queues.get("Adding")
                    if adding_queue and adding_queue.get_contents(): # Check if Adding queue exists and has items
                        is_in_adding = any(adding_item.get('imdb_id') == item_imdb_id for adding_item in adding_queue.get_contents())
                        if is_in_adding:
                            if str(item_id_being_processed) == DEBUG_ITEM_ID:
                                # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] DEFERRED - related item found in Adding Queue.")
                                pass
                            # logging.info(f"Deferring processing for {item_identifier} (IMDb: {item_imdb_id}) - related item found in Adding Queue.")
                            return False # Defer processing, keep item in queue
                        elif str(item_id_being_processed) == DEBUG_ITEM_ID:
                            # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] PASSED Adding Queue check (no related item found).")
                            pass
                except Exception as e:
                    if str(item_id_being_processed) == DEBUG_ITEM_ID:
                        # logging.error(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Error checking Adding Queue: {e}")
                        pass
                    # logging.error(f"Error checking Adding Queue for {item_identifier}: {e}")
                    pass
            elif str(item_id_being_processed) == DEBUG_ITEM_ID:
                # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] SKIPPED Adding Queue check (no IMDb ID).")
                pass
            # --- END: Check if related item is in Adding Queue ---

            try:
                if str(item_id_being_processed) == DEBUG_ITEM_ID:
                    # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Entering main try block for scraping. Item details: EarlyRelease={item_to_process.get('early_release', False)}, ContentSource={item_to_process.get('content_source')}, ReleaseDate='{item_to_process.get('release_date')}'")
                    pass

                # logging.info(f"Starting to process scraping results for {item_identifier}")
                processed_an_item_this_cycle = True # Mark that we started processing

                # --- START EDIT: Add content source check ---
                is_magnet_assigned = item_to_process.get('content_source') == 'Magnet_Assigner'
                # --- END EDIT ---

                # Check release date logic - skip for early release items AND magnet assigned items
                # --- Use item_to_process instead of item throughout ---
                if not item_to_process.get('early_release', False) and not is_magnet_assigned: # <-- Added is_magnet_assigned check
                    if str(item_id_being_processed) == DEBUG_ITEM_ID:
                        # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Evaluating standard release date logic. EarlyRelease={item_to_process.get('early_release', False)}, IsMagnetAssigned={is_magnet_assigned}")
                        pass
                    if item_to_process['release_date'] == 'Unknown':
                        if str(item_id_being_processed) == DEBUG_ITEM_ID:
                            # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Release date is 'Unknown'. Moving to Wanted.")
                            pass
                        # logging.info(f"Item {item_identifier} has an unknown release date. Moving back to Wanted queue.")
                        queue_manager.move_to_wanted(item_to_process, "Scraping")
                        processed_successfully_or_moved = True # Handled by move
                        processed_count += 1
                        # No return here, let finally handle removal check if needed
                elif is_magnet_assigned:
                    logging.info(f"Processing Magnet Assigned item {item_identifier} regardless of release date")
                elif item_to_process.get('early_release', False): # Existing early release logic
                    logging.info(f"Processing early release item {item_identifier} regardless of release date")

                # Proceed only if the item wasn't immediately moved back to Wanted
                if not processed_successfully_or_moved:
                    try:
                        # --- Physical Release Check is now handled earlier before alternate scrape window ---
                        # Only need to handle missing physical release date case here
                        scraping_versions = get_setting('Scraping', 'versions', {})
                        version_settings = scraping_versions.get(item_to_process.get('version', ''), {})
                        require_physical = version_settings.get('require_physical_release', False)

                        if not is_magnet_assigned and item_to_process.get('type') == 'movie' and require_physical:
                            physical_release_date = item_to_process.get('physical_release_date')
                            if not physical_release_date:  # Only check for missing physical date
                                logging.info(f"Movie {item_identifier} requires physical release but no date available. Moving back to Wanted queue.")
                                queue_manager.move_to_wanted(item_to_process, "Scraping")
                                processed_successfully_or_moved = True
                        
                        # --- Digital Release Check (bypassed by early_release, magnet_assigner, or if already moved) ---
                        if not processed_successfully_or_moved and not item_to_process.get('early_release', False) and not is_magnet_assigned:
                            release_date_str = item_to_process.get('release_date')
                            if release_date_str == 'Unknown':
                                logging.info(f"Item {item_identifier} has an unknown release date. Moving back to Wanted queue.")
                                queue_manager.move_to_wanted(item_to_process, "Scraping")
                                processed_successfully_or_moved = True
                            else:
                                release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                                if release_date > today:
                                    logging.info(f"Item {item_identifier} has a future release date ({release_date}). Moving back to Wanted queue.")
                                    queue_manager.move_to_wanted(item_to_process, "Scraping")
                                    processed_successfully_or_moved = True

                    except ValueError:
                         # --- START EDIT: Add content source check ---
                        # Only move back if not magnet assigned and date is bad
                        if not is_magnet_assigned:
                            if str(item_id_being_processed) == DEBUG_ITEM_ID:
                                # logging.warning(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Invalid release date format: {item_to_process['release_date']}. Moving to Wanted.")
                                pass
                            # logging.warning(f"Item {item_identifier} has an invalid release date format: {item_to_process['release_date']}. Moving back to Wanted queue.")
                            queue_manager.move_to_wanted(item_to_process, "Scraping")
                            processed_successfully_or_moved = True
                        else:
                            if str(item_id_being_processed) == DEBUG_ITEM_ID:
                                # logging.warning(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Magnet Assigned. Invalid release date format: {item_to_process['release_date']}. Proceeding.")
                                pass
                            # Log but allow Magnet Assigned items to proceed
                            # logging.warning(f"Magnet Assigned item {item_identifier} has an invalid release date format: {item_to_process['release_date']}. Proceeding anyway.")
                            pass
                        # --- END EDIT ---
                        # Removed return

                # Proceed only if the item wasn't moved back to Wanted due to release date
                if not processed_successfully_or_moved:
                    # --- Multi-pack check logic ---
                    is_multi_pack = False # Default to false

                    if str(item_id_being_processed) == DEBUG_ITEM_ID and not processed_successfully_or_moved:
                        # logging.info(f"[DEBUG_ITEM_{DEBUG_ITEM_ID}] Passed all preliminary checks. Proceeding to multi-pack logic and scraping attempts.")
                        pass

                    can_attempt_multi_pack = False # Assume false unless it's a valid episode case

                    if item_to_process['type'] == 'episode':
                        logging.info(f"Checking multi-pack eligibility for {item_to_process['title']} S{item_to_process['season_number']:02d}E{item_to_process['episode_number']:02d}")
                        can_attempt_multi_pack = True # Eligible for check if it's an episode
                        show_metadata, _ = DirectAPI.get_show_metadata(item_to_process['imdb_id'])

                        # Calculate other pending episodes for the show *once*
                        other_pending_episodes = []
                        if show_metadata: # Only check if we have metadata
                            all_items_for_show = get_all_media_items(imdb_id=item_to_process['imdb_id'])
                            other_pending_episodes = [
                                ep for ep in all_items_for_show
                                if ep.get('type') == 'episode' # Ensure it's an episode
                                and ep.get('id') != item_to_process['id'] # Exclude the current item
                                and ep.get('state') in ["Wanted", "Scraping"]
                            ]
                            logging.info(f"Found {len(other_pending_episodes)} other pending episodes for this show in Wanted/Scraping state.")


                        if show_metadata and 'seasons' in show_metadata:
                            season_num = item_to_process['season_number'] # Use integer directly
                            if season_num in show_metadata['seasons']: # Check for integer key
                                season_data = show_metadata['seasons'][season_num] # Access using integer key
                                if 'episodes' in season_data:
                                    # Check if this is the season finale
                                    total_episodes = len(season_data['episodes'])
                                    is_finale = item_to_process['episode_number'] == total_episodes

                                    if is_finale:
                                        logging.info(f"Episode {item_to_process['episode_number']} is the season finale.")
                                        # Use the pre-calculated list
                                        if not other_pending_episodes:
                                            logging.info("No other pending episodes found for this show. Disabling multi-pack search for this finale.")
                                            can_attempt_multi_pack = False # Disable if finale and no others pending anywhere in the show
                                        else:
                                            logging.info(f"Other pending episodes exist. Multi-pack search remains possible for this finale.")
                                            # can_attempt_multi_pack remains True

                                    # Only check air dates if multi-pack is still a possibility
                                    if can_attempt_multi_pack:
                                        # First pass - log all episode dates
                                        logging.info(f"Checking air dates for {total_episodes} episodes in season {season_num}:")
                                        sorted_episodes = sorted(season_data['episodes'].items(), key=lambda x: int(x[0]))
                                        for ep_num, ep_data in sorted_episodes:
                                            first_aired = ep_data.get('first_aired', 'unknown')
                                            if first_aired and first_aired != 'unknown':
                                                try:
                                                    # Parse ISO 8601 datetime and convert to date
                                                    air_date = datetime.strptime(first_aired.split('T')[0], '%Y-%m-%d').date()
                                                    status = "future" if air_date > today else "aired"
                                                    logging.info(f"  Episode {ep_num}: {air_date} ({status})")
                                                except (ValueError, TypeError):
                                                    logging.info(f"  Episode {ep_num}: {first_aired} (invalid format)")
                                            else:
                                                logging.info(f"  Episode {ep_num}: unknown air date")

                                        # --- START: New logic for 'old season' check ---
                                        is_likely_old_season = False
                                        one_year_ago = today - timedelta(days=365)
                                        for ep_num, ep_data in season_data['episodes'].items():
                                            first_aired_str = ep_data.get('first_aired')
                                            if first_aired_str:
                                                try:
                                                    air_date = datetime.strptime(first_aired_str.split('T')[0], '%Y-%m-%d').date()
                                                    if air_date < one_year_ago:
                                                        logging.info(f"Found episode {ep_num} aired on {air_date} (more than a year ago). Assuming season is old enough for multi-pack.")
                                                        is_likely_old_season = True
                                                        break # Found one, no need to check others
                                                except (ValueError, TypeError):
                                                    continue # Ignore episodes with invalid date formats for this check
                                        # --- END: New logic for 'old season' check ---


                                        # Check for any unaired episodes (original logic)
                                        has_unaired_episodes = False
                                        for ep_num, ep_data in season_data['episodes'].items(): # Iterate again for the original check
                                            if 'first_aired' not in ep_data or not ep_data['first_aired']:
                                                logging.info(f"Episode {ep_data.get('episode_number', 'unknown')} has unknown air date")
                                                has_unaired_episodes = True
                                                break
                                            try:
                                                air_date = datetime.strptime(ep_data['first_aired'].split('T')[0], '%Y-%m-%d').date()
                                                if air_date > today:
                                                    has_unaired_episodes = True
                                                    logging.info(f"Episode {ep_data.get('episode_number', 'unknown')} hasn't aired yet (releases {air_date})")
                                                    break
                                            except (ValueError, TypeError):
                                                logging.info(f"Episode {ep_data.get('episode_number', 'unknown')} has invalid air date format")
                                                has_unaired_episodes = True
                                                break

                                        # Enable multi-pack if all episodes have aired OR if it's likely an old season,
                                        # AND it wasn't disabled by the finale check,
                                        # AND there are other pending episodes for the show.
                                        if not has_unaired_episodes or is_likely_old_season:
                                            if other_pending_episodes:
                                                is_multi_pack = True # Enable multi-pack
                                                if is_likely_old_season and has_unaired_episodes:
                                                    logging.info("Enabling multi-pack based on 'old season' heuristic despite some unknown/invalid dates.")
                                                elif not has_unaired_episodes:
                                                     logging.info("All episodes have aired and other episodes are pending - enabling multi-pack")
                                                # else: (is_likely_old_season and not has_unaired_episodes) - handled by the above case
                                            else: # No other pending episodes for the show
                                                if not has_unaired_episodes:
                                                    logging.info("All episodes have aired, but no other episodes are pending for this show - using single episode scrape")
                                                elif is_likely_old_season:
                                                    logging.info("Season is likely old, but no other episodes are pending for this show - using single episode scrape")
                                                # is_multi_pack remains False
                                        else: # has_unaired_episodes is True AND not is_likely_old_season
                                            logging.info("Some episodes haven't aired yet in this recent season - skipping multi-pack")
                                            # is_multi_pack remains False
                                    # else: (can_attempt_multi_pack is False)
                                        # is_multi_pack remains False - already logged reason above (finale check)

                                else: # No 'episodes' in season_data
                                    logging.info("No episodes data found in season metadata - skipping multi-pack check")
                                    # is_multi_pack remains False
                            else: # Season not found
                                logging.info(f"Season {season_num} not found in show metadata - skipping multi-pack check") # Log integer season
                                # is_multi_pack remains False
                        else: # No 'seasons' in show_metadata
                            logging.info("No seasons data found in show metadata - skipping multi-pack check")
                            # is_multi_pack remains False
                    # else: Not an episode, is_multi_pack remains False

                    # --- End of Multi-pack logic ---

                    # Determine initial check_pack_wantedness based on age
                    check_pack_wantedness_for_initial_scrape = True # Default
                    is_older_than_7_days = False # Flag to check age
                    try:
                        release_date_str = item_to_process.get('release_date')
                        if release_date_str and release_date_str != 'Unknown':
                            release_date_obj = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                            # 'today' is already defined in this method
                            if release_date_obj < (today - timedelta(days=7)):
                                is_older_than_7_days = True
                                check_pack_wantedness_for_initial_scrape = False
                                logging.info(f"Item {item_identifier} release date {release_date_str} is older than 7 days. Disabling pack wantedness for initial scrape.")
                        # If release_date is 'Unknown' or not present, default check_pack_wantedness_for_initial_scrape (True) is used.
                    except ValueError:
                        logging.warning(f"Could not parse release date '{release_date_str}' for {item_identifier} to check age for pack wantedness/multi-pack modification. Defaulting to check_pack_wantedness={check_pack_wantedness_for_initial_scrape}.")

                    # If item is an episode and older than 7 days, force multi-pack for initial scrape
                    if item_to_process['type'] == 'episode' and is_older_than_7_days:
                        if not is_multi_pack: # Log only if we are changing it
                            logging.info(f"Episode {item_identifier} is older than 7 days. Forcing multi-pack scrape for initial attempt.")
                        is_multi_pack = True
                    
                    logging.info(f"Scraping for {item_identifier} (multi-pack: {is_multi_pack}) with initial check_pack_wantedness={check_pack_wantedness_for_initial_scrape}")
                    results, filtered_out_results = self.scrape_with_fallback(
                        item_to_process, 
                        is_multi_pack, 
                        queue_manager, 
                        check_pack_wantedness=check_pack_wantedness_for_initial_scrape # Use the determined value
                    )

                    # Ensure both results and filtered_out_results are lists
                    results = results if results is not None else []
                    filtered_out_results = filtered_out_results if filtered_out_results is not None else []

                    # Filter and process results from the first attempt
                    filtered_results = []
                    if results: # Only filter if there are raw results
                        for result in results:
                            if not item_to_process.get('disable_not_wanted_check'):
                                if is_magnet_not_wanted(result['magnet']):
                                    continue
                                if is_url_not_wanted(result['magnet']):
                                    continue
                            filtered_results.append(result)

                    # --- START: Delayed Scrape Based on Score Logic ---
                    delayed_scrape_enabled = get_setting("Debug", "delayed_scrape_based_on_score", False)
                    delayed_scrape_time_limit = float(get_setting("Debug", "delayed_scrape_time_limit", 6.0))
                    minimum_scrape_score = float(get_setting("Debug", "minimum_scrape_score", 0.0))
                    now = datetime.now()
                    release_date_str = item_to_process.get('release_date')
                    airtime_str = item_to_process.get('airtime')
                    release_datetime = None
                    if release_date_str and release_date_str != 'Unknown':
                        try:
                            release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                            # Parse airtime, fallback to 00:00
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
                            
                            # If a movie has no specific airtime, shift the base release time to the start of the next day.
                            # The user-defined offset will then be applied to this adjusted time.
                            if item_to_process.get('type') == 'movie' and not airtime_str:
                                release_datetime += timedelta(days=1)

                            # Apply offset based on type
                            offset_hours = 0.0
                            if item_to_process.get('type') == 'movie':
                                offset_setting = get_setting("Queue", "movie_airtime_offset", "19")
                                try:
                                    offset_hours = float(offset_setting)
                                except (ValueError, TypeError):
                                    offset_hours = 19.0
                            elif item_to_process.get('type') == 'episode':
                                offset_setting = get_setting("Queue", "episode_airtime_offset", "0")
                                try:
                                    offset_hours = float(offset_setting)
                                except (ValueError, TypeError):
                                    offset_hours = 0.0
                            release_datetime += timedelta(hours=offset_hours)
                        except Exception:
                            release_datetime = None
                    # --- NEW: Restrict delayed scrape to items released within the past 7 days ---
                    if delayed_scrape_enabled and release_datetime:
                        try:
                            if (now - release_datetime).days > 7:
                                logging.info(f"Delayed scrape disabled for {item_identifier}: release is older than 7 days.")
                                delayed_scrape_enabled = False
                        except Exception:
                            # Fallback: if any error occurs, leave delayed_scrape_enabled unchanged
                            pass
                    # --- END NEW CODE ---
                    # If release_datetime is None, treat as if enough time has passed (allow all results)
                    if delayed_scrape_enabled and minimum_scrape_score > 0:
                        # Split results by score
                        high_score_results = [r for r in filtered_results if r.get('score_breakdown', {}).get('total_score', 0) >= minimum_scrape_score]
                        low_score_results = [r for r in filtered_results if r not in high_score_results]
                        use_low_score = False
                        if not high_score_results and filtered_results:
                            # Only fallback to low score if enough time has passed
                            if release_datetime:
                                hours_since_release = (now - release_datetime).total_seconds() / 3600.0
                                if hours_since_release >= delayed_scrape_time_limit:
                                    use_low_score = True
                                    logging.info(f"Delayed scrape: {hours_since_release:.2f}h since release+airtime, allowing lower scored results for {item_identifier}.")
                                else:
                                    logging.info(f"Delayed scrape: Only {hours_since_release:.2f}h since release+airtime, not enough to allow lower scored results for {item_identifier}.")
                                    filtered_results = [] # Treat as no suitable results
                            else:
                                # No valid release date, allow all results
                                use_low_score = True
                                logging.info(f"Delayed scrape: No valid release date/airtime, allowing lower scored results for {item_identifier}.")
                        if high_score_results:
                            filtered_results = high_score_results
                        elif use_low_score:
                            filtered_results = low_score_results
                        # else: filtered_results already set to [] if not enough time has passed
                    # --- END: Delayed Scrape Based on Score Logic ---

                    # If no filtered results from the first attempt (which includes multi->single internal fallback)
                    # AND the item is an episode, try the final fallback: multi-pack with check_pack_wantedness=False
                    if not filtered_results and item_to_process['type'] == 'episode':
                        logging.info(f"No valid results after initial scraping for {item_identifier}. "
                                     f"Attempting final multi-pack fallback (check_pack_wantedness=False).")
                        
                        # For this final attempt, force multi-pack and disable pack wantedness check
                        fallback_results, fallback_filtered_out = self.scrape_with_fallback(
                            item_to_process, 
                            is_multi_pack=True, # Force multi-pack
                            queue_manager=queue_manager, 
                            check_pack_wantedness=False # Disable pack wantedness
                        )
                        
                        fallback_results = fallback_results if fallback_results is not None else []
                        # fallback_filtered_out = fallback_filtered_out if fallback_filtered_out is not None else [] # Not used directly

                        if fallback_results: # Only filter if there are raw results from fallback
                            current_filtered_fallback_results = []
                            for result in fallback_results:
                                if not item_to_process.get('disable_not_wanted_check'):
                                    if is_magnet_not_wanted(result['magnet']):
                                        continue
                                    if is_url_not_wanted(result['magnet']):
                                        continue
                                current_filtered_fallback_results.append(result)
                            
                            if current_filtered_fallback_results:
                                logging.info(f"Found {len(current_filtered_fallback_results)} results in final multi-pack fallback for {item_identifier}.")
                                filtered_results = current_filtered_fallback_results # Use these results
                            else:
                                logging.info(f"No valid (post-filter) results from final multi-pack fallback for {item_identifier}.")
                        else:
                            logging.info(f"No raw results from final multi-pack fallback for {item_identifier}.")

                    # After all scraping attempts, check if we have any filtered_results
                    if not filtered_results:
                        logging.warning(f"No suitable results found for {item_identifier} after all scraping attempts.")
                        self.handle_no_results(item_to_process, queue_manager)
                        processed_successfully_or_moved = True
                        processed_count += 1
                    else:
                        # We have filtered_results, proceed to process them
                        best_result = filtered_results[0]
                        logging.info(f"Best result for {item_identifier}: {best_result['title']}")

                        if get_setting("Debug", "enable_reverse_order_scraping", default=False):
                            filtered_results.reverse()

                        logging.info(f"Moving {item_identifier} to Adding queue with {len(filtered_results)} results")
                        try:
                            queue_manager.move_to_adding(item_to_process, "Scraping", best_result['title'], filtered_results)
                            self.reset_not_wanted_check(item_to_process['id'])
                            processed_successfully_or_moved = True
                        except Exception as e:
                            logging.error(f"Failed to move {item_identifier} to Adding queue: {str(e)}", exc_info=True)
                            had_error = True
            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                try:
                    # Attempt to move to sleeping on error
                    queue_manager.move_to_sleeping(item_to_process, "Scraping")
                    processed_successfully_or_moved = True # Mark as handled (moved to sleeping)
                except Exception as move_err:
                    logging.error(f"Failed to move item {item_identifier} to sleeping after error: {move_err}")
                    # If move fails, processed_successfully_or_moved remains False
                had_error = True
                # Don't increment processed_count here, let the main logic do it if needed

            finally:
                # Check if the item we processed is *still* at the front of the list.
                # This means it wasn't successfully moved by any of the processing steps or error handling.
                if not processed_successfully_or_moved:
                    # Double check it's the same item we started with
                    if self.items and self.items[0]['id'] == item_id_being_processed:
                        logging.warning(f"Item {item_identifier} completed processing cycle in ScrapingQueue without being moved. Removing explicitly.")
                        self.remove_item(item_to_process) # Remove the item we peeked at
                    elif not self.items or self.items[0]['id'] != item_id_being_processed:
                         logging.warning(f"Item {item_identifier} was expected at index 0 for removal but wasn't found (likely removed concurrently or list empty).")

                # Increment processed count if we actually started processing this item
                if processed_an_item_this_cycle:
                    processed_count += 1

            # --- START EDIT: Fetch setting from Queue section ---
            if processed_an_item_this_cycle:
                # --- START EDIT: Add try-except for float conversion ---
                try:
                    delay_seconds = float(get_setting('Queue', 'item_process_delay_seconds', 0.0))
                except (ValueError, TypeError):
                    delay_seconds = 0.0
                # --- END EDIT ---
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
            # --- END EDIT ---

        # Return True if there are more items potentially left to process in the queue
        # Or if we actually processed an item in this call (even if it resulted in removal)
        return len(self.items) > 0 or processed_an_item_this_cycle

    def scrape_with_fallback(self, item, is_multi_pack, queue_manager, skip_filter=False, check_pack_wantedness: bool = False):
        item_identifier = queue_manager.generate_identifier(item)
        original_season = item.get('season_number')
        original_episode = item.get('episode_number')

        # Get the stored original torrent title for comparison if this is a rescrape
        stored_rescrape_title = item.get('rescrape_original_torrent_title')

        from database import get_media_item_by_id
        if get_media_item_by_id(item['id']).get('fall_back_to_single_scraper'):
            is_multi_pack = False

        results, filtered_out = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            is_multi_pack,  # This will now be False if fall_back_to_single_scraper is True
            item.get('genres'),
            check_pack_wantedness=check_pack_wantedness # Pass parameter
        )

        # Ensure results and filtered_out are lists
        results = results if results is not None else []
        filtered_out = filtered_out if filtered_out is not None else []

        if not skip_filter: # Apply existing filters
            # Filter out unwanted magnets and URLs
            results = [
                r for r in results 
                if not (
                    not item.get('disable_not_wanted_check') and 
                    (is_magnet_not_wanted(r['magnet']) or is_url_not_wanted(r['magnet']))
                )
            ]
            
            # New filter: if stored_rescrape_title exists, filter out results matching it
            if stored_rescrape_title:
                def _normalize_title(title_str: str) -> str:
                    """Normalize a torrent title for loose comparison.

                    - Lower-case
                    - Strip file extension
                    - Remove non-alphanumerics (keeps digits and letters)
                    """
                    if not title_str:
                        return ""
                    # Lower-case first
                    normalized = title_str.lower()
                    # Remove extension
                    normalized = os.path.splitext(normalized)[0]
                    # Collapse non-alphanumeric characters
                    normalized = re.sub(r"[^a-z0-9]+", "", normalized)
                    return normalized

                bad_title_norm = _normalize_title(stored_rescrape_title)

                original_results_count = len(results)
                filtered_list = []
                for r in results:
                    cand_title = r.get('original_title')
                    if cand_title:
                        if _normalize_title(cand_title) == bad_title_norm:
                            # Exact or loose match – skip this result
                            continue
                    filtered_list.append(r)

                results = filtered_list

                if len(results) < original_results_count:
                    logging.info(
                        f"Filtered out {original_results_count - len(results)} results matching stored rescrape title (loose comparison) for {item_identifier}. Bad title: '{stored_rescrape_title}'"
                    )


        is_anime = True if item.get('genres') and 'anime' in item['genres'] else False
        
        # For episodes, filter by exact season/episode match, considering XEM mapping
        if not is_anime:
            if item['type'] == 'episode' and not is_multi_pack:
                # Filter results based on scene mapping if available, otherwise original item S/E
                filtered_results_using_mapping = []
                for r in results:
                    parsed_info = r.get('parsed_info', {})
                    season_episode_info = parsed_info.get('season_episode_info', {})
                    parsed_seasons = season_episode_info.get('seasons', [])
                    parsed_episodes = season_episode_info.get('episodes', [])
                    
                    scene_mapping = r.get('xem_scene_mapping')
                    
                    target_season = None
                    target_episode = None
                    
                    if scene_mapping: # Use scene mapping if present in result
                        target_season = scene_mapping.get('season')
                        target_episode = scene_mapping.get('episode')
                        # logging.debug(f"Using scene mapping S{target_season}E{target_episode} for result: {r.get('original_title')}")
                    else: # Fallback to original item numbers if no mapping attached
                        target_season = original_season
                        target_episode = original_episode
                        # logging.debug(f"Using original item S{target_season}E{target_episode} for result: {r.get('original_title')}")
                        
                    # Perform the check using the determined target season/episode
                    season_match = False
                    episode_match = False
                    item_title_for_check = item.get('title', '').lower()
                    is_formula_1_item = "formula 1" in item_title_for_check

                    if is_formula_1_item and not scene_mapping: # Apply F1 logic only if not using XEM override
                        parsed_torrent_year_val = parsed_info.get('year')
                        actual_torrent_year = None
                        if isinstance(parsed_torrent_year_val, list):
                            if parsed_torrent_year_val and str(parsed_torrent_year_val[0]).isdigit():
                                actual_torrent_year = int(parsed_torrent_year_val[0])
                        elif parsed_torrent_year_val and str(parsed_torrent_year_val).isdigit():
                            actual_torrent_year = int(parsed_torrent_year_val)

                        # For F1, target_season is event year. PTT season from torrent is S01 or empty.
                        season_match = (
                            (not parsed_seasons or parsed_seasons == [1]) and
                            (actual_torrent_year is not None and actual_torrent_year == target_season)
                        )
                        # Episode match for F1: be lenient if torrent has no parsed episodes, or match if it does.
                        episode_match = (target_episode is None or not parsed_episodes or parsed_episodes == [target_episode])
                        
                        # if not (season_match and episode_match):
                        #     logging.debug(f"F1 DEBUG (initial scrape): Post-filter mismatch for '{r.get('original_title')}': "
                        #                   f"Item S{target_season}E{target_episode}. "
                        #                   f"Torrent ParsedYear: {actual_torrent_year}, ParsedSeasonPTT: {parsed_seasons}, ParsedEpisodePTT: {parsed_episodes}. "
                        #                   f"Match S:{season_match}, E:{episode_match}")
                    else: # Original logic for non-F1 or F1 with XEM
                        season_match = (target_season is None or parsed_seasons == [target_season])
                        episode_match = (target_episode is None or parsed_episodes == [target_episode])
                    
                    if season_match and episode_match:
                        filtered_results_using_mapping.append(r)
                    # else: 
                    #    logging.debug(f"Filtering out result {r.get('original_title')} - Parsed: S{parsed_seasons}E{parsed_episodes}, Target: S{target_season}E{target_episode}")
                
                results = filtered_results_using_mapping # Update results list

        if results or item['type'] != 'episode':
            return results, filtered_out

        logging.info(f"No results for multi-pack {item_identifier}. Falling back to individual episode scraping.")

        individual_results, individual_filtered_out = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            False,
            item.get('genres'),
            check_pack_wantedness=check_pack_wantedness # Pass parameter
        )

        # Ensure individual results and filtered_out are lists
        individual_results = individual_results if individual_results is not None else []
        individual_filtered_out = individual_filtered_out if individual_filtered_out is not None else []
        logging.info(f"Individual episode scrape for {item_identifier} initially returned {len(individual_results)} results.")
        if individual_results:
            logging.debug(f"  Initial individual titles: {[r.get('original_title', 'N/A') for r in individual_results]}")


        # Filter out unwanted magnets and URLs for individual results
        if not skip_filter: # Apply existing filters
            temp_individual_results = []
            for r_idx, r_val in enumerate(individual_results):
                logging.debug(f"  Checking individual result #{r_idx + 1} ('{r_val.get('original_title', 'N/A')}') for not_wanted/rescrape filters.")
                if not item.get('disable_not_wanted_check'):
                    if is_magnet_not_wanted(r_val['magnet']):
                        logging.info(f"    Filtered out '{r_val.get('original_title')}' due to is_magnet_not_wanted.")
                        continue
                    if is_url_not_wanted(r_val['magnet']):
                        logging.info(f"    Filtered out '{r_val.get('original_title')}' due to is_url_not_wanted.")
                        continue
                if stored_rescrape_title and r_val.get('original_title') and r_val.get('original_title') == stored_rescrape_title:
                    logging.info(f"    Filtered out '{r_val.get('original_title')}' due to matching stored rescrape title: '{stored_rescrape_title}'.")
                    continue
                temp_individual_results.append(r_val)
            
            if len(individual_results) != len(temp_individual_results):
                logging.info(f"  After not_wanted/rescrape filters, {len(temp_individual_results)} individual results remain for {item_identifier}.")
            individual_results = temp_individual_results


        # For episodes, use the original season/episode numbers from the item for the fallback filtering logic as well
        # The logic below compares against these original numbers unless overridden by scene_mapping in the result
        season = original_season # Ensure we use the original item season number here
        episode = original_episode # Ensure we use the original item episode number here
        
        if season is not None and episode is not None:
            date_based_results = []
            regular_results = []
            logging.debug(f"Processing {len(individual_results)} individual results for S/E matching for {item_identifier} (Item S{season}E{episode}).")
            # Separate date-based and regular results
            for result_idx, result in enumerate(individual_results):
                logging.debug(f"  S/E Pre-sort check for individual result #{result_idx + 1} '{result.get('original_title', 'N/A')}': Parsed PTT: {result.get('parsed_info')}")
                if result.get('parsed_info', {}).get('date'):
                    result['is_date_based'] = True # Mark it
                    date_based_results.append(result)
                    logging.debug(f"    '{result.get('original_title')}' marked as date_based.")
                else:
                    regular_results.append(result)
                    logging.debug(f"    '{result.get('original_title')}' marked as regular (non-date_based PTT).")
            
            filtered_regular_results = []
            logging.debug(f"  Checking {len(regular_results)} regular (non-date PTT) results for S/E match for {item_identifier}.")
            for r_idx, r in enumerate(regular_results):
                parsed_info = r.get('parsed_info', {})
                season_episode_info = parsed_info.get('season_episode_info', {})
                parsed_seasons = season_episode_info.get('seasons', [])
                parsed_episodes = season_episode_info.get('episodes', [])
                
                scene_mapping = r.get('xem_scene_mapping')
                
                target_season = None
                target_episode = None
                
                log_prefix = f"    Regular result #{r_idx+1} ('{r.get('original_title', 'N/A')}'):"

                if scene_mapping: # Use scene mapping if present
                    target_season = scene_mapping.get('season')
                    target_episode = scene_mapping.get('episode')
                    logging.debug(f"{log_prefix} Using XEM S{target_season}E{target_episode}. PTT S{parsed_seasons}E{parsed_episodes}.")
                else: # Fallback to original item numbers
                    target_season = season # Uses the original item season from above
                    target_episode = episode # Uses the original item episode from above
                    logging.debug(f"{log_prefix} Using Item S{target_season}E{target_episode}. PTT S{parsed_seasons}E{parsed_episodes}Year{parsed_info.get('year')}.")
                    
                # Perform the check using the determined target season/episode
                season_match = False
                episode_match = False
                item_title_for_check = item.get('title', '').lower()
                is_formula_1_item = "formula 1" in item_title_for_check

                if is_formula_1_item and not scene_mapping: # Apply F1 logic only if not using XEM override
                    logging.debug(f"{log_prefix} Applying F1 matching logic.")
                    parsed_torrent_year_val = parsed_info.get('year')
                    actual_torrent_year = None
                    if isinstance(parsed_torrent_year_val, list):
                        if parsed_torrent_year_val and str(parsed_torrent_year_val[0]).isdigit():
                            actual_torrent_year = int(parsed_torrent_year_val[0])
                    elif parsed_torrent_year_val and str(parsed_torrent_year_val).isdigit():
                        actual_torrent_year = int(parsed_torrent_year_val)

                    season_match = (
                        (not parsed_seasons or parsed_seasons == [1]) and
                        (actual_torrent_year is not None and actual_torrent_year == target_season)
                    )
                    episode_match = (target_episode is None or not parsed_episodes or parsed_episodes == [target_episode])
                    logging.debug(f"{log_prefix} F1 S_match:{season_match} (PTT_S:{parsed_seasons} vs Item_Year(TargetS):{target_season}, Torrent_Year:{actual_torrent_year}), E_match:{episode_match} (PTT_E:{parsed_episodes} vs Item_E:{target_episode}).")
                else: # Original logic for non-F1 or F1 with XEM
                    logging.debug(f"{log_prefix} Applying Non-F1/XEM matching logic.")
                    season_match = (target_season is None or parsed_seasons == [target_season])
                    episode_match = (target_episode is None or parsed_episodes == [target_episode])
                    logging.debug(f"{log_prefix} Non-F1/XEM S_match:{season_match} (PTT_S:{parsed_seasons} vs TargetS:{target_season}), E_match:{episode_match} (PTT_E:{parsed_episodes} vs TargetE:{target_episode}).")

                if season_match and episode_match:
                    logging.debug(f"{log_prefix} PASSED S/E match. Adding to filtered_regular_results.")
                    filtered_regular_results.append(r)
                else:
                    logging.debug(f"{log_prefix} FAILED S/E match. Discarding.")

            logging.debug(f"  Finished S/E checks. Date-based: {len(date_based_results)}, Filtered Regular: {len(filtered_regular_results)} for {item_identifier}.")
            # Combine date-based and filtered regular results
            individual_results = date_based_results + filtered_regular_results
            if individual_results:
                 logging.info(f"  After all S/E filtering, {len(individual_results)} individual results remain for {item_identifier}.")
                 logging.debug(f"    Final individual titles: {[r.get('original_title', 'N/A') for r in individual_results]}")
            else:
                 logging.info(f"  After all S/E filtering, 0 individual results remain for {item_identifier}.")


        if individual_results:
            logging.info(f"Found results for individual episode scraping of {item_identifier}.")
        else:
            logging.warning(f"No results found even after individual episode scraping for {item_identifier}.")

        return individual_results, individual_filtered_out
     
    def handle_no_results(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        is_upgrade = item.get('upgrading') or item.get('upgrading_from') is not None

        # --- Handle Upgrade Failure ---
        if is_upgrade:
            logging.warning(f"Handling failed upgrade for {item_identifier}: No suitable scrape results found.")
            try:
                from queues.upgrading_queue import UpgradingQueue
                # Import datetime if not already imported at the top
                from datetime import datetime # Added import
                upgrading_queue = UpgradingQueue()

                # Send notification
                notification_data = {
                    'title': item.get('title', 'Unknown Title'),
                    'year': item.get('year', ''),
                    'reason': 'Scraping Queue Failure: No suitable results found'
                }
                send_upgrade_failed_notification(notification_data)

                # Log the failed attempt
                upgrading_queue.log_failed_upgrade(
                    item,
                    'N/A - No scrape result' if not item.get('filled_by_title') else item.get('filled_by_title'), # More accurate placeholder
                    'Scraping Queue Failure: No suitable results found'
                )

                # Attempt to restore previous state
                if upgrading_queue.restore_item_state(item):
                    # Add the failed attempt to tracking
                    upgrading_queue.add_failed_upgrade(
                        item['id'],
                        {
                            'title': 'N/A - No scrape result',
                            'magnet': 'N/A',
                            'reason': 'scraping_queue_no_results'
                        }
                    )
                    logging.info(f"Successfully reverted failed upgrade for {item_identifier} due to no scrape results.")
                    # State is reverted in DB. Proceed to reset flags and remove from queue memory below.
                else: # Restore failed
                    # This is where the log "Failed to restore previous state..." comes from
                    logging.error(f"Failed to restore previous state for {item_identifier} after scraping failure. Attempting manual state reset.")
                    # --- NEW FALLBACK LOGIC ---
                    try:
                        from database import update_media_item # Use direct DB update function

                        original_file = item.get('upgrading_from')
                        new_state = 'Collected' if original_file else 'Wanted'
                        log_message_state = 'Collected' if original_file else 'Wanted (original file unknown)'

                        update_data = {
                            'state': new_state,
                            'upgrading': False,
                            'upgrading_from': None,
                            'upgrading_from_torrent_id': None,
                            'last_updated': datetime.now()
                        }
                        update_media_item(item['id'], **update_data)
                        logging.info(f"Manually reset state to '{log_message_state}' and cleared upgrade flags for {item_identifier}.")

                        # Still record the failed attempt even though restore failed
                        upgrading_queue.add_failed_upgrade(
                            item['id'],
                            {
                                'title': 'N/A - No scrape result',
                                'magnet': 'N/A',
                                'reason': 'scraping_queue_no_results_restore_failed'
                            }
                        )

                    except Exception as reset_err:
                         logging.error(f"CRITICAL: Failed to manually reset state for {item_identifier} after restore failure: {reset_err}")
                         # Item might be truly stuck now. Requires manual intervention.

                    # --- END NEW FALLBACK LOGIC ---

                # Always reset the not wanted check flag after handling an upgrade failure in scraping
                self.reset_not_wanted_check(item['id'])
                # Always remove from ScrapingQueue memory after handling (whether restore worked or failed)
                # Ensure item exists before removing
                if self.contains_item_id(item.get('id')):
                    self.remove_item(item)

            except Exception as e:
                logging.error(f"Error handling failed upgrade in scraping queue for {item_identifier}: {e}", exc_info=True)
                # Fallback: Ensure removal from memory if an error occurs during handling
                if self.contains_item_id(item.get('id')):
                    self.remove_item(item)
            return # Stop processing for this item (it's either handled or logged as critically failed)

        # --- Original Logic for Non-Upgrade Items ---
        if self.is_item_old(item):
            item_title_for_f1_check = (item.get('title', '') or item.get('series_title', '')).lower()
            is_formula_1_item = "formula 1" in item_title_for_f1_check

            if item['type'] == 'episode':
                if is_formula_1_item:
                    logging.info(f"No results found for old Formula 1 item {item_identifier}. Blacklisting this specific item.")
                    queue_manager.move_to_blacklisted(item, "Scraping") # Blacklist only this F1 item
                else:
                    logging.info(f"No results found for old episode {item_identifier}. Blacklisting item and related season items.")
                    queue_manager.queues["Blacklisted"].blacklist_old_season_items(item, queue_manager)
                
                self.reset_not_wanted_check(item['id'])
                self.remove_item(item) # Remove from current queue
            elif item['type'] == 'movie': # Movies (including F1 if ever classified as such) are blacklisted individually
                logging.info(f"No results found for old movie {item_identifier}. Blacklisting item.")
                queue_manager.move_to_blacklisted(item, "Scraping") # Direct blacklist
                self.reset_not_wanted_check(item['id'])
                self.remove_item(item) # Remove from current queue
            else: # Unknown types
                logging.warning(f"Unknown item type {item['type']} for {item_identifier} (is_formula_1_item: {is_formula_1_item}). Blacklisting item.")
                queue_manager.move_to_blacklisted(item, "Scraping") # Direct blacklist
                self.reset_not_wanted_check(item['id'])
                self.remove_item(item) # Remove from current queue
        else: # Item is NOT old, check wake limits
            logging.warning(f"No results found for {item_identifier}. Checking wake count limits before moving to Sleeping or Final_Check/Blacklisted.")
            # Get wake count settings for this item's version
            version_settings = get_setting('Scraping', 'versions', {}).get(item.get('version', ''), {})
            # Fetch the global default wake limit, using 5 as an ultimate fallback
            global_default_wake_limit = int(get_setting("Queue", "wake_limit", default=5))
            # Use the version-specific limit if available, otherwise use the global default
            max_wake_count = version_settings.get('max_wake_count', global_default_wake_limit)

            # Get current wake count from DB
            from database import get_wake_count
            current_wake_count = get_wake_count(item['id'])

            moved = False # Flag to track if moved
            if max_wake_count <= 0:
                logging.info(f"Item {item_identifier} version '{item.get('version')}' has max_wake_count <= 0 ({max_wake_count}). Initiating final check or blacklist.")
                queue_manager.initiate_final_check_or_blacklist(item, "Scraping") 
                moved = True
            elif current_wake_count >= max_wake_count:
                logging.info(f"Item {item_identifier} reached max wake count ({current_wake_count}/{max_wake_count}). Initiating final check or blacklist.")
                queue_manager.initiate_final_check_or_blacklist(item, "Scraping") 
                moved = True
            else:
                logging.info(f"Item {item_identifier} (Wake count: {current_wake_count}/{max_wake_count}) moving to Sleeping queue.")
                queue_manager.move_to_sleeping(item, "Scraping")
                moved = True

            self.reset_not_wanted_check(item['id'])
            if moved and self.contains_item_id(item.get('id')) and max_wake_count > 0 and current_wake_count < max_wake_count:
                 self.remove_item(item)

    def is_item_old(self, item: Dict[str, Any]) -> bool:
        # If early release flag is set, it's never considered old for the purpose of immediate blacklisting
        if item.get('early_release', False):
            return False
            
        if 'release_date' not in item or item['release_date'] is None or item['release_date'] == 'Unknown':
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            days_since_release = (datetime.now().date() - release_date).days
            
            # Define thresholds for considering items as old
            movie_threshold = 30  # Consider movies old after 30 days
            episode_threshold = 7  # Consider episodes old after 7 days
            
            if item['type'] == 'movie':
                return days_since_release > movie_threshold
            elif item['type'] == 'episode':
                return days_since_release > episode_threshold
            else:
                return True  # Consider unknown item types as old
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old
            
    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{'_'.join(item['version'].split())}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{'_'.join(item['version'].split())}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)

    def _is_within_alternate_scrape_window(self, item, now=None):
        if now is None:
            now = datetime.now()
        use_alt = get_setting('Debug', 'use_alternate_scrape_time_strategy', False)
        if not use_alt:
            return True  # If not enabled, always allow
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
