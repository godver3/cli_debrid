import logging
from typing import Dict, Any
from datetime import datetime, timedelta
from database import get_all_media_items, update_media_item_state
from queues.scraping_queue import ScrapingQueue

class UpgradingQueue:
    def __init__(self):
        self.items = []
        self.upgrade_times = {}
        self.last_scrape_times = {}
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

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.upgrade_times:
            del self.upgrade_times[item['id']]
        if item['id'] in self.last_scrape_times:
            del self.last_scrape_times[item['id']]

    def clean_up_upgrade_times(self):
        for item_id in list(self.upgrade_times.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.upgrade_times[item_id]
                if item_id in self.last_scrape_times:
                    del self.last_scrape_times[item_id]
                logging.debug(f"Cleaned up upgrade time for item ID: {item_id}")

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
                    
                    # Update the item's state to "Collected" in the database
                    update_media_item_state(item_id, "Collected")
                    
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

        results, filtered_out = self.scraping_queue.scrape_with_fallback(item, is_multi_pack, queue_manager or self)

        if results:
            best_result = results[0]
            logging.info(f"Found new result for {item_identifier}: {best_result['title']}")
            # Here you can implement logic to compare the new result with the existing one
            # and decide whether to update the item or not
            # For example:
            # if self.is_better_quality(best_result, item['current_quality']):
            #     self.update_item_with_new_result(item, best_result)
        else:
            logging.info(f"No new results found for {item_identifier} during hourly scrape")

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