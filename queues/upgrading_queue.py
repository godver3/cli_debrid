import logging
from typing import Dict, Any
from datetime import datetime, timedelta
from database import get_all_media_items, update_media_item_state, get_media_item_by_id
from database.database_writing import add_to_collected_notifications
from queues.scraping_queue import ScrapingQueue
from queues.adding_queue import AddingQueue
from settings import get_setting
from utilities.plex_functions import remove_file_from_plex
import os
import pickle
from pathlib import Path
from database.database_writing import update_media_item
from database.core import get_db_connection
from difflib import SequenceMatcher

class UpgradingQueue:
    def __init__(self):
        self.items = []
        self.upgrade_times = {}
        self.last_scrape_times = {}
        self.upgrades_found = {}  # New dictionary to track upgrades found
        self.scraping_queue = ScrapingQueue()
        # Get db_content directory from environment variable with fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        self.upgrades_file = Path(db_content_dir) / "upgrades.pkl"
        self.upgrades_data = self.load_upgrades_data()

    def load_upgrades_data(self):
        if self.upgrades_file.exists():
            with open(self.upgrades_file, 'rb') as f:
                return pickle.load(f)
        return {}

    def save_upgrades_data(self):
        with open(self.upgrades_file, 'wb') as f:
            pickle.dump(self.upgrades_data, f)

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
            
            # Add upgrade history information
            item_copy['upgrades_found'] = self.upgrades_data.get(item['id'], {}).get('count', 0)
            item_copy['upgrade_history'] = self.upgrades_data.get(item['id'], {}).get('history', [])
            
            contents.append(item_copy)
        return contents

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        collected_at = item.get('original_collected_at', datetime.now())
        logging.info(f"collected_at: {collected_at}")
        self.upgrade_times[item['id']] = {
            'start_time': datetime.now(),
            'time_added': collected_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(collected_at, datetime) else str(collected_at)
        }
        self.last_scrape_times[item['id']] = datetime.now()
        self.upgrades_found[item['id']] = 0  # Initialize upgrades found count
        
        # Ensure the upgrades_data entry is initialized
        if item['id'] not in self.upgrades_data:
            self.upgrades_data[item['id']] = {'count': 0, 'history': []}
        
        self.save_upgrades_data()

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.upgrade_times:
            del self.upgrade_times[item['id']]
        if item['id'] in self.last_scrape_times:
            del self.last_scrape_times[item['id']]
        if item['id'] in self.upgrades_found:
            del self.upgrades_found[item['id']]
        if item['id'] in self.upgrades_data:
            del self.upgrades_data[item['id']]
            self.save_upgrades_data()

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
        for item_id in list(self.upgrades_data.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.upgrades_data[item_id]
        self.save_upgrades_data()

    def process(self, queue_manager=None):
        current_time = datetime.now()
        for item in self.items[:]:  # Create a copy of the list to iterate over
            try:
                item_id = item['id']
                upgrade_info = self.upgrade_times.get(item_id)
                
                if upgrade_info:
                    collected_at = datetime.fromisoformat(item['original_collected_at']) if isinstance(item['original_collected_at'], str) else item['original_collected_at']
                    time_in_queue = current_time - collected_at
                    
                    logging.info(f"Item {item_id} has been in the Upgrading queue for {time_in_queue}.")

                    # Check if the item has been in the queue for more than 24 hours
                    if time_in_queue > timedelta(hours=24):
                        logging.info(f"Item {item_id} has been in the Upgrading queue for over 24 hours.")
                                            
                        # Remove the item from the queue
                        self.remove_item(item)
                        
                        update_media_item_state(item_id, state="Collected")

                        logging.info(f"Moved item {item_id} to Collected state after 24 hours in Upgrading queue.")
                    
                    # Check if an hour has passed since the last scrape
                    elif self.should_perform_hourly_scrape(item_id, current_time):
                        self.hourly_scrape(item, queue_manager)
                        self.last_scrape_times[item_id] = current_time
            except Exception as e:
                logging.error(f"Error processing item {item.get('id', 'unknown')}: {str(e)}")
                logging.exception("Traceback:")

        # Clean up upgrade times for items no longer in the queue
        self.clean_up_upgrade_times()

    def should_perform_hourly_scrape(self, item_id: str, current_time: datetime) -> bool:
        #return True
        last_scrape_time = self.last_scrape_times.get(item_id)
        if last_scrape_time is None:
            return True
        return (current_time - last_scrape_time) >= timedelta(hours=1)

    def log_upgrade(self, item: Dict[str, Any], adding_queue: AddingQueue):
        # Get db_content directory from environment variable with fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        log_file = os.path.join(db_content_dir, "upgrades.log")
        item_identifier = self.generate_identifier(item)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_file = adding_queue.get_new_item_values(item)
        log_entry = f"{timestamp} - Upgraded: {item_identifier} - New File: {new_file['filled_by_file']} - Original File: {item['upgrading_from']}\n"

        # Create the log file if it doesn't exist
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        if not os.path.exists(log_file):
            open(log_file, 'w').close()

        # Append the log entry to the file
        with open(log_file, 'a') as f:
            f.write(log_entry)

        # Update upgrades_data
        if item['id'] not in self.upgrades_data:
            self.upgrades_data[item['id']] = {'count': 0, 'history': []}
        
        self.upgrades_data[item['id']]['count'] += 1
        self.upgrades_data[item['id']]['history'].append({
            'datetime': datetime.now(),
            'new_file': item['filled_by_file'],
            'original_file': item['upgrading_from']
        })
        self.save_upgrades_data()

    def hourly_scrape(self, item: Dict[str, Any], queue_manager=None):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Performing hourly scrape for {item_identifier}")

        update_media_item(item['id'], upgrading=True)

        is_multi_pack = self.check_multi_pack(item)
        is_multi_pack = False

        # Perform scraping
        results, filtered_out = self.scraping_queue.scrape_with_fallback(item, is_multi_pack, queue_manager or self, skip_filter=True)

        if results:
            # Find the position of the current item's 'filled_by_magnet' in the results
            current_title = item.get('original_scraped_torrent_title')
            if current_title is None:
                logging.warning(f"No original_scraped_torrent_title found for item {item_identifier}, using filled_by_title as fallback")
                current_title = item.get('filled_by_title')
                if current_title is None:
                    logging.error(f"No title information found for item {item_identifier}, skipping upgrade check")
                    return

            current_position = next((index for index, result in enumerate(results) if result.get('title') == current_title), None)

            # Get similarity threshold from settings, default to 95%
            similarity_threshold = 0.95 #float(get_setting('Scraping', 'upgrade_similarity_threshold', '0.95'))

            for index, result in enumerate(results):
                # Calculate similarity score
                similarity = SequenceMatcher(None, current_title.lower(), result['title'].lower()).ratio()
                logging.info(f"Result {index}: {result['title']} (Similarity: {similarity:.2%})")

            if current_position is None:
                logging.info(f"Current item {item_identifier} not found in scrape results")
                # Filter out results that are too similar to current title
                better_results = [
                    result for result in results 
                    if SequenceMatcher(None, current_title.lower(), result['title'].lower()).ratio() < similarity_threshold
                ]
            else:
                logging.info(f"Current item {item_identifier} is at position {current_position + 1} in the scrape results")
                logging.info(f"Current item title: {current_title}")
                
                # Only consider results that are in higher positions AND not too similar
                better_results = [
                    result for result in results[:current_position]
                    if SequenceMatcher(None, current_title.lower(), result['title'].lower()).ratio() < similarity_threshold
                ]
            
            if better_results:
                logging.info(f"Found {len(better_results)} potential upgrade(s) for {item_identifier} after similarity filtering")
                logging.info("Better results to try:")
                for i, result in enumerate(better_results):
                    similarity = SequenceMatcher(None, current_title.lower(), result['title'].lower()).ratio()
                    logging.info(f"  {i}: {result['title']} (Similarity: {similarity:.2%})")

                # Update item with scrape results in database first
                best_result = better_results[0]
                update_media_item_state(item['id'], 'Adding', filled_by_title=best_result['title'], scrape_results=better_results)
                updated_item = get_media_item_by_id(item['id'])

                # Use AddingQueue to attempt the upgrade with updated item
                adding_queue = AddingQueue()
                uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None').lower()
                adding_queue.add_item(updated_item)
                adding_queue.process(queue_manager)

                # Check if the item was successfully moved to Checking queue
                from database.core import get_db_connection
                conn = get_db_connection()
                cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item['id'],))
                current_state = cursor.fetchone()['state']
                conn.close()

                if current_state == 'Checking':
                    logging.info(f"Successfully initiated upgrade for item {item_identifier}")
                    
                    # Ensure the upgrades_data entry is initialized
                    if item['id'] not in self.upgrades_data:
                        self.upgrades_data[item['id']] = {'count': 0, 'history': []}
                    
                    # Increment the upgrades found count
                    self.upgrades_data[item['id']]['count'] += 1
                
                    item['upgrading_from'] = item['filled_by_file'] 

                    logging.info(f"Item {item_identifier} is upgrading from {item['upgrading_from']} (Upgrades found: {self.upgrades_data[item['id']]['count']})")

                    # Log the upgrade
                    self.log_upgrade(item, adding_queue)

                    # Update the item in the database with new values from the upgrade
                    self.update_item_with_upgrade(item, adding_queue)

                    # Remove the item from the Upgrading queue
                    self.remove_item(item)

                    logging.info(f"Successfully upgraded item {item_identifier} to Checking state")
                else:
                    logging.info(f"Failed to upgrade item {item_identifier} - current state: {current_state}")
                    # Return the item to Upgrading state since the upgrade attempt failed
                    update_media_item_state(item['id'], 'Upgrading')
                    logging.info(f"Returned item {item_identifier} to Upgrading state after failed upgrade attempt")
            else:
                logging.info(f"No better results found for {item_identifier}")
        else:
            logging.info(f"No new results found for {item_identifier} during hourly scrape")

    def update_item_with_upgrade(self, item: Dict[str, Any], adding_queue: AddingQueue):
        # Fetch the new values from the adding queue
        new_values = adding_queue.get_new_item_values(item)

        if new_values:
            # Begin a transaction
            conn = get_db_connection()
            try:
                conn.execute('BEGIN TRANSACTION')

                # Set upgrading_from to the current filled_by_file before updating
                upgrading_from = item['filled_by_file']

                # Update the item in the database with new values
                conn.execute('''
                    UPDATE media_items
                    SET upgrading_from = ?, filled_by_file = ?, filled_by_magnet = ?, version = ?, last_updated = ?, state = ?, upgrading_from_torrent_id = ?, upgraded = 1
                    WHERE id = ?
                ''', (
                    upgrading_from,
                    new_values['filled_by_file'],
                    new_values['filled_by_magnet'],
                    new_values['version'],
                    datetime.now(),
                    'Checking',
                    item['filled_by_torrent_id'],
                    item['id']
                ))

                conn.commit()
                logging.info(f"Updated item in database with new values for {self.generate_identifier(item)}")

                # Update the item dictionary as well
                item['upgrading_from'] = upgrading_from
                item['filled_by_file'] = new_values['filled_by_file']
                item['filled_by_magnet'] = new_values['filled_by_magnet']
                item['upgrading_from_torrent_id'] = item['filled_by_torrent_id']
                item['version'] = new_values['version']
                item['last_updated'] = datetime.now()
                item['state'] = 'Checking'

                # Send notification for the upgrade
                try:
                    from notifications import send_notifications
                    from routes.settings_routes import get_enabled_notifications_for_category
                    from extensions import app

                    with app.app_context():
                        response = get_enabled_notifications_for_category('upgrading')
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
                                    'new_state': 'Upgrading',
                                    'is_upgrade': True,
                                    'upgrading_from': upgrading_from
                                }
                                send_notifications([notification_data], enabled_notifications, notification_category='collected')
                                logging.debug(f"Sent upgrade notification for item {item['id']}")
                except Exception as e:
                    logging.error(f"Failed to send upgrade notification: {str(e)}")

            except Exception as e:
                conn.rollback()
                logging.error(f"Error updating item {self.generate_identifier(item)}: {str(e)}", exc_info=True)
            finally:
                conn.close()
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
        
def log_successful_upgrade(item: Dict[str, Any]):
    # Get db_content directory from environment variable with fallback
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    log_file = os.path.join(db_content_dir, "upgrades.log")
    item_identifier = UpgradingQueue.generate_identifier(item)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} - Upgrade Complete: {item_identifier}\n"

    # Create the log file if it doesn't exist
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not os.path.exists(log_file):
        open(log_file, 'w').close()

    # Append the log entry to the file
    with open(log_file, 'a') as f:
        f.write(log_entry)