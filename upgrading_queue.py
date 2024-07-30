import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
import json
import os
from scraper.scraper import scrape
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, RealDebridUnavailableError, extract_hash_from_magnet
from database import get_media_item_status
from not_wanted_magnets import add_to_not_wanted, is_magnet_not_wanted

class UpgradingQueue:
    def __init__(self):
        self.queue: List[Dict[str, Any]] = []
        self.last_checked: Dict[str, datetime] = {}
        self.added_time: Dict[str, datetime] = {}
        self.collected_time: Dict[str, datetime] = {}
        self.queue_file = 'upgrading_queue.json'
        self.load_queue()

    def generate_identifier(self, item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['imdb_id']}"
        elif item['type'] == 'episode':
            return f"episode_{item['imdb_id']}_{item['season_number']}_{item['episode_number']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def add_item(self, item: Dict[str, Any]):
        identifier = self.generate_identifier(item)
        if identifier not in [self.generate_identifier(i) for i in self.queue]:
            item['last_checked'] = datetime.now()
            self.queue.append(item)
            self.last_checked[identifier] = datetime.now()
            self.added_time[identifier] = datetime.now()
            logging.info(f"Added item {item['title']} (ID: {identifier}) to UpgradingQueue. Queue size: {len(self.queue)}")
            self.save_queue()
        else:
            logging.info(f"Item {item['title']} (ID: {identifier}) already in UpgradingQueue. Skipping. Queue size: {len(self.queue)}")

    def process_queue(self):
        current_time = datetime.now()
        items_to_remove = []
        for item in self.queue:
            identifier = self.generate_identifier(item)
            time_since_last_check = current_time - self.last_checked.get(identifier, datetime.min)
            if time_since_last_check >= timedelta(hours=1):
                self.last_checked[identifier] = current_time
                if identifier not in self.collected_time:
                    # Check if the item has been collected using get_media_item_status
                    item_state = get_media_item_status(
                        imdb_id=item['imdb_id'],
                        tmdb_id=item['tmdb_id'],
                        title=item['title'],
                        year=item['year'],
                        season_number=item.get('season_number'),
                        episode_number=item.get('episode_number')
                    )
                    if item_state == "Collected":
                        self.collected_time[identifier] = current_time
                        logging.info(f"Item {item['title']} (ID: {identifier}) has been collected. Starting upgrade checks.")
                else:
                    # Item has been collected, check for upgrades
                    time_since_collection = current_time - self.collected_time[identifier]
                    if time_since_collection <= timedelta(hours=24):
                        self.check_for_upgrade(item)
                    else:
                        # Item has been in the queue for over 24 hours since collection
                        items_to_remove.append(identifier)
                        logging.info(f"Item {item['title']} (ID: {identifier}) has been in UpgradingQueue for 24 hours since collection. Removing.")
        for identifier in items_to_remove:
            self.remove_item(identifier)
        self.save_queue()
        logging.info(f"UpgradingQueue processed. Current items: {len(self.queue)}")

    def check_for_upgrade(self, item: Dict[str, Any]):
        identifier = self.generate_identifier(item)
        logging.info(f"Checking for upgrade: {item['title']} (ID: {identifier})")
        
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
            logging.info(f"No valid results found for {item['title']} (ID: {identifier}). Skipping upgrade check.")
            return

        current_magnet = item.get('filled_by_magnet')
        if not current_magnet:
            logging.warning(f"No current magnet for {item['title']} (ID: {identifier}). Setting to top result.")
            self.try_upgrade(item, results[0])
            return

        current_index = next((i for i, result in enumerate(results) if result['magnet'] == current_magnet), -1)

        if current_index == -1:
            logging.warning(f"Current magnet not found in results for {item['title']} (ID: {identifier}). Keeping current magnet.")
            return

        if current_index > 0:
            # We found a better result
            logging.info(f"Potential upgrade found for {item['title']} (ID: {identifier}) from position {current_index} to top result.")
            self.try_upgrade(item, results[0])
        else:
            logging.info(f"No better result found for {item['title']} (ID: {identifier}). Keeping current magnet.")

    def try_upgrade(self, item: Dict[str, Any], new_result: Dict[str, Any]):
        identifier = self.generate_identifier(item)
        new_magnet = new_result['magnet']
        hash_value = extract_hash_from_magnet(new_magnet)
        
        if not hash_value:
            logging.warning(f"Failed to extract hash from magnet link for {item['title']} (ID: {identifier})")
            return

        try:
            cache_status = is_cached_on_rd(hash_value)
            logging.debug(f"Cache status for {hash_value}: {cache_status}")
            
            if hash_value in cache_status and cache_status[hash_value]:
                logging.info(f"Upgrade for {item['title']} (ID: {identifier}) is cached on Real-Debrid. Adding...")
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
                # update_media_item_state(identifier, 'Checking', filled_by_magnet=new_magnet)
                item['filled_by_magnet'] = new_magnet
                logging.info(f"Successfully upgraded {item['title']} (ID: {identifier}) to new magnet.")
            else:
                logging.info(f"Upgrade for {item['title']} (ID: {identifier}) is not cached on Real-Debrid. Adding to not wanted.")
                add_to_not_wanted(new_magnet)
        except Exception as e:
            logging.error(f"Error checking cache status: {str(e)}")

    def remove_item(self, identifier: str):
        self.queue = [item for item in self.queue if self.generate_identifier(item) != identifier]
        if identifier in self.last_checked:
            del self.last_checked[identifier]
        if identifier in self.added_time:
            del self.added_time[identifier]
        if identifier in self.collected_time:
            del self.collected_time[identifier]
        logging.info(f"Removed item (ID: {identifier}) from UpgradingQueue")
        self.save_queue()

    def save_queue(self):
        queue_data = {
            'queue': [self.item_to_dict(item) for item in self.queue],
            'last_checked': {k: v.isoformat() for k, v in self.last_checked.items()},
            'added_time': {k: v.isoformat() for k, v in self.added_time.items()},
            'collected_time': {k: v.isoformat() for k, v in self.collected_time.items()}
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
                self.last_checked = {k: datetime.fromisoformat(v) for k, v in queue_data['last_checked'].items()}
                self.added_time = {k: datetime.fromisoformat(v) for k, v in queue_data['added_time'].items()}
                self.collected_time = {k: datetime.fromisoformat(v) for k, v in queue_data['collected_time'].items()}
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

    def get_queue_contents(self) -> List[Dict[str, Any]]:
        """
        Returns the current contents of the upgrading queue.
        
        Returns:
            List[Dict[str, Any]]: A list of dictionaries representing the items in the queue.
        """
        return self.queue