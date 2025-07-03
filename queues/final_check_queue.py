import logging
from typing import Dict, Any
from datetime import datetime, timedelta, timezone

from utilities.settings import get_setting
from queues.base_queue import BaseQueue # <-- Change this line
from queues.scraping_queue import ScrapingQueue # To use scrape_with_fallback
from database.not_wanted_magnets import is_magnet_not_wanted, is_url_not_wanted
from database.database_reading import get_all_media_items # To fetch items
from database.database_writing import update_media_item # Use this import

class FinalCheckQueue(BaseQueue):
    def __init__(self):
        self.items = []
        self._item_ids = set() # Use a set for efficient ID lookup

    def update(self):
        """Update the queue contents from the database."""
        db_items_raw = get_all_media_items(state="Final_Check")
        db_items_dict = {item['id']: dict(item) for item in db_items_raw}
        db_item_ids = set(db_items_dict.keys())

        # Remove items no longer in 'Final_Check' state
        items_to_remove_ids = self._item_ids - db_item_ids
        if items_to_remove_ids:
            self.items = [item for item in self.items if item['id'] not in items_to_remove_ids]
            self._item_ids -= items_to_remove_ids

        # Add new items found in DB
        items_to_add_ids = db_item_ids - self._item_ids
        if items_to_add_ids:
            for item_id in items_to_add_ids:
                if item_id not in self._item_ids: # Double check
                    self.items.append(db_items_dict[item_id])
                    self._item_ids.add(item_id)

        # Optional: Sort if needed, e.g., by last_state_change ascending?
        self.items.sort(key=lambda x: x.get('last_state_change', ''))

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        """Add an item to the in-memory queue if not already present."""
        item_id = item.get('id')
        if item_id and item_id not in self._item_ids:
            self.items.append(item)
            self._item_ids.add(item_id)

    def remove_item(self, item: Dict[str, Any]):
        """Remove an item from the in-memory queue."""
        item_id = item.get('id')
        if item_id and item_id in self._item_ids:
            self.items = [i for i in self.items if i['id'] != item_id]
            self._item_ids.remove(item_id)

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return item_id in self._item_ids

    def process(self, queue_manager):
        """Process items waiting for their final scrape attempt."""
        delay_hours = get_setting("Queue", "blacklist_final_scrape_delay_hours", 0)
        if delay_hours <= 0:
            # If setting disabled while items are here, move them directly to blacklist
            if self.items:
                logging.warning(f"Final scrape delay is disabled, moving {len(self.items)} items from Final_Check directly to Blacklisted/Fallback.")
                items_to_process = list(self.items) # Process a copy
                for item in items_to_process:
                    item_identifier = queue_manager.generate_identifier(item)
                    logging.info(f"Moving {item_identifier} from Final_Check to Blacklisted (delay disabled).")
                    queue_manager.move_to_blacklisted(item, "Final_Check") # Apply fallback logic
            return

        processed_count = 0
        # Use naive local time for comparison if DB stores naive local time
        now_local = datetime.now()
        delay_delta = timedelta(hours=delay_hours)
        scrape_instance = ScrapingQueue() # Instantiate to use its scrape method

        # Process a copy of the list to avoid modification issues while iterating
        items_to_process = list(self.items)

        for item in items_to_process:
            item_id = item.get('id')
            if not item_id: continue # Skip items without ID

            item_identifier = queue_manager.generate_identifier(item)

            # --- Backfill missing final_check_add_timestamp ---
            if item.get('final_check_add_timestamp') is None:
                last_updated_ts = item.get('last_updated')
                if last_updated_ts:
                    logging.debug(f"Item {item_identifier} missing final_check_add_timestamp. Backfilling with last_updated: {last_updated_ts}")
                    item['final_check_add_timestamp'] = last_updated_ts # Update in-memory item too
                    # Persist this backfill to the database using the correct function
                    try:
                        update_media_item(item_id, final_check_add_timestamp=last_updated_ts)
                    except Exception as db_update_err:
                        logging.error(f"Failed to persist backfilled final_check_add_timestamp for {item_identifier}: {db_update_err}", exc_info=True)
                        # Decide if processing should continue or skip this item
                        # continue # Example: skip if persistence failed
                else:
                    logging.error(f"Item {item_identifier} missing final_check_add_timestamp and last_updated timestamp. Cannot backfill.")
                    # Potentially skip this item if timestamp is critical?
                    # continue
            # --- End Backfill ---

            # Use last_state_change for delay calculation
            last_change_str = item.get('final_check_add_timestamp')

            if not last_change_str:
                logging.warning(f"Item {item_identifier} in Final_Check queue missing final_check_add_timestamp timestamp. Cannot process delay.")
                continue
            
            # Convert potential datetime object to string if needed (might happen if backfilled)
            if isinstance(last_change_str, datetime):
                last_change_str = last_change_str.strftime('%Y-%m-%d %H:%M:%S.%f')

            logging.info(f"Checking item {item_identifier} with timestamp {last_change_str}")

            try:
                # Parse DB timestamp string as naive datetime (assuming local)
                # Include microseconds in the format string
                last_change_dt_naive = datetime.strptime(last_change_str, '%Y-%m-%d %H:%M:%S.%f')
                time_elapsed = now_local - last_change_dt_naive # Compare naive local times

                if time_elapsed >= delay_delta:
                    logging.info(f"Final scrape delay elapsed for {item_identifier}. Performing final scrape attempt.")
                    processed_count += 1

                    try:
                        # Perform scrape - force single episode check, not multi-pack
                        results, filtered_out = scrape_instance.scrape_with_fallback(item, is_multi_pack=False, queue_manager=queue_manager)

                        # Ensure results are lists
                        results = results if results is not None else []
                        filtered_out = filtered_out if filtered_out is not None else []

                        # Filter results like in ScrapingQueue
                        filtered_results = []
                        for result in results:
                            # Check if 'disable_not_wanted_check' flag should apply (less likely here, but for consistency)
                            # if not item.get('disable_not_wanted_check'):
                            if is_magnet_not_wanted(result['magnet']) or is_url_not_wanted(result['magnet']):
                                continue
                            filtered_results.append(result)

                        if filtered_results:
                            best_result = filtered_results[0]
                            logging.info(f"Final scrape successful for {item_identifier}. Found result: {best_result['title']}. Moving to Adding queue.")
                            # --- Record exit from Final_Check ---
                            self._record_item_exited(queue_manager, item)
                            # --- Move to Adding ---
                            queue_manager.move_to_adding(item, "Final_Check", best_result['title'], filtered_results)
                        else:
                            logging.warning(f"Final scrape failed for {item_identifier}. No results found. Moving to Blacklisted/Fallback.")
                            # --- Record exit from Final_Check ---
                            self._record_item_exited(queue_manager, item)
                            # --- Move to Blacklisted (applies fallback) ---
                            queue_manager.move_to_blacklisted(item, "Final_Check")

                    except Exception as scrape_err:
                        logging.error(f"Error during final scrape for {item_identifier}: {scrape_err}", exc_info=True)
                        # If scrape fails, move to Blacklisted/Fallback
                        logging.warning(f"Moving {item_identifier} to Blacklisted/Fallback due to scrape error.")
                        self._record_item_exited(queue_manager, item)
                        queue_manager.move_to_blacklisted(item, "Final_Check")
                else:
                    logging.info(f"Final scrape delay not yet passed for {item_identifier}. Leaving in queue. Time elapsed: {time_elapsed}")
                # else: Delay not yet passed, leave item in queue

            except ValueError:
                # Log the specific error and the string that failed parsing
                logging.error(f"Invalid final_check_add_timestamp format for item {item_identifier}: '{last_change_str}'. Cannot process.", exc_info=True)
            except Exception as e:
                logging.error(f"Unexpected error processing item {item_identifier} in FinalCheckQueue: {e}", exc_info=True)

        if processed_count > 0:
            logging.info(f"Processed {processed_count} items in FinalCheckQueue.")
