"""Mock queue manager for testing"""

import logging
from typing import Dict, Any, List
from database import (
    update_media_item_state,
    get_all_media_items
)

class MockQueueManager:
    """Minimal queue manager for testing"""
    
    def move_to_checking(self, item: Dict[str, Any], from_queue: str, title: str, link: str, filled_by_file: str, torrent_id: str = None):
        """Move an item to checking state"""
        item_id = item['id']
        logging.info(f"Moving item {item_id} to checking state")
        update_media_item_state(
            item_id,
            'Checking',
            filled_by_title=title,
            filled_by_magnet=link,
            filled_by_file=filled_by_file,
            filled_by_torrent_id=torrent_id
        )
        
    def get_scraping_items(self) -> List[Dict]:
        """Get all items in Scraping state"""
        return [dict(row) for row in get_all_media_items(state="Scraping")]
