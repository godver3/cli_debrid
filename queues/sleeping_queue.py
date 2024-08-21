import logging
from typing import Dict, Any
from datetime import datetime, timedelta

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting

class SleepingQueue:
    def __init__(self):
        self.items = []
        self.sleeping_queue_times = {}
        self.wake_counts = {}

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Sleeping")]
        # Initialize sleeping times and wake counts for new items
        for item in self.items:
            if item['id'] not in self.sleeping_queue_times:
                self.sleeping_queue_times[item['id']] = datetime.now()
            if item['id'] not in self.wake_counts:
                self.wake_counts[item['id']] = 0

    def get_contents(self):
        return self.items

    def get_wake_count(self, item_id):
        return self.wake_counts.get(item_id, 0)

    def increment_wake_count(self, item_id):
        self.wake_counts[item_id] = self.wake_counts.get(item_id, 0) + 1

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.sleeping_queue_times[item['id']] = datetime.now()
        # Only initialize wake count if it doesn't exist
        if item['id'] not in self.wake_counts:
            self.wake_counts[item['id']] = 0

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.sleeping_queue_times:
            del self.sleeping_queue_times[item['id']]
        # Don't remove the wake count when removing the item

    def process(self, queue_manager):
        logging.debug("Processing sleeping queue")
        current_time = datetime.now()
        wake_limit = int(get_setting("Queue", "wake_limit", default=3))
        sleep_duration = timedelta(minutes=int(get_setting("Queue", "sleep_duration", default=30)))
        one_week_ago = current_time - timedelta(days=7)

        items_to_wake = []
        items_to_blacklist = []

        for item in self.items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            logging.debug(f"Processing sleeping item: {item_identifier}")

            time_asleep = current_time - self.sleeping_queue_times[item_id]
            logging.debug(f"Item {item_identifier} has been asleep for {time_asleep}")
            logging.debug(f"Current wake count for item {item_identifier}: {self.wake_counts[item_id]}")

            release_date = item.get('release_date')
            if release_date and datetime.strptime(release_date, '%Y-%m-%d').date() < one_week_ago.date():
                items_to_blacklist.append(item)
                logging.debug(f"Adding {item_identifier} to items_to_blacklist list due to old release date")
            elif time_asleep >= sleep_duration:
                self.wake_counts[item_id] += 1
                logging.debug(f"Incremented wake count for {item_identifier} to {self.wake_counts[item_id]}")

                if self.wake_counts[item_id] > wake_limit:
                    items_to_blacklist.append(item)
                    logging.debug(f"Adding {item_identifier} to items_to_blacklist list due to exceeding wake limit")
                else:
                    items_to_wake.append(item)
                    logging.debug(f"Adding {item_identifier} to items_to_wake list")
            else:
                logging.debug(f"Item {item_identifier} hasn't slept long enough yet. Time left: {sleep_duration - time_asleep}")

        if items_to_wake:
            logging.info(f"Waking {len(items_to_wake)} items")
            self.wake_items(queue_manager, items_to_wake)
        else:
            logging.debug("No items to wake")

        if items_to_blacklist:
            logging.info(f"Blacklisting {len(items_to_blacklist)} items")
            self.blacklist_items(queue_manager, items_to_blacklist)
        else:
            logging.debug("No items to blacklist")

        self.clean_up_sleeping_data()

        logging.debug(f"Finished processing sleeping queue. Remaining items: {len(self.items)}")

    def wake_items(self, queue_manager, items):
        logging.debug(f"Attempting to wake {len(items)} items")
        for item in items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            wake_count = self.wake_counts.get(item_id, 0)
            logging.debug(f"Waking item: {item_identifier} (Wake count: {wake_count})")

            queue_manager.move_to_wanted(item, "Sleeping")
            self.remove_item(item)
            logging.info(f"Moved item {item_identifier} from Sleeping to Wanted queue (Wake count: {wake_count})")

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
        
        # Only remove wake counts for items that haven't been in the queue for a long time
        # (e.g., 30 days)
        thirty_days_ago = datetime.now() - timedelta(days=30)
        for item_id in list(self.wake_counts.keys()):
            if item_id not in [item['id'] for item in self.items] and \
               (item_id not in self.sleeping_queue_times or self.sleeping_queue_times[item_id] < thirty_days_ago):
                del self.wake_counts[item_id]
                logging.debug(f"Cleaned up wake count for item ID: {item_id}")

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