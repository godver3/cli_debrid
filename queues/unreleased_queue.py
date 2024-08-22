import logging
from typing import Dict, Any
from datetime import datetime, timedelta

from database import get_all_media_items, get_db_connection
from settings import get_setting

class UnreleasedQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Unreleased")]

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        logging.debug(f"Processing unreleased queue. Items: {len(self.items)}")
        current_datetime = datetime.now()
        items_to_move = []

        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            release_date_str = item.get('release_date')

            if not release_date_str or release_date_str.lower() == 'unknown':
                logging.warning(f"Item {item_identifier} has no release date. Keeping in Unreleased queue.")
                continue

            try:
                release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                
                # Determine airtime offset based on content type
                if item['type'] == 'movie':
                    airtime_offset = int(get_setting("Queue", "movie_airtime_offset", "19"))
                    airtime_cutoff = (datetime.combine(release_date, datetime.min.time()) + timedelta(hours=airtime_offset)).time()
                elif item['type'] == 'episode':
                    airtime_offset = int(get_setting("Queue", "episode_airtime_offset", "0"))
                    
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
                    airtime_cutoff = (datetime.combine(release_date, airtime) + timedelta(minutes=airtime_offset)).time()
                else:
                    airtime_cutoff = datetime.min.time()  # No delay for unknown types

                release_datetime = datetime.combine(release_date, airtime_cutoff)

                if current_datetime >= release_datetime:
                    logging.info(f"Item {item_identifier} is now released. Moving to Wanted queue.")
                    items_to_move.append(item)
                else:
                    time_until_release = release_datetime - current_datetime
                    logging.debug(f"Item {item_identifier} will be released in {time_until_release}.")
            except ValueError:
                logging.error(f"Invalid release date format for item {item_identifier}: {release_date_str}")

        # Move items to Wanted queue
        for item in items_to_move:
            queue_manager.move_to_wanted(item, "Unreleased")
            self.remove_item(item)

        logging.debug(f"Unreleased queue processing complete. Items moved to Wanted queue: {len(items_to_move)}")
        logging.debug(f"Remaining items in Unreleased queue: {len(self.items)}")