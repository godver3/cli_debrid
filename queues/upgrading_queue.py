import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from database.database_writing import add_to_collected_notifications
from queues.scraping_queue import ScrapingQueue
from queues.adding_queue import AddingQueue
from utilities.settings import get_setting
from utilities.plex_functions import remove_file_from_plex
from database.not_wanted_magnets import is_magnet_not_wanted, is_url_not_wanted
import os
import pickle
from pathlib import Path
from database.database_writing import update_media_item
from database.core import get_db_connection
from difflib import SequenceMatcher
from debrid.common import extract_hash_from_magnet, extract_hash_from_file
from database.torrent_tracking import record_torrent_addition, update_torrent_tracking, get_torrent_history
from PTT import parse_title
import re
from scraper.functions.ptt_parser import parse_with_ptt

class UpgradingQueue:
    def __init__(self):
        self.items = []
        self.upgrade_times = {}
        self.last_scrape_times = {}
        self.upgrades_found = {}
        self.scraping_queue = ScrapingQueue()
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        self.upgrades_file = Path(db_content_dir) / "upgrades.pkl"
        self.failed_upgrades_file = Path(db_content_dir) / "failed_upgrades.pkl"
        self.upgrade_states_file = Path(db_content_dir) / "upgrade_states.pkl"  # New file for complete states
        self.upgrades_data = self.load_upgrades_data()
        self.failed_upgrades = self.load_failed_upgrades()
        self.upgrade_states = self.load_upgrade_states()  # Load saved states

    def load_upgrades_data(self):
        try:
            if self.upgrades_file.exists():
                if self.upgrades_file.stat().st_size == 0:
                    logging.info(f"Upgrades file is empty, initializing new data")
                    return {}
                    
                with open(self.upgrades_file, 'rb') as f:
                    try:
                        return pickle.load(f)
                    except (EOFError, pickle.UnpicklingError) as e:
                        logging.error(f"Error loading upgrades data, file may be corrupted: {str(e)}")
                        # Backup the corrupted file
                        backup_path = str(self.upgrades_file) + '.bak'
                        try:
                            import shutil
                            shutil.copy2(self.upgrades_file, backup_path)
                            logging.info(f"Backed up corrupted upgrades file to {backup_path}")
                        except Exception as backup_err:
                            logging.error(f"Failed to backup corrupted file: {str(backup_err)}")
                        return {}
            return {}
        except Exception as e:
            logging.error(f"Unexpected error loading upgrades data: {str(e)}")
            return {}

    def save_upgrades_data(self):
        with open(self.upgrades_file, 'wb') as f:
            pickle.dump(self.upgrades_data, f)

    def load_failed_upgrades(self):
        try:
            if self.failed_upgrades_file.exists():
                if self.failed_upgrades_file.stat().st_size == 0:
                    logging.info(f"Failed upgrades file is empty, initializing new data")
                    return {}
                    
                with open(self.failed_upgrades_file, 'rb') as f:
                    try:
                        return pickle.load(f)
                    except (EOFError, pickle.UnpicklingError) as e:
                        logging.error(f"Error loading failed upgrades data, file may be corrupted: {str(e)}")
                        # Backup the corrupted file
                        backup_path = str(self.failed_upgrades_file) + '.bak'
                        try:
                            import shutil
                            shutil.copy2(self.failed_upgrades_file, backup_path)
                            logging.info(f"Backed up corrupted failed upgrades file to {backup_path}")
                        except Exception as backup_err:
                            logging.error(f"Failed to backup corrupted file: {str(backup_err)}")
                        return {}
            return {}
        except Exception as e:
            logging.error(f"Unexpected error loading failed upgrades data: {str(e)}")
            return {}

    def save_failed_upgrades(self):
        with open(self.failed_upgrades_file, 'wb') as f:
            pickle.dump(self.failed_upgrades, f)

    def load_upgrade_states(self):
        try:
            if self.upgrade_states_file.exists():
                if self.upgrade_states_file.stat().st_size == 0:
                    logging.info(f"Upgrade states file is empty, initializing new data")
                    return {}
                    
                with open(self.upgrade_states_file, 'rb') as f:
                    try:
                        return pickle.load(f)
                    except (EOFError, pickle.UnpicklingError) as e:
                        logging.error(f"Error loading upgrade states data, file may be corrupted: {str(e)}")
                        # Backup the corrupted file
                        backup_path = str(self.upgrade_states_file) + '.bak'
                        try:
                            import shutil
                            shutil.copy2(self.upgrade_states_file, backup_path)
                            logging.info(f"Backed up corrupted upgrade states file to {backup_path}")
                        except Exception as backup_err:
                            logging.error(f"Failed to backup corrupted file: {str(backup_err)}")
                        return {}
            return {}
        except Exception as e:
            logging.error(f"Unexpected error loading upgrade states data: {str(e)}")
            return {}

    def save_upgrade_states(self):
        with open(self.upgrade_states_file, 'wb') as f:
            pickle.dump(self.upgrade_states, f)

    def save_item_state(self, item: Dict[str, Any]):
        """Save complete item state before attempting an upgrade"""
        item_id = item['id']
        if item_id not in self.upgrade_states:
            self.upgrade_states[item_id] = []
        
        # Save complete item state with timestamp
        self.upgrade_states[item_id].append({
            'timestamp': datetime.now(),
            'state': item.copy()  # Save complete copy of item
        })
        self.save_upgrade_states()
        logging.info(f"Saved complete state for item {item_id} before upgrade attempt")

    def get_last_stable_state(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent stable state for an item"""
        if item_id not in self.upgrade_states or not self.upgrade_states[item_id]:
            return None
        
        return self.upgrade_states[item_id][-1]['state']

    def restore_item_state(self, item: Dict[str, Any]) -> bool:
        """Restore item to its last stable state"""
        item_id = item['id']
        last_state = self.get_last_stable_state(item_id)
        
        if not last_state:
            logging.warning(f"No previous state found for item {item_id}")
            return False

        try:
            conn = get_db_connection()
            conn.execute('BEGIN TRANSACTION')
            
            # Update all fields from the saved state
            placeholders = ', '.join(f'{k} = ?' for k in last_state.keys())
            values = list(last_state.values())
            
            query = f'''
                UPDATE media_items
                SET {placeholders}
                WHERE id = ?
            '''
            values.append(item_id)
            
            conn.execute(query, values)
            conn.commit()
            
            # Remove the used state from history
            if self.upgrade_states[item_id]:
                self.upgrade_states[item_id].pop()
                self.save_upgrade_states()
            
            logging.info(f"Successfully restored previous state for item {item_id}")
            return True
            
        except Exception as e:
            conn.rollback()
            logging.error(f"Failed to restore previous state for item {item_id}: {str(e)}")
            return False
        finally:
            conn.close()

    def add_failed_upgrade(self, item_id: str, result_info: Dict[str, Any]):
        if item_id not in self.failed_upgrades:
            self.failed_upgrades[item_id] = []
        
        # Add the failed upgrade info with timestamp
        self.failed_upgrades[item_id].append({
            'title': result_info.get('title'),
            'magnet': result_info.get('magnet'),
            'timestamp': datetime.now(),
            'reason': 'no_progress'
        })
        self.save_failed_upgrades()

    def revert_failed_upgrade(self, item: Dict[str, Any]):
        """Revert an item back to its previous state when an upgrade fails"""
        logging.info(f"Reverting failed upgrade for item {self.generate_identifier(item)}")
        
        # Get the previous file information
        upgrading_from = item.get('upgrading_from')
        upgrading_from_torrent_id = item.get('upgrading_from_torrent_id')
        
        if upgrading_from:
            # Update the database to revert the upgrade
            conn = get_db_connection()
            try:
                conn.execute('BEGIN TRANSACTION')
                conn.execute('''
                    UPDATE media_items
                    SET filled_by_file = ?,
                        filled_by_torrent_id = ?,
                        upgrading_from = NULL,
                        upgrading_from_torrent_id = NULL,
                        state = 'Upgrading',
                        last_updated = ?
                    WHERE id = ?
                ''', (
                    upgrading_from,
                    upgrading_from_torrent_id,
                    datetime.now(),
                    item['id']
                ))
                conn.commit()
                logging.info(f"Successfully reverted upgrade for item {self.generate_identifier(item)}")
            except Exception as e:
                conn.rollback()
                logging.error(f"Failed to revert upgrade: {str(e)}")
            finally:
                conn.close()
        else:
            logging.warning(f"No previous version found for item {self.generate_identifier(item)}")

    def update(self):
        from database import get_all_media_items
        self.items = [dict(row) for row in get_all_media_items(state="Upgrading")]
        for item in self.items:
            if item['id'] not in self.upgrade_times:
                collected_at = item.get('original_collected_at', datetime.now())
                self.upgrade_times[item['id']] = {
                    'start_time': datetime.now(),
                    'time_added': collected_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(collected_at, datetime) else str(collected_at)
                }

    def get_contents(self):
        contents = []
        for item in self.items:
            item_copy = item.copy()
            upgrade_info = self.upgrade_times.get(item['id'])
            if upgrade_info:
                item_copy['time_added'] = upgrade_info['time_added']
            else:
                item_copy['time_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Add upgrade history information
            item_copy['upgrades_found'] = self.upgrades_data.get(item['id'], {}).get('count', 0)
            item_copy['upgrade_history'] = self.upgrades_data.get(item['id'], {}).get('history', [])
            
            contents.append(item_copy)
        return contents

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        collected_at = item.get('original_collected_at', datetime.now())
        logging.info(f"collected_at: {collected_at}")
        self.upgrade_times[item['id']] = {
            'start_time': datetime.now(),
            'time_added': collected_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(collected_at, datetime) else str(collected_at)
        }
        self.last_scrape_times[item['id']] = datetime.now()
        self.upgrades_found[item['id']] = 0  # Initialize upgrades found count
        
        # Ensure the upgrades_data entry is initialized
        if item['id'] not in self.upgrades_data:
            self.upgrades_data[item['id']] = {'count': 0, 'history': []}
        
        self.save_upgrades_data()

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.upgrade_times:
            del self.upgrade_times[item['id']]
        if item['id'] in self.last_scrape_times:
            del self.last_scrape_times[item['id']]
        if item['id'] in self.upgrades_found:
            del self.upgrades_found[item['id']]
        if item['id'] in self.upgrades_data:
            del self.upgrades_data[item['id']]
            self.save_upgrades_data()

    def clean_up_upgrade_times(self):
        for item_id in list(self.upgrade_times.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.upgrade_times[item_id]
                if item_id in self.last_scrape_times:
                    del self.last_scrape_times[item_id]
                logging.debug(f"Cleaned up upgrade time for item ID: {item_id}")
        for item_id in list(self.upgrades_found.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.upgrades_found[item_id]
        for item_id in list(self.upgrades_data.keys()):
            if item_id not in [item['id'] for item in self.items]:
                del self.upgrades_data[item_id]
        self.save_upgrades_data()

    def process(self, queue_manager=None):
        current_time = datetime.now()
        for item in self.items[:]:  # Create a copy of the list to iterate over
            try:
                item_id = item['id']
                upgrade_info = self.upgrade_times.get(item_id)
                
                if upgrade_info:
                    collected_at = datetime.fromisoformat(item['original_collected_at']) if isinstance(item['original_collected_at'], str) else item['original_collected_at']
                    time_in_queue = current_time - collected_at
                    
                    logging.info(f"Item {item_id} has been in the Upgrading queue for {time_in_queue}.")

                    # Get the configured duration from settings, default to 24 hours if blank or invalid
                    try:
                        setting_value = get_setting('Debug', 'upgrade_queue_duration_hours', '24')
                        queue_duration_hours = int(setting_value) if setting_value.strip() else 24
                    except (ValueError, AttributeError):
                        queue_duration_hours = 24
                    max_duration = timedelta(hours=queue_duration_hours)

                    # Perform the hourly scrape if due
                    if self.should_perform_hourly_scrape(item_id, current_time):
                        logging.info(f"Performing hourly scrape for item {item_id} which has been in queue for {time_in_queue}.")
                        self.hourly_scrape(item, queue_manager) # This might remove the item if upgraded
                        self.last_scrape_times[item_id] = current_time

                        # Nested Check: After scrape, check if item still exists AND has timed out
                        if any(i['id'] == item_id for i in self.items):
                            if time_in_queue > max_duration:
                                logging.info(f"Item {item_id} timed out after scrape attempt (in queue > {queue_duration_hours} hours).")
                                # Remove the item due to timeout
                                self.remove_item(item)
                                from database import update_media_item_state
                                update_media_item_state(item_id, state="Collected")
                                logging.info(f"Moved item {item_id} to Collected state due to timeout.")
                            # else: 
                                # Optional: Log if item survived scrape and hasn't timed out
                                # logging.debug(f"Item {item_id} survived scrape and has not timed out.")
                        else:
                            # Item was removed during the scrape (upgraded)
                            logging.info(f"Item {item_id} was removed during hourly scrape (likely upgraded). Skipping timeout check.")
                    else:
                        # This case is unlikely given the hourly task execution, but handles it
                        logging.debug(f"Skipping scrape for item {item_id} - not time yet.")

            except Exception as e:
                logging.error(f"Error processing item {item.get('id', 'unknown')}: {str(e)}")
                logging.exception("Traceback:")

        # Clean up upgrade times for items no longer in the queue
        self.clean_up_upgrade_times()

    def should_perform_hourly_scrape(self, item_id: str, current_time: datetime) -> bool:
        #return True
        last_scrape_time = self.last_scrape_times.get(item_id)
        if last_scrape_time is None:
            logging.info(f"Item {item_id} has never been scraped before, running first scrape")
            return True
            
        time_since_last_scrape = current_time - last_scrape_time
        should_run = time_since_last_scrape >= timedelta(hours=1)
        
        if should_run:
            logging.info(f"Running scrape for item {item_id} - Last scrape was {time_since_last_scrape} ago")
        else:
            logging.info(f"Skipping scrape for item {item_id} - Only {time_since_last_scrape} since last scrape, waiting for 1 hour")
            
        return should_run

    def log_upgrade(self, item: Dict[str, Any], adding_queue: AddingQueue):
        # Get db_content directory from environment variable with fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        log_file = os.path.join(db_content_dir, "upgrades.log")
        item_identifier = self.generate_identifier(item)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_file = adding_queue.get_new_item_values(item)
        log_entry = f"{timestamp} - Upgraded: {item_identifier} - New File: {new_file['filled_by_file']} - Original File: {item['upgrading_from']}\n"

        # Create the log file if it doesn't exist
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        if not os.path.exists(log_file):
            open(log_file, 'w').close()

        # Append the log entry to the file
        with open(log_file, 'a') as f:
            f.write(log_entry)

        # Update upgrades_data
        if item['id'] not in self.upgrades_data:
            self.upgrades_data[item['id']] = {'count': 0, 'history': []}
        
        self.upgrades_data[item['id']]['count'] += 1
        self.upgrades_data[item['id']]['history'].append({
            'datetime': datetime.now(),
            'new_file': item['filled_by_file'],
            'original_file': item['upgrading_from']
        })
        self.save_upgrades_data()

    def log_failed_upgrade(self, item: Dict[str, Any], target_title: str, reason: str):
        """Log a failed upgrade attempt to the upgrades log"""
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        log_file = os.path.join(db_content_dir, "upgrades.log")
        item_identifier = self.generate_identifier(item)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} - Failed Upgrade: {item_identifier} - Target: {target_title} - Reason: {reason}\n"

        # Create the log file if it doesn't exist
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        if not os.path.exists(log_file):
            open(log_file, 'w').close()

        # Append the log entry to the file
        with open(log_file, 'a') as f:
            f.write(log_entry)

    def hourly_scrape(self, item: Dict[str, Any], queue_manager=None):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Starting hourly scrape for {item_identifier}")

        # *** Assume item['current_score'] is fetched from DB when item is loaded ***
        # If it might be missing/NULL, provide a default
        current_score_from_db = item.get('current_score', 0)
        if current_score_from_db is None: # Handle potential NULL from DB
             current_score_from_db = 0
        logging.info(f"[{item_identifier}] Current score from DB: {current_score_from_db:.2f}")


        update_media_item(item['id'], upgrading=True)

        # Determine if the current item is a multi-pack using PTT parser
        is_multi_pack = False # Default to false
        current_title_original = item.get('original_scraped_torrent_title')
        current_title_fallback_file = item.get('filled_by_file')
        current_title_for_similarity = None # Use this only for similarity check, not score

        if current_title_original:
            current_title_for_similarity = current_title_original
            logging.info(f"Using original_scraped_torrent_title for similarity check: {current_title_for_similarity}")
        elif current_title_fallback_file:
            current_title_for_similarity = current_title_fallback_file
            logging.warning(f"No original_scraped_torrent_title found, using filled_by_file for similarity check: {current_title_for_similarity}")
        else:
             logging.error(f"No current title found for item {item_identifier}, cannot perform similarity check accurately.")
             # Proceed without similarity title if needed, or handle error

        # Get unfiltered results first
        logging.info(f"[{item_identifier}] Calling scrape_with_fallback with is_multi_pack={is_multi_pack} to get results")
        # Use skip_filter=False here - we want the scraper's default filtering initially
        results, filtered_out = self.scraping_queue.scrape_with_fallback(item, is_multi_pack, queue_manager or self, skip_filter=False)

        if not results:
             logging.info(f"No results returned from scrape_with_fallback for {item_identifier}")
             # Potentially reset upgrading flag if no results consistently? Or just wait.
             # update_media_item(item['id'], upgrading=False) # Optional: Reset if no results?
             return

        # --- Start Filtering ---

        # Get similarity threshold from settings, default to 95%
        similarity_threshold = 0.95 # Default, consider making configurable if not already indirectly
        try:
            # Note: This threshold seems high (0.95), maybe meant to be lower?
            # Re-using upgrading_percentage_threshold name, but it's for title similarity here.
            # Let's clarify the setting name or use a different one if needed.
            # Assuming 'upgrading_percentage_threshold' IS for score diff, and 0.95 is hardcoded/intended for title similarity.
            # If 0.95 is meant for score diff, the logic below needs adjustment.
            # If 'upgrading_percentage_threshold' is for title similarity, rename setting variable.
            # --> Let's assume similarity_threshold = 0.95 is for TITLE similarity <--

            # Get SCORE percentage threshold
            threshold_value = get_setting('Scraping', 'upgrading_percentage_threshold', '0.1')
            upgrading_score_percentage_threshold = float(threshold_value) if threshold_value.strip() else 0.1
        except (ValueError, AttributeError):
            logging.warning("Invalid upgrading_percentage_threshold setting, using default value of 0.1 for score increase.")
            upgrading_score_percentage_threshold = 0.1

        # Apply filtering: not wanted, failed upgrades
        filtered_results = []
        failed_upgrades = self.failed_upgrades.get(item['id'], [])
        failed_magnets = {fu['magnet'] for fu in failed_upgrades}

        for result in results:
            # 1. Check Not Wanted (unless disabled)
            if not item.get('disable_not_wanted_check'):
                if is_magnet_not_wanted(result['magnet']):
                    logging.info(f"Result '{result.get('title', 'N/A')}' filtered out by not_wanted_magnets check")
                    continue
                if is_url_not_wanted(result['magnet']): # Assuming magnet field might contain URL for some reason? Or separate field? Adapt if needed.
                    logging.info(f"Result '{result.get('title', 'N/A')}' filtered out by not_wanted_urls check")
                    continue

            # 2. Check Failed Upgrades
            if result.get('magnet') in failed_magnets:
                 logging.info(f"Result '{result.get('title', 'N/A')}' filtered out as a previously failed upgrade attempt.")
                 continue

            # 3. Check Title Similarity (if we have a title to compare against)
            # This prevents replacing with something that has the same name but might be slightly different release/encoding if scores are close
            if current_title_for_similarity:
                similarity = SequenceMatcher(None, current_title_for_similarity.lower(), result.get('title', '').lower()).ratio()
                if similarity >= similarity_threshold:
                    logging.info(f"Result '{result.get('title', 'N/A')}' filtered out due to high title similarity ({similarity:.2%}) to current item.")
                    continue

            # If passed all filters, add to list
            filtered_results.append(result)

        if not filtered_results:
            logging.info(f"All results were filtered out for {item_identifier}")
            # update_media_item(item['id'], upgrading=False) # Optional: Reset if no results pass filters?
            return

        # --- Find Best Upgrade Candidate ---

        logging.info(f"[{item_identifier}] Comparing {len(filtered_results)} filtered results against current score {current_score_from_db:.2f}")

        better_results = []
        for result in filtered_results:
            result_score = result.get('score_breakdown', {}).get('total_score', 0)

            # Check if the result score is actually better than the stored score
            is_better_score = False
            if result_score > current_score_from_db:
                if current_score_from_db <= 0:
                    # Any positive score is better than non-positive
                    is_better_score = True
                    logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) is better than non-positive current score ({current_score_from_db:.2f}).")
                else:
                    # Check percentage increase threshold for positive scores
                    score_increase_percent = (result_score - current_score_from_db) / current_score_from_db
                    if score_increase_percent > upgrading_score_percentage_threshold:
                        is_better_score = True
                        logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) meets score threshold ({score_increase_percent:+.2%} > {upgrading_score_percentage_threshold:.2%}) compared to current ({current_score_from_db:.2f}).")
                    else:
                        logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) score increase ({score_increase_percent:+.2%}) does NOT meet threshold ({upgrading_score_percentage_threshold:.2%}) compared to current ({current_score_from_db:.2f}).")

            if is_better_score:
                better_results.append(result)
            else:
                # Log why it wasn't considered better if score wasn't higher
                if result_score <= current_score_from_db:
                     logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) score is not higher than current ({current_score_from_db:.2f}).")


        # Sort better_results by score descending to pick the best
        better_results.sort(key=lambda r: r.get('score_breakdown', {}).get('total_score', 0), reverse=True)

        if better_results:
            best_result = better_results[0]
            best_score = best_result.get('score_breakdown', {}).get('total_score', 0)
            logging.info(f"Found {len(better_results)} potential upgrade(s) for {item_identifier}.")
            logging.info(f"Best candidate: '{best_result.get('title', 'N/A')}' with score {best_score:.2f} (Current score: {current_score_from_db:.2f})")

            # --- Proceed with Upgrade Attempt ---
            self.save_item_state(item) # Save state before attempting

            logging.info(f"[{item_identifier}] Updating item state to Adding with best result title: {best_result.get('title', 'N/A')}")
            from database import update_media_item_state, get_media_item_by_id

            # Prepare update data - include the new score!
            update_data = {
                'state': 'Adding',
                'filled_by_title': best_result.get('title'),
                'scrape_results': better_results, # Store candidates
                'upgrading_from': item['filled_by_file'],
                # Store the score that triggered the upgrade attempt
                # Note: This score might not be persisted if the adding fails,
                # but it's useful for the AddingQueue logic.
                # The final score update happens in update_item_with_upgrade upon success.
                # Let's add 'potential_upgrade_score' to scrape_results or similar if needed by AddingQueue
            }
            # We might want to pass the best_result score explicitly if AddingQueue needs it immediately
            # For now, assume AddingQueue recalculates or uses scrape_results

            update_media_item_state(item['id'], **update_data)
            updated_item = get_media_item_by_id(item['id']) # Reload item with updated state

            # Use AddingQueue to attempt the upgrade with updated item
            adding_queue = AddingQueue()
            logging.info(f"[{item_identifier}] Adding item to adding queue for upgrade attempt")
            adding_queue.add_item(updated_item) # Pass the reloaded item

            lock_acquired = False
            try:
                if queue_manager and hasattr(queue_manager, 'upgrade_process_locks'):
                    queue_manager.upgrade_process_locks.add(updated_item['id'])
                    lock_acquired = True
                    logging.debug(f"[{item_identifier}] Added lock for upgrade process: {updated_item['id']}")
                else:
                     logging.warning(f"[{item_identifier}] Could not acquire upgrade lock - QueueManager or lock set missing.")

                logging.info(f"[{item_identifier}] Processing adding queue for upgrade attempt")
                adding_queue.process(queue_manager, ignore_upgrade_lock=True) # Synchronous call

            finally:
                if lock_acquired and queue_manager and hasattr(queue_manager, 'upgrade_process_locks'):
                     queue_manager.upgrade_process_locks.discard(updated_item['id'])
                     logging.debug(f"[{item_identifier}] Removed lock for upgrade process: {updated_item['id']}")

            # Check final state after AddingQueue processing
            from database.core import get_db_connection
            conn = get_db_connection()
            cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item['id'],))
            current_state_after_add = cursor.fetchone()['state']
            conn.close()

            if current_state_after_add == 'Checking':
                logging.info(f"Successfully initiated upgrade for item {item_identifier}. Item moved to Checking.")

                # Update item data with the successful upgrade details, including the NEW score
                self.update_item_with_upgrade(item, adding_queue, best_result) # Pass best_result to get score

                # Log success, record tracking etc. (combine logic from original code)
                self.log_upgrade(item, adding_queue) # Needs updated item dict after update_item_with_upgrade?
                # Record tracking based on best_result
                hash_value = extract_hash_from_magnet(best_result.get('magnet')) if best_result.get('magnet') else None
                if hash_value:
                     # Simplified - use existing item data merged with best_result details
                    tracking_item_data = {**item, 'version': best_result.get('version'), 'state': 'Checking'}
                    history = get_torrent_history(hash_value)
                    trigger_details = {
                        'source': 'upgrading_queue',
                        'queue_initiated': True,
                        'upgrade_check': True,
                        'current_version': item.get('version'),
                        'target_version': best_result.get('version'),
                        'score_improvement': best_score - current_score_from_db # Calculate diff
                    }
                    rationale = f"Upgrading from version {item.get('version')} (score {current_score_from_db:.2f}) to {best_result.get('version')} (score {best_score:.2f})"

                    if history:
                        update_torrent_tracking(
                            torrent_hash=hash_value, item_data=tracking_item_data,
                            trigger_details=trigger_details, trigger_source='queue_upgrade', rationale=rationale
                        )
                        logging.info(f"[{item_identifier}] Updated torrent tracking for hash {hash_value}")
                    else:
                        try:
                            record_torrent_addition(
                                torrent_hash=hash_value, trigger_source="queue_upgrade",
                                trigger_details={**trigger_details, 'selected_files': best_result.get('files')},
                                rationale=rationale, item_data=tracking_item_data # Use combined data
                            )
                            logging.info(f"Recorded upgrade torrent addition for {item['title']}.")
                        except Exception as e:
                            logging.error(f"Error recording upgrade torrent addition for {item['title']}: {e}", exc_info=True)


                # Update internal tracking data
                if item['id'] not in self.upgrades_data:
                    self.upgrades_data[item['id']] = {'count': 0, 'history': []}
                self.upgrades_data[item['id']]['count'] += 1
                # History logging is inside log_upgrade, which itself calls save_upgrades_data

                # Remove item from this queue as it's now handled by CheckingQueue
                logging.info(f"[{item_identifier}] Removing item from upgrading queue after successful upgrade initiation.")
                self.remove_item(item)

            else:
                logging.warning(f"Failed to upgrade item {item_identifier} - state after AddingQueue process: {current_state_after_add}")
                from routes.notifications import send_upgrade_failed_notification
                notification_data = {
                    'title': item.get('title', 'Unknown Title'),
                    'year': item.get('year', ''),
                    'reason': f'Failed in AddingQueue (State: {current_state_after_add})'
                }
                send_upgrade_failed_notification(notification_data)

                self.log_failed_upgrade(item, best_result.get('title', 'N/A'), f'Failed in AddingQueue (State: {current_state_after_add})')

                # Restore complete previous state
                if self.restore_item_state(item):
                    # Track the failed upgrade attempt
                    self.add_failed_upgrade(item['id'], best_result) # Log the one we tried
                    logging.info(f"Restored previous state and added to failed upgrades list for {item_identifier}")
                    # Item remains in Upgrading queue, but state reset in DB
                    # We might need to update the 'upgrading' flag back to False if restore_item_state doesn't
                    update_media_item(item['id'], upgrading=False)
                else:
                    logging.error(f"Failed to restore previous state for {item_identifier}, manual intervention may be needed")
                    # Item might be stuck, consider moving to a failed state?

        else:
            logging.info(f"No better results found for {item_identifier} based on current score {current_score_from_db:.2f} and thresholds.")
            # Reset upgrading flag if nothing found?
            update_media_item(item['id'], upgrading=False)


    def update_item_with_upgrade(self, item: Dict[str, Any], adding_queue: AddingQueue, best_result: Dict[str, Any]):
        """Updates the database item after a successful upgrade initiation (moved to Checking)."""
        new_values = adding_queue.get_new_item_values(item) # Get details from AddingQueue's perspective (e.g., selected files)
        new_score = best_result.get('score_breakdown', {}).get('total_score', 0) # Get score from the chosen result

        if new_values:
            conn = get_db_connection()
            try:
                conn.execute('BEGIN TRANSACTION')

                upgrading_from = item['filled_by_file']
                upgrading_from_version = item.get('version')
                clean_version = new_values.get('version', '').strip('*') if new_values.get('version') else best_result.get('version', '').strip('*')

                # Update the item in the database including the new score
                conn.execute('''
                    UPDATE media_items
                    SET upgrading_from = ?,
                        filled_by_file = ?,
                        filled_by_magnet = ?,
                        version = ?,
                        current_score = ?,  -- Update the score
                        last_updated = ?,
                        state = ?,
                        upgrading_from_torrent_id = ?,
                        upgraded = 1,
                        upgrading_from_version = ?,
                        upgrading = 0 -- Reset upgrading flag as it's now Checking
                    WHERE id = ?
                ''', (
                    upgrading_from,
                    new_values.get('filled_by_file'),
                    new_values.get('filled_by_magnet'),
                    clean_version,
                    new_score, # Store the new score
                    datetime.now(),
                    'Checking', # State confirmed by caller
                    item['filled_by_torrent_id'], # Old torrent ID
                    upgrading_from_version, # Old version
                    item['id']
                ))

                conn.commit()
                logging.info(f"Updated item in database with new values (including score {new_score:.2f}) for {self.generate_identifier(item)}")

                # Update the local item dictionary (important if used further)
                item['upgrading_from'] = upgrading_from
                item['filled_by_file'] = new_values.get('filled_by_file')
                item['filled_by_magnet'] = new_values.get('filled_by_magnet')
                item['upgrading_from_torrent_id'] = item.get('filled_by_torrent_id') # Store old ID
                item['version'] = clean_version
                item['current_score'] = new_score # Update local score
                item['last_updated'] = datetime.now()
                item['state'] = 'Checking'
                item['upgrading'] = 0 # Sync with DB

                # Send notification logic
                try:
                    # Import dynamically to avoid circular dependencies at module level if any
                    from routes.notifications import get_enabled_notifications, send_notifications
                    enabled_notifications = get_enabled_notifications()
                    if enabled_notifications:
                        # Prepare data for the notification service
                        notification_data = [{
                            'title': item['title'],
                            'year': item.get('year'),
                            'version': item['version'], # Use the new version
                            'type': item['type'],
                            'season_number': item.get('season_number'),
                            'episode_number': item.get('episode_number'),
                            'new_state': 'Checking', # Explicitly set state for notification formatting
                            'is_upgrade': True,      # Mark as an upgrade for formatting
                            # Include details if available and enabled in settings
                            'content_source': item.get('content_source'),
                            'content_source_detail': item.get('content_source_detail'),
                            'filled_by_file': item.get('filled_by_file') # Use the new file
                        }]
                        # Use 'upgrading' category to link to user's upgrade notification settings
                        send_notifications(notification_data, enabled_notifications, notification_category='upgrading')
                        logging.info(f"Sent upgrade initiation notification for {self.generate_identifier(item)}")
                    else:
                         logging.info("No enabled notifications found, skipping upgrade initiation notification.")
                except Exception as notify_exc:
                    # Log failure but don't rollback the main transaction
                    logging.error(f"Failed to send upgrade initiation notification for {self.generate_identifier(item)}: {str(notify_exc)}", exc_info=True)


            except Exception as e:
                conn.rollback()
                logging.error(f"Error updating item {self.generate_identifier(item)} after upgrade: {str(e)}", exc_info=True)
            finally:
                conn.close()
        else:
            logging.warning(f"No new values obtained from AddingQueue for item {self.generate_identifier(item)} during upgrade finalization.")

    def check_multi_pack(self, item: Dict[str, Any]) -> bool:
        if item['type'] != 'episode':
            return False

        return any(
            other_item['type'] == 'episode' and
            other_item['imdb_id'] == item['imdb_id'] and
            other_item['season_number'] == item['season_number'] and
            other_item['id'] != item['id']
            for other_item in self.items
        )

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{'_'.join(item['version'].split())}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{'_'.join(item['version'].split())}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)

    def _normalize_title_for_comparison(self, title_string: Optional[str]) -> str:
        """Normalizes a title string by replacing spaces with periods for comparison."""
        if not title_string:
            return ""
        try:
            # Only replace spaces with periods
            return title_string.replace(' ', '.')
        except Exception as e:
            # Fallback in case of unexpected error, return original (though unlikely for replace)
            logging.error(f"Unexpected error during simple title normalization: '{title_string}'. Error: {e}. Returning original string.")
            return title_string

def log_successful_upgrade(item: Dict[str, Any]):
    # Get db_content directory from environment variable with fallback
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    log_file = os.path.join(db_content_dir, "upgrades.log")
    item_identifier = UpgradingQueue.generate_identifier(item)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} - Upgrade Complete: {item_identifier}\n"

    # Create the log file if it doesn't exist
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not os.path.exists(log_file):
        open(log_file, 'w').close()

    # Append the log entry to the file
    with open(log_file, 'a') as f:
        f.write(log_entry)
