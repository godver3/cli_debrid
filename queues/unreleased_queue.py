import logging
from typing import Dict, Any
from datetime import datetime, timedelta

from database import get_all_media_items, get_db_connection
from settings import get_setting

class UnreleasedQueue:
    def __init__(self):
        self.items = []
        self.last_report_time = datetime.min

    def update(self):
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

        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            release_date_str = item.get('release_date')
            version = item.get('version')

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
                logging.warning(f"Item {item_identifier} has no release date. Keeping in Unreleased queue.")
                continue

            try:
                release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                release_datetime = datetime.combine(release_date, datetime.min.time())
                logging.info(f"Item {item_identifier} release date: {release_datetime}")
                
                # Check if version requires physical release
                scraping_versions = get_setting('Scraping', 'versions', {})
                version_settings = scraping_versions.get(version, {})
                require_physical = version_settings.get('require_physical_release', False)
                physical_release_date = item.get('physical_release_date')
                
                if require_physical and not physical_release_date:
                    logging.info(f"Item {item_identifier} requires physical release date but none available. Keeping in Unreleased queue.")
                    continue
                
                # If physical release is required, use that date instead
                if require_physical and physical_release_date:
                    try:
                        physical_date = datetime.strptime(physical_release_date, '%Y-%m-%d').date()
                        release_datetime = datetime.combine(physical_date, datetime.min.time())
                        logging.info(f"Item {item_identifier} using physical release date: {release_datetime}")
                    except ValueError:
                        logging.warning(f"Invalid physical release date format for item {item_identifier}: {physical_release_date}")
                        continue

                # If physical release is required, ignore early release flag
                if require_physical:
                    if current_datetime >= release_datetime - timedelta(hours=24):
                        logging.info(f"Item {item_identifier} is within 24 hours of physical release. Moving to Wanted queue.")
                        items_to_move.append(item)
                    else:
                        time_until_release = release_datetime - current_datetime
                        logging.debug(f"Item {item_identifier} will be physically released in {time_until_release}. Keeping in Unreleased queue.")
                        unreleased_report.append((item_identifier, release_datetime, time_until_release))
                # If no physical release required, check early release flag
                elif item.get('early_release', False):
                    logging.info(f"Item {item_identifier} is an early release. Moving to Wanted queue immediately.")
                    items_to_move.append(item)
                # Otherwise check if it's within 24 hours of release
                elif current_datetime >= release_datetime - timedelta(hours=24):
                    logging.info(f"Item {item_identifier} is within 24 hours of release. Moving to Wanted queue.")
                    items_to_move.append(item)
                else:
                    time_until_release = release_datetime - current_datetime
                    logging.debug(f"Item {item_identifier} will be released in {time_until_release}. Keeping in Unreleased queue.")
                    unreleased_report.append((item_identifier, release_datetime, time_until_release))
            except ValueError:
                logging.error(f"Invalid release date format for item {item_identifier}: {release_date_str}")

        # Move items to Wanted queue
        for item in items_to_move:
            queue_manager.move_to_wanted(item, "Unreleased")
            self.remove_item(item)

        # Print debug report for unreleased items every hour
        if current_datetime - self.last_report_time >= timedelta(hours=1):
            if unreleased_report:
                logging.debug("Hourly unreleased items report:")
                for item_id, release_time, time_until in unreleased_report:
                    logging.debug(f"  {item_id}: Move to Wanted at {release_time - timedelta(hours=24)}, time until: {time_until - timedelta(hours=24)}")
            self.last_report_time = current_datetime

        logging.debug(f"Unreleased queue processing complete. Items moved to Wanted queue: {len(items_to_move)}")
        logging.debug(f"Remaining items in Unreleased queue: {len(self.items)}")