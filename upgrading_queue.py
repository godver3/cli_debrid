import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
import json
import os
from database import get_item_state, update_media_item_state
from scraper.scraper import scrape
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, RealDebridUnavailableError, extract_hash_from_magnet
from not_wanted_magnets import add_to_not_wanted, is_magnet_not_wanted

# Placeholder functions (to be implemented later)
def scrape_for_upgrade(item: Dict[str, Any]) -> Dict[str, Any]:
    # Placeholder implementation
    logging.info(f"Scraping for upgrade: {item['title']}")
    return None  # Return None if no upgrade found, or return upgrade details if found

def verify_item_collected(item: Dict[str, Any]) -> bool:
    # Placeholder implementation
    logging.info(f"Verifying if item is collected: {item['title']}")
    return False  # Return True if item is collected, False otherwise

class UpgradingQueue:
    def __init__(self):
        self.queue: List[Dict[str, Any]] = []
        self.last_checked: Dict[int, datetime] = {}
        self.added_time: Dict[int, datetime] = {}
        self.collected_time: Dict[int, datetime] = {}
        self.queue_file = 'upgrading_queue.json'
        self.load_queue()

    def add_item(self, item: Dict[str, Any]):
        if item['id'] not in [i['id'] for i in self.queue]:
            item['last_checked'] = datetime.now()
            self.queue.append(item)
            self.last_checked[item['id']] = datetime.now()
            self.added_time[item['id']] = datetime.now()
            logging.info(f"Added item {item['title']} (ID: {item['id']}) to UpgradingQueue. Queue size: {len(self.queue)}")
            self.save_queue()
        else:
            logging.info(f"Item {item['title']} (ID: {item['id']}) already in UpgradingQueue. Skipping. Queue size: {len(self.queue)}")

    def remove_item(self, item_id: int):
        self.queue = [item for item in self.queue if item['id'] != item_id]
        if item_id in self.last_checked:
            del self.last_checked[item_id]
        if item_id in self.added_time:
            del self.added_time[item_id]
        if item_id in self.collected_time:
            del self.collected_time[item_id]
        logging.info(f"Removed item (ID: {item_id}) from UpgradingQueue")
        self.save_queue()

    def check_for_upgrade(self, item: Dict[str, Any]):
        logging.info(f"Checking for upgrade: {item['title']} (ID: {item['id']})")
        
        results = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item.get('season_number'),
            item.get('episode_number'),
            False  # Set multi to False during upgrades
        )

        # Filter out not wanted magnets
        results = [result for result in results if not is_magnet_not_wanted(result['magnet'])]

        if not results:
            logging.info(f"No valid results found for {item['title']}. Skipping upgrade check.")
            return

        current_magnet = item.get('filled_by_magnet')
        if not current_magnet:
            logging.warning(f"No current magnet for {item['title']}. Setting to top result.")
            self.try_upgrade(item, results[0])
            return

        current_index = next((i for i, result in enumerate(results) if result['magnet'] == current_magnet), -1)

        if current_index == -1:
            logging.warning(f"Current magnet not found in results for {item['title']}. Keeping current magnet.")
            return

        if current_index > 0:
            # We found a better result
            logging.info(f"Potential upgrade found for {item['title']} from position {current_index} to top result.")
            self.try_upgrade(item, results[0])
        else:
            logging.info(f"No better result found for {item['title']}. Keeping current magnet.")

    def try_upgrade(self, item: Dict[str, Any], new_result: Dict[str, Any]):
        new_magnet = new_result['magnet']
        hash_value = extract_hash_from_magnet(new_magnet)
        
        if not hash_value:
            logging.warning(f"Failed to extract hash from magnet link for {item['title']}")
            return

        try:
            cache_status = is_cached_on_rd(hash_value)
            logging.debug(f"Cache status for {hash_value}: {cache_status}")
            
            if hash_value in cache_status and cache_status[hash_value]:
                logging.info(f"Upgrade for {item['title']} is cached on Real-Debrid. Adding...")
                try:
                    add_to_real_debrid(new_magnet)
                except RealDebridUnavailableError:
                    logging.error(f"Real-Debrid service is unavailable. Skipping upgrade for now.")
                    return
                except Exception as e:
                    logging.error(f"Error adding magnet to Real-Debrid: {str(e)}")
                    add_to_not_wanted(new_magnet)
                    return

                # If we reach here, addition was successful
                update_media_item_state(item['id'], 'Checking', filled_by_magnet=new_magnet)
                item['filled_by_magnet'] = new_magnet
                logging.info(f"Successfully upgraded {item['title']} to new magnet.")
            else:
                logging.info(f"Upgrade for {item['title']} is not cached on Real-Debrid. Adding to not wanted.")
                add_to_not_wanted(new_magnet)
        except Exception as e:
            logging.error(f"Error checking cache status: {str(e)}")
            
    def process_queue(self):
        current_time = datetime.now()
        items_to_remove = []

        for item in self.queue:
            item_id = item['id']
            time_since_last_check = current_time - self.last_checked[item_id]

            if time_since_last_check >= timedelta(hours=1):
                self.last_checked[item_id] = current_time

                if item_id not in self.collected_time:
                    # Check if the item has been collected using get_item_state
                    item_state = get_item_state(item_id)
                    if item_state == "Collected":
                        self.collected_time[item_id] = current_time
                        logging.info(f"Item {item['title']} has been collected. Starting upgrade checks.")
                else:
                    # Item has been collected, check for upgrades
                    time_since_collection = current_time - self.collected_time[item_id]
                    if time_since_collection <= timedelta(hours=24):
                        self.check_for_upgrade(item)
                    else:
                        # Item has been in the queue for over 24 hours since collection
                        items_to_remove.append(item_id)
                        logging.info(f"Item {item['title']} has been in UpgradingQueue for 24 hours since collection. Removing.")

        for item_id in items_to_remove:
            self.remove_item(item_id)

        self.save_queue()
        logging.info(f"UpgradingQueue processed. Current items: {len(self.queue)}")

        
    def get_queue_contents(self):
        return self.queue

    def save_queue(self):
        queue_data = {
            'queue': [self.item_to_dict(item) for item in self.queue],
            'last_checked': {str(k): v.isoformat() for k, v in self.last_checked.items()},
            'added_time': {str(k): v.isoformat() for k, v in self.added_time.items()},
            'collected_time': {str(k): v.isoformat() for k, v in self.collected_time.items()}
        }
        temp_file = f"{self.queue_file}.tmp"
        try:
            with open(temp_file, 'w') as f:
                json.dump(queue_data, f, default=str)
            os.replace(temp_file, self.queue_file)
        except Exception as e:
            logging.error(f"Error saving queue: {str(e)}")
            if os.path.exists(temp_file):
                os.remove(temp_file)

    def load_queue(self):
        if os.path.exists(self.queue_file):
            try:
                with open(self.queue_file, 'r') as f:
                    queue_data = json.load(f)
                self.queue = [self.dict_to_item(item) for item in queue_data['queue']]
                self.last_checked = {int(k): datetime.fromisoformat(v) for k, v in queue_data['last_checked'].items()}
                self.added_time = {int(k): datetime.fromisoformat(v) for k, v in queue_data['added_time'].items()}
                self.collected_time = {int(k): datetime.fromisoformat(v) for k, v in queue_data['collected_time'].items()}
                logging.info(f"Loaded UpgradingQueue from file. Items: {len(self.queue)}")
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON from {self.queue_file}: {str(e)}")
                self._reset_queue()
            except Exception as e:
                logging.error(f"Unexpected error loading queue: {str(e)}")
                self._reset_queue()
        else:
            logging.info("No existing UpgradingQueue file found. Starting with an empty queue.")

    def _reset_queue(self):
        logging.warning("Resetting UpgradingQueue due to load error.")
        self.queue = []
        self.last_checked = {}
        self.added_time = {}
        self.collected_time = {}
        if os.path.exists(self.queue_file):
            os.rename(self.queue_file, f"{self.queue_file}.bak")
        self.save_queue()

    @staticmethod
    def item_to_dict(item: Dict[str, Any]) -> Dict[str, Any]:
        """Convert an item to a JSON-serializable dictionary."""
        serializable_item = item.copy()
        for key, value in serializable_item.items():
            if isinstance(value, datetime):
                serializable_item[key] = value.isoformat()
        return serializable_item

    @staticmethod
    def dict_to_item(item_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a dictionary to an item, parsing datetime strings."""
        item = item_dict.copy()
        for key, value in item.items():
            if key in ['last_checked', 'added_time', 'collected_time'] and isinstance(value, str):
                try:
                    item[key] = datetime.fromisoformat(value)
                except ValueError:
                    # If parsing fails, keep the original string
                    pass
        return item