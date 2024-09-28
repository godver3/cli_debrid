import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta

from database import get_all_media_items, get_media_item_by_id, update_media_item_state, update_blacklisted_date
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
            if 'blacklisted_date' not in item or item['blacklisted_date'] is None:
                item['blacklisted_date'] = current_time
                update_blacklisted_date(item['id'], item['blacklisted_date'])
                logging.warning(f"Item {queue_manager.generate_identifier(item)} had no blacklisted_date. Setting it to current time.")
            
            item_identifier = queue_manager.generate_identifier(item)
            try:
                # Convert blacklisted_date to datetime if it's a string
                if isinstance(item['blacklisted_date'], str):
                    item['blacklisted_date'] = datetime.fromisoformat(item['blacklisted_date'])
                
                time_blacklisted = current_time - item['blacklisted_date']
                logging.info(f"Item {item_identifier} has been blacklisted for {time_blacklisted.days} days. Will be unblacklisted in {blacklist_duration.days - time_blacklisted.days} days.")
                if time_blacklisted >= blacklist_duration:
                    items_to_unblacklist.append(item)
                    logging.info(f"Item {item_identifier} has been blacklisted for {time_blacklisted.days} days. Unblacklisting.")
            except (TypeError, ValueError) as e:
                logging.error(f"Error processing blacklisted item {item_identifier}: {str(e)}")
                logging.error(f"Item details: {item}")
                continue

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

    def blacklist_item(self, item: Dict[str, Any], queue_manager):
        item_id = item['id']
        item_identifier = queue_manager.generate_identifier(item)
        update_media_item_state(item_id, 'Blacklisted')
        
        # Update the blacklisted_date in the database
        blacklisted_date = datetime.now()
        update_blacklisted_date(item_id, blacklisted_date)
        
        # Add to blacklisted queue
        item['blacklisted_date'] = blacklisted_date  # Store as datetime object
        self.add_item(item)
        
        # Remove from current queue
        current_queue = queue_manager.get_item_queue(item)
        if current_queue:
            queue_manager.queues[current_queue].remove_item(item)
        
        logging.info(f"Moved item {item_identifier} to Blacklisted state and updated blacklisted_date")

    def blacklist_old_season_items(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Blacklisting item {item_identifier} and related old season items with the same version")

        # Check if the item is in the Checking queue before blacklisting
        if queue_manager.get_item_queue(item) != 'Checking':
            self.blacklist_item(item, queue_manager)
        else:
            logging.info(f"Skipping blacklisting of {item_identifier} as it's already in Checking queue")

        # Find and blacklist related items in the same season with the same version that are also old
        related_items = self.find_related_season_items(item, queue_manager)
        for related_item in related_items:
            related_identifier = queue_manager.generate_identifier(related_item)
            if self.is_item_old(related_item) and related_item['version'] == item['version']:
                # Check if the related item is in the Checking queue before blacklisting
                if queue_manager.get_item_queue(related_item) != 'Checking':
                    self.blacklist_item(related_item, queue_manager)
                else:
                    logging.info(f"Skipping blacklisting of {related_identifier} as it's already in Checking queue")
            else:
                logging.debug(f"Not blacklisting {related_identifier} as it's either not old enough or has a different version")

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
        if 'release_date' not in item or item['release_date'] is None or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date, None, or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            days_since_release = (datetime.now().date() - release_date).days
            
            # Define thresholds for considering items as old
            movie_threshold = 7  # Consider movies old after 30 days
            episode_threshold = 7  # Consider episodes old after 7 days
            
            if item['type'] == 'movie':
                return days_since_release > movie_threshold
            elif item['type'] == 'episode':
                return days_since_release > episode_threshold
            else:
                logging.warning(f"Unknown item type: {item['type']}. Considering it as old.")
                return True
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