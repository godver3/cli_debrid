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
                    movie_airtime_offset = float(get_setting("Queue", "movie_airtime_offset", "19")) * 60
                    airtime_cutoff = (datetime.combine(release_date, datetime.min.time()) + timedelta(minutes=movie_airtime_offset)).time()
                elif item['type'] == 'episode':
                    episode_airtime_offset = float(get_setting("Queue", "episode_airtime_offset", "0")) * 60
                    airtime_str = item.get('airtime') or "19:00"  # Use "19:00" if airtime is None
                    airtime = datetime.strptime(airtime_str, '%H:%M').time()
                    airtime_cutoff = (datetime.combine(release_date, airtime) + timedelta(minutes=episode_airtime_offset)).time()
                else:
                    airtime_cutoff = datetime.now().time()

                scrape_time = datetime.combine(release_date, airtime_cutoff)
                item['scrape_time'] = scrape_time.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                item['scrape_time'] = "Invalid date"

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        logging.debug("Processing wanted queue")
        current_datetime = datetime.now()
        items_to_move_scraping = []
        items_to_move_unreleased = []
        
        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            release_date_str = item.get('release_date')
            airtime_str = item.get('airtime')

            if not release_date_str or (isinstance(release_date_str, str) and release_date_str.lower() == 'unknown'):
                logging.info(f"Item {item_identifier} has no scrape time. Moving to Unreleased queue.")
                items_to_move_unreleased.append(item)
                continue  # Skip further processing for this item

            try:
                release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                
                # Handle case where airtime is None or invalid
                if airtime_str:
                    try:
                        airtime = datetime.strptime(airtime_str, '%H:%M').time()
                    except ValueError:
                        logging.warning(f"Invalid airtime format for item {item_identifier}: {airtime_str}. Using default.")
                        airtime = datetime.strptime("00:00", '%H:%M').time()
                else:
                    logging.debug(f"No airtime set for item {item_identifier}. Using default.")
                    airtime = datetime.strptime("00:00", '%H:%M').time()

                release_datetime = datetime.combine(release_date, airtime)

                # Apply airtime offset
                if item['type'] == 'movie':
                    offset = float(get_setting("Queue", "movie_airtime_offset", "19"))
                else:  # episode
                    offset = float(get_setting("Queue", "episode_airtime_offset", "0"))
                
                release_datetime += timedelta(hours=offset)

                time_until_release = release_datetime - current_datetime

                if time_until_release <= timedelta():
                    logging.info(f"Item {item_identifier} has met its airtime requirement. Moving to Scraping queue.")
                    items_to_move_scraping.append(item)
                elif time_until_release > timedelta(hours=24):
                    logging.info(f"Item {item_identifier} is more than 24 hours away. Moving to Unreleased queue.")
                    items_to_move_unreleased.append(item)
                else:
                    logging.debug(f"Item {item_identifier} will be ready for scraping in {time_until_release}. Keeping in Wanted queue.")

            except ValueError as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}")
            except Exception as e:
                logging.error(f"Unexpected error processing item {item_identifier}: {str(e)}", exc_info=True)

        # Move marked items to Scraping queue
        for item in items_to_move_scraping:
            queue_manager.move_to_scraping(item, "Wanted")
        
        # Move marked items to Unreleased queue
        for item in items_to_move_unreleased:
            queue_manager.move_to_unreleased(item, "Wanted")
        
        logging.debug(f"Wanted queue processing complete. Items moved to Scraping queue: {len(items_to_move_scraping)}")
        logging.debug(f"Items moved to Unreleased queue: {len(items_to_move_unreleased)}")
        logging.debug(f"Remaining items in Wanted queue: {len(self.items)}")