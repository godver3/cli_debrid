import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
import json
import os
from scraper.scraper import scrape
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, RealDebridUnavailableError, extract_hash_from_magnet
from database import get_media_item_status
from not_wanted_magnets import add_to_not_wanted, is_magnet_not_wanted
from settings import get_setting

class UpgradingQueue:
    def __init__(self):
        self.queue: List[Dict[str, Any]] = []
        self.last_checked: Dict[str, datetime] = {}
        self.added_time: Dict[str, datetime] = {}
        self.collected_time: Dict[str, datetime] = {}
        self.queue_file = 'user/db_content/upgrading_queue.json'
        self.load_queue()

    def generate_identifier(self, item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['imdb_id']}_{item.get('version', 'Unknown')}"
        elif item['type'] == 'episode':
            return f"episode_{item['imdb_id']}_{item['season_number']}_{item['episode_number']}_{item.get('version', 'Unknown')}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def add_item(self, item: Dict[str, Any]):
        identifier = self.generate_identifier(item)
        
        # Check if the item is already in the queue
        if identifier not in [self.generate_identifier(i) for i in self.queue]:
            # Check the age of the item
            release_date_str = item.get('release_date')
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                    current_date = datetime.now().date()
                    age = current_date - release_date
                    
                    if age <= timedelta(days=7):
                        item['last_checked'] = datetime.now()
                        self.queue.append(item)
                        self.last_checked[identifier] = datetime.now()
                        self.added_time[identifier] = datetime.now()
                        logging.info(f"Added item {item['title']} (ID: {identifier}) to UpgradingQueue. Age: {age.days} days. Queue size: {len(self.queue)}")
                        self.save_queue()
                    else:
                        logging.info(f"Skipped adding item {item['title']} (ID: {identifier}) to UpgradingQueue. Age: {age.days} days (older than 7 days).")
                except ValueError:
                    logging.error(f"Invalid release date format for item {item['title']} (ID: {identifier}): {release_date_str}")
            else:
                logging.warning(f"No release date found for item {item['title']} (ID: {identifier}). Adding to queue anyway.")
                item['last_checked'] = datetime.now()
                self.queue.append(item)
                self.last_checked[identifier] = datetime.now()
                self.added_time[identifier] = datetime.now()
                logging.info(f"Added item {item['title']} (ID: {identifier}) to UpgradingQueue without age check. Queue size: {len(self.queue)}")
                self.save_queue()
        else:
            logging.info(f"Item {item['title']} (ID: {identifier}) already in UpgradingQueue. Skipping. Queue size: {len(self.queue)}")

    def process_queue(self):
        current_time = datetime.now()
        items_to_remove = []
        for item in self.queue:
            identifier = self.generate_identifier(item)
            time_since_last_check = current_time - self.last_checked.get(identifier, datetime.min)
            if time_since_last_check >= timedelta(minutes=30):
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
                        logging.info(f"Item {item['title']} (ID: {identifier}) has been collected. Starting immediate upgrade check.")
                        self.check_for_upgrade(item)  # Immediate upgrade check
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
            item.get('version', 'Unknown'),  # Include version in scrape call
            item.get('season_number'),
            item.get('episode_number'),
            False,
            item.get('genres')
        )

        # Filter out not wanted magnets
        results = [result for result in results if not is_magnet_not_wanted(result['magnet'])]

        if not results:
            logging.info(f"No valid results found for {item['title']} (ID: {identifier}). Skipping upgrade check.")
            return

        current_magnet = item.get('filled_by_magnet')
        current_title = item.get('filled_by_title', 'Unknown')

        def get_magnet_hash(magnet):
            return magnet.split('&dn=')[0] if magnet else ''

        current_magnet_hash = get_magnet_hash(current_magnet)

        # Log original release details
        logging.debug(f"Original release for {item['title']} (ID: {identifier}):")
        logging.debug(f"Title: {current_title}")
        logging.debug(f"Magnet hash: {current_magnet_hash}")

        # Log ranked listing of potential upgrades
        logging.debug(f"Potential upgrades for {item['title']} (ID: {identifier}):")
        for idx, result in enumerate(results):
            result_magnet_hash = get_magnet_hash(result['magnet'])
            result_title = result.get('title', 'Unknown')
            logging.debug(f"Rank {idx + 1}:")
            logging.debug(f"  Title: {result_title}")
            logging.debug(f"  Magnet hash: {result_magnet_hash}")
            if result_magnet_hash == current_magnet_hash:
                logging.debug(f"  ** Current release (Rank {idx + 1}) **")

        current_index = next((i for i, result in enumerate(results) if get_magnet_hash(result['magnet']) == current_magnet_hash), -1)

        uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None')

        if current_index == -1:
            logging.info(f"Current magnet not found in results for {item['title']} (ID: {identifier}). Attempting upgrade to top result.")
            self.try_upgrade(item, results[0], uncached_handling)
        elif current_index > 0:
            # We found a better result
            logging.info(f"Potential upgrade found for {item['title']} (ID: {identifier}) from position {current_index + 1} to top result.")
            self.try_upgrade(item, results[0], uncached_handling)
        else:
            logging.info(f"No better result found for {item['title']} (ID: {identifier}). Keeping current magnet.")
            logging.debug("Rationale: Current release is already the top-ranked result.")

    def try_upgrade(self, item: Dict[str, Any], new_result: Dict[str, Any], uncached_handling: str):
        identifier = self.generate_identifier(item)
        new_magnet = new_result['magnet']
        new_title = new_result.get('title', 'Unknown')
        hash_value = extract_hash_from_magnet(new_magnet)

        logging.debug(f"Attempting upgrade for {item['title']} (ID: {identifier}):")
        logging.debug(f"Current title: {item.get('filled_by_title', 'Unknown')}")
        logging.debug(f"Current magnet: {item.get('filled_by_magnet', 'Unknown')}")
        logging.debug(f"New title: {new_title}")
        logging.debug(f"New magnet: {new_magnet}")
        logging.debug(f"Uncached content handling: {uncached_handling}")

        if not hash_value:
            logging.warning(f"Failed to extract hash from magnet link for {item['title']} (ID: {identifier})")
            logging.debug("Rationale: Unable to process magnet link, cannot proceed with upgrade.")
            return

        try:
            cache_status = is_cached_on_rd(hash_value)
            logging.debug(f"Cache status for {hash_value}: {cache_status}")

            is_cached = hash_value in cache_status and cache_status[hash_value]
            
            if is_cached or uncached_handling in ['Hybrid', 'Full']:
                if not is_cached:
                    logging.info(f"Upgrade for {item['title']} (ID: {identifier}) is not cached, but {uncached_handling} mode allows uncached content.")
                
                try:
                    add_to_real_debrid(new_magnet)
                except RealDebridUnavailableError:
                    logging.error(f"Real-Debrid service is unavailable. Skipping upgrade for now.")
                    logging.debug("Rationale: Real-Debrid service is down, cannot add new magnet.")
                    return
                except Exception as e:
                    logging.error(f"Error adding magnet to Real-Debrid: {str(e)}")
                    add_to_not_wanted(new_magnet)
                    logging.debug(f"Rationale: Failed to add magnet to Real-Debrid due to error: {str(e)}")
                    return

                # If we reach here, addition was successful
                old_magnet = item['filled_by_magnet']
                old_title = item['filled_by_title']
                item['filled_by_magnet'] = new_magnet
                item['filled_by_title'] = new_title
                logging.info(f"Successfully upgraded {item['title']} (ID: {identifier}):")
                logging.info(f"Old title: {old_title}")
                logging.info(f"New title: {new_title}")
                logging.info(f"Old magnet: {old_magnet}")
                logging.info(f"New magnet: {new_magnet}")
                logging.debug(f"Rationale: New release is higher-ranked and {'cached' if is_cached else 'uncached but allowed'} on Real-Debrid.")
            else:
                logging.info(f"Upgrade for {item['title']} (ID: {identifier}) is not cached on Real-Debrid and uncached content is not allowed. Skipping upgrade.")
                logging.debug("Rationale: Potential upgrade is not cached on Real-Debrid and uncached content handling is set to 'None'.")
        except Exception as e:
            logging.error(f"Error checking cache status: {str(e)}")
            logging.debug(f"Rationale: Failed to check Real-Debrid cache status due to error: {str(e)}")

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

    def item_to_dict(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Convert an item to a JSON-serializable dictionary."""
        serializable_item = item.copy()
        for key, value in serializable_item.items():
            if isinstance(value, datetime):
                serializable_item[key] = value.isoformat()
        # Ensure version is included
        if 'version' not in serializable_item:
            serializable_item['version'] = 'Unknown'
        return serializable_item

    def dict_to_item(self, item_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a dictionary to an item, parsing datetime strings."""
        item = item_dict.copy()
        for key, value in item.items():
            if key in ['last_checked', 'added_time', 'collected_time'] and isinstance(value, str):
                try:
                    item[key] = datetime.fromisoformat(value)
                except ValueError:
                    # If parsing fails, keep the original string
                    pass
        # Ensure version is included
        if 'version' not in item:
            item['version'] = 'Unknown'
        return item

    def get_queue_contents(self) -> List[Dict[str, Any]]:
        """
        Returns the current contents of the upgrading queue.
        
        Returns:
            List[Dict[str, Any]]: A list of dictionaries representing the items in the queue.
        """
        return self.queue
