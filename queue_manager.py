import logging
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Any, List

from database import update_media_item_state, get_media_item_by_id, update_media_item
from queues.wanted_queue import WantedQueue
from queues.scraping_queue import ScrapingQueue
from queues.adding_queue import AddingQueue
from queues.checking_queue import CheckingQueue
from queues.sleeping_queue import SleepingQueue
from queues.unreleased_queue import UnreleasedQueue
from queues.blacklisted_queue import BlacklistedQueue
from queues.pending_uncached_queue import PendingUncachedQueue
from queues.upgrading_queue import UpgradingQueue
from wake_count_manager import wake_count_manager

class QueueManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(QueueManager, cls).__new__(cls)
            cls._instance.initialize()
        return cls._instance

    def initialize(self):
        self.queues = {
            "Wanted": WantedQueue(),
            "Scraping": ScrapingQueue(),
            "Adding": AddingQueue(),
            "Checking": CheckingQueue(),
            "Sleeping": SleepingQueue(),
            "Unreleased": UnreleasedQueue(),
            "Blacklisted": BlacklistedQueue(),
            "Pending Uncached": PendingUncachedQueue(),
            "Upgrading": UpgradingQueue()
        }
        self.paused = False

    def reinitialize_queues(self):
        """Force reinitialization of all queues to pick up new settings"""
        self.initialize()

    def update_all_queues(self):
        for queue_name, queue in self.queues.items():
            before_count = len(queue.get_contents())
            queue.update()
            after_count = len(queue.get_contents())
            # logging.debug(f"Queue {queue_name} update: {before_count} -> {after_count} items")

    def get_queue_contents(self):
        contents = OrderedDict()
        for state, queue in self.queues.items():
            contents[state] = queue.get_contents()
        return contents

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item.get('title', 'Unknown')}_{item.get('imdb_id', 'Unknown')}_{item.get('version', 'Unknown')}"
        elif item['type'] == 'episode':
            season = f"S{item.get('season_number', 0):02d}" if item.get('season_number') is not None else "S00"
            episode = f"E{item.get('episode_number', 0):02d}" if item.get('episode_number') is not None else "E00"
            return f"episode_{item.get('title', 'Unknown')}_{item.get('imdb_id', 'Unknown')}_{season}{episode}_{item.get('version', 'Unknown')}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def get_item_queue(self, item: Dict[str, Any]) -> str:
        for queue_name, queue in self.queues.items():
            if any(i['id'] == item['id'] for i in queue.get_contents()):
                return queue_name
        return None  # or raise an exception if the item should always be in a queue
        
    def process_checking(self):
        if not self.paused:
            self.queues["Checking"].process(self)
            self.queues["Checking"].clean_up_checking_times()
        # else:
            # logging.debug("Skipping Checking queue processing: Queue is paused")

    def process_wanted(self):
        if not self.paused:
            # logging.debug("Processing Wanted queue")
            queue_contents = self.queues["Wanted"].get_contents()
            # logging.debug(f"Wanted queue contains {len(queue_contents)} items")
            # if queue_contents:
                # for item in queue_contents:
                    # logging.debug(f"Processing Wanted item: {self.generate_identifier(item)}")
            self.queues["Wanted"].process(self)
        # else:
            # logging.debug("Skipping Wanted queue processing: Queue is paused")

    def process_scraping(self):
        if not self.paused:
            # logging.debug("Processing Scraping queue")
            # Update queue before processing
            self.queues["Scraping"].update()
            queue_contents = self.queues["Scraping"].get_contents()
            # logging.info(f"Scraping queue contains {len(queue_contents)} items after update")
            
            if queue_contents:
                for item in queue_contents:
                    logging.debug(f"Scraping queue item: {self.generate_identifier(item)}")
                result = self.queues["Scraping"].process(self)
                logging.info(f"Scraping queue process result: {result}")
                return result
        else:
            logging.debug("Skipping Scraping queue processing: Queue is paused")
        return False

    def process_adding(self):
        if not self.paused:
            self.queues["Adding"].process(self)
        else:
            logging.debug("Skipping Adding queue processing: Queue is paused")

    def process_unreleased(self):
        if not self.paused:
            self.queues["Unreleased"].process(self)
        else:
            logging.debug("Skipping Unreleased queue processing: Queue is paused")

    def process_sleeping(self):
        if not self.paused:
            self.queues["Sleeping"].process(self)
        else:
            logging.debug("Skipping Sleeping queue processing: Queue is paused")

    def process_blacklisted(self):
        if not self.paused:
            self.queues["Blacklisted"].process(self)
        else:
            logging.debug("Skipping Blacklisted queue processing: Queue is paused")

    def process_pending_uncached(self):
        if not self.paused:
            self.queues["Pending Uncached"].process(self)
        else:
            logging.debug("Skipping Pending Uncached queue processing: Queue is paused")

    def process_upgrading(self):
        if not self.paused:
            self.queues["Upgrading"].process(self)
            self.queues["Upgrading"].clean_up_upgrade_times()
        else:
            logging.debug("Skipping Upgrading queue processing: Queue is paused")
            
    def blacklist_item(self, item: Dict[str, Any], from_queue: str):
        self.queues["Blacklisted"].blacklist_item(item, self)
        self.queues[from_queue].remove_item(item)

    def blacklist_old_season_items(self, item: Dict[str, Any], from_queue: str):
        self.queues["Blacklisted"].blacklist_old_season_items(item, self)
        self.queues[from_queue].remove_item(item)

    def move_to_wanted(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Wanted queue")
        
        wake_count = wake_count_manager.get_wake_count(item['id'])
        logging.debug(f"Wake count before moving to Wanted: {wake_count}")
        
        update_media_item_state(item['id'], 'Wanted', filled_by_title=None, filled_by_magnet=None)
        
        wanted_item = get_media_item_by_id(item['id'])
        if wanted_item:
            wanted_item_identifier = self.generate_identifier(wanted_item)
            
            self.queues["Wanted"].add_item(wanted_item)
            self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {item_identifier} to Wanted queue")
        else:
            logging.error(f"Failed to retrieve wanted item for ID: {item['id']}")

    def move_to_upgrading(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Upgrading: {item_identifier}")
        update_media_item_state(item['id'], 'Upgrading')
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Upgrading"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Upgrading queue")

    def move_to_scraping(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Scraping: {item_identifier}")
        update_media_item_state(item['id'], 'Scraping')
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Scraping"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Scraping queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_adding(self, item: Dict[str, Any], from_queue: str, filled_by_title: str, scrape_results: List[Dict]):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Adding: {item_identifier}")
        update_media_item_state(item['id'], 'Adding', filled_by_title=filled_by_title, scrape_results=scrape_results)
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Adding"].add_item(updated_item)
            # Remove the item from the Scraping queue, but not from the Wanted queue
            if from_queue == "Scraping":
                self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Adding queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_checking(self, item: Dict[str, Any], from_queue: str, title: str, link: str, filled_by_file: str, torrent_id: str = None):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Checking: {item_identifier}")
        
        from settings import get_setting

        '''
        # Check if Plex library checks are disabled
        if get_setting('Plex', 'disable_plex_library_checks') and not get_setting('Plex', 'mounted_file_location'):
            logging.info(f"Plex library checks disabled and no file location set. Moving {item_identifier} directly to Collected")
            # Update item state to Collected
            update_media_item_state(item['id'], 'Collected', filled_by_title=title, filled_by_magnet=link, filled_by_file=filled_by_file, filled_by_torrent_id=torrent_id)
            update_media_item(item['id'], collected_at= datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            
            try:
                from notifications import send_notifications
                from routes.settings_routes import get_enabled_notifications_for_category
                from extensions import app

                with app.app_context():
                    response = get_enabled_notifications_for_category('collected')
                    if response.json['success']:
                        enabled_notifications = response.json['enabled_notifications']
                        if enabled_notifications:
                            notification_data = {
                                'id': item['id'],
                                'title': item.get('title', 'Unknown Title'),
                                'type': item.get('type', 'unknown'),
                                'year': item.get('year', ''),
                                'version': item.get('version', ''),
                                'season_number': item.get('season_number'),
                                'episode_number': item.get('episode_number'),
                                'new_state': 'Collected'
                            }
                            send_notifications([notification_data], enabled_notifications, notification_category='collected')
            except Exception as e:
                logging.error(f"Failed to send collected notification: {str(e)}")
            
            # Remove from source queue
            if from_queue in ["Adding", "Wanted"]:
                self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Collected state")
            return
        '''

        # Normal flow - move to checking
        update_media_item_state(item['id'], 'Checking', filled_by_title=title, filled_by_magnet=link, filled_by_file=filled_by_file, filled_by_torrent_id=torrent_id)
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            # Copy downloading flag from original item
            if 'downloading' in item:
                updated_item['downloading'] = item['downloading']
            self.queues["Checking"].add_item(updated_item)
            # Remove the item from the Adding queue and the Wanted queue
            if from_queue in ["Adding", "Wanted"]:
                self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Checking queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_sleeping(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Sleeping queue")
        
        wake_count = wake_count_manager.get_wake_count(item['id'])
        logging.debug(f"Wake count before moving to Sleeping: {wake_count}")
        
        update_media_item_state(item['id'], 'Sleeping')
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            updated_item['wake_count'] = wake_count
            self.queues["Sleeping"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {item_identifier} to Sleeping queue (Wake count: {wake_count})")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_unreleased(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Unreleased queue")
        update_media_item_state(item['id'], 'Unreleased')
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Unreleased"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {item_identifier} to Unreleased queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_blacklisted(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Blacklisted queue")
        update_media_item_state(item['id'], 'Blacklisted')
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Blacklisted"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {item_identifier} to Blacklisted queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_pending_uncached(self, item: Dict[str, Any], from_queue: str, title: str, link: str, scrape_results: List[Dict]):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Pending Uncached Additions queue")
        update_media_item_state(item['id'], 'Pending Uncached', filled_by_title=title, filled_by_magnet=link, scrape_results=scrape_results)
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Pending Uncached"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {item_identifier} to Pending Uncached Additions queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def get_scraping_items(self) -> List[Dict]:
        """Get all items currently in the Scraping state"""
        return self.queues["Scraping"].get_contents()
        
    def get_wake_count(self, item_id):
        return wake_count_manager.get_wake_count(item_id)

    def pause_queue(self, reason=None):
        if not self.paused:
            self.paused = True
            pause_message = "Queue processing paused"
            if reason:
                pause_message += f": {reason}"
            logging.info(pause_message)
            from notifications import send_queue_pause_notification
            send_queue_pause_notification(pause_message)
        else:
            logging.warning("Queue is already paused")

    def resume_queue(self):
        if self.paused:
            self.paused = False
            logging.info("Queue processing resumed")
            from notifications import send_queue_resume_notification
            send_queue_resume_notification("Queue processing resumed")
        else:
            logging.warning("Queue is not paused")

    def is_paused(self):
        return self.paused

    def move_to_collected(self, item: Dict[str, Any], from_queue: str, skip_notification: bool = False):
        """Move an item to the Collected state after symlink is created."""
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Collected state")
        
        from datetime import datetime
        collected_at = datetime.now()
        
        # Update the item state in the database
        from database import update_media_item_state, get_media_item_by_id
        update_media_item_state(item['id'], 'Collected', collected_at=collected_at)
        
        # Get the updated item
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            # Remove from the source queue
            self.queues[from_queue].remove_item(item)
            logging.info(f"Successfully moved item {item_identifier} to Collected state")
            
            # Add to collected notifications if not skipped
            if not skip_notification:
                from database.collected_items import add_to_collected_notifications
                updated_item_dict = dict(updated_item)
                updated_item_dict['is_upgrade'] = False  # Not an upgrade since it's a new collection
                updated_item_dict['original_collected_at'] = collected_at
                add_to_collected_notifications(updated_item_dict)
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    # Add other methods as needed