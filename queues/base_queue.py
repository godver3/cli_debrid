import logging
from typing import Dict, Any

class BaseQueue:
    """Base interface for all queue classes"""

    def update(self):
        """Update the queue contents"""
        raise NotImplementedError("Each queue must implement update method")

    def process(self, queue_manager):
        """Process items in the queue"""
        raise NotImplementedError("Each queue must implement process method")

    def get_contents(self):
        """Get all items in the queue"""
        raise NotImplementedError("Each queue must implement get_contents method")

    def add_item(self, item: Dict[str, Any]):
        """Add an item to the queue"""
        raise NotImplementedError("Each queue must implement add_item method")

    def remove_item(self, item: Dict[str, Any]):
        """Remove an item from the queue"""
        raise NotImplementedError("Each queue must implement remove_item method")

    def contains_item_id(self, item_id: Any) -> bool:
        """Check if the queue contains an item with the given ID (optimized)"""
        # Default implementation, queues should override this with more efficient implementations
        return any(i['id'] == item_id for i in self.get_contents())

    def _record_item_entered(self, queue_manager, item: Dict[str, Any]):
        """Record that an item entered this queue"""
        if hasattr(queue_manager, 'queue_timer') and item and 'id' in item:
            queue_name = self.__class__.__name__.replace('Queue', '')
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            queue_manager.queue_timer.item_entered_queue(item_id, queue_name, item_identifier)

    def _record_item_exited(self, queue_manager, item: Dict[str, Any]):
        """Record that an item exited this queue"""
        if hasattr(queue_manager, 'queue_timer') and item and 'id' in item:
            queue_name = self.__class__.__name__.replace('Queue', '')
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            queue_manager.queue_timer.item_exited_queue(item_id, queue_name, item_identifier)
