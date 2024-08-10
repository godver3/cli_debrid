import logging
from datetime import datetime
from typing import Dict, Any, List

from database import get_all_media_items
from settings import get_setting

class WantedQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Wanted")]

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        logging.debug("Processing wanted queue")
        current_date = datetime.now().date()
        seasons_in_queues = set()
        # Get seasons already in Scraping or Adding queue
        for queue_name in ["Scraping", "Adding"]:
            for item in queue_manager.queues[queue_name].get_contents():
                if item['type'] == 'episode':
                    seasons_in_queues.add((item['imdb_id'], item['season_number'], item['version']))
        
        items_to_move = []
        items_to_unreleased = []
        
        # Process each item in the Wanted queue
        for item in list(self.items):
            item_identifier = queue_manager.generate_identifier(item)
            try:
                # Check if release_date is None, empty string, or "Unknown"
                if not item['release_date'] or item['release_date'].lower() == "unknown":
                    logging.debug(f"Release date is missing or unknown for item: {item_identifier}. Moving to Unreleased state.")
                    items_to_unreleased.append(item)
                    continue
                
                release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                if release_date > current_date:
                    logging.info(f"Item {item_identifier} is not released yet. Moving to Unreleased state.")
                    items_to_unreleased.append(item)
                else:
                    # Check if we've reached the scraping cap
                    if len(queue_manager.queues["Scraping"].get_contents()) + len(items_to_move) >= get_setting("Queue", "scraping_cap", 5):
                        logging.debug(f"Scraping cap reached. Keeping {item_identifier} in Wanted queue.")
                        break  # Exit the loop as we've reached the cap
                    # Check if we're already processing an item from this season
                    if item['type'] == 'episode':
                        season_key = (item['imdb_id'], item['season_number'], item['version'])
                        if season_key in seasons_in_queues:
                            logging.debug(f"Already processing an item from {item_identifier}. Keeping in Wanted queue.")
                            continue
                    logging.info(f"Item {item_identifier} has been released. Marking for move to Scraping.")
                    items_to_move.append(item)
                    if item['type'] == 'episode':
                        seasons_in_queues.add(season_key)
            except ValueError as e:
                logging.error(f"Error processing release date for item {item_identifier}: {str(e)}")
                logging.error(f"Item details: {item}")
                # Move to Unreleased state if there's an error processing the date
                items_to_unreleased.append(item)
            except Exception as e:
                logging.error(f"Unexpected error processing item {item_identifier}: {str(e)}")
                logging.error(f"Item details: {item}")
                # Move to Unreleased state if there's an unexpected error
                items_to_unreleased.append(item)
        
        # Move marked items to Scraping queue
        for item in items_to_move:
            queue_manager.move_to_scraping(item, "Wanted")
        
        # Move items to Unreleased state and remove from Wanted queue
        for item in items_to_unreleased:
            queue_manager.move_to_unreleased(item, "Wanted")
        
        logging.debug(f"Wanted queue processing complete. Items moved to Scraping queue: {len(items_to_move)}")
        logging.debug(f"Items moved to Unreleased state and removed from Wanted queue: {len(items_to_unreleased)}")
        logging.debug(f"Remaining items in Wanted queue: {len(self.items)}")