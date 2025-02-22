import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

from database import get_all_media_items, get_db_connection
from settings import get_setting

class WantedQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Wanted")]
        self._calculate_scrape_times()

    def _calculate_scrape_times(self):
        for item in self.items:
            if not item['release_date'] or item['release_date'].lower() == "unknown":
                item['scrape_time'] = "Unknown"
                continue

            try:
                release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                
                if item['type'] == 'movie':
                    if get_setting("Queue", "movie_airtime_offset", 19) == '':
                        movie_airtime_offset = 19
                    else:
                        movie_airtime_offset = get_setting("Queue", "movie_airtime_offset", 19)
                    movie_airtime_offset = float(movie_airtime_offset) if movie_airtime_offset else 19.0
                    airtime_cutoff = (datetime.combine(release_date, datetime.min.time()) + timedelta(hours=movie_airtime_offset)).time()
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
                        # If that fails, try HH:MM format
                        airtime = datetime.strptime(airtime_str, '%H:%M').time()
                    airtime_cutoff = (datetime.combine(release_date, airtime) + timedelta(hours=episode_airtime_offset)).time()
                else:
                    airtime_cutoff = datetime.now().time()

                scrape_time = datetime.combine(release_date, airtime_cutoff)
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

    def process(self, queue_manager):
        try:
            # logging.debug("Processing wanted queue")
            current_datetime = datetime.now()
            items_to_move_scraping = []
            items_to_move_unreleased = []
            
            for item in self.items:
                try:
                    item_identifier = queue_manager.generate_identifier(item)
                    release_date_str = item.get('release_date')
                    airtime_str = item.get('airtime')

                    if not release_date_str or (isinstance(release_date_str, str) and release_date_str.lower() == 'unknown'):
                        logging.debug(f"Item {item_identifier} has no scrape time. Moving to Unreleased queue.")
                        items_to_move_unreleased.append(item)
                        continue  # Skip further processing for this item

                    try:
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

                        # If it's an early release and ready to be scraped, move to scraping
                        if item.get('early_release', False):
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