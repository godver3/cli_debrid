from queue_manager import QueueManager
import logging

queue_manager = QueueManager()

def safe_process_queue(queue_name):
    try:
        getattr(queue_manager, f'process_{queue_name.lower()}')()
        # Update stats if needed
    except Exception as e:
        logging.error(f"Error processing {queue_name} queue: {str(e)}")
        # Update stats if needed