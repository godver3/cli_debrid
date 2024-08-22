import logging
from typing import Dict, Any
from datetime import datetime, timedelta

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting
from wake_count_manager import wake_count_manager

class SleepingQueue:
    def __init__(self):
        self.items = []
        self.sleeping_queue_times = {}

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Sleeping")]
        # Initialize sleeping times for new items
        for item in self.items:
            if item['id'] not in self.sleeping_queue_times:
                self.sleeping_queue_times[item['id']] = datetime.now()

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.sleeping_queue_times[item['id']] = datetime.now()
        logging.debug(f"Added item to Sleeping queue: {item['id']}")

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.sleeping_queue_times:
            del self.sleeping_queue_times[item['id']]
        logging.debug(f"Removed item from Sleeping queue: {item['id']}")

    def process(self, queue_manager):
        logging.debug("Processing sleeping queue")
        current_time = datetime.now()
        wake_limit = int(get_setting("Queue", "wake_limit", default=3))
        sleep_duration = timedelta(minutes=30)

        items_to_wake = []
        items_to_blacklist = []

        for item in self.items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            logging.debug(f"Processing sleeping item: {item_identifier}")

            time_asleep = current_time - self.sleeping_queue_times[item_id]
            wake_count = wake_count_manager.get_wake_count(item_id)
            logging.debug(f"Item {item_identifier} has been asleep for {time_asleep}. Current wake count: {wake_count}")

            if time_asleep >= sleep_duration:
                if wake_count < wake_limit:
                    items_to_wake.append(item)
                else:
                    items_to_blacklist.append(item)

        self.wake_items(queue_manager, items_to_wake)
        self.blacklist_items(queue_manager, items_to_blacklist)

    def wake_items(self, queue_manager, items):
        logging.debug(f"Attempting to wake {len(items)} items")
        for item in items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            old_wake_count = wake_count_manager.get_wake_count(item_id)
            logging.debug(f"Waking item: {item_identifier} (Current wake count: {old_wake_count})")

            new_wake_count = wake_count_manager.increment_wake_count(item_id)
            queue_manager.move_to_wanted(item, "Sleeping")
            self.remove_item(item)
            logging.info(f"Moved item {item_identifier} from Sleeping to Wanted queue (Wake count: {old_wake_count} -> {new_wake_count})")

        logging.debug(f"Woke up {len(items)} items")

    def blacklist_items(self, queue_manager, items):
        for item in items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            queue_manager.move_to_blacklisted(item, "Sleeping")
            self.remove_item(item)
            logging.info(f"Moved item {item_identifier} to Blacklisted state")
        
        logging.debug(f"Blacklisted {len(items)} items")

    def clean_up_sleeping_data(self):
        # Remove sleeping times for items no longer in the queue
        for item_id in list(self.sleeping_queue_times.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.sleeping_queue_times[item_id]
        
        # Don't remove wake counts here, as we want to preserve them even when items leave the queue
        # We'll log the wake counts for debugging purposes
        for item_id, wake_count in wake_count_manager.wake_counts.items():
            if item_id not in [item['id'] for item in self.items]:
                logging.debug(f"Preserving wake count for item ID: {item_id}. Current wake count: {wake_count}")

    def is_item_old(self, item):
        if 'release_date' not in item or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            return (datetime.now().date() - release_date).days > 7
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old