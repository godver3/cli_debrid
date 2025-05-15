import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta, date

from utilities.settings import get_setting
from queues.config_manager import get_version_settings, load_config
from database.database_reading import check_existing_media_item, get_media_item_by_id, get_media_item_presence
from database.database_writing import update_release_date_and_state, update_media_item_state, update_blacklisted_date
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

            conn = get_db_connection()
            cursor = conn.cursor()

            query = """
                SELECT id, title, type, imdb_id, tmdb_id, season_number, episode_number, version, blacklisted_date
                FROM media_items
                WHERE state = 'Blacklisted'
                  AND blacklisted_date IS NOT NULL
                  AND blacklisted_date <= ?
            """
            cursor.execute(query, (cutoff_date.isoformat(),))
            items_to_unblacklist_rows = cursor.fetchall()
            conn.close()

            items_to_unblacklist = [dict(row) for row in items_to_unblacklist_rows]

            if not items_to_unblacklist:
                logging.debug("No items found eligible for unblacklisting.")
                return

            logging.info(f"Found {len(items_to_unblacklist)} items eligible for unblacklisting.")

            unblacklisted_count = 0
            for item in items_to_unblacklist:
                item_identifier = queue_manager.generate_identifier(item)
                try:
                    blacklisted_date_str = item.get('blacklisted_date', 'N/A')
                    logging.info(f"Item {item_identifier} blacklisted on {blacklisted_date_str} is eligible for unblacklisting (cutoff: {cutoff_date.isoformat()}).")

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
                # Check if item exists in any state with the fallback version
                item['version'] = fallback_version  # Temporarily set version for the check
                presence = get_media_item_presence(item.get('imdb_id'), item.get('tmdb_id'))
                item['version'] = current_version  # Reset version back
                
                if presence != "Missing":
                    logging.info(f"Item {item_identifier} failed for version '{current_version}'. Fallback version '{fallback_version}' already exists in state '{presence}'. Proceeding with blacklisting original.")
                    pass
                else:
                    logging.info(f"Item {item_identifier} failed for version '{current_version}'. Attempting fallback to version '{fallback_version}'.")
                    
                    current_queue_name = queue_manager.get_item_queue(item)
                    if current_queue_name and current_queue_name != 'Blacklisted':
                        try:
                            queue_manager.queues[current_queue_name].remove_item(item)
                            logging.debug(f"Removed {item_identifier} from {current_queue_name} queue for fallback.")
                        except KeyError:
                             logging.warning(f"Could not find queue '{current_queue_name}' to remove item {item_identifier} during fallback.")
                        except Exception as e:
                             logging.error(f"Error removing item {item_identifier} from {current_queue_name} queue during fallback: {e}")
                    
                    item['version'] = fallback_version
                    item_identifier_new_version = queue_manager.generate_identifier(item)
                    
                    logging.debug(f"Calling move_to_wanted for {item_identifier_new_version} with new_version={fallback_version}")
                    queue_manager.move_to_wanted(item, f"Fallback from {current_version}", new_version=fallback_version)
                    logging.info(f"Moved item {item_identifier_new_version} back to Wanted queue with fallback version '{fallback_version}'.")
                    return
                if fallback_version and fallback_version != 'None' and fallback_version not in all_versions:
                     logging.warning(f"Configured fallback version '{fallback_version}' for version '{current_version}' does not exist. Proceeding with blacklisting {item_identifier}.")
                pass
            else:
                logging.debug(f"Item {item_identifier} does not have a fallback version associated. Blacklisting.")
        else:
            logging.warning(f"Item {item_identifier} does not have a version associated. Cannot perform fallback check. Proceeding with blacklisting.")

        logging.info(f"Blacklisting item {item_identifier} (version: {current_version or 'N/A'}). No fallback applied.")

        update_media_item_state(item_id, 'Blacklisted')
        
        update_blacklisted_date(item_id, datetime.now())

        if hasattr(queue_manager, 'queue_timer'):
             queue_manager.queue_timer.item_entered_queue(item_id, 'Blacklisted', item_identifier)

        logging.info(f"Moved item {item_identifier} to Blacklisted state and updated blacklisted_date")

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