import logging
from typing import Dict, Any
from datetime import datetime, date

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting

class UnreleasedQueue:
    def __init__(self):
        self.items = []

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
        current_date = date.today()
        items_to_move = []

        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            release_date_str = item.get('release_date')

            if not release_date_str or release_date_str.lower() == 'unknown':
                logging.warning(f"Item {item_identifier} has no release date. Keeping in Unreleased queue.")
                continue

            try:
                release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                days_until_release = (release_date - current_date).days

                if days_until_release <= 0:
                    logging.info(f"Item {item_identifier} is now released. Moving to Wanted queue.")
                    items_to_move.append(item)
                else:
                    logging.debug(f"Item {item_identifier} will be released in {days_until_release} days.")
            except ValueError:
                logging.error(f"Invalid release date format for item {item_identifier}: {release_date_str}")

        # Move items to Wanted queue
        for item in items_to_move:
            queue_manager.move_to_wanted(item, "Unreleased")
            self.remove_item(item)

        logging.debug(f"Unreleased queue processing complete. Items moved to Wanted queue: {len(items_to_move)}")
        logging.debug(f"Remaining items in Unreleased queue: {len(self.items)}")

    def check_release_dates(self):
        """
        Check release dates of items in the Unreleased queue and return a list of items ready to be released.
        """
        current_date = date.today()
        items_ready = []

        for item in self.items:
            release_date_str = item.get('release_date')
            if release_date_str and release_date_str.lower() != 'unknown':
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                    if release_date <= current_date:
                        items_ready.append(item)
                except ValueError:
                    logging.error(f"Invalid release date format for item {item['id']}: {release_date_str}")

        return items_ready