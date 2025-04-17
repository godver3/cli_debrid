import logging
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import json
import os
import time

from database.database_writing import update_media_item_state
from database.database_reading import get_media_item_by_id
from database.collected_items import add_to_collected_notifications
from routes.notifications import send_queue_pause_notification, send_queue_resume_notification

from queues.wanted_queue import WantedQueue
from queues.scraping_queue import ScrapingQueue
from queues.adding_queue import AddingQueue
from queues.checking_queue import CheckingQueue
from queues.sleeping_queue import SleepingQueue
from queues.unreleased_queue import UnreleasedQueue
from queues.blacklisted_queue import BlacklistedQueue
from queues.pending_uncached_queue import PendingUncachedQueue
from queues.upgrading_queue import UpgradingQueue

# Get the item tracker logger
item_tracker_logger = logging.getLogger('item_tracker')

class QueueTimer:
    """Tracks how long items spend in each queue"""
    
    def __init__(self):
        # Initialize stats tracking first
        self.queue_stats = {
            queue_name: {'count': 0, 'total_time': 0, 'min_time': float('inf'), 'max_time': 0}
            for queue_name in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 
                             'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading']
        }
        
        # Initialize queue times
        self.queue_times = {}
        
        # Set up file paths
        self.db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        self.timing_file = os.path.join(self.db_content_dir, 'queue_timing_data.json')
        
        # Load timing data after all attributes are initialized
        self.load_timing_data()
        
    def load_timing_data(self):
        """Load timing data from disk"""
        try:
            if os.path.exists(self.timing_file):
                with open(self.timing_file, 'r') as f:
                    data = json.load(f)
                    self.queue_times = data.get('queue_times', {})
                    loaded_stats = data.get('queue_stats', {})
                    # Update queue_stats with loaded data while preserving structure
                    for queue_name, stats in loaded_stats.items():
                        if queue_name in self.queue_stats:
                            self.queue_stats[queue_name].update(stats)
                    logging.info(f"Loaded queue timing data for {len(self.queue_times)} items")
        except Exception as e:
            logging.error(f"Error loading queue timing data: {e}")
            self.queue_times = {}
            
    def save_timing_data(self, force=False):
        """Save timing data to disk, but not too frequently"""
        if not hasattr(self, '_last_save_time'):
            self._last_save_time = 0
            
        current_time = time.time()
        # Only save every 5 minutes unless forced
        if force or current_time - self._last_save_time >= 300:
            try:
                os.makedirs(os.path.dirname(self.timing_file), exist_ok=True)
                
                # Prune very old or completed items to prevent file growth
                self._prune_old_timing_data()
                
                with open(self.timing_file, 'w') as f:
                    json.dump({'queue_times': self.queue_times, 'queue_stats': self.queue_stats}, f)
                    
                self._last_save_time = current_time
                logging.debug(f"Saved queue timing data for {len(self.queue_times)} items")
            except Exception as e:
                logging.error(f"Error saving queue timing data: {e}")
                
    def _prune_old_timing_data(self):
        """Remove timing data for completed items or items older than 30 days"""
        current_time = datetime.now().timestamp()
        thirty_days_ago = current_time - (30 * 24 * 60 * 60)
        
        # Find items to remove
        items_to_remove = []
        for item_id, queue_data in self.queue_times.items():
            all_completed = True
            has_old_entry = False
            
            for queue_name, times in queue_data.items():
                entry_time = times[0]
                exit_time = times[1]
                
                # Check if entry is older than 30 days
                if entry_time and entry_time < thirty_days_ago:
                    has_old_entry = True
                    
                # If any queue doesn't have an exit time, item is not completed
                if exit_time is None:
                    all_completed = False
                    
            # Remove if completed or all entries are old
            if all_completed or has_old_entry:
                items_to_remove.append(item_id)
                
        # Remove the identified items
        for item_id in items_to_remove:
            del self.queue_times[item_id]
            
        if items_to_remove:
            logging.debug(f"Pruned {len(items_to_remove)} old items from queue timing data")
    
    def item_entered_queue(self, item_id, queue_name, item_identifier=None):
        """Record when an item enters a queue"""
        if not item_id:
            return
            
        # Initialize item's timing data if not present
        if item_id not in self.queue_times:
            self.queue_times[item_id] = {}
            
        # Record entry time
        current_time = datetime.now().timestamp()
        
        # Store as [entry_time, exit_time] where exit_time is initially None
        self.queue_times[item_id][queue_name] = [current_time, None]
        
        if item_identifier:
            logging.debug(f"Item {item_identifier} (ID: {item_id}) entered {queue_name} queue")
    
    def item_exited_queue(self, item_id, queue_name, item_identifier=None):
        """Record when an item exits a queue"""
        if not item_id or item_id not in self.queue_times:
            return
            
        # Get the item's queue timing data
        item_timing = self.queue_times.get(item_id, {})
        queue_timing = item_timing.get(queue_name)
        
        # If we have entry timing for this queue
        if queue_timing and queue_timing[0] is not None:
            current_time = datetime.now().timestamp()
            entry_time = queue_timing[0]
            duration = current_time - entry_time  # Time in seconds
            
            # Set the exit time
            self.queue_times[item_id][queue_name][1] = current_time
            
            # Update stats for this queue
            if queue_name in self.queue_stats:
                self.queue_stats[queue_name]['count'] += 1
                self.queue_stats[queue_name]['total_time'] += duration
                self.queue_stats[queue_name]['min_time'] = min(self.queue_stats[queue_name]['min_time'], duration)
                self.queue_stats[queue_name]['max_time'] = max(self.queue_stats[queue_name]['max_time'], duration)
            
            if item_identifier:
                time_str = self._format_duration(duration)
                logging.debug(f"Item {item_identifier} (ID: {item_id}) exited {queue_name} queue after {time_str}")
                
            # Periodically save timing data
            self.save_timing_data()
            
    def _format_duration(self, seconds):
        """Format duration in seconds to a human-readable string"""
        if seconds < 60:
            return f"{seconds:.1f} seconds"
        elif seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.1f} minutes"
        else:
            hours = seconds / 3600
            return f"{hours:.1f} hours"
            
    def get_item_timing(self, item_id):
        """Get timing data for a specific item"""
        return self.queue_times.get(item_id, {})
        
    def generate_timing_report(self):
        """Generate a report of queue timing statistics"""
        report = ["Queue Timing Statistics:"]
        
        for queue_name, stats in self.queue_stats.items():
            count = stats['count']
            if count > 0:
                avg_time = stats['total_time'] / count
                min_time = stats['min_time'] if stats['min_time'] != float('inf') else 0
                max_time = stats['max_time']
                
                report.append(f"{queue_name} Queue:")
                report.append(f"  Items processed: {count}")
                report.append(f"  Average time: {self._format_duration(avg_time)}")
                report.append(f"  Min time: {self._format_duration(min_time)}")
                report.append(f"  Max time: {self._format_duration(max_time)}")
                
        return "\n".join(report)
    
    def get_current_queue_items(self):
        """Get all items currently in queues with their entry times"""
        current_items = {}
        current_time = datetime.now().timestamp()
        
        for item_id, queue_data in self.queue_times.items():
            for queue_name, times in queue_data.items():
                entry_time, exit_time = times
                
                # If item is still in this queue (no exit time)
                if exit_time is None:
                    duration = current_time - entry_time
                    if queue_name not in current_items:
                        current_items[queue_name] = []
                    
                    current_items[queue_name].append({
                        'item_id': item_id,
                        'time_in_queue': self._format_duration(duration),
                        'entry_time': datetime.fromtimestamp(entry_time).strftime('%Y-%m-%d %H:%M:%S')
                    })
        
        return current_items

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
        
    def add_item(self, item):
        """Add an item to the queue"""
        raise NotImplementedError("Each queue must implement add_item method")
        
    def remove_item(self, item):
        """Remove an item from the queue"""
        raise NotImplementedError("Each queue must implement remove_item method")
        
    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID (optimized)"""
        # Default implementation, queues should override this with more efficient implementations
        return any(i['id'] == item_id for i in self.get_contents())
        
    def _record_item_entered(self, queue_manager, item):
        """Record that an item entered this queue"""
        if hasattr(queue_manager, 'queue_timer') and item and 'id' in item:
            queue_name = self.__class__.__name__.replace('Queue', '')
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            queue_manager.queue_timer.item_entered_queue(item_id, queue_name, item_identifier)
            
    def _record_item_exited(self, queue_manager, item):
        """Record that an item exited this queue"""
        if hasattr(queue_manager, 'queue_timer') and item and 'id' in item:
            queue_name = self.__class__.__name__.replace('Queue', '')
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            queue_manager.queue_timer.item_exited_queue(item_id, queue_name, item_identifier)

class QueueManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(QueueManager, cls).__new__(cls)
            cls._instance.initialize()
        return cls._instance

    def initialize(self):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.queues = {
            "Wanted": WantedQueue(),
            "Scraping": ScrapingQueue(),
            "Adding": AddingQueue(),
            "Checking": CheckingQueue(),
            "Sleeping": SleepingQueue(),
            "Unreleased": UnreleasedQueue(),
            "Blacklisted": BlacklistedQueue(),
            "Pending Uncached": PendingUncachedQueue(),
            "Upgrading": UpgradingQueue()
        }
        self.paused = False
        
        # Initialize the queue timer
        self.queue_timer = QueueTimer()
        
        # Get the item tracker logger instance
        self.item_tracker = logging.getLogger('item_tracker')
        
        self.upgrade_process_locks = set()
        self._initialized = True

    def reinitialize(self):
        """Force reinitialization of all queues to pick up new settings"""
        self._initialized = False
        self.initialize()
        
    def reinitialize_queue(self, queue_name):
        """Reinitialize a specific queue to pick up new settings"""
        if queue_name not in self.queues:
            logging.error(f"Cannot reinitialize unknown queue: {queue_name}")
            return False
            
        queue_class = type(self.queues[queue_name])
        self.queues[queue_name] = queue_class()
        logging.info(f"Reinitialized {queue_name} queue")
        return True

    def update_all_queues(self):
        """
        Update all queues efficiently, logging only significant changes
        """
        for queue_name, queue in self.queues.items():
            before_count = len(queue.get_contents())
            queue.update()
            after_count = len(queue.get_contents())
            
            # Only log if there was an actual change in the queue size
            if before_count != after_count:
                logging.debug(f"Queue {queue_name} updated: {before_count} -> {after_count} items")

    def get_queue_contents(self):
        contents = OrderedDict()
        for state, queue in self.queues.items():
            contents[state] = queue.get_contents()
        return contents

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item.get('title', 'Unknown')}_{item.get('imdb_id', 'Unknown')}_{item.get('version', 'Unknown')}"
        elif item['type'] == 'episode':
            season = f"S{item.get('season_number', 0):02d}" if item.get('season_number') is not None else "S00"
            episode = f"E{item.get('episode_number', 0):02d}" if item.get('episode_number') is not None else "E00"
            return f"episode_{item.get('title', 'Unknown')}_{item.get('imdb_id', 'Unknown')}_{season}{episode}_{item.get('version', 'Unknown')}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def get_item_queue(self, item: Dict[str, Any]) -> str:
        """Find which queue contains the specified item"""
        item_id = item['id']
        
        # First check if we can find by ID (faster)
        for queue_name, queue in self.queues.items():
            # We assume each queue implements a fast way to check for item existence
            if queue.contains_item_id(item_id):
                return queue_name
            
        # Fallback to traditional search if queue doesn't implement contains_item_id
        for queue_name, queue in self.queues.items():
            if any(i['id'] == item_id for i in queue.get_contents()):
                return queue_name
                
        return None  # or raise an exception if the item should always be in a queue
        
    def _process_queue_safely(self, queue_name, with_result=False):
        """
        Process a queue safely with pause checking and error handling
        
        Args:
            queue_name: Name of the queue to process
            with_result: Whether the process method returns a result to be passed back
            
        Returns:
            The result of processing if with_result=True, otherwise None
        """
        if self.paused:
            logging.debug(f"Skipping {queue_name} queue processing: Queue is paused")
            return False if with_result else None
            
        try:
            if with_result:
                return self.queues[queue_name].process(self)
            else:
                self.queues[queue_name].process(self)
                return None
        except Exception as e:
            logging.error(f"Error processing {queue_name} queue: {str(e)}", exc_info=True)
            return False if with_result else None
    
    def process_checking(self, program_runner):
        """Process the Checking queue, requires ProgramRunner instance."""
        if self.paused:
            logging.debug("Skipping Checking queue processing: Queue is paused")
            return

        try:
            # Call the CheckingQueue process method directly, passing QueueManager (self) and ProgramRunner
            self.queues["Checking"].process(self, program_runner)
            
            # Clean up checking times after successful processing
            if not self.paused:
                self.queues["Checking"].clean_up_checking_times()

        except Exception as e:
            # Added exc_info=True for detailed traceback
            logging.error(f"Error processing Checking queue: {str(e)}", exc_info=True)

    def process_wanted(self):
        self._process_queue_safely("Wanted")

    def process_scraping(self):
        # Skip processing if paused
        if self.paused:
            logging.debug("Skipping Scraping queue processing: Queue is paused")
            return False

        # Update the queue contents before processing
        self.queues["Scraping"].update()

        # Now process items if any exist
        queue_contents = self.queues["Scraping"].get_contents()

        if queue_contents:
            # Process the queue safely (catches exceptions, including RateLimitError)
            result = self._process_queue_safely("Scraping", with_result=True)
            logging.debug(f"Scraping queue process result: {result}")
            return result # Return True if items were processed, False otherwise

        # Return False if queue was empty after update
        return False

    def process_adding(self):
        self._process_queue_safely("Adding")

    def process_unreleased(self):
        self._process_queue_safely("Unreleased")

    def process_sleeping(self):
        self._process_queue_safely("Sleeping")

    def process_blacklisted(self):
        self._process_queue_safely("Blacklisted")

    def process_pending_uncached(self):
        self._process_queue_safely("Pending Uncached")

    def process_upgrading(self):
        result = self._process_queue_safely("Upgrading")
        if not self.paused:
            self.queues["Upgrading"].clean_up_upgrade_times()

    def blacklist_item(self, item: Dict[str, Any], from_queue: str):
        self.queues["Blacklisted"].blacklist_item(item, self)
        self.queues[from_queue].remove_item(item)

    def blacklist_old_season_items(self, item: Dict[str, Any], from_queue: str):
        self.queues["Blacklisted"].blacklist_old_season_items(item, self)
        self.queues[from_queue].remove_item(item)

    def move_to_wanted(self, item: Dict[str, Any], from_queue: str, new_version: str = None):
        item_identifier = self.generate_identifier(item)
        target_version_str = f" (Version: {new_version})" if new_version else ""
        logging.debug(f"Moving item {item_identifier}{target_version_str} to Wanted queue from {from_queue}")
        
        # If moving from Sleeping, preserve wake count (though usually reset when entering Wanted)
        from database import get_wake_count
        wake_count = get_wake_count(item['id'])
        logging.debug(f"Wake count before moving to Wanted: {wake_count}")
        
        updated_item = self._move_item_to_queue(item, from_queue, "Wanted", "Wanted", new_version=new_version, filled_by_title=None, filled_by_magnet=None)
        
        if updated_item:
            # No additional processing needed for Wanted queue itself
            pass

    def move_to_upgrading(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Upgrading: {item_identifier}")
        self._move_item_to_queue(item, from_queue, "Upgrading", "Upgrading")

    def move_to_scraping(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Scraping: {item_identifier}")
        self._move_item_to_queue(item, from_queue, "Scraping", "Scraping")

    def move_to_adding(self, item: Dict[str, Any], from_queue: str, filled_by_title: str, scrape_results: List[Dict]):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Adding: {item_identifier}")
        
        updated_item = self._move_item_to_queue(
            item, 
            from_queue if from_queue != "Wanted" else None,  # Don't remove from Wanted queue
            "Adding", 
            "Adding", 
            filled_by_title=filled_by_title, 
            scrape_results=scrape_results
        )

    def move_to_checking(self, item: Dict[str, Any], from_queue: str, title: str, link: str, filled_by_file: str, torrent_id: str = None):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item to Checking: {item_identifier}")
        
        from utilities.settings import get_setting

        '''
        # Check if Plex library checks are disabled
        if get_setting('Plex', 'disable_plex_library_checks') and not get_setting('Plex', 'mounted_file_location'):
            logging.info(f"Plex library checks disabled and no file location set. Moving {item_identifier} directly to Collected")
            # Update item state to Collected
            update_media_item_state(item['id'], 'Collected', filled_by_title=title, filled_by_magnet=link, filled_by_file=filled_by_file, filled_by_torrent_id=torrent_id)
            update_media_item(item['id'], collected_at= datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            
            try:
                from notifications import send_notifications
                from routes.settings_routes import get_enabled_notifications_for_category
                from extensions import app

                with app.app_context():
                    response = get_enabled_notifications_for_category('collected')
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
                                'new_state': 'Collected'
                            }
                            send_notifications([notification_data], enabled_notifications, notification_category='collected')
            except Exception as e:
                logging.error(f"Failed to send collected notification: {str(e)}")
            
            # Remove from source queue
            if from_queue in ["Adding", "Wanted"]:
                self.queues[from_queue].remove_item(item)
            logging.info(f"Moved item {item_identifier} to Collected state")
            return
        '''
        
        updated_item = self._move_item_to_queue(
            item,
            from_queue if from_queue in ["Adding", "Wanted"] else None,
            "Checking",
            "Checking",
            filled_by_title=title,
            filled_by_magnet=link,
            filled_by_file=filled_by_file,
            filled_by_torrent_id=torrent_id
        )
        
        # Copy downloading flag from original item
        if updated_item and 'downloading' in item:
            updated_item['downloading'] = item['downloading']
            self.queues["Checking"].add_item(updated_item)  # Re-add with updated flag

    def move_to_sleeping(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item {item_identifier} to Sleeping queue")
        
        from database import get_wake_count
        wake_count = get_wake_count(item['id'])
        logging.debug(f"Wake count before moving to Sleeping: {wake_count}")

        updated_item = self._move_item_to_queue(item, from_queue, "Sleeping", "Sleeping")
        
        if updated_item:
            updated_item['wake_count'] = wake_count
            self.queues["Sleeping"].add_item(updated_item)  # Re-add with wake count

    def move_to_unreleased(self, item: Dict[str, Any], from_queue: str):
        self._move_item_to_queue(item, from_queue, "Unreleased", "Unreleased")

    def move_to_blacklisted(self, item: Dict[str, Any], from_queue: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Initiating move for item {item_identifier} from {from_queue} to Blacklisted state via blacklist_item method")
        
        # Log initiation of move
        self.item_tracker.info({
            'event': 'MOVE_INITIATED',
            'item_id': item.get('id'),
            'item_identifier': item_identifier,
            'from_queue': from_queue,
            'to_queue': 'Blacklisted'
        })
        
        # Record exit from source queue *before* calling blacklist_item
        time_in_from_queue_seconds = None
        if from_queue and from_queue in self.queues and item and 'id' in item:
            self.queue_timer.item_exited_queue(item['id'], from_queue, item_identifier)
            # Calculate time spent in the from_queue
            item_timing = self.queue_timer.get_item_timing(item['id'])
            if from_queue in item_timing:
                entry_time, exit_time = item_timing[from_queue]
                if entry_time and exit_time:
                    time_in_from_queue_seconds = exit_time - entry_time

        # Call the specific blacklisting method which contains the fallback logic
        # blacklist_item now handles DB update, adding to its list, and recording its own entry time.
        blacklisted_queue = self.queues["Blacklisted"]
        blacklisted_queue.blacklist_item(item, self) # Pass self (QueueManager)

        # Now, remove the item from the source queue's in-memory list AFTER blacklist_item has run
        # This prevents issues if fallback logic moves it back to Wanted before this step
        item_id = item.get('id')
        item_removed_from_source = False
        if from_queue and from_queue in self.queues and item_id:
             # Check if the item still exists in the source queue *by ID*
             # This handles the case where fallback might have already moved/removed it.
             source_queue_items = self.queues[from_queue].get_contents()
             item_in_source = next((i for i in source_queue_items if i['id'] == item_id), None)
             
             if item_in_source:
                 try:
                     self.queues[from_queue].remove_item(item_in_source) # Pass the actual item object found
                     item_removed_from_source = True
                     logging.debug(f"Removed item {item_identifier} from source queue {from_queue} after processing blacklist/fallback.")
                 except Exception as e:
                     logging.error(f"Error removing item {item_identifier} from source queue {from_queue}: {e}")
             else:
                 logging.debug(f"Item {item_identifier} was not found in source queue {from_queue} for removal (likely handled by fallback).")

        # Log completion of the move operation initiated here
        log_data = {
            'event': 'MOVE_COMPLETED', # Or 'FALLBACK_APPLIED' if we could detect it? blacklist_item doesn't return status.
            'item_id': item.get('id'), 
            'item_identifier': item_identifier,
            'from_queue': from_queue,
            'final_state': 'Blacklisted or Wanted (via Fallback)', # Reflect uncertainty
            'removed_from_source_queue': item_removed_from_source
        }
        if time_in_from_queue_seconds is not None:
            log_data['time_in_from_queue_seconds'] = round(time_in_from_queue_seconds, 2)
        self.item_tracker.info(log_data)

        logging.debug(f"Finished processing move for item {item_identifier} (to Blacklisted or Fallback)")

    def move_to_pending_uncached(self, item: Dict[str, Any], from_queue: str, title: str, link: str, scrape_results: List[Dict]):
        self._move_item_to_queue(
            item, 
            from_queue, 
            "Pending Uncached", 
            "Pending Uncached", 
            filled_by_title=title, 
            filled_by_magnet=link, 
            scrape_results=scrape_results
        )

    def get_scraping_items(self) -> List[Dict]:
        """Get all items currently in the Scraping state"""
        return self.queues["Scraping"].get_contents()
        
    def get_wake_count(self, item_id):
        from database import get_wake_count
        return get_wake_count(item_id)

    def pause_queue(self, reason=None):
        if not self.paused:
            self.paused = True
            pause_message = "Queue processing paused"
            if reason:
                pause_message += f": {reason}"
            logging.info(pause_message)
            send_queue_pause_notification(pause_message)
        else:
            logging.warning("Queue is already paused")

    def resume_queue(self):
        if self.paused:
            self.paused = False
            logging.info("Queue processing resumed")
            send_queue_resume_notification("Queue processing resumed")
        else:
            logging.warning("Queue is not paused")

    def is_paused(self):
        return self.paused

    def are_main_queues_empty(self):
        """Check if the critical processing queues (Scraping, Adding, Checking) are empty."""
        critical_queues = ['Scraping', 'Adding', 'Checking']
        
        for queue_name in critical_queues:
            if queue_name in self.queues:
                # Use the length of the internal list directly if available and efficient
                # Assuming each queue object has an efficient way to check emptiness
                # For example, checking the length of an internal list `self.queues[queue_name].items`
                # If not, fall back to get_contents(), but prefer direct length check.
                # Placeholder: Let's assume a simple length check on a hypothetical 'items' attribute
                # Replace with the actual way queues store items if different.
                try:
                    # Attempt to check the length of the internal list directly
                    # This is a guess based on common queue implementations. Adjust if needed.
                    if hasattr(self.queues[queue_name], 'items') and len(self.queues[queue_name].items) > 0:
                        return False # Found items in a critical queue
                    # Fallback if 'items' attribute doesn't exist or another method is used
                    elif len(self.queues[queue_name].get_contents()) > 0:
                         return False # Found items in a critical queue
                except AttributeError:
                    # Fallback if direct length check fails
                    if len(self.queues[queue_name].get_contents()) > 0:
                        return False # Found items in a critical queue
            else:
                 logging.warning(f"Critical queue '{queue_name}' not found in QueueManager. Assuming empty.")

        # If we checked all critical queues and found no items
        return True

    def _move_item_to_queue(self, item, from_queue, to_queue_name, new_state, new_version=None, **additional_params):
        """
        Common method to handle moving items between queues.
        
        Args:
            item: The item to move
            from_queue: The source queue name
            to_queue_name: The target queue name 
            new_state: The new state for the item
            new_version: [Optional] The new version for the item
            additional_params: Additional parameters to pass to update_media_item_state
        
        Returns:
            The updated item if successful, None otherwise
        """
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item {item_identifier} to {to_queue_name}")
        
        # Log initiation of move
        self.item_tracker.info({
            'event': 'MOVE_INITIATED',
            'item_id': item.get('id'),
            'item_identifier': item_identifier,
            'from_queue': from_queue,
            'to_queue': to_queue_name
        })
        
        # Record exit from source queue if applicable
        time_in_from_queue_seconds = None
        if from_queue and from_queue in self.queues and item and 'id' in item:
            self.queue_timer.item_exited_queue(item['id'], from_queue, item_identifier)
            # Try to get timing immediately after exit
            item_timing = self.queue_timer.get_item_timing(item['id'])
            if from_queue in item_timing:
                entry_time, exit_time = item_timing[from_queue]
                if entry_time and exit_time:
                    time_in_from_queue_seconds = exit_time - entry_time
        
        # If a new version is provided, add it to the params for the DB update
        if new_version:
            additional_params['version'] = new_version
            
        # Update the item's state (and potentially version) in the database - returns updated dict
        updated_item = update_media_item_state(item['id'], new_state, **additional_params)
        
        if updated_item:
            # 'updated_item' is now the dictionary returned by the update function
            
            # Add the item to the target queue
            if to_queue_name in self.queues:
                # Record entry into target queue 
                self.queue_timer.item_entered_queue(updated_item['id'], to_queue_name, item_identifier)
                self.queues[to_queue_name].add_item(updated_item)
            
            # Remove from source queue if needed
            if from_queue and from_queue in self.queues:
                self.queues[from_queue].remove_item(item)
                
            logging.debug(f"Successfully moved item {item_identifier} to {to_queue_name}")
            
            # Log completion of move
            log_data = {
                'event': 'MOVE_COMPLETED',
                'item_id': updated_item.get('id'),
                'item_identifier': item_identifier,
                'from_queue': from_queue,
                'to_queue': to_queue_name
            }
            if time_in_from_queue_seconds is not None:
                log_data['time_in_from_queue_seconds'] = round(time_in_from_queue_seconds, 2)
            self.item_tracker.info(log_data)
                
            return updated_item
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
            # Log failed move attempt
            self.item_tracker.error({
                'event': 'MOVE_FAILED',
                'item_id': item.get('id'),
                'item_identifier': item_identifier,
                'from_queue': from_queue,
                'to_queue': to_queue_name,
                'reason': 'Failed to retrieve updated item from database'
            })
            return None

    def move_to_collected(self, item: Dict[str, Any], from_queue: str, skip_notification: bool = False):
        """Move an item to the Collected state after symlink is created."""
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Moving item {item_identifier} to Collected state")
        
        # Log initiation of move to Collected
        self.item_tracker.info({
            'event': 'MOVE_INITIATED',
            'item_id': item.get('id'),
            'item_identifier': item_identifier,
            'from_queue': from_queue,
            'to_state': 'Collected'
        })
        
        # Record exit from source queue
        time_in_from_queue_seconds = None
        if from_queue in self.queues and item and 'id' in item:
            self.queue_timer.item_exited_queue(item['id'], from_queue, item_identifier)
            # Calculate time spent in the from_queue
            item_timing = self.queue_timer.get_item_timing(item['id'])
            if from_queue in item_timing:
                entry_time, exit_time = item_timing[from_queue]
                if entry_time and exit_time:
                    time_in_from_queue_seconds = exit_time - entry_time
        
        from datetime import datetime
        collected_at = datetime.now()
        
        # Update the item state in the database - no need to add to a queue since Collected is a state, not a queue
        update_media_item_state(item['id'], 'Collected', collected_at=collected_at)
        
        # Get the updated item
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            # Remove from the source queue
            if from_queue in self.queues:
                self.queues[from_queue].remove_item(item)
            logging.debug(f"Successfully moved item {item_identifier} to Collected state")
            
            # Log completion of move to Collected
            log_data = {
                'event': 'MOVE_COMPLETED',
                'item_id': updated_item.get('id'),
                'item_identifier': item_identifier,
                'from_queue': from_queue,
                'to_state': 'Collected'
            }
            if time_in_from_queue_seconds is not None:
                log_data['time_in_from_queue_seconds'] = round(time_in_from_queue_seconds, 2)
            self.item_tracker.info(log_data)
            
            # Add to collected notifications if not skipped
            if not skip_notification:
                updated_item_dict = dict(updated_item)
                updated_item_dict['is_upgrade'] = False  # Not an upgrade since it's a new collection
                updated_item_dict['original_collected_at'] = collected_at
                add_to_collected_notifications(updated_item_dict)
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
            # Log failed move attempt
            self.item_tracker.error({
                'event': 'MOVE_FAILED',
                'item_id': item.get('id'),
                'item_identifier': item_identifier,
                'from_queue': from_queue,
                'to_state': 'Collected',
                'reason': 'Failed to retrieve updated item from database after state update'
            })
            
    def generate_queue_timing_report(self):
        """Generate a report of queue timing statistics"""
        if hasattr(self, 'queue_timer'):
            return self.queue_timer.generate_timing_report()
        return "Queue timing not available"
        
    def get_current_queue_timing(self):
        """Get timing information for all items currently in queues"""
        if hasattr(self, 'queue_timer'):
            return self.queue_timer.get_current_queue_items()
        return {}