import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta, date

from utilities.settings import get_setting
from queues.config_manager import get_version_settings, load_config
from database.database_reading import check_existing_media_item
from database.database_writing import update_release_date_and_state

class BlacklistedQueue:
    def __init__(self):
        self.items = []
        self.blacklist_times = {}

    def update(self):
        from database import get_all_media_items
        self.items = [dict(row) for row in get_all_media_items(state="Blacklisted")]
        # Initialize blacklist times for new items
        for item in self.items:
            if item['id'] not in self.blacklist_times:
                self.blacklist_times[item['id']] = datetime.now()

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.blacklist_times[item['id']] = datetime.now()

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.blacklist_times:
            del self.blacklist_times[item['id']]

    def process(self, queue_manager):
        logging.debug(f"Processing blacklisted queue. Items: {len(self.items)}")
        
        # Check if unblacklisting is disabled
        if get_setting("Debug", "disable_unblacklisting", True):
            logging.info("Automatic unblacklisting is disabled. Skipping unblacklist checks.")
            return
            
        blacklist_duration = timedelta(days=int(get_setting("Queue", "blacklist_duration", default=30)))
        current_time = datetime.now()

        items_to_unblacklist = []

        for item in self.items:
            if 'blacklisted_date' not in item or item['blacklisted_date'] is None:
                item['blacklisted_date'] = current_time
                from database import update_blacklisted_date
                update_blacklisted_date(item['id'], item['blacklisted_date'])
                logging.warning(f"Item {queue_manager.generate_identifier(item)} had no blacklisted_date. Setting it to current time.")
            
            item_identifier = queue_manager.generate_identifier(item)
            try:
                # Convert blacklisted_date to datetime if it's a string
                if isinstance(item['blacklisted_date'], str):
                    item['blacklisted_date'] = datetime.fromisoformat(item['blacklisted_date'])
                
                # Ensure both times are naive (no timezone) for comparison
                if hasattr(item['blacklisted_date'], 'tzinfo') and item['blacklisted_date'].tzinfo is not None:
                    item['blacklisted_date'] = item['blacklisted_date'].replace(tzinfo=None)
                
                time_blacklisted = current_time - item['blacklisted_date']
                days_until_unblacklist = blacklist_duration.days - time_blacklisted.days
                
                logging.info(f"Item {item_identifier} has been blacklisted for {time_blacklisted.days} days. Will be unblacklisted in {days_until_unblacklist} days.")
                if time_blacklisted >= blacklist_duration:
                    items_to_unblacklist.append(item)
                    logging.info(f"Item {item_identifier} has been blacklisted for {time_blacklisted.days} days. Unblacklisting.")
            except (TypeError, ValueError) as e:
                logging.error(f"Error processing blacklisted item {item_identifier}: {str(e)}")
                logging.error(f"Item details: {item}")
                continue

        for item in items_to_unblacklist:
            self.unblacklist_item(queue_manager, item)

        logging.debug(f"Blacklisted queue processing complete. Items unblacklisted: {len(items_to_unblacklist)}")
        logging.debug(f"Remaining items in Blacklisted queue: {len(self.items)}")

    def unblacklist_item(self, queue_manager, item: Dict[str, Any]):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Unblacklisting item: {item_identifier}")
        
        # Reset the blacklisted_date to None when unblacklisting
        from database import update_blacklisted_date
        update_blacklisted_date(item['id'], None)
        
        # Move the item back to the Wanted queue
        queue_manager.move_to_wanted(item, "Blacklisted")
        self.remove_item(item)

    def blacklist_item(self, item: Dict[str, Any], queue_manager):
        item_id = item['id']
        item_identifier = queue_manager.generate_identifier(item)
        current_version = item.get('version')

        # --- Handle failed early releases for unreleased items --- START ---
        if item.get('early_release'):
            release_date_str = item.get('release_date')
            should_reset = False
            
            if release_date_str == 'Unknown':
                should_reset = True
                logging.info(f"Item {item_identifier} is early_release with Unknown release date.")
            else:
                try:
                    release_date_obj = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                    if release_date_obj > date.today():
                        should_reset = True
                        logging.info(f"Item {item_identifier} is early_release with future release date {release_date_str}.")
                except (ValueError, TypeError) as e:
                    # If release date is invalid format, treat as Unknown for this purpose
                    logging.warning(f"Item {item_identifier} has invalid release date format '{release_date_str}'. Treating as Unknown for early release reset check. Error: {e}")
                    should_reset = True 

            if should_reset:
                logging.info(f"Intercepting blacklist for early_release item {item_identifier}. Setting state to Unreleased and flagging no_early_release.")
                try:
                    update_release_date_and_state(
                        item_id,
                        release_date=release_date_str, # Keep original release date
                        state='Unreleased',          # Set state back to Unreleased
                        airtime=item.get('airtime'),
                        early_release=False,         # Reset the early_release flag (optional but good practice)
                        physical_release_date=item.get('physical_release_date'),
                        no_early_release=True        # Set the new flag
                    )
                    logging.info(f"Set state=Unreleased and no_early_release=True for {item_identifier}. It will be re-evaluated later without Trakt early check.")
                except Exception as e:
                    logging.error(f"Failed to update state/no_early_release flag for {item_identifier}: {e}")
                    # If DB update fails, maybe proceed to blacklist? For now, return to avoid potential loop.
                    return

                # Remove the item from its current queue (e.g., Checking, Wanted)
                current_queue_name = queue_manager.get_item_queue(item)
                if current_queue_name and current_queue_name != 'Blacklisted':
                    try:
                        if hasattr(queue_manager, 'remove_item_from_specific_queue'):
                             queue_manager.remove_item_from_specific_queue(item, current_queue_name)
                        elif hasattr(queue_manager, 'remove_item'):
                             queue_manager.remove_item(item)
                        else:
                             logging.warning(f"Could not find a suitable method on queue_manager to remove {item_identifier} from {current_queue_name}.")
                        logging.info(f"Removed {item_identifier} from {current_queue_name} queue after failed early release handling.")
                    except Exception as e:
                        logging.error(f"Error removing {item_identifier} from {current_queue_name} queue after failed early release handling: {e}")
                
                return # Prevent standard blacklisting/fallback
        # --- Handle failed early releases --- END ---

        # --- Fallback Logic Start ---
        if current_version:
            version_settings = get_version_settings(current_version)
            fallback_version = version_settings.get('fallback_version', 'None')
            config = load_config()
            all_versions = list(config.get('Scraping', {}).get('versions', {}).keys())

            if fallback_version and fallback_version != 'None' and fallback_version in all_versions:
                # --- Check for existing item with fallback version --- START
                target_states_to_check = ['Collected', 'Upgrading']
                if check_existing_media_item(item, fallback_version, target_states_to_check):
                    logging.info(f"Item {item_identifier} failed for version '{current_version}'. Fallback version '{fallback_version}' already exists in state {target_states_to_check}. Proceeding with blacklisting original.")
                    # Do not apply fallback, proceed to original blacklisting logic below
                    pass # Explicitly doing nothing here, will fall through to blacklist
                else:
                    # --- Original Fallback Logic --- START
                    logging.info(f"Item {item_identifier} failed for version '{current_version}'. Attempting fallback to version '{fallback_version}'.")
                    
                    # Remove from current queue before modifying item state/version
                    current_queue_name = queue_manager.get_item_queue(item)
                    if current_queue_name and current_queue_name != 'Blacklisted': # Avoid double removal if somehow already blacklisted
                        try:
                            queue_manager.queues[current_queue_name].remove_item(item)
                            logging.debug(f"Removed {item_identifier} from {current_queue_name} queue for fallback.")
                        except KeyError:
                             logging.warning(f"Could not find queue '{current_queue_name}' to remove item {item_identifier} during fallback.")
                        except Exception as e:
                             logging.error(f"Error removing item {item_identifier} from {current_queue_name} queue during fallback: {e}")
                    
                    # Update item's version in the local dictionary
                    item['version'] = fallback_version
                    item_identifier_new_version = queue_manager.generate_identifier(item) # Generate identifier with new version
                    
                    # Move to Wanted queue with the new version, passing it explicitly
                    # We pass the original version name as the 'source' for context
                    logging.debug(f"Calling move_to_wanted for {item_identifier_new_version} with new_version={fallback_version}")
                    queue_manager.move_to_wanted(item, f"Fallback from {current_version}", new_version=fallback_version)
                    logging.info(f"Moved item {item_identifier_new_version} back to Wanted queue with fallback version '{fallback_version}'.")
                    return # Skip the rest of the blacklisting process
                    # --- Original Fallback Logic --- END
                # --- Check for existing item with fallback version --- END
            else:
                if fallback_version and fallback_version != 'None' and fallback_version not in all_versions:
                     logging.warning(f"Configured fallback version '{fallback_version}' for version '{current_version}' does not exist. Proceeding with blacklisting {item_identifier}.")
                # Else (no fallback configured or fallback is 'None'), just continue to blacklist
        else:
            logging.warning(f"Item {item_identifier} does not have a version associated. Cannot perform fallback check. Proceeding with blacklisting.")
        # --- Fallback Logic End ---

        # Original blacklisting logic starts here
        logging.info(f"Blacklisting item {item_identifier} (version: {current_version or 'N/A'}). No fallback applied.")
        from database import update_media_item_state, update_blacklisted_date
        
        # Update database state first
        update_media_item_state(item_id, 'Blacklisted') 
        
        # Update the blacklisted_date in the database
        blacklisted_date = datetime.now()
        update_blacklisted_date(item_id, blacklisted_date)
        
        # Get potentially updated item details (though state/date are main changes)
        # Re-fetch might be safer if update_media_item_state doesn't return updated item
        # For now, assume 'item' dict is sufficiently up-to-date for adding to list
        updated_item = item # Use existing item dict for now
        updated_item['state'] = 'Blacklisted' # Ensure state is correct in the dict
        updated_item['blacklisted_date'] = blacklisted_date # Store as datetime object
        
        # Record entry into this queue
        # Note: Need to import BaseQueue or pass queue_manager correctly if needed
        # Assuming self._record_item_entered exists via inheritance or mixin
        if hasattr(self, '_record_item_entered'):
             self._record_item_entered(queue_manager, updated_item)
        else:
             # Fallback to direct timer call if method not inherited
             if hasattr(queue_manager, 'queue_timer'):
                 queue_manager.queue_timer.item_entered_queue(item_id, 'Blacklisted', item_identifier)

        # Add to blacklisted queue in memory
        self.add_item(updated_item) # Use the updated_item dict
        
        logging.info(f"Moved item {item_identifier} to Blacklisted state and updated blacklisted_date")

    def blacklist_old_season_items(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Blacklisting item {item_identifier} and related old season items with the same version")

        # Check if the item is in the Checking queue before blacklisting
        if queue_manager.get_item_queue(item) != 'Checking':
            self.blacklist_item(item, queue_manager)
        else:
            logging.info(f"Skipping blacklisting of {item_identifier} as it's already in Checking queue")

        # Find and blacklist related items in the same season with the same version that are also old
        related_items = self.find_related_season_items(item, queue_manager)
        for related_item in related_items:
            related_identifier = queue_manager.generate_identifier(related_item)
            if self.is_item_old(related_item) and related_item['version'] == item['version']:
                # Check if the related item is in the Checking queue before blacklisting
                if queue_manager.get_item_queue(related_item) != 'Checking':
                    self.blacklist_item(related_item, queue_manager)
                else:
                    logging.info(f"Skipping blacklisting of {related_identifier} as it's already in Checking queue")
            else:
                logging.debug(f"Not blacklisting {related_identifier} as it's either not old enough or has a different version")

    def find_related_season_items(self, item: Dict[str, Any], queue_manager) -> List[Dict[str, Any]]:
        related_items = []
        if item['type'] == 'episode':
            for queue in queue_manager.queues.values():
                for queue_item in queue.get_contents():
                    if (queue_item['type'] == 'episode' and
                        queue_item['imdb_id'] == item['imdb_id'] and
                        queue_item['season_number'] == item['season_number'] and
                        queue_item['id'] != item['id'] and
                        queue_item['version'] == item['version']):
                        related_items.append(queue_item)
        return related_items

    def is_item_old(self, item: Dict[str, Any]) -> bool:
        # If early release flag is set, it's never considered old for the purpose of immediate blacklisting
        if item.get('early_release', False):
            return False
            
        if 'release_date' not in item or item['release_date'] is None or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date, None, or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            days_since_release = (datetime.now().date() - release_date).days
            
            # Define thresholds for considering items as old
            movie_threshold = 7  # Consider movies old after 30 days
            episode_threshold = 7  # Consider episodes old after 7 days
            
            if item['type'] == 'movie':
                return days_since_release > movie_threshold
            elif item['type'] == 'episode':
                return days_since_release > episode_threshold
            else:
                logging.warning(f"Unknown item type: {item['type']}. Considering it as old.")
                return True
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)