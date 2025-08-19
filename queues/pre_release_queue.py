import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from database.database_writing import update_media_item_state
from scraper.scraper import scrape
from utilities.settings import get_setting
import os
import pickle
from pathlib import Path
from database.core import get_db_connection


class PreReleaseQueue:
    def __init__(self):
        self.items = []
        self.last_scrape_times = {}
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        self.pre_release_data_file = Path(db_content_dir) / "pre_release_data.pkl"
        self.pre_release_data = self.load_pre_release_data()
        self.currently_processing_item_id: Optional[str] = None

    def load_pre_release_data(self):
        """Load pre-release data from disk"""
        try:
            if self.pre_release_data_file.exists():
                if self.pre_release_data_file.stat().st_size == 0:
                    logging.info(f"Pre-release data file is empty, initializing new data")
                    return {}
                    
                with open(self.pre_release_data_file, 'rb') as f:
                    try:
                        return pickle.load(f)
                    except (EOFError, pickle.UnpicklingError) as e:
                        logging.error(f"Error loading pre-release data, file may be corrupted: {str(e)}")
                        # Backup the corrupted file
                        backup_path = str(self.pre_release_data_file) + '.bak'
                        try:
                            import shutil
                            shutil.copy2(self.pre_release_data_file, backup_path)
                            logging.info(f"Backed up corrupted pre-release data file to {backup_path}")
                        except Exception as backup_err:
                            logging.error(f"Failed to backup corrupted file: {str(backup_err)}")
                        return {}
            return {}
        except Exception as e:
            logging.error(f"Unexpected error loading pre-release data: {str(e)}")
            return {}

    def save_pre_release_data(self):
        """Save pre-release data to disk"""
        try:
            os.makedirs(os.path.dirname(self.pre_release_data_file), exist_ok=True)
            with open(self.pre_release_data_file, 'wb') as f:
                pickle.dump(self.pre_release_data, f)
        except Exception as e:
            logging.error(f"Error saving pre-release data: {str(e)}")

    def update(self):
        """Update the queue by loading eligible movie items from the database"""
        try:
            # Get the pre-release scrape days setting
            pre_release_days = get_setting('Queue', 'pre_release_scrape_days', 0)
            if pre_release_days <= 0:
                logging.debug("Pre-release scraping is disabled (pre_release_scrape_days <= 0)")
                return

            # Get current date
            current_date = datetime.now().date()
            
            # Calculate the date range for pre-release scraping
            # We want items that will be released within the next 'pre_release_days' days
            start_date = current_date
            end_date = current_date + timedelta(days=pre_release_days)
            
            # Get all movie items from the database that are in Unreleased state
            # and have a release date within our pre-release window
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM media_items 
                    WHERE state = 'Unreleased' 
                    AND type = 'movie' 
                    AND release_date >= ?
                    AND release_date <= ?
                    AND release_date IS NOT NULL
                    AND release_date != 'Unknown'
                    AND release_date != 'None'
                    AND release_date != ''
                """, (start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
                
                items = cursor.fetchall()
                
                # Convert to list of dictionaries
                movie_items = []
                for item in items:
                    item_dict = dict(zip([col[0] for col in cursor.description], item))
                    movie_items.append(item_dict)
                
                logging.info(f"Found {len(movie_items)} movie items with release dates between {start_date} and {end_date} for pre-release scraping")
                
                # Update the queue items (these stay in Unreleased state in DB)
                self.items = movie_items
                
        except Exception as e:
            logging.error(f"Error updating pre-release queue: {str(e)}")

    def get_contents(self):
        """Get current queue contents"""
        return self.items.copy()

    def add_item(self, item: Dict[str, Any]):
        """Add an item to the queue"""
        if not any(i['id'] == item['id'] for i in self.items):
            self.items.append(item)
            logging.debug(f"Added item {item.get('id')} to pre-release queue")

    def remove_item(self, item: Dict[str, Any]):
        """Remove an item from the queue"""
        self.items = [i for i in self.items if i['id'] != item['id']]
        logging.debug(f"Removed item {item.get('id')} from pre-release queue")

    def contains_item_id(self, item_id):
        """Check if an item with the given ID is in the queue"""
        return any(item['id'] == item_id for item in self.items)

    def should_perform_daily_scrape(self, item_id: str, current_time: datetime) -> bool:
        """Check if we should perform a daily scrape for this item"""
        last_scrape_time = self.last_scrape_times.get(item_id)
        if last_scrape_time is None:
            logging.info(f"Item {item_id} has never been scraped before, running first scrape")
            return True
            
        time_since_last_scrape = current_time - last_scrape_time
        should_run = time_since_last_scrape >= timedelta(days=1)
        
        if should_run:
            logging.info(f"Running daily scrape for item {item_id} - Last scrape was {time_since_last_scrape} ago")
        else:
            logging.info(f"Skipping scrape for item {item_id} - Only {time_since_last_scrape} since last scrape, waiting for 1 day")
            
        return should_run

    def is_within_24_hours_of_release(self, item: Dict[str, Any]) -> bool:
        """Check if the item is within 24 hours of its release date"""
        release_date_str = item.get('release_date')
        if not release_date_str or release_date_str.lower() in ['unknown', 'none']:
            return False
            
        try:
            release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
            current_date = datetime.now().date()
            time_until_release = release_date - current_date
            
            # Return True if release is within 24 hours (1 day or less)
            return time_until_release <= timedelta(days=1)
        except Exception as e:
            logging.error(f"Error checking release date for item {item.get('id')}: {str(e)}")
            return False

    def daily_scrape(self, item: Dict[str, Any], queue_manager=None):
        """Perform daily scraping for an item"""
        item_id = item['id']
        item_identifier = self.generate_identifier(item)
        
        logging.info(f"Performing daily scrape for pre-release item: {item_identifier}")
        
        try:
            # Use the scrape function directly
            results, filtered_out = scrape(
                item['imdb_id'],
                item['tmdb_id'],
                item['title'],
                item['year'],
                item['type'],
                item['version'],
                item.get('season_number'),
                item.get('episode_number'),
                False,  # multi_pack = False for pre-release scraping
                item.get('genres')
            )
            
            # Ensure results is a list
            results = results if results is not None else []
            
            if results:
                logging.info(f"Found {len(results)} results for pre-release item {item_identifier}")
                
                # If we found results, remove from Pre-Release queue (item stays in Unreleased state in DB)
                # The normal queue processing will handle moving it to Wanted when appropriate
                logging.info(f"Found results for pre-release item {item_identifier}, removing from Pre-Release queue")
                # Note: We don't change the database state - item stays in Unreleased
            else:
                logging.info(f"No results found for pre-release item {item_identifier}, will retry tomorrow")
                # Item stays in Pre-Release queue for next daily scrape
                
        except Exception as e:
            logging.error(f"Error during daily scrape for pre-release item {item_identifier}: {str(e)}")

    def process(self, queue_manager=None):
        """Process items in the pre-release queue"""
        current_time = datetime.now()
        
        # Update the queue first
        self.update()
        
        for item in self.items[:]:  # Create a copy of the list to iterate over
            try:
                item_id = item['id']
                item_identifier = self.generate_identifier(item)
                
                # Check if we should perform daily scrape
                if self.should_perform_daily_scrape(item_id, current_time):
                    logging.info(f"Processing pre-release item: {item_identifier}")
                    
                    # Check if item is within 24 hours of release
                    if self.is_within_24_hours_of_release(item):
                        logging.info(f"Pre-release item {item_identifier} is within 24 hours of release, removing from Pre-Release queue (will be picked up by normal Wanted queue processing)")
                        # Remove from Pre-Release queue - item stays in Unreleased state in DB
                        # The normal queue processing will move it to Wanted when appropriate
                        self.remove_item(item)
                    else:
                        # Perform daily scrape
                        self.daily_scrape(item, queue_manager)
                        self.last_scrape_times[item_id] = current_time
                        
                        # Update pre-release data
                        if item_id not in self.pre_release_data:
                            self.pre_release_data[item_id] = {'scrape_count': 0, 'last_scrape': None}
                        
                        self.pre_release_data[item_id]['scrape_count'] += 1
                        self.pre_release_data[item_id]['last_scrape'] = current_time.isoformat()
                        self.save_pre_release_data()
                        
            except Exception as e:
                logging.error(f"Error processing pre-release item {item.get('id', 'unknown')}: {str(e)}")
                logging.exception("Traceback:")

    def get_contents(self):
        """Return the current contents of the pre-release queue"""
        return self.items.copy()

    def contains_item_id(self, item_id):
        """Check if an item with the given ID is in the queue"""
        return any(item.get('id') == item_id for item in self.items)

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        """Generate a human-readable identifier for an item"""
        if item['type'] == 'movie':
            return f"movie_{item.get('title', 'Unknown')}_{item.get('imdb_id', 'Unknown')}_{item.get('version', 'Unknown')}"
        else:
            return f"unknown_{item.get('id', 'Unknown')}"
