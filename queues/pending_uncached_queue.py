import logging
import json
from typing import Dict, Any
from database import get_all_media_items, get_media_item_by_id
from debrid.real_debrid import get_active_downloads, add_to_real_debrid

class PendingUncachedQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Pending Uncached")]
        # Deserialize scrape_results if they're stored as a JSON string
        for item in self.items:
            if 'scrape_results' in item:
                try:
                    if isinstance(item['scrape_results'], str):
                        item['scrape_results'] = json.loads(item['scrape_results'])
                    elif isinstance(item['scrape_results'], list) and all(isinstance(x, str) for x in item['scrape_results']):
                        # If it's a list of strings, join them and parse as JSON
                        joined_string = ''.join(item['scrape_results'])
                        item['scrape_results'] = json.loads(joined_string)
                except json.JSONDecodeError:
                    logging.error(f"Failed to parse scrape_results for item {item.get('id')}")
                    item['scrape_results'] = []

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        from queue_manager import QueueManager

        queue_manager = QueueManager()
        
        logging.debug("Processing pending uncached queue")
        
        # Continue processing until we reach the download limit
        while True:
            active_downloads, download_limit = get_active_downloads()
            
            if active_downloads >= download_limit:
                logging.info("Download limit reached. Stopping pending uncached queue processing.")
                break
            
            for item in self.items:
                item_identifier = queue_manager.generate_identifier(item)
                logging.info(f"Attempting to add pending uncached item: {item_identifier}")
                
                link = item.get('filled_by_magnet')
                if not link:
                    logging.error(f"No magnet link found for {item_identifier}")
                    continue

                add_result = add_to_real_debrid(link)
                if add_result:
                    logging.info(f"Successfully added pending uncached item: {item_identifier}")
                    # Ensure scrape_results is a list of dictionaries
                    scrape_results = item.get('scrape_results', [])
                    if isinstance(scrape_results, str):
                        try:
                            scrape_results = json.loads(scrape_results)
                        except json.JSONDecodeError:
                            logging.error(f"Failed to parse scrape_results for {item_identifier}")
                            scrape_results = []
                    elif isinstance(scrape_results, list) and all(isinstance(x, str) for x in scrape_results):
                        try:
                            joined_string = ''.join(scrape_results)
                            scrape_results = json.loads(joined_string)
                        except json.JSONDecodeError:
                            logging.error(f"Failed to parse joined scrape_results for {item_identifier}")
                            scrape_results = []
                    
                    if not isinstance(scrape_results, list):
                        logging.error(f"Invalid scrape_results format for {item_identifier}")
                        scrape_results = []
                    
                    logging.debug(f"Parsed scrape_results: {scrape_results[:5]}...")  # Log first 5 items
                    
                    queue_manager.move_to_adding(item, "Pending Uncached", item.get('filled_by_title'), scrape_results)
                    break  # Process one item at a time
                else:
                    logging.error(f"Failed to add pending uncached item: {item_identifier}")
                    queue_manager.move_to_upgrading(item, "Pending Uncached")

    # Add other methods as needed