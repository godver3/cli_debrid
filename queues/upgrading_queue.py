import logging
from typing import Dict, Any
from datetime import datetime, timedelta
from database import get_all_media_items, update_media_item, get_media_item_by_id, add_to_collected_notifications
from queues.scraping_queue import ScrapingQueue
from queues.adding_queue import AddingQueue
from settings import get_setting
from utilities.plex_functions import remove_file_from_plex

class UpgradingQueue:
    def __init__(self):
        self.items = []
        self.upgrade_times = {}
        self.last_scrape_times = {}
        self.upgrades_found = {}  # New dictionary to track upgrades found
        self.scraping_queue = ScrapingQueue()

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Upgrading")]
        for item in self.items:
            if item['id'] not in self.upgrade_times:
                collected_at = item.get('original_collected_at', datetime.now())
                self.upgrade_times[item['id']] = {
                    'start_time': datetime.now(),
                    'time_added': collected_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(collected_at, datetime) else str(collected_at)
                }

    def get_contents(self):
        contents = []
        for item in self.items:
            item_copy = item.copy()
            upgrade_info = self.upgrade_times.get(item['id'])
            if upgrade_info:
                item_copy['time_added'] = upgrade_info['time_added']
            else:
                item_copy['time_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            contents.append(item_copy)
        return contents

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        collected_at = item.get('original_collected_at', datetime.now())
        self.upgrade_times[item['id']] = {
            'start_time': datetime.now(),
            'time_added': collected_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(collected_at, datetime) else str(collected_at)
        }
        self.last_scrape_times[item['id']] = datetime.now()
        self.upgrades_found[item['id']] = 0  # Initialize upgrades found count

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.upgrade_times:
            del self.upgrade_times[item['id']]
        if item['id'] in self.last_scrape_times:
            del self.last_scrape_times[item['id']]
        if item['id'] in self.upgrades_found:
            del self.upgrades_found[item['id']]

    def clean_up_upgrade_times(self):
        for item_id in list(self.upgrade_times.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.upgrade_times[item_id]
                if item_id in self.last_scrape_times:
                    del self.last_scrape_times[item_id]
                logging.debug(f"Cleaned up upgrade time for item ID: {item_id}")
        for item_id in list(self.upgrades_found.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.upgrades_found[item_id]

    def process(self, queue_manager=None):
        current_time = datetime.now()
        for item in self.items[:]:  # Create a copy of the list to iterate over
            item_id = item['id']
            upgrade_info = self.upgrade_times.get(item_id)
            
            if upgrade_info:
                time_in_queue = current_time - upgrade_info['start_time']
                
                # Check if the item has been in the queue for more than 24 hours
                if time_in_queue > timedelta(hours=24):
                    logging.info(f"Item {item_id} has been in the Upgrading queue for over 24 hours.")
                                        
                    # Remove the item from the queue
                    self.remove_item(item)
                    
                    logging.info(f"Moved item {item_id} to Collected state after 24 hours in Upgrading queue.")
                
                # Check if an hour has passed since the last scrape
                elif self.should_perform_hourly_scrape(item_id, current_time):
                    self.hourly_scrape(item, queue_manager)
                    self.last_scrape_times[item_id] = current_time

        # Clean up upgrade times for items no longer in the queue
        self.clean_up_upgrade_times()

    def should_perform_hourly_scrape(self, item_id: str, current_time: datetime) -> bool:
        last_scrape_time = self.last_scrape_times.get(item_id)
        if last_scrape_time is None:
            return True
        return (current_time - last_scrape_time) >= timedelta(hours=1)

    def hourly_scrape(self, item: Dict[str, Any], queue_manager=None):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Performing hourly scrape for {item_identifier}")

        is_multi_pack = self.check_multi_pack(item)

        # Perform scraping
        results, filtered_out = self.scraping_queue.scrape_with_fallback(item, is_multi_pack, queue_manager or self)

        if results:
            # Find the position of the current item's 'filled_by_magnet' in the results
            current_title = item.get('filled_by_title')
            current_position = next((index for index, result in enumerate(results) if result.get('title') == current_title), None)

            if current_position is not None:
                logging.info(f"Current item {item_identifier} is at position {current_position + 1} in the scrape results")
                
                # Only consider results that are in higher positions than the current item
                better_results = results[:current_position]
                
                if better_results:
                    logging.info(f"Found {len(better_results)} potential upgrade(s) for {item_identifier}")

                    # Use AddingQueue to attempt the upgrade
                    adding_queue = AddingQueue()
                    uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None').lower()
                    success = adding_queue.process_item(queue_manager, item, better_results, uncached_handling)

                    if success:
                        logging.info(f"Successfully upgraded item {item_identifier}")
                        
                        # Increment the upgrades found count
                        self.upgrades_found[item['id']] = self.upgrades_found.get(item['id'], 0) + 1
                        
                        if get_setting('Scraping', 'enable_upgrading_cleanup', False):  
                            # Remove original item from Plex
                            self.remove_original_item_from_plex(item)
                            # Remove original item from account
                            self.remove_original_item_from_account(item)

                        # Update the item in the database with new values from the upgrade
                        self.update_item_with_upgrade(item, adding_queue)

                        # Remove the item from the Upgrading queue
                        self.remove_item(item)

                        logging.info(f"Maintained item {item_identifier} in Upgrading state after successful upgrade.")
                    else:
                        logging.info(f"Failed to upgrade item {item_identifier}")
                else:
                    logging.info(f"No better results found for {item_identifier}")
            else:
                logging.info(f"Current item {item_identifier} not found in scrape results, skipping upgrade")
        else:
            logging.info(f"No new results found for {item_identifier} during hourly scrape")

    def remove_original_item_from_plex(self, item: Dict[str, Any]):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Removing original file from Plex: {item_identifier}")

        # Get the file path and title of the original item
        original_file_path = item.get('location')
        original_title = item.get('title')

        if original_file_path and original_title:
            # Use the updated remove_file_from_plex function
            success = remove_file_from_plex(original_title, original_file_path)
            if success:
                logging.info(f"Successfully removed file from Plex: {item_identifier}")
            else:
                logging.warning(f"Failed to remove file from Plex: {item_identifier}")
        else:
            logging.warning(f"No file path or title found for item: {item_identifier}")

    def remove_original_item_from_account(self, item: Dict[str, Any]):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Removing original item from account: {item_identifier}")

        # Retrieve the original torrent ID from the item
        original_torrent_id = item.get('filled_by_torrent_id')

        if original_torrent_id:
            # Use AddingQueue's remove_unwanted_torrent method
            adding_queue = AddingQueue()
            adding_queue.remove_unwanted_torrent(original_torrent_id)
            logging.info(f"Removed original torrent with ID {original_torrent_id} from account")
        else:
            logging.warning(f"No original torrent ID found for {item_identifier}")

    def update_item_with_upgrade(self, item: Dict[str, Any], adding_queue: AddingQueue):
        # Fetch the new values from the adding queue
        new_values = adding_queue.get_new_item_values(item)

        if new_values:
            # Update the item in the database with new values
            update_media_item(item['id'], **new_values)
            logging.info(f"Updated item in database with new values for {self.generate_identifier(item)}")

            # Optionally, add to collected notifications
            add_to_collected_notifications(item)
        else:
            logging.warning(f"No new values obtained for item {self.generate_identifier(item)}")

    def check_multi_pack(self, item: Dict[str, Any]) -> bool:
        if item['type'] != 'episode':
            return False

        return any(
            other_item['type'] == 'episode' and
            other_item['imdb_id'] == item['imdb_id'] and
            other_item['season_number'] == item['season_number'] and
            other_item['id'] != item['id']
            for other_item in self.items
        )

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{'_'.join(item['version'].split())}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{'_'.join(item['version'].split())}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")