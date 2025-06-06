import logging
from typing import Dict, Any
from datetime import datetime, timedelta, time as dt_time
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

                # --- Alternate scrape time strategy ---
                if self._is_within_alternate_scrape_window(item, current_datetime):
                    logging.info(f"{item_identifier} eligible by alternate scrape time strategy. Moving to Wanted queue.")
                    items_to_move.append(item)
                    continue

                try:
                    release_date_str = item.get('release_date')
                    version = item.get('version')
                    airtime_str = item.get('airtime')

                    # Determine the base date string, considering physical release requirements first.
                    base_date_str = release_date_str
                    is_physical = False
                    scraping_versions = get_setting('Scraping', 'versions', {})
                    version_settings = scraping_versions.get(version, {})
                    require_physical = version_settings.get('require_physical_release', False)
                    
                    if item.get('type') == 'movie' and require_physical:
                        physical_release_date_str = item.get('physical_release_date')
                        is_physical_date_invalid = not physical_release_date_str or \
                                                   (isinstance(physical_release_date_str, str) and physical_release_date_str.lower() == 'none')

                        if is_physical_date_invalid:
                            logging.info(f"Movie {item_identifier} requires a physical release date, but it is missing or invalid. Keeping in Unreleased state.")
                            continue # Keep in Unreleased, do not process further.
                        else:
                            base_date_str = physical_release_date_str
                            is_physical = True

                    if item.get('early_release', False):
                        # The physical check has already passed if we are here.
                        logging.info(f"Early release item {item_identifier}. Moving to Wanted queue immediately.")
                        items_to_move.append(item)
                        continue

                    if not base_date_str or (isinstance(base_date_str, str) and base_date_str.lower() in ['unknown', 'none']):
                        continue

                    try:
                        base_date = datetime.strptime(base_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        logging.warning(f"Invalid date format encountered for item {item_identifier}: {base_date_str}")
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