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

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        logging.debug("Processing wanted queue")
        current_date = datetime.now().date()
        current_time = datetime.now().time()
        seasons_in_queues = set()

        # Get seasons already in Scraping or Adding queue
        for queue_name in ["Scraping", "Adding"]:
            for item in queue_manager.queues[queue_name].get_contents():
                if item['type'] == 'episode':
                    seasons_in_queues.add((item['imdb_id'], item['season_number'], item['version']))
        
        items_to_move = []
        items_to_unreleased = []
        
        # Process each item in the Wanted queue
        for item in list(self.items):
            logging.debug(f"Processing item in Wanted queue: {item}")
            item_identifier = queue_manager.generate_identifier(item)
            try:
                # Determine airtime offset based on content type
                if item['type'] == 'movie':
                    movie_airtime_offset = (float(get_setting("Queue", "movie_airtime_offset", "19"))*60)
                    if not movie_airtime_offset:
                        logging.warning("movie_airtime_offset setting is empty, using default value of 19")
                    airtime_offset = float(movie_airtime_offset) if movie_airtime_offset else 19*60
                    airtime_cutoff = (datetime.combine(current_date, datetime.min.time()) + timedelta(hours=airtime_offset)).time()
                elif item['type'] == 'episode':
                    episode_airtime_offset = (float(get_setting("Queue", "episode_airtime_offset", "0"))*60)
                    if not episode_airtime_offset:
                        logging.warning("episode_airtime_offset setting is empty, using default value of 0")
                    airtime_offset = float(episode_airtime_offset) if episode_airtime_offset else 0
                    
                    # Get the airtime from the database
                    conn = get_db_connection()
                    cursor = conn.execute('SELECT airtime FROM media_items WHERE id = ?', (item['id'],))
                    result = cursor.fetchone()
                    conn.close()

                    if result and result['airtime']:
                        airtime_str = result['airtime']
                        airtime = datetime.strptime(airtime_str, '%H:%M').time()
                    else:
                        # Default to 19:00 if no airtime is set
                        airtime = datetime.strptime("19:00", '%H:%M').time()

                    # Apply the offset to the airtime
                    airtime_cutoff = (datetime.combine(current_date, airtime) + timedelta(minutes=airtime_offset)).time()
                else:
                    airtime_offset = 0
                    airtime_cutoff = current_time  # No delay for unknown types

                logging.debug(f"Processing item: {item_identifier}, Type: {item['type']}, Airtime offset: {airtime_offset}, Airtime cutoff: {airtime_cutoff}")

                # Check if release_date is None, empty string, or "Unknown"
                if not item['release_date'] or item['release_date'].lower() == "unknown":
                    logging.debug(f"Release date is missing or unknown for item: {item_identifier}. Moving to Unreleased state.")
                    items_to_unreleased.append(item)
                    continue
                
                try:
                    release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                except ValueError as e:
                    logging.error(f"Error parsing release date for item {item_identifier}: {str(e)}")
                    logging.error(f"Item details: {item}")
                    items_to_unreleased.append(item)
                    continue

                logging.debug(f"Item: {item_identifier}, Release date: {release_date}, Current date: {current_date}")

                # Calculate the release datetime with airtime offset
                release_datetime = datetime.combine(release_date, airtime_cutoff)
                current_datetime = datetime.now()

                if current_datetime < release_datetime:
                    time_until_release = release_datetime - current_datetime
                    logging.info(f"Item {item_identifier} is not released yet. Moving to Unreleased state.")
                    logging.debug(f"Will start scraping for {item_identifier} at {release_datetime}, time until: {time_until_release}")
                    items_to_unreleased.append(item)
                    continue
                
                # Check if we've reached the scraping cap
                scraping_cap = int(get_setting("Queue", "scraping_cap", 5))
                current_scraping_count = len(queue_manager.queues["Scraping"].get_contents()) + len(items_to_move)
                logging.debug(f"Current scraping count: {current_scraping_count}, Scraping cap: {scraping_cap}")
                if current_scraping_count >= scraping_cap:
                    logging.debug(f"Scraping cap reached. Keeping {item_identifier} in Wanted queue.")
                    break  # Exit the loop as we've reached the cap
                
                # Check if we're already processing an item from this season
                if item['type'] == 'episode':
                    season_key = (item['imdb_id'], item['season_number'], item['version'])
                    if season_key in seasons_in_queues:
                        logging.debug(f"Already processing an item from {item_identifier}. Keeping in Wanted queue.")
                        continue
                logging.info(f"Item {item_identifier} has been released and meets airtime offset. Marking for move to Scraping.")
                items_to_move.append(item)
                if item['type'] == 'episode':
                    seasons_in_queues.add(season_key)
            except Exception as e:
                logging.error(f"Unexpected error processing item {item_identifier}: {str(e)}", exc_info=True)
                logging.error(f"Item details: {item}")
                # Move to Unreleased state if there's an unexpected error
                items_to_unreleased.append(item)
        
        # Move marked items to Scraping queue
        for item in items_to_move:
            queue_manager.move_to_scraping(item, "Wanted")
        
        # Move items to Unreleased state and remove from Wanted queue
        for item in items_to_unreleased:
            queue_manager.move_to_unreleased(item, "Wanted")
        
        logging.debug(f"Wanted queue processing complete. Items moved to Scraping queue: {len(items_to_move)}")
        logging.debug(f"Items moved to Unreleased state and removed from Wanted queue: {len(items_to_unreleased)}")
        logging.debug(f"Remaining items in Wanted queue: {len(self.items)}")