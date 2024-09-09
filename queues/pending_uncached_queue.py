import logging
from typing import Dict, Any
from database import get_all_media_items, get_media_item_by_id
from debrid.real_debrid import get_active_downloads, add_to_real_debrid

class PendingUncachedQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Pending Uncached")]

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        logging.debug("Processing pending uncached queue")
        active_downloads, download_limit = get_active_downloads()
        
        if active_downloads < download_limit:
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
                    # Retrieve the stored scrape results
                    scrape_results = item.get('scrape_results', [])
                    queue_manager.move_to_adding(item, "Pending Uncached", item.get('filled_by_title'), scrape_results)
                    break  # Process one item at a time
                else:
                    logging.error(f"Failed to add pending uncached item: {item_identifier}")

    # Add other methods as needed