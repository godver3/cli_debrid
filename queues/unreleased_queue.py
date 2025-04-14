import logging
from typing import Dict, Any
from datetime import datetime, timedelta
from utilities.settings import get_setting

class UnreleasedQueue:
    def __init__(self):
        self.items = []
        self.last_report_time = datetime.now()

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)

    def update(self):
        from database import get_all_media_items
        self.items = [dict(row) for row in get_all_media_items(state="Unreleased")]

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        logging.debug(f"Processing unreleased queue. Items: {len(self.items)}")
        current_datetime = datetime.now()
        items_to_move = []
        unreleased_report = []
        
        # Early exit if no items to process
        if not self.items:
            return

        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            # --- START EDIT: Check for Magnet Assigner ---
            is_magnet_assigned = item.get('content_source') == 'Magnet_Assigner'
            if is_magnet_assigned:
                logging.info(f"Item {item_identifier} from Magnet Assigner found in Unreleased. Moving to Wanted immediately.")
                items_to_move.append(item)
                continue # Skip date checks for this item
            # --- END EDIT ---

            release_date_str = item.get('release_date')
            version = item.get('version')
            airtime_str = item.get('airtime')

            # Handle early release items without release date
            if item.get('early_release', False):
                if not release_date_str or release_date_str.lower() == 'unknown':
                    logging.info(f"Early release item {item_identifier} with no release date. Moving to Wanted queue immediately.")
                    items_to_move.append(item)
                    continue
                else:
                    logging.info(f"Early release item {item_identifier}. Moving to Wanted queue immediately.")
                    items_to_move.append(item)
                    continue

            if not release_date_str or release_date_str.lower() == 'unknown':
                continue

            try:
                # Determine which date to use
                base_date_str = release_date_str
                is_physical = False
                
                # Check if version requires physical release
                scraping_versions = get_setting('Scraping', 'versions', {})
                version_settings = scraping_versions.get(version, {})
                require_physical = version_settings.get('require_physical_release', False)
                physical_release_date_str = item.get('physical_release_date')
                
                if require_physical:
                    # Check if physical release date is missing, None, or the string "None" (case-insensitive)
                    is_physical_date_invalid = not physical_release_date_str or \
                                               (isinstance(physical_release_date_str, str) and physical_release_date_str.lower() == 'none')

                    if is_physical_date_invalid:
                        # Use the retrieved value in the log message for clarity
                        logging.info(f"Item {item_identifier} requires physical release date but it is missing or invalid ('{physical_release_date_str}'). Keeping in Unreleased queue.")
                        continue # Skip if physical required but date missing/invalid
                    else:
                        # Physical date exists and seems valid
                        base_date_str = physical_release_date_str
                        is_physical = True
                
                # Parse the base date
                try:
                    # Ensure base_date_str is not None or 'unknown' before parsing
                    # The checks on L60 and L71 should prevent this, but adding safety
                    if not base_date_str or (isinstance(base_date_str, str) and base_date_str.lower() in ['unknown', 'none']):
                         logging.warning(f"Skipping item {item_identifier} due to invalid base_date_str before parsing: {base_date_str}")
                         continue

                    base_date = datetime.strptime(base_date_str, '%Y-%m-%d').date()
                except ValueError:
                    # This warning should ideally not be reached if the above checks are correct
                    logging.warning(f"Invalid date format encountered for item {item_identifier} despite checks: {base_date_str}")
                    continue # Skip if date is invalid

                # --- Calculate precise target scrape time (logic adapted from WantedQueue) ---
                # Handle airtime parsing with fallbacks
                if airtime_str:
                    try:
                        airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                    except ValueError:
                        try:
                            airtime = datetime.strptime(airtime_str, '%H:%M').time()
                        except ValueError:
                            airtime = datetime.strptime("00:00", '%H:%M').time() # Default if invalid
                else:
                    airtime = datetime.strptime("00:00", '%H:%M').time() # Default if None

                # Combine date and time
                target_datetime = datetime.combine(base_date, airtime)

                # Apply airtime offset
                if item['type'] == 'movie':
                    movie_airtime_offset = get_setting("Queue", "movie_airtime_offset", 19)
                    offset = float(movie_airtime_offset) if movie_airtime_offset else 19.0
                else:  # episode
                    episode_airtime_offset = get_setting("Queue", "episode_airtime_offset", 0)
                    offset = float(episode_airtime_offset) if episode_airtime_offset else 0.0
                
                target_scrape_time = target_datetime + timedelta(hours=offset)
                # --- End precise calculation ---
                
                time_until_target = target_scrape_time - current_datetime

                # Check if item should be moved (within 24 hours of precise target time)
                # Note: Early release handled separately above
                if time_until_target <= timedelta(hours=24):
                    release_type = "physical release" if is_physical else "release"
                    logging.info(f"Item {item_identifier} is within 24 hours of {release_type} + offset ({target_scrape_time}). Moving to Wanted queue.")
                    items_to_move.append(item)
                else:
                    # Only report if not moved
                    unreleased_report.append((item_identifier, target_scrape_time, time_until_target))
                    
            except Exception as e:
                logging.error(f"Error processing dates/times for item {item_identifier}: {e}")
                # Potentially skip or keep item based on error handling preference
                continue

        # Move items to Wanted queue
        if items_to_move:
            logging.info(f"Moving {len(items_to_move)} items from Unreleased to Wanted queue")
            for item in items_to_move:
                queue_manager.move_to_wanted(item, "Unreleased")
                self.remove_item(item)

        # Print debug report for unreleased items every hour
        if current_datetime - self.last_report_time >= timedelta(hours=1):
            if unreleased_report:
                logging.debug("Hourly unreleased items report:")
                # Sort by scheduled move time
                sorted_report = sorted(unreleased_report, key=lambda x: x[1] - timedelta(hours=24))
                for item_id, release_time, time_until in sorted_report:
                    move_time = release_time - timedelta(hours=24)
                    # Corrected logging message to reflect target time used for report sorting
                    # logging.debug(f"  {item_id}: Target scrape at {release_time}, time until target: {time_until}")
            self.last_report_time = current_datetime

        if items_to_move:
            logging.info(f"Unreleased queue processing complete. Items moved to Wanted queue: {len(items_to_move)}")
        logging.debug(f"Remaining items in Unreleased queue: {len(self.items)}")