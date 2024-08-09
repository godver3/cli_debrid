import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta

from database import get_all_media_items, get_media_item_by_id, update_media_item_state
from settings import get_setting

class BlacklistedQueue:
    def __init__(self):
        self.items = []
        self.blacklist_times = {}

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Blacklisted")]
        # Initialize blacklist times for new items
        for item in self.items:
            if item['id'] not in self.blacklist_times:
                self.blacklist_times[item['id']] = datetime.now()

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.blacklist_times[item['id']] = datetime.now()

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.blacklist_times:
            del self.blacklist_times[item['id']]

    def process(self, queue_manager):
        logging.debug(f"Processing blacklisted queue. Items: {len(self.items)}")
        blacklist_duration = timedelta(days=int(get_setting("Queue", "blacklist_duration", default=30)))
        current_time = datetime.now()

        items_to_unblacklist = []

        for item in self.items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            time_blacklisted = current_time - self.blacklist_times[item_id]

            if time_blacklisted >= blacklist_duration:
                items_to_unblacklist.append(item)
                logging.info(f"Item {item_identifier} has been blacklisted for {time_blacklisted.days} days. Unblacklisting.")

        for item in items_to_unblacklist:
            self.unblacklist_item(queue_manager, item)

        logging.debug(f"Blacklisted queue processing complete. Items unblacklisted: {len(items_to_unblacklist)}")
        logging.debug(f"Remaining items in Blacklisted queue: {len(self.items)}")

    def unblacklist_item(self, queue_manager, item: Dict[str, Any]):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Unblacklisting item: {item_identifier}")
        
        # Move the item back to the Wanted queue
        queue_manager.move_to_wanted(item, "Blacklisted")
        self.remove_item(item)

    def blacklist_item(self, item: Dict[str, Any]):
        item_id = item['id']
        item_identifier = self.generate_identifier(item)
        update_media_item_state(item_id, 'Blacklisted')
        
        # Add to blacklisted queue
        self.add_item(item)
        
        logging.info(f"Moved item {item_identifier} to Blacklisted state")

    def blacklist_old_season_items(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Blacklisting item {item_identifier} and related old season items with the same version")

        # Blacklist the current item
        self.blacklist_item(item)

        # Find and blacklist related items in the same season with the same version that are also old
        related_items = self.find_related_season_items(item, queue_manager)
        for related_item in related_items:
            if self.is_item_old(related_item) and related_item['version'] == item['version']:
                self.blacklist_item(related_item)
            else:
                logging.debug(f"Not blacklisting {queue_manager.generate_identifier(related_item)} as it's either not old enough or has a different version")

    def find_related_season_items(self, item: Dict[str, Any], queue_manager) -> List[Dict[str, Any]]:
        related_items = []
        if item['type'] == 'episode':
            for queue in queue_manager.queues.values():
                for queue_item in queue.get_contents():
                    if (queue_item['type'] == 'episode' and
                        queue_item['imdb_id'] == item['imdb_id'] and
                        queue_item['season_number'] == item['season_number'] and
                        queue_item['id'] != item['id'] and
                        queue_item['version'] == item['version']):
                        related_items.append(queue_item)
        return related_items

    def is_item_old(self, item: Dict[str, Any]) -> bool:
        if 'release_date' not in item or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            return (datetime.now().date() - release_date).days > 7
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")