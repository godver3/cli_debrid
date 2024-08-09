import logging
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Any, List

from database import update_media_item_state, get_media_item_by_id
from queues.wanted_queue import WantedQueue
from queues.scraping_queue import ScrapingQueue
from queues.adding_queue import AddingQueue
from queues.checking_queue import CheckingQueue
from queues.sleeping_queue import SleepingQueue
from queues.unreleased_queue import UnreleasedQueue
from queues.blacklisted_queue import BlacklistedQueue

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
            "Blacklisted": BlacklistedQueue()
        }

    def update_all_queues(self):
        logging.debug("Updating all queues")
        for queue in self.queues.values():
            queue.update()

    def get_queue_contents(self):
        contents = OrderedDict()
        for state, queue in self.queues.items():
            contents[state] = queue.get_contents()
        return contents

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def get_item_queue(self, item: Dict[str, Any]) -> str:
        for queue_name, queue in self.queues.items():
            if any(i['id'] == item['id'] for i in queue.get_contents()):
                return queue_name
        return None  # or raise an exception if the item should always be in a queue
        
    def process_checking(self):
        self.queues["Checking"].process(self)
        self.queues["Checking"].clean_up_checking_times()

    def process_wanted(self):
        self.queues["Wanted"].process(self)
        
    def process_scraping(self):
        self.queues["Scraping"].process(self)
        
    def process_adding(self):
        self.queues["Adding"].process(self)
        
    def process_unreleased(self):
        self.queues["Unreleased"].process(self)
        
    def process_sleeping(self):
        self.queues["Sleeping"].process(self)
        
    def get_wake_count(self, item_id):
        return self.queues["Sleeping"].get_wake_count(item_id)

    def increment_wake_count(self, item_id):
        self.queues["Sleeping"].increment_wake_count(item_id)
        
    def process_blacklisted(self):
        self.queues["Blacklisted"].process(self)

    def blacklist_item(self, item: Dict[str, Any], from_queue: str):
        self.queues["Blacklisted"].blacklist_item(item)
        self.queues[from_queue].remove_item(item)

    def blacklist_old_season_items(self, item: Dict[str, Any], from_queue: str):
        self.queues["Blacklisted"].blacklist_old_season_items(item, self)
        self.queues[from_queue].remove_item(item)

    def move_to_wanted(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Wanted queue")
        update_media_item_state(item['id'], 'Wanted', filled_by_title=None, filled_by_magnet=None)
        wanted_item = get_media_item_by_id(item['id'])
        if wanted_item:
            wanted_item_identifier = self.generate_identifier(wanted_item)
            self.queues["Wanted"].add_item(wanted_item)
            self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {wanted_item_identifier} to Wanted queue")
        else:
            logging.error(f"Failed to retrieve wanted item for ID: {item['id']}")

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
            self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Adding queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_checking(self, item: Dict[str, Any], from_queue: str, title: str, link: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Checking: {item_identifier}")
        update_media_item_state(item['id'], 'Checking', filled_by_title=title, filled_by_magnet=link)
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Checking"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Checking queue")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def move_to_sleeping(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Sleeping queue")
        update_media_item_state(item['id'], 'Sleeping')
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            self.queues["Sleeping"].add_item(updated_item)
            self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {item_identifier} to Sleeping queue")
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

    # Add other methods as needed