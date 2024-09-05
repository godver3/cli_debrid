import logging
import time
from typing import Dict, Any
from database import get_all_media_items
from run_program import get_and_add_recent_collected_from_plex
from not_wanted_magnets import add_to_not_wanted
from queues.adding_queue import AddingQueue  

class CheckingQueue:
    def __init__(self):
        self.items = []
        self.checking_queue_times = {}

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Checking")]
        # Initialize checking times for new items
        for item in self.items:
            if item['id'] not in self.checking_queue_times:
                self.checking_queue_times[item['id']] = time.time()

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.checking_queue_times[item['id']] = time.time()

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.checking_queue_times:
            del self.checking_queue_times[item['id']]

    def process(self, queue_manager):
        logging.debug("Processing checking queue")
        current_time = time.time()

        # Process collected content from Plex
        get_and_add_recent_collected_from_plex()

        # Create an instance of AddingQueue to use its remove_unwanted_torrent method
        adding_queue = AddingQueue()

        # Debug log: Print torrent IDs for all items in the checking queue
        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            torrent_id = item.get('filled_by_torrent_id')  # Change this line
            logging.debug(f"Item: {item_identifier}, Torrent ID: {torrent_id}")

        # Process items in the Checking queue
        items_to_remove = []
        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            time_in_queue = current_time - self.checking_queue_times[item['id']]
            logging.debug(f"{item_identifier} has been in checking queue for {time_in_queue:.2f} seconds")

            # Check if the item has been in the queue for more than 1 hour
            if time_in_queue > 3600:  # 1 hour / 3600
                magnet = item.get('filled_by_magnet')
                if magnet:
                    add_to_not_wanted(magnet)
                    logging.info(f"Marked magnet as unwanted for item: {item_identifier}")

                    torrent_id = item.get('filled_by_torrent_id')  # Change this line
                    if torrent_id:
                        adding_queue.remove_unwanted_torrent(torrent_id)
                        logging.info(f"Removed unwanted torrent from Real-Debrid for item: {item_identifier}")

                queue_manager.move_to_wanted(item, "Checking")
                items_to_remove.append(item)
                logging.info(f"Moving item back to Wanted: {item_identifier}")

        # Remove processed items from the Checking queue
        for item in items_to_remove:
            self.remove_item(item)

        logging.debug(f"Finished processing checking queue. Remaining items: {len(self.items)}")

    def clean_up_checking_times(self):
        # Remove checking times for items no longer in the queue
        for item_id in list(self.checking_queue_times.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.checking_queue_times[item_id]
                logging.debug(f"Cleaned up checking time for item ID: {item_id}")