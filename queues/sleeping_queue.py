import logging
from typing import Dict, Any
from datetime import datetime, timedelta

from utilities.settings import get_setting
from queues.config_manager import load_config

def _get_int_setting(section: str, key: str, default: int) -> int:
    """Helper function to safely get an integer setting."""
    value = get_setting(section, key, default=default)
    if isinstance(value, str) and not value.strip(): # Handle empty string
        logging.warning(
            f"Setting '{key}' in section '{section}' is empty. "
            f"Using default value: {default}."
        )
        return default
    try:
        return int(value)
    except (ValueError, TypeError): # Added TypeError for good measure
        logging.warning(
            f"Invalid value '{value}' for setting '{key}' in section '{section}'. "
            f"Expected an integer. Using default value: {default}."
        )
        return default

class SleepingQueue:
    def __init__(self):
        self.items = []
        self.sleeping_queue_times = {}

    def update(self):
        from database import get_all_media_items, get_media_item_by_id, get_wake_count, increment_wake_count
        self.items = [dict(row) for row in get_all_media_items(state="Sleeping")]
        # Initialize sleeping times for new items and fetch wake count from DB
        for item in self.items:
            if item['id'] not in self.sleeping_queue_times:
                self.sleeping_queue_times[item['id']] = datetime.now()
            # Get wake_count from the database, default to 0 if not present (should exist now)
            item['wake_count'] = get_wake_count(item['id']) 

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        # Fetch wake count from DB when adding
        from database import get_wake_count
        item['wake_count'] = get_wake_count(item['id'])
        self.items.append(item)
        self.sleeping_queue_times[item['id']] = datetime.now()
        logging.debug(f"Added item to Sleeping queue: {item['id']} (Wake count: {item['wake_count']})")
                
        from routes.notifications import send_notifications
        from routes.settings_routes import get_enabled_notifications, get_enabled_notifications_for_category
        from routes.extensions import app

        # Send notification for the state change
        try:
            with app.app_context():
                response = get_enabled_notifications_for_category('sleeping')
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
                            'new_state': 'Sleeping',
                            'is_upgrade': False,
                            'upgrading_from': None
                        }
                        send_notifications([notification_data], enabled_notifications, notification_category='state_change')
        except Exception as e:
            logging.error(f"Failed to send state change notification: {str(e)}")

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.sleeping_queue_times:
            del self.sleeping_queue_times[item['id']]
        logging.debug(f"Removed item from Sleeping queue: {item['id']}")

    def process(self, queue_manager):
        #logging.debug("Processing sleeping queue")
        current_time = datetime.now()
        default_wake_limit = _get_int_setting("Queue", "wake_limit", default=24)
        # Read sleep duration from settings, default to 30 minutes
        sleep_duration_minutes = _get_int_setting("Queue", "sleep_duration_minutes", default=30)
        # Ensure a minimum duration to prevent potential issues with very small values
        if sleep_duration_minutes < 10:
            sleep_duration_minutes = 10
            logging.warning("Sleep duration setting was less than 10 minutes, using 10 minutes instead.")
        sleep_duration = timedelta(minutes=sleep_duration_minutes) # Use the setting

        items_to_wake = []
        items_to_blacklist = []
        config = load_config()

        for item in self.items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            #logging.debug(f"Processing sleeping item: {item_identifier}")

            # Get version-specific wake limit if it exists
            version = item.get('version', 'Default')
            version_settings = config.get('Scraping', {}).get('versions', {}).get(version, {})
            version_wake_count = version_settings.get('wake_count')
            # Use version wake count if it's a positive number, -1 means never sleep
            if version_wake_count == -1:
                # Move directly to blacklist if wake_count is -1 (never sleep)
                items_to_blacklist.append(item)
                continue
            # Otherwise use version wake count if positive, or default
            wake_limit = version_wake_count if version_wake_count and version_wake_count > 0 else default_wake_limit

            time_asleep = current_time - self.sleeping_queue_times[item_id]
            # Fetch current wake count from DB
            from database import get_wake_count
            wake_count = get_wake_count(item_id) 
            logging.debug(f"Item {item_identifier} has been asleep for {time_asleep}. Current wake count: {wake_count}/{wake_limit}")

            if time_asleep >= sleep_duration:
                if wake_count < wake_limit:
                    items_to_wake.append(item)
                else:
                    items_to_blacklist.append(item)

        self.wake_items(queue_manager, items_to_wake)
        self.blacklist_items(queue_manager, items_to_blacklist)

    def wake_items(self, queue_manager, items):
        logging.debug(f"Attempting to wake {len(items)} items")
        for item in items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            # Get old count from DB for logging
            from database import get_wake_count, increment_wake_count
            old_wake_count = get_wake_count(item_id) 
            logging.debug(f"Waking item: {item_identifier} (Current wake count: {old_wake_count})")

            # Increment wake count in DB
            new_wake_count = increment_wake_count(item_id) 
            queue_manager.move_to_wanted(item, "Sleeping")
            # self.remove_item(item) # remove_item is called within move_to_wanted
            logging.info(f"Moved item {item_identifier} from Sleeping to Wanted queue (Wake count: {old_wake_count} -> {new_wake_count})")

        logging.debug(f"Woke up {len(items)} items")

    def blacklist_items(self, queue_manager, items):
        for item in items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)

            # Call the new method in queue_manager
            queue_manager.initiate_final_check_or_blacklist(item, "Sleeping")

            # REMOVE ITEM IS NO LONGER NEEDED HERE - initiate_final_check_or_blacklist handles removal via _move_item_to_queue or move_to_blacklisted

            logging.info(f"Initiated final check or blacklist process for item {item_identifier} from Sleeping queue")

        logging.debug(f"Finished processing final check/blacklist attempts for {len(items)} items from Sleeping queue")

    def clean_up_sleeping_data(self):
        # Remove sleeping times for items no longer in the queue
        for item_id in list(self.sleeping_queue_times.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.sleeping_queue_times[item_id]
        
        # No need to manage wake counts here anymore, it's in the DB
        # We can log the counts from the DB if needed for debugging
        # for item_id, wake_count in wake_count_manager.wake_counts.items():
        #     if item_id not in [item['id'] for item in self.items]:
        #         logging.debug(f"Preserving wake count for item ID: {item_id}. Current wake count: {wake_count}")

    def is_item_old(self, item):
        if 'release_date' not in item or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            return (datetime.now().date() - release_date).days > 7
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)