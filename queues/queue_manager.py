"""
Manages queue state transitions and database interactions.
"""

import logging
from typing import Dict, List, Optional
from database import (
    get_all_media_items,
    get_media_item_by_id,
    update_media_item_state,
    update_media_item
)

class QueueManager:
    """Manages queue state and transitions"""
    
    def get_scraping_items(self) -> List[Dict]:
        """
        Get all items currently in the Scraping state
        
        Returns:
            List of items in Scraping state
        """
        return [dict(row) for row in get_all_media_items(state="Scraping")]
        
    def move_to_checking(
        self,
        item_id: str,
        torrent_info: Dict,
        magnet: str,
        matched_files: Optional[List[Dict]] = None
    ) -> bool:
        """
        Move an item to the Checking state
        
        Args:
            item_id: ID of the item to move
            torrent_info: Information about the added torrent
            magnet: Magnet link that was added
            matched_files: Optional list of matched files
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Get current item state
            item = get_media_item_by_id(item_id)
            if not item:
                logging.error(f"Item {item_id} not found")
                return False
                
            # Update item with torrent info
            updates = {
                'torrent_id': torrent_info.get('id'),
                'magnet': magnet,
                'state': 'Checking'
            }
            
            # Add matched files if provided
            if matched_files:
                updates['matched_files'] = matched_files
                
            # Update the item
            update_media_item(item_id, updates)
            return True
            
        except Exception as e:
            logging.error(f"Error moving item {item_id} to checking: {str(e)}")
            return False
            
    def move_to_failed(
        self,
        item_id: str,
        error: str,
        retry: bool = True
    ) -> bool:
        """
        Move an item to the Failed state
        
        Args:
            item_id: ID of the item to move
            error: Error message
            retry: Whether to allow retry
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Get current item state
            item = get_media_item_by_id(item_id)
            if not item:
                logging.error(f"Item {item_id} not found")
                return False
                
            # Update item state
            updates = {
                'state': 'Failed',
                'error': error,
                'can_retry': retry
            }
            
            # Update the item
            update_media_item(item_id, updates)
            return True
            
        except Exception as e:
            logging.error(f"Error moving item {item_id} to failed: {str(e)}")
            return False
            
    def get_item_by_id(self, item_id: str) -> Optional[Dict]:
        """
        Get an item by its ID
        
        Args:
            item_id: ID of the item to get
            
        Returns:
            Item if found, None otherwise
        """
        try:
            return get_media_item_by_id(item_id)
        except Exception as e:
            logging.error(f"Error getting item {item_id}: {str(e)}")
            return None
