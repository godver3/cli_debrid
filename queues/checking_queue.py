import logging
import time
from typing import Dict, Any
from database import get_all_media_items
from run_program import get_and_add_recent_collected_from_plex
from not_wanted_magnets import add_to_not_wanted
from queues.adding_queue import AddingQueue
from debrid import get_debrid_provider
from settings import get_setting
from debrid.common import timed_lru_cache

class CheckingQueue:
    def __init__(self):
        self.items = []
        self.checking_queue_times = {}
        self.progress_checks = {}
        self.debrid_provider = get_debrid_provider()

    def update(self):
        db_items = get_all_media_items(state="Checking")
        # logging.debug(f"Database returned {len(db_items)} items in Checking state")
        # if db_items:
            # logging.debug(f"First checking item from DB: {dict(db_items[0])}")
        
        self.items = [dict(row) for row in db_items]
        # Initialize checking times for new items
        for item in self.items:
            if item['id'] not in self.checking_queue_times:
                self.checking_queue_times[item['id']] = time.time()
        # logging.debug(f"Updated checking queue - current item count: {len(self.items)}")
        # if self.items:
            # logging.debug(f"Items in queue: {[item['id'] for item in self.items]}")

    def get_contents(self):
        # Add progress and state information to each item
        items_with_info = []
        for item in self.items:
            item_info = dict(item)  # Create a copy of the item
            torrent_id = item.get('filled_by_torrent_id')
            if torrent_id:
                progress = self.get_torrent_progress(torrent_id)
                state = self.get_torrent_state(torrent_id)
                item_info['progress'] = progress
                item_info['state'] = state
            items_with_info.append(item_info)
        return items_with_info

    @timed_lru_cache(seconds=30)
    def get_torrent_progress(self, torrent_id: str) -> int:
        """Get the current progress percentage for a torrent"""
        try:
            torrent_info = self.debrid_provider.get_torrent_info(torrent_id)
            if torrent_info:
                return torrent_info.get('progress', 0)
        except Exception as e:
            logging.error(f"Failed to get progress for torrent {torrent_id}: {str(e)}")
        return 0

    def get_torrent_state(self, torrent_id: str) -> str:
        """Get the current state of a torrent (downloaded or downloading)"""
        try:
            current_progress = self.get_torrent_progress(torrent_id)
            
            # If progress is 100%, it's downloaded
            if current_progress == 100:
                return 'downloaded'
            
            # If we have any progress > 0, it's downloading
            if current_progress > 0:
                return 'downloading'
            
            # Check progress history if available
            if torrent_id in self.progress_checks:
                last_progress = self.progress_checks[torrent_id]['last_progress']
                if last_progress == 100:
                    return 'downloaded'
                if current_progress > last_progress:
                    return 'downloading'
            
            return 'unknown'
        except Exception as e:
            logging.error(f"Failed to get state for torrent {torrent_id}: {str(e)}")
            return 'unknown'

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.checking_queue_times[item['id']] = time.time()
        logging.debug(f"Added item {item['id']} to checking queue")

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.checking_queue_times:
            del self.checking_queue_times[item['id']]
        logging.debug(f"Removed item {item['id']} from checking queue")

    def process(self, queue_manager):
        logging.debug(f"Starting to process checking queue with {len(self.items)} items")
        logging.debug("Processing checking queue")
        current_time = time.time()

        # Process collected content from Plex
        get_and_add_recent_collected_from_plex()

        adding_queue = AddingQueue()

        # Group items by torrent ID
        items_by_torrent = {}
        items_to_remove = []
        current_time = time.time()

        # Group all items by their torrent ID
        for item in self.items:
            torrent_id = item['filled_by_torrent_id']
            if torrent_id not in items_by_torrent:
                items_by_torrent[torrent_id] = []
            items_by_torrent[torrent_id].append(item)
            if item['id'] not in self.checking_queue_times:
                self.checking_queue_times[item['id']] = current_time

        # Process items by torrent ID
        for torrent_id, items in items_by_torrent.items():
            try:
                torrent_info = self.debrid_provider.get_torrent_info(torrent_id)
                
                if torrent_info:
                    current_progress = self.get_torrent_progress(torrent_id)
                    
                    if torrent_id not in self.progress_checks:
                        self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}
                    
                    last_check = self.progress_checks[torrent_id]['last_check']
                    last_progress = self.progress_checks[torrent_id]['last_progress']
                    logging.debug(f"Last check for torrent {torrent_id}: {last_check}, Last progress: {last_progress}")
                    
                    # Skip all checks if the torrent is already at 100%
                    if last_progress == 100:
                        continue

                    # Check if we've exceeded the checking queue period, but only if not actively downloading
                    if current_progress == last_progress:
                        oldest_item_time = min(self.checking_queue_times.get(item['id'], current_time) for item in items)
                        time_in_queue = current_time - oldest_item_time
                        
                        if time_in_queue > get_setting('Debug', 'checking_queue_period'):
                            logging.info(f"Removing torrent {torrent_id} from debrid service as content was not found within {get_setting('Debug', 'checking_queue_period')} seconds")
                            try:
                                self.debrid_provider.remove_torrent(torrent_id)
                            except Exception as e:
                                logging.error(f"Failed to remove torrent {torrent_id}: {str(e)}")
                            # Move all items for this torrent back to Wanted
                            for item in items:
                                queue_manager.move_to_wanted(item, "Checking")
                                items_to_remove.append(item)
                            continue
                    
                    if current_time - last_check >= 300:  # 5 minutes
                        if current_progress == last_progress:
                            logging.info(f"No progress for torrent {torrent_id} in 5 minutes. Moving all associated items back to Wanted queue.")
                            try:
                                self.debrid_provider.remove_torrent(torrent_id)
                            except Exception as e:
                                logging.error(f"Failed to remove torrent {torrent_id}: {str(e)}")
                            self.move_items_to_wanted(items, queue_manager, adding_queue, torrent_id)
                            items_to_remove.extend(items)
                        else:
                            self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}

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

    def clean_up_checking_times(self):
        """Clean up old entries from checking times and progress cache"""
        # Clean up checking times for removed items
        current_item_ids = {item['id'] for item in self.items}
        self.checking_queue_times = {k: v for k, v in self.checking_queue_times.items() if k in current_item_ids}
        
        # Clean up progress cache for removed torrents
        current_torrent_ids = {item.get('filled_by_torrent_id') for item in self.items if item.get('filled_by_torrent_id')}
        # self.progress_cache = {k: v for k, v in self.progress_cache.items() if k in current_torrent_ids}
        # self.progress_cache_times = {k: v for k, v in self.progress_cache_times.items() if k in current_torrent_ids}
        logging.debug(f"Cleaned up checking time for item ID: {current_item_ids}")