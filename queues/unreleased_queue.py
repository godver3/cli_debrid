import logging
from typing import Dict, Any
from datetime import datetime, timedelta
from utilities.settings import get_setting
from database.database_reading import get_all_media_items

class UnreleasedQueue:
    def __init__(self):
        self.last_report_time = datetime.now()
        logging.info("UnreleasedQueue initialized (no longer stores items in memory).")

    def contains_item_id(self, item_id):
        """Check if the DB contains an item with the given ID in Unreleased state"""
        conn = None
        try:
            from database.core import get_db_connection
            conn = get_db_connection()
            cursor = conn.execute("SELECT 1 FROM media_items WHERE id = ? AND state = 'Unreleased' LIMIT 1", (item_id,))
            result = cursor.fetchone()
            return result is not None
        except Exception as e:
            logging.error(f"Error checking DB for unreleased item ID {item_id}: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def update(self):
        #logging.debug("UnreleasedQueue.update called - no longer loads items into memory.")
        pass

    def get_contents(self):
        return []

    def add_item(self, item: Dict[str, Any]):
        logging.debug(f"UnreleasedQueue.add_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def remove_item(self, item: Dict[str, Any]):
        logging.debug(f"UnreleasedQueue.remove_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def process(self, queue_manager):
        logging.debug("Processing unreleased queue using direct DB query.")
        current_datetime = datetime.now()
        items_to_move = []
        unreleased_report = []
        processed_count = 0

        try:
            unreleased_items = get_all_media_items(state="Unreleased")
            processed_count = len(unreleased_items)

            if not unreleased_items:
                logging.debug("No items found in Unreleased state.")
                return

            for item in unreleased_items:
                item_identifier = queue_manager.generate_identifier(item)
                is_magnet_assigned = item.get('content_source') == 'Magnet_Assigner'
                if is_magnet_assigned:
                    logging.info(f"Item {item_identifier} from Magnet Assigner found in Unreleased. Moving to Wanted immediately.")
                    items_to_move.append(item)
                    continue

                release_date_str = item.get('release_date')
                version = item.get('version')
                airtime_str = item.get('airtime')

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
                    base_date_str = release_date_str
                    is_physical = False
                    
                    scraping_versions = get_setting('Scraping', 'versions', {})
                    version_settings = scraping_versions.get(version, {})
                    require_physical = version_settings.get('require_physical_release', False)
                    physical_release_date_str = item.get('physical_release_date')
                    
                    if item.get('type') == 'movie' and require_physical:
                        is_physical_date_invalid = not physical_release_date_str or \
                                                   (isinstance(physical_release_date_str, str) and physical_release_date_str.lower() == 'none')

                        if is_physical_date_invalid:
                            logging.info(f"Movie {item_identifier} requires physical release date but it is missing or invalid ('{physical_release_date_str}'). Keeping in Unreleased state (DB).")
                            continue
                        else:
                            base_date_str = physical_release_date_str
                            is_physical = True
                    
                    try:
                        if not base_date_str or (isinstance(base_date_str, str) and base_date_str.lower() in ['unknown', 'none']):
                             logging.warning(f"Skipping item {item_identifier} due to invalid base_date_str before parsing: {base_date_str}")
                             continue
                        base_date = datetime.strptime(base_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        logging.warning(f"Invalid date format encountered for item {item_identifier} despite checks: {base_date_str}")
                        continue

                    if airtime_str:
                        try: airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                        except ValueError:
                            try: airtime = datetime.strptime(airtime_str, '%H:%M').time()
                            except ValueError: airtime = datetime.strptime("00:00", '%H:%M').time()
                    else: airtime = datetime.strptime("00:00", '%H:%M').time()

                    target_datetime = datetime.combine(base_date, airtime)

                    # Apply the appropriate offset based on type
                    offset = 0.0
                    item_type = item['type']
                    if item_type == 'movie':
                        movie_offset_setting = get_setting("Queue", "movie_airtime_offset", "19")
                        try:
                            offset = float(movie_offset_setting)
                        except (ValueError, TypeError):
                            logging.warning(f"Invalid movie_airtime_offset setting ('{movie_offset_setting}') in UnreleasedQueue. Using default 19.")
                            offset = 19.0
                    elif item_type == 'episode':
                        episode_offset_setting = get_setting("Queue", "episode_airtime_offset", "0")
                        try:
                            offset = float(episode_offset_setting)
                        except (ValueError, TypeError):
                            logging.warning(f"Invalid episode_airtime_offset setting ('{episode_offset_setting}') in UnreleasedQueue. Using default 0.")
                            offset = 0.0

                    target_scrape_time = target_datetime + timedelta(hours=offset)
                    
                    time_until_target = target_scrape_time - current_datetime

                    if time_until_target <= timedelta(hours=24):
                        release_type = "physical release" if is_physical else "release"
                        logging.info(f"Item {item_identifier} is within 24 hours of {release_type} + offset ({target_scrape_time}). Moving to Wanted queue.")
                        items_to_move.append(item)
                    else:
                        unreleased_report.append((item_identifier, target_scrape_time, time_until_target))

                except Exception as e:
                    logging.error(f"Error processing dates/times for item {item_identifier}: {e}")
                    continue

            if items_to_move:
                logging.info(f"Moving {len(items_to_move)} items from Unreleased to Wanted queue")
                for item_to_move in items_to_move:
                    queue_manager.move_to_wanted(item_to_move, "Unreleased")

            if current_datetime - self.last_report_time >= timedelta(hours=1):
                if unreleased_report:
                    logging.debug("Hourly unreleased items report:")
                    sorted_report = sorted(unreleased_report, key=lambda x: x[1] - timedelta(hours=24))
                    for item_id, release_time, time_until in sorted_report:
                         move_time = release_time - timedelta(hours=24)
                         # Corrected log message
                         # logging.debug(f"  {item_id}: Target scrape at {release_time}, time until target: {time_until}")
                self.last_report_time = current_datetime

            if items_to_move:
                logging.info(f"Unreleased queue processing complete. Items moved to Wanted queue: {len(items_to_move)}")
            logging.debug(f"Unreleased queue processing finished. Checked {processed_count} items from DB.")

        except Exception as e:
            logging.error(f"Error during UnreleasedQueue processing: {e}", exc_info=True)