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
            # Fetch items that have either a release_date or physical_release_date in a broad window
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM media_items 
                    WHERE state = 'Unreleased' 
                    AND type = 'movie'
                    AND (
                        (release_date IS NOT NULL AND release_date NOT IN ('Unknown','None','') )
                        OR (physical_release_date IS NOT NULL)
                    )
                """)
                
                items = cursor.fetchall()
                
                # Convert to list of dictionaries
                movie_items = []
                for item in items:
                    item_dict = dict(zip([col[0] for col in cursor.description], item))
                    movie_items.append(item_dict)
                
                # Determine effective release date per item using version setting 'require_physical_release'
                scraping_versions = get_setting('Scraping', 'versions', {})
                eligible_items = []
                for item in movie_items:
                    try:
                        version = item.get('version')
                        version_settings = scraping_versions.get(version, {}) if version else {}
                        require_physical = version_settings.get('require_physical_release', False)
                        release_date_str = item.get('release_date')
                        physical_release_date_str = item.get('physical_release_date')

                        effective_release_date_str = None
                        if require_physical and physical_release_date_str:
                            effective_release_date_str = physical_release_date_str
                        elif (not require_physical) and release_date_str and str(release_date_str).lower() not in ['unknown', 'none', '']:
                            effective_release_date_str = str(release_date_str)
                        elif require_physical and not physical_release_date_str:
                            # If physical is required but unknown, skip from pre-release tracking
                            continue
                        else:
                            # No valid date available
                            continue

                        try:
                            effective_release_date = datetime.strptime(effective_release_date_str, '%Y-%m-%d').date()
                        except Exception:
                            # Bad date format; skip
                            continue

                        # Include only items with effective_release_date within [start_date, end_date]
                        if start_date <= effective_release_date <= end_date:
                            # Attach computed effective date for later logic
                            item['effective_release_date'] = effective_release_date_str
                            eligible_items.append(item)
                    except Exception as item_err:
                        logging.warning(f"Failed to evaluate effective release for item {item.get('id')}: {item_err}")
                        continue
                
                logging.info(f"Found {len(eligible_items)} movie items with effective release dates between {start_date} and {end_date} for pre-release scraping")
                
                # Update the queue items (these stay in Unreleased state in DB)
                self.items = eligible_items
                
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
        """Check if the item is within 24 hours of its effective release date"""
        # Prefer computed effective date if present (from update()), else compute on the fly
        effective_release_date_str = item.get('effective_release_date')
        if not effective_release_date_str:
            try:
                scraping_versions = get_setting('Scraping', 'versions', {})
                version_settings = scraping_versions.get(item.get('version', ''), {})
                require_physical = version_settings.get('require_physical_release', False)
                if require_physical and item.get('physical_release_date'):
                    effective_release_date_str = item.get('physical_release_date')
                else:
                    effective_release_date_str = item.get('release_date')
            except Exception:
                effective_release_date_str = item.get('release_date')

        if not effective_release_date_str or str(effective_release_date_str).lower() in ['unknown', 'none', '']:
            return False
            
        try:
            release_date = datetime.strptime(str(effective_release_date_str), '%Y-%m-%d').date()
            current_date = datetime.now().date()
            time_until_release = release_date - current_date
            
            # Return True if release is within 24 hours (1 day or less)
            return time_until_release <= timedelta(days=1)
        except Exception as e:
            logging.error(f"Error checking effective release date for item {item.get('id')}: {str(e)}")
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
                # Rank results: prefer highest score if available
                try:
                    results_sorted = sorted(
                        results,
                        key=lambda r: r.get('score_breakdown', {}).get('total_score', 0),
                        reverse=True
                    )
                except Exception:
                    results_sorted = results

                # --- START: Minimum Scrape Score Filtering for Pre-Release ---
                delayed_scrape_enabled = get_setting("Debug", "delayed_scrape_based_on_score", False)
                minimum_scrape_score = float(get_setting("Debug", "minimum_scrape_score", 0.0))

                if delayed_scrape_enabled and minimum_scrape_score > 0:
                    # Filter results by minimum score
                    original_count = len(results_sorted)
                    results_sorted = [r for r in results_sorted if r.get('score_breakdown', {}).get('total_score', 0) >= minimum_scrape_score]

                    if not results_sorted:
                        logging.info(f"No results meet minimum score ({minimum_scrape_score}) for pre-release item {item_identifier}, will retry tomorrow")
                        # Item stays in Pre-Release queue for next daily scrape
                    else:
                        best_result = results_sorted[0]
                        logging.info(f"Found {len(results)} results for pre-release item {item_identifier} (filtered from {original_count} to {len(results_sorted)} by minimum score {minimum_scrape_score})")
                else:
                    # No minimum score filtering
                    best_result = results_sorted[0]
                    logging.info(f"Found {len(results)} results for pre-release item {item_identifier}")

                # If a queue_manager is available, attempt to add immediately (like upgrading flow)
                if queue_manager is not None:
                    try:
                        # Move to Adding with results
                        queue_manager.move_to_adding(item, "Pre_release", best_result.get('title', ''), results_sorted)

                        # Process Adding synchronously
                        try:
                            queue_manager.queues["Adding"].process(queue_manager)
                        except Exception as add_proc_err:
                            logging.error(f"Error during Adding processing for {item_identifier}: {add_proc_err}")

                        # Check final state after Adding processing
                        try:
                            from database.database_reading import get_media_item_by_id
                            db_item = get_media_item_by_id(item_id)
                            final_state = db_item.get('state') if db_item else None
                        except Exception as state_err:
                            logging.error(f"Could not fetch final state after Adding for {item_identifier}: {state_err}")
                            db_item = None
                            final_state = None

                        # Treat Checking, Pending Uncached, Upgrading, or Collected as success (no return to Pre-Release)
                        if final_state in ["Checking", "Pending Uncached", "Upgrading", "Collected"]:
                            logging.info(f"Pre-release add succeeded for {item_identifier}; final state: {final_state}")
                            return

                        # Otherwise, move back to Unreleased and re-queue for pre-release
                        logging.info(f"Pre-release add did not succeed for {item_identifier} (state: {final_state}). Returning to Pre-Release queue.")
                        try:
                            from_queue_name = final_state if final_state in getattr(queue_manager, 'queues', {}) else None
                            queue_manager.move_to_unreleased(db_item or item, from_queue_name or "Pre_release")
                        except Exception as move_err:
                            logging.error(f"Failed moving {item_identifier} back to Unreleased: {move_err}")
                        # Ensure it remains tracked for future pre-release scrapes
                        self.add_item(db_item or item)
                        return

                    except Exception as add_err:
                        logging.error(f"Failed to initiate Adding for pre-release item {item_identifier}: {add_err}")
                        # Keep item in Pre-Release for retry
                        return

                # Fallback: persist results to DB without state change if no queue_manager is available
                try:
                    update_media_item_state(
                        item_id,
                        state=item.get('state', 'Unreleased'),
                        filled_by_title=best_result.get('title'),
                        scrape_results=results_sorted
                    )
                    logging.info(f"Stored pre-release results for {item_identifier} (best: {best_result.get('title', 'N/A')})")
                except Exception as persist_err:
                    logging.error(f"Failed to store pre-release results for {item_identifier}: {persist_err}")

                # Do not remove from queue here; leave for retry if no queue manager
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
