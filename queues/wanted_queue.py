import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

from database import get_all_media_items, get_db_connection, remove_from_media_items
from settings import get_setting
from manual_blacklist import is_blacklisted

class WantedQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Wanted")]
        # Move any blacklisted items to blacklisted state before calculating scrape times
        self.move_blacklisted_items()
        self._calculate_scrape_times()

    def _calculate_scrape_times(self):
        for item in self.items:
            # For early release items without release date, set scrape time to now
            if item.get('early_release', False) and (not item.get('release_date') or str(item.get('release_date')).lower() in ["unknown", "none"]):
                item['scrape_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logging.info(f"Early release item without release date - setting immediate scrape time for {item.get('title', 'Unknown')}")
                continue

            if not item.get('release_date') or str(item.get('release_date')).lower() in ["unknown", "none"]:
                item['scrape_time'] = "Unknown"
                continue

            try:
                release_date = datetime.strptime(str(item['release_date']), '%Y-%m-%d').date()
                
                if item['type'] == 'movie':
                    if get_setting("Queue", "movie_airtime_offset", 19) == '':
                        movie_airtime_offset = 19
                    else:
                        movie_airtime_offset = get_setting("Queue", "movie_airtime_offset", 19)
                    movie_airtime_offset = float(movie_airtime_offset) if movie_airtime_offset else 19.0
                    # Calculate the full datetime with offset for movies
                    scrape_time = datetime.combine(release_date, datetime.min.time()) + timedelta(hours=movie_airtime_offset)
                    item['scrape_time'] = scrape_time.strftime('%Y-%m-%d %H:%M:%S')
                elif item['type'] == 'episode':
                    if get_setting("Queue", "episode_airtime_offset", 0) == '':
                        episode_airtime_offset = 0
                    else:
                        episode_airtime_offset = get_setting("Queue", "episode_airtime_offset", 0)
                    episode_airtime_offset = float(episode_airtime_offset) if episode_airtime_offset else 0.0
                    airtime_str = item.get('airtime') or "19:00"  # Use "19:00" if airtime is None
                    try:
                        # First try HH:MM:SS format
                        airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                    except ValueError:
                        try:
                            # If that fails, try HH:MM format
                            airtime = datetime.strptime(airtime_str, '%H:%M').time()
                        except ValueError:
                            # If both formats fail, use default time
                            airtime = datetime.strptime("19:00", '%H:%M').time()
                    # Calculate the full datetime with offset, allowing it to roll over to next day
                    scrape_time = datetime.combine(release_date, airtime) + timedelta(hours=episode_airtime_offset)
                    item['scrape_time'] = scrape_time.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    # For unknown types, use current time
                    scrape_time = datetime.now()
                    item['scrape_time'] = scrape_time.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError as e:
                logging.error(f"Error calculating scrape time for item {item.get('id', 'Unknown')}: {str(e)}")
                item['scrape_time'] = "Invalid date or time"

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def _reconcile_with_existing_items(self, item: Dict[str, Any]) -> bool:
        """
        Check if an item already exists in Collected or Upgrading state with the same version.
        If found, remove the current item from the wanted queue.
        
        Args:
            item: The item to check for reconciliation
            
        Returns:
            bool: True if item was reconciled (found existing), False otherwise
        """
        conn = get_db_connection()
        try:
            # Query for existing items with same identifiers and version in Collected or Upgrading state
            cursor = conn.execute('''
                SELECT * FROM media_items 
                WHERE state IN ('Collected', 'Upgrading')
                AND version = ?
                AND (
                    (imdb_id = ? AND imdb_id IS NOT NULL) OR
                    (tmdb_id = ? AND tmdb_id IS NOT NULL)
                )
                AND type = ?
                AND (
                    (type != 'episode') OR
                    (season_number = ? AND episode_number = ?)
                )
            ''', (
                item.get('version'),
                item.get('imdb_id'),
                item.get('tmdb_id'),
                item.get('type'),
                item.get('season_number'),
                item.get('episode_number')
            ))
            
            existing_item = cursor.fetchone()
            if existing_item:
                logging.info(f"Found existing item in {existing_item['state']} state with same version. "
                           f"Removing duplicate from Wanted queue.")
                remove_from_media_items(item['id'])
                self.remove_item(item)
                return True
                
            return False
            
        except Exception as e:
            logging.error(f"Error during item reconciliation: {str(e)}")
            return False
        finally:
            conn.close()

    def process(self, queue_manager):
        try:
            # logging.debug("Processing wanted queue")
            current_datetime = datetime.now()
            items_to_move_scraping = []
            items_to_move_unreleased = []
            
            for item in self.items:
                try:
                    # First check if this item already exists in Collected/Upgrading state
                    if self._reconcile_with_existing_items(item):
                        continue
                        
                    item_identifier = queue_manager.generate_identifier(item)
                    release_date_str = item.get('release_date')
                    airtime_str = item.get('airtime')
                    version = item.get('version')

                    # Check if version requires physical release
                    scraping_versions = get_setting('Scraping', 'versions', {})
                    version_settings = scraping_versions.get(version, {})
                    require_physical = version_settings.get('require_physical_release', False)
                    physical_release_date = item.get('physical_release_date')
                    
                    if require_physical and not physical_release_date:
                        logging.info(f"Item {item_identifier} requires physical release date but none available. Moving to Unreleased queue.")
                        items_to_move_unreleased.append(item)
                        continue

                    # Handle early release items without release date
                    if item.get('early_release', False):
                        logging.info(f"Early release item {item_identifier} - moving to Scraping queue regardless of release date")
                        items_to_move_scraping.append(item)
                        continue

                    if not release_date_str or release_date_str is None or (isinstance(release_date_str, str) and release_date_str.lower() == 'unknown'):
                        logging.debug(f"Item {item_identifier} has no scrape time. Moving to Unreleased queue.")
                        items_to_move_unreleased.append(item)
                        continue  # Skip further processing for this item

                    try:
                        # If physical release is required, use that date instead
                        if require_physical and physical_release_date:
                            try:
                                release_date = datetime.strptime(physical_release_date, '%Y-%m-%d').date()
                                logging.info(f"Item {item_identifier} using physical release date: {release_date}")
                            except ValueError:
                                logging.warning(f"Invalid physical release date format for item {item_identifier}: {physical_release_date}")
                                items_to_move_unreleased.append(item)
                                continue
                        else:
                            release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                        
                        # Handle case where airtime is None or invalid
                        if airtime_str:
                            try:
                                airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                            except ValueError:
                                try:
                                    airtime = datetime.strptime(airtime_str, '%H:%M').time()
                                except ValueError:
                                    logging.debug(f"Invalid airtime format for item {item_identifier}: {airtime_str}. Using default.")
                                    airtime = datetime.strptime("00:00", '%H:%M').time()
                        else:
                            airtime = datetime.strptime("00:00", '%H:%M').time()

                        release_datetime = datetime.combine(release_date, airtime)

                        # Apply airtime offset
                        if item['type'] == 'movie':
                            if get_setting("Queue", "movie_airtime_offset", 19) == '':
                                movie_airtime_offset = 19
                            else:
                                movie_airtime_offset = get_setting("Queue", "movie_airtime_offset", 19)
                            offset = float(movie_airtime_offset) if movie_airtime_offset else 19.0
                        else:  # episode
                            if get_setting("Queue", "episode_airtime_offset", 0) == '':
                                episode_airtime_offset = 0
                            else:
                                episode_airtime_offset = get_setting("Queue", "episode_airtime_offset", 0)
                            offset = float(episode_airtime_offset) if episode_airtime_offset else 0.0
                        
                        release_datetime += timedelta(hours=offset)

                        time_until_release = release_datetime - current_datetime

                        # If physical release is required, ignore early release flag
                        if require_physical:
                            if time_until_release <= timedelta():
                                logging.debug(f"Item {item_identifier} has met its physical release requirement. Moving to Scraping queue.")
                                items_to_move_scraping.append(item)
                            elif time_until_release > timedelta(hours=24):
                                logging.debug(f"Item {item_identifier} is more than 24 hours away from physical release. Moving to Unreleased queue.")
                                items_to_move_unreleased.append(item)
                        # If no physical release required, check early release flag
                        elif item.get('early_release', False):
                            logging.debug(f"Item {item_identifier} is an early release. Moving to Scraping queue.")
                            items_to_move_scraping.append(item)
                        # Otherwise check normal release timing
                        elif time_until_release <= timedelta():
                            logging.debug(f"Item {item_identifier} has met its airtime requirement. Moving to Scraping queue.")
                            items_to_move_scraping.append(item)
                        elif time_until_release > timedelta(hours=24):
                            logging.debug(f"Item {item_identifier} is more than 24 hours away. Moving to Unreleased queue.")
                            items_to_move_unreleased.append(item)
                    except ValueError as e:
                        logging.error(f"Error processing item {item_identifier}: {str(e)}")
                        # Add to unreleased if there's an error parsing dates
                        items_to_move_unreleased.append(item)
                except Exception as e:
                    logging.error(f"Unexpected error processing item {item.get('id', 'Unknown')}: {str(e)}", exc_info=True)
                    # Skip this item and continue with others
                    continue

            # Move marked items to respective queues
            try:
                for item in items_to_move_scraping:
                    queue_manager.move_to_scraping(item, "Wanted")
            except Exception as e:
                logging.error(f"Error moving items to Scraping queue: {str(e)}", exc_info=True)
            
            try:
                for item in items_to_move_unreleased:
                    queue_manager.move_to_unreleased(item, "Wanted")
            except Exception as e:
                logging.error(f"Error moving items to Unreleased queue: {str(e)}", exc_info=True)

        except Exception as e:
            logging.error(f"Fatal error in wanted queue processing: {str(e)}", exc_info=True)
            # Even if there's a fatal error, we want to continue program execution
            return False

        return True

    def move_blacklisted_items(self):
        """
        Check all items in the Wanted queue against the blacklist and move any that are blacklisted
        to the Blacklisted state.
        """
        items_to_remove = []
        blacklisted_count = 0

        for item in self.items:
            season_number = item.get('season_number') if item.get('type') == 'episode' else None
            is_item_blacklisted = (
                is_blacklisted(item.get('imdb_id', ''), season_number) or 
                is_blacklisted(item.get('tmdb_id', ''), season_number)
            )

            if is_item_blacklisted:
                items_to_remove.append(item)
                try:
                    # Update the item's state to Blacklisted and set the blacklisted date
                    with get_db_connection() as conn:
                        conn.execute("""
                            UPDATE media_items 
                            SET state = 'Blacklisted', blacklisted_date = ? 
                            WHERE id = ?
                        """, (datetime.now(), item['id']))
                    blacklisted_count += 1
                    logging.info(f"Moved item to blacklisted state: {item.get('title', 'Unknown')} "
                               f"(ID: {item['id']}, IMDb: {item.get('imdb_id', 'N/A')}, "
                               f"TMDB: {item.get('tmdb_id', 'N/A')})")
                except Exception as e:
                    logging.error(f"Error updating blacklist state for item {item['id']}: {str(e)}")

        # Remove items from the wanted queue
        for item in items_to_remove:
            self.remove_item(item)

        if blacklisted_count > 0:
            logging.info(f"Moved {blacklisted_count} items to blacklisted state")

        return blacklisted_count