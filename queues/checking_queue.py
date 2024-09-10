import logging
import time
from typing import Dict, Any
from database import get_all_media_items
from run_program import get_and_add_recent_collected_from_plex
from not_wanted_magnets import add_to_not_wanted
from queues.adding_queue import AddingQueue
from debrid.real_debrid import get_torrent_info  # Import the new function
from settings import get_setting

class CheckingQueue:
    def __init__(self):
        self.items = []
        self.checking_queue_times = {}
        self.progress_checks = {}

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

        adding_queue = AddingQueue()

        # Group items by torrent ID
        items_by_torrent = {}
        for item in self.items:
            torrent_id = item.get('filled_by_torrent_id')
            if torrent_id:
                if torrent_id not in items_by_torrent:
                    items_by_torrent[torrent_id] = []
                items_by_torrent[torrent_id].append(item)

        items_to_remove = []
        for torrent_id, items in items_by_torrent.items():
            try:
                torrent_info = get_torrent_info(torrent_id)
                
                if torrent_info:
                    current_progress = torrent_info.get('progress', 0)
                    
                    if torrent_id not in self.progress_checks:
                        self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}
                    
                    last_check = self.progress_checks[torrent_id]['last_check']
                    last_progress = self.progress_checks[torrent_id]['last_progress']
                    logging.debug(f"Last check for torrent {torrent_id}: {last_check}, Last progress: {last_progress}")
                    
                    # Skip progress check if the torrent is already at 100%
                    if last_progress == 100:
                        continue
                    
                    if current_time - last_check >= 300:  # 5 minutes
                        if current_progress == last_progress:
                            logging.info(f"No progress for torrent {torrent_id} in 5 minutes. Moving all associated items back to Wanted queue.")
                            self.move_items_to_wanted(items, queue_manager, adding_queue, torrent_id)
                            items_to_remove.extend(items)
                        else:
                            self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}

                # Check if any item in the group has been in the queue for more than 1 hour
                oldest_item_time = min(self.checking_queue_times.get(item['id'], current_time) for item in items)
                if current_time - oldest_item_time > get_setting('Checking', 'checking_queue_period'):
                    logging.info(f"Items for torrent {torrent_id} have been in queue for over an hour. Moving all back to Wanted queue.")
                    self.move_items_to_wanted(items, queue_manager, adding_queue, torrent_id)
                    items_to_remove.extend(items)

            except Exception as e:
                logging.error(f"Error processing items for torrent {torrent_id} in checking queue: {e}", exc_info=True)

        # Remove processed items from the Checking queue
        for item in items_to_remove:
            self.remove_item(item)

        logging.debug(f"Finished processing checking queue. Remaining items: {len(self.items)}")

    def move_items_to_wanted(self, items, queue_manager, adding_queue, torrent_id):
        for item in items:
            item_identifier = queue_manager.generate_identifier(item)
            magnet = item.get('filled_by_magnet')
            if magnet:
                add_to_not_wanted(magnet)
                logging.info(f"Marked magnet as unwanted for item: {item_identifier}")

        # Remove the unwanted torrent only once for all items
        adding_queue.remove_unwanted_torrent(torrent_id)
        logging.info(f"Removed unwanted torrent {torrent_id} from Real-Debrid for all associated items")

        for item in items:
            queue_manager.move_to_wanted(item, "Checking")
            logging.info(f"Moving item back to Wanted: {queue_manager.generate_identifier(item)}")

    def move_items_to_wanted(self, items, queue_manager, adding_queue, torrent_id):
        for item in items:
            item_identifier = queue_manager.generate_identifier(item)
            magnet = item.get('filled_by_magnet')
            if magnet:
                add_to_not_wanted(magnet)
                logging.info(f"Marked magnet as unwanted for item: {item_identifier}")

        # Remove the unwanted torrent only once for all items
        adding_queue.remove_unwanted_torrent(torrent_id)
        logging.info(f"Removed unwanted torrent {torrent_id} from Real-Debrid for all associated items")

        for item in items:
            queue_manager.move_to_wanted(item, "Checking")
            logging.info(f"Moving item back to Wanted: {queue_manager.generate_identifier(item)}")

    def clean_up_checking_times(self):
        # Remove checking times for items no longer in the queue
        for item_id in list(self.checking_queue_times.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.checking_queue_times[item_id]
                if item_id in self.progress_checks:
                    del self.progress_checks[item_id]
                logging.debug(f"Cleaned up checking time for item ID: {item_id}")