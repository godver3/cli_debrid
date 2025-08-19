import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta, date

from utilities.settings import get_setting
from queues.config_manager import get_version_settings, load_config
from database.database_reading import check_existing_media_item, get_media_item_by_id, get_media_item_presence
from database.database_writing import (
    update_release_date_and_state,
    update_media_item_state,
    update_blacklisted_date,
    add_to_collected_notifications
)
from database.core import get_db_connection

class BlacklistedQueue:
    def __init__(self):
        logging.info("BlacklistedQueue initialized (no longer stores items in memory).")

    def update(self):
        #logging.debug("BlacklistedQueue.update called - no longer loads items into memory.")
        pass

    def get_contents(self):
        return []

    def add_item(self, item: Dict[str, Any]):
        logging.debug(f"BlacklistedQueue.add_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def remove_item(self, item: Dict[str, Any]):
        logging.debug(f"BlacklistedQueue.remove_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def _parse_cutoff_date_setting(self, cutoff_date_str: str) -> date:
        """
        Parse the cutoff date setting which can be either:
        - A date in YYYY-MM-DD format
        - A number representing days ago (e.g., '30' for 30 days ago)
        
        Returns the parsed date or None if invalid/empty.
        """
        if not cutoff_date_str or not cutoff_date_str.strip():
            return None
            
        cutoff_date_str = cutoff_date_str.strip()
        
        try:
            # Try parsing as YYYY-MM-DD date format first
            if '-' in cutoff_date_str and len(cutoff_date_str) >= 8:
                return datetime.strptime(cutoff_date_str, '%Y-%m-%d').date()
            else:
                # Try parsing as number of days ago
                days_ago = int(cutoff_date_str)
                if days_ago < 0:
                    logging.warning(f"Negative days value '{cutoff_date_str}' for unblacklisting cutoff date. Using 0.")
                    days_ago = 0
                return date.today() - timedelta(days=days_ago)
        except (ValueError, TypeError) as e:
            logging.error(f"Invalid unblacklisting cutoff date format '{cutoff_date_str}': {e}. Expected YYYY-MM-DD or number of days.")
            return None

    def process(self, queue_manager):
        logging.debug("Processing blacklisted queue using direct DB query.")

        if get_setting("Debug", "disable_unblacklisting", True):
            logging.info("Automatic unblacklisting is disabled. Skipping unblacklist checks.")
            return

        try:
            blacklist_duration_days = int(get_setting("Queue", "blacklist_duration", default=30))
            if blacklist_duration_days <= 0:
                logging.info("Blacklist duration is zero or negative, skipping unblacklist checks.")
                return

            blacklist_duration = timedelta(days=blacklist_duration_days)
            current_time = datetime.now()
            cutoff_date = current_time - blacklist_duration

            # Parse the unblacklisting cutoff date setting
            cutoff_date_setting = get_setting("Debug", "unblacklisting_cutoff_date", "")
            release_date_cutoff = self._parse_cutoff_date_setting(cutoff_date_setting)

            conn = get_db_connection()
            cursor = conn.cursor()

            # Build the base query
            query = """
                SELECT id, title, type, imdb_id, tmdb_id, season_number, episode_number, version, blacklisted_date, release_date
                FROM media_items
                WHERE state = 'Blacklisted'
                  AND blacklisted_date IS NOT NULL
                  AND blacklisted_date <= ?
                  AND (ghostlisted = FALSE OR ghostlisted IS NULL)
            """
            params = [cutoff_date.isoformat()]

            # Add release date filter if cutoff date is specified
            if release_date_cutoff:
                query += " AND (release_date IS NULL OR release_date = 'Unknown' OR release_date >= ?)"
                params.append(release_date_cutoff.isoformat())
                logging.info(f"Applying release date cutoff filter: only unblacklisting items with release date >= {release_date_cutoff} or unknown release dates.")

            cursor.execute(query, params)
            items_to_unblacklist_rows = cursor.fetchall()
            conn.close()

            items_to_unblacklist = [dict(row) for row in items_to_unblacklist_rows]

            if not items_to_unblacklist:
                if release_date_cutoff:
                    logging.debug(f"No items found eligible for unblacklisting with release date cutoff {release_date_cutoff}.")
                else:
                    logging.debug("No items found eligible for unblacklisting.")
                return

            if release_date_cutoff:
                logging.info(f"Found {len(items_to_unblacklist)} items eligible for unblacklisting with release date cutoff {release_date_cutoff}.")
            else:
                logging.info(f"Found {len(items_to_unblacklist)} items eligible for unblacklisting.")

            unblacklisted_count = 0
            for item in items_to_unblacklist:
                item_identifier = queue_manager.generate_identifier(item)
                try:
                    blacklisted_date_str = item.get('blacklisted_date', 'N/A')
                    release_date_str = item.get('release_date', 'Unknown')
                    logging.info(f"Item {item_identifier} (release: {release_date_str}) blacklisted on {blacklisted_date_str} is eligible for unblacklisting (cutoff: {cutoff_date.isoformat()}).")

                    self.unblacklist_item(queue_manager, item)
                    unblacklisted_count += 1
                except Exception as e_unblacklist:
                    logging.error(f"Error during unblacklist_item call for {item_identifier}: {e_unblacklist}", exc_info=True)
                    continue

            logging.debug(f"Blacklisted queue processing complete. Items unblacklisted: {unblacklisted_count}/{len(items_to_unblacklist)}")

        except Exception as e:
            logging.error(f"Error during BlacklistedQueue processing: {e}", exc_info=True)

    def unblacklist_item(self, queue_manager, item: Dict[str, Any]):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Unblacklisting item: {item_identifier}")

        update_blacklisted_date(item['id'], None)

        queue_manager.move_to_wanted(item, "Blacklisted")

    def blacklist_item(self, item: Dict[str, Any], queue_manager):
        item_id = item['id']
        item_identifier = queue_manager.generate_identifier(item)
        current_version = item.get('version')
        current_state = item.get('state') # Get current state from the item dict if available

        # If state is not in the item dict, fetch it from DB
        if current_state is None:
            db_item_details = get_media_item_by_id(item_id)
            if db_item_details:
                current_state = db_item_details.get('state')
            else:
                logging.warning(f"Could not retrieve current state for item {item_identifier} (ID: {item_id}) from DB. Proceeding with blacklist logic.")

        if current_state == 'Blacklisted':
            logging.info(f"Item {item_identifier} (ID: {item_id}) is already in 'Blacklisted' state. Skipping blacklist.")
            return

        if current_state == 'Collected':
            logging.info(f"Item {item_identifier} (ID: {item_id}) is already in 'Collected' state. Skipping blacklist.")
            return

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
                    logging.warning(f"Item {item_identifier} has invalid release date format '{release_date_str}'. Treating as Unknown for early release reset check. Error: {e}")
                    should_reset = True 

            if should_reset:
                logging.info(f"Intercepting blacklist for early_release item {item_identifier}. Setting state to Unreleased and flagging no_early_release.")
                try:
                    update_release_date_and_state(
                        item_id,
                        release_date=release_date_str,
                        state='Unreleased',
                        airtime=item.get('airtime'),
                        early_release=False,
                        physical_release_date=item.get('physical_release_date'),
                        no_early_release=True
                    )
                    logging.info(f"Set state=Unreleased and no_early_release=True for {item_identifier}. It will be re-evaluated later without Trakt early check.")
                except Exception as e:
                    logging.error(f"Failed to update state/no_early_release flag for {item_identifier}: {e}")
                    return

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
                
                return

        if current_version:
            version_settings = get_version_settings(current_version)
            fallback_version = version_settings.get('fallback_version', 'None')
            config = load_config()
            all_versions = list(config.get('Scraping', {}).get('versions', {}).keys())

            if fallback_version and fallback_version != 'None' and fallback_version in all_versions:
                # Prepare item_details for the check
                item_details_for_check = {
                    'type': item.get('type'),
                    'imdb_id': item.get('imdb_id'),
                    'tmdb_id': item.get('tmdb_id'),
                    'season_number': item.get('season_number') if item.get('type') == 'episode' else None,
                    'episode_number': item.get('episode_number') if item.get('type') == 'episode' else None
                }
                
                # Define states that indicate the fallback version is already actively managed or collected
                target_states_for_fallback_check = [
                    'Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 
                    'Unreleased', 'Pending Uncached', 'Upgrading', 'Collected', 'Final_Check'
                ]

                logging.debug(
                    f"Checking for existing specific fallback item: "
                    f"Details='{item_details_for_check}', "
                    f"Target Version='{fallback_version}', "
                    f"Target States='{target_states_for_fallback_check}'"
                )
                
                # Call with correct arguments
                fallback_item_exists = check_existing_media_item(
                    item_details=item_details_for_check,
                    target_version=fallback_version,
                    target_states=target_states_for_fallback_check
                )
                
                if fallback_item_exists:
                    logging.info(f"Item {item_identifier} (version '{current_version}') failed. Specific fallback version '{fallback_version}' already exists in one of the states: {target_states_for_fallback_check}. Proceeding with blacklisting original item ({current_version}) with notification.")
                    # Let execution continue to the standard blacklisting section at the end of the function.
                else:
                    # Fallback version does not exist in the specified states, attempt to blacklist original silently and create new fallback item.
                    logging.info(f"Item {item_identifier} (version '{current_version}') failed. Specific fallback version '{fallback_version}' not found in states {target_states_for_fallback_check}. Blacklisting original item silently and attempting to create a new item with fallback version.")

                    # Step 1: Silently blacklist the original item
                    logging.info(f"Silently blacklisting original item {item_identifier} (ID: {item_id}, version: {current_version}).")
                    update_media_item_state(item_id, 'Blacklisted') # We get details back but won't use for notification here
                    update_blacklisted_date(item_id, datetime.now())
                    if hasattr(queue_manager, 'queue_timer'):
                        queue_manager.queue_timer.item_entered_queue(item_id, 'Blacklisted', item_identifier)
                    logging.info(f"Original item {item_identifier} (ID: {item_id}) state set to Blacklisted and blacklisted_date updated. Notification for this specific blacklisting is skipped due to fallback attempt.")

                    # Step 2: Prepare data for the new fallback item
                    new_item_data = {
                        'title': item.get('title'),
                        'type': item.get('type'),
                        'imdb_id': item.get('imdb_id'),
                        'tmdb_id': item.get('tmdb_id'),
                        'year': item.get('year'),
                        'version': fallback_version,
                        'state': 'Wanted',
                        'release_date': item.get('release_date'),
                        'airtime': item.get('airtime'),
                        'season_number': item.get('season_number') if item.get('type') == 'episode' else None,
                        'episode_number': item.get('episode_number') if item.get('type') == 'episode' else None,
                    }
                    new_item_identifier = queue_manager.generate_identifier(new_item_data)
                    logging.debug(f"Prepared new item data for fallback: {new_item_identifier}")

                    # Step 3: Attempt to create and queue the new fallback item via QueueManager
                    try:
                        # This assumes QueueManager has a method to handle DB creation and queuing.
                        # This method should internally call database_writing to create the item,
                        # then add it to the Wanted logic (e.g., WantedQueue), and handle its own notifications.
                        logging.info(f"Requesting QueueManager to create and queue new item: {new_item_identifier} from original {item_identifier}")
                        fallback_creation_successful = queue_manager.create_and_add_item_to_wanted_queue(
                            new_item_data, 
                            reason=f"Fallback from {current_version}"
                        )

                        if fallback_creation_successful:
                            logging.info(f"Successfully created and queued new fallback item {new_item_identifier}.")
                        else:
                            logging.error(f"QueueManager reported failure in creating/queuing fallback item {new_item_identifier}. Original item {item_identifier} remains blacklisted (silently).")
                    except AttributeError:
                        logging.error(f"QueueManager does not have 'create_and_add_item_to_wanted_queue' method. Fallback for {item_identifier} cannot be created as a new item. Original item remains blacklisted (silently). This requires a feature addition to QueueManager.")
                    except Exception as e_create_fallback:
                        logging.error(f"Error during QueueManager's creation/queuing of fallback item {new_item_identifier}: {e_create_fallback}", exc_info=True)
                    
                    return # This path is complete: original blacklisted (silently), new fallback item creation attempted.

            elif fallback_version and fallback_version != 'None' and fallback_version not in all_versions:
                 logging.warning(f"Configured fallback version '{fallback_version}' for version '{current_version}' does not exist or is invalid (not in scraping versions list). Proceeding with standard blacklisting of {item_identifier}.")
            else: # This covers cases where fallback_version is None, 'None'.
                logging.debug(f"Item {item_identifier} does not have a fallback version configured or the configured one is 'None'. Proceeding with standard blacklisting.")
        else:
            logging.warning(f"Item {item_identifier} does not have a version associated. Cannot perform fallback check. Proceeding with standard blacklisting.")

        # Standard Blacklisting Logic (reached if not returned by early_release or successful fallback creation)
        logging.info(f"Blacklisting item {item_identifier} (version: {current_version or 'N/A'}). Fallback not applied, or fallback version already existed, or no valid fallback path configured.")

        updated_db_item_details = update_media_item_state(item_id, 'Blacklisted')
        
        update_blacklisted_date(item_id, datetime.now())

        if hasattr(queue_manager, 'queue_timer'):
             queue_manager.queue_timer.item_entered_queue(item_id, 'Blacklisted', item_identifier)

        logging.info(f"Moved item {item_identifier} to Blacklisted state and updated blacklisted_date.")

        # --- Add Notification Trigger for Standard Blacklisting ---
        if updated_db_item_details: 
            try:
                notification_data = dict(updated_db_item_details) 
                notification_data['new_state'] = 'Blacklisted' 
                
                # Ensure common fields for notification formatter are present
                if 'title' not in notification_data: notification_data['title'] = item.get('title', 'Unknown Title')
                if 'year' not in notification_data: notification_data['year'] = item.get('year', '')
                if 'type' not in notification_data: notification_data['type'] = item.get('type', 'movie')
                if 'version' not in notification_data: notification_data['version'] = current_version 
                if notification_data['type'] == 'episode':
                    if 'season_number' not in notification_data: notification_data['season_number'] = item.get('season_number')
                    if 'episode_number' not in notification_data: notification_data['episode_number'] = item.get('episode_number')

                add_to_collected_notifications(notification_data)
                logging.info(f"Queued notification for standard blacklisted item: {item_identifier} (ID: {item_id})")
            except Exception as e_notif:
                logging.error(f"Error queueing standard blacklist notification for {item_identifier} (ID: {item_id}): {e_notif}", exc_info=True)
        else:
            logging.warning(f"Skipped standard blacklist notification for {item_identifier} (ID: {item_id}) because item details after state update were not available.")
        # --- End Notification Trigger ---
        return # End of standard blacklisting path

    def blacklist_old_season_items(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Blacklisting item {item_identifier} and related old season items with the same version")

        if queue_manager.get_item_queue(item) != 'Checking':
            self.blacklist_item(item, queue_manager)
        else:
            logging.info(f"Skipping blacklisting of {item_identifier} as it's already in Checking queue")

        related_items = self.find_related_season_items(item, queue_manager)
        for related_item in related_items:
            related_identifier = queue_manager.generate_identifier(related_item)
            if self.is_item_old(related_item) and related_item['version'] == item['version']:
                if queue_manager.get_item_queue(related_item) != 'Checking':
                    self.blacklist_item(related_item, queue_manager)
                else:
                    logging.info(f"Skipping blacklisting of {related_identifier} as it's already in Checking queue")
            else:
                logging.debug(f"Not blacklisting {related_identifier} as it's either not old enough or has a different version")

    def find_related_season_items(self, item: Dict[str, Any], queue_manager) -> List[Dict[str, Any]]:
        """
        Find related season items for a given episode item directly from the database,
        matching imdb_id, season_number, and version, excluding the item itself.
        """
        related_items = []
        if item.get('type') != 'episode' or not item.get('imdb_id') or item.get('season_number') is None or not item.get('version'):
            logging.warning(f"Cannot find related items for {queue_manager.generate_identifier(item)}: Missing required fields (type, imdb_id, season_number, version).")
            return []

        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            query = """
                SELECT * FROM media_items
                WHERE type = 'episode'
                  AND imdb_id = ?
                  AND season_number = ?
                  AND version = ?
                  AND id != ?
            """
            params = (
                item['imdb_id'],
                item['season_number'],
                item['version'],
                item['id']
            )
            cursor.execute(query, params)
            related_rows = cursor.fetchall()
            related_items = [dict(row) for row in related_rows]
            logging.debug(f"Found {len(related_items)} related season items in DB for {queue_manager.generate_identifier(item)} (IMDb: {item['imdb_id']}, S{item['season_number']}, V: {item['version']})")

        except Exception as e:
            logging.error(f"Error querying database for related season items for ID {item.get('id')}: {e}", exc_info=True)
            # Return empty list on error to avoid unintended side effects
            return []
        finally:
            if conn:
                conn.close()

        return related_items

    def is_item_old(self, item: Dict[str, Any]) -> bool:
        if item.get('early_release', False):
            return False
            
        if 'release_date' not in item or item['release_date'] is None or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date, None, or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            days_since_release = (datetime.now().date() - release_date).days
            
            movie_threshold = 7
            episode_threshold = 7
            
            if item['type'] == 'movie':
                return days_since_release > movie_threshold
            elif item['type'] == 'episode':
                return days_since_release > episode_threshold
            else:
                logging.warning(f"Unknown item type: {item['type']}. Considering it as old.")
                return True
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def contains_item_id(self, item_id):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.execute("SELECT 1 FROM media_items WHERE id = ? AND state = 'Blacklisted' LIMIT 1", (item_id,))
            result = cursor.fetchone()
            return result is not None
        except Exception as e:
            logging.error(f"Error checking DB for blacklisted item ID {item_id}: {e}")
            return False