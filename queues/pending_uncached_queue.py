import logging
import json
from typing import Dict, Any
from database import get_all_media_items
from debrid.real_debrid import get_active_downloads, add_to_real_debrid

class PendingUncachedQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Pending Uncached")]
        self._deserialize_scrape_results()

    def _deserialize_scrape_results(self):
        for item in self.items:
            if 'scrape_results' in item:
                try:
                    if isinstance(item['scrape_results'], str):
                        item['scrape_results'] = json.loads(item['scrape_results'])
                    elif isinstance(item['scrape_results'], list) and all(isinstance(x, str) for x in item['scrape_results']):
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
        logging.debug(f"Processing pending uncached queue. Items: {len(self.items)}")
        
        active_downloads, download_limit = get_active_downloads()
        
        if active_downloads >= download_limit:
            logging.info("Download limit reached. Stopping pending uncached queue processing.")
            return

        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            logging.info(f"Attempting to add pending uncached item: {item_identifier}")
            
            link = item.get('filled_by_magnet')
            if not link:
                logging.error(f"No magnet link found for {item_identifier}")
                continue

            add_result = add_to_real_debrid(link)
            if add_result:
                self._handle_successful_add(item, queue_manager)
                break  # Process one item at a time
            else:
                self._handle_failed_add(item, queue_manager)

            active_downloads, download_limit = get_active_downloads()
            if active_downloads >= download_limit:
                logging.info("Download limit reached during processing. Stopping.")
                break

        logging.debug(f"Pending uncached queue processing complete. Remaining items: {len(self.items)}")

    def _handle_successful_add(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Successfully added pending uncached item: {item_identifier}")
        
        scrape_results = self._get_parsed_scrape_results(item)
        logging.debug(f"Parsed scrape_results: {scrape_results[:5]}...")  # Log first 5 items
        
        queue_manager.move_to_adding(item, "Pending Uncached", item.get('filled_by_title'), scrape_results)
        self.remove_item(item)

    def _handle_failed_add(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.error(f"Failed to add pending uncached item: {item_identifier}")
        queue_manager.move_to_upgrading(item, "Pending Uncached")
        self.remove_item(item)

    def _get_parsed_scrape_results(self, item: Dict[str, Any]):
        scrape_results = item.get('scrape_results', [])
        if isinstance(scrape_results, str):
            try:
                return json.loads(scrape_results)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse scrape_results for {item.get('id')}")
                return []
        elif isinstance(scrape_results, list) and all(isinstance(x, str) for x in scrape_results):
            try:
                joined_string = ''.join(scrape_results)
                return json.loads(joined_string)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse joined scrape_results for {item.get('id')}")
                return []
        elif isinstance(scrape_results, list):
            return scrape_results
        else:
            logging.error(f"Invalid scrape_results format for {item.get('id')}")
            return []

    # Add other methods as needed