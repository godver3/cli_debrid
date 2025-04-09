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

        update_media_item(item['id'], upgrading=True)

        # Determine if the current item is a multi-pack using PTT parser
        is_multi_pack = False # Default to false
        current_title_original = item.get('original_scraped_torrent_title')
        current_title_fallback_file = item.get('filled_by_file')
        current_title = None

        if current_title_original:
            current_title = current_title_original
            logging.info(f"Using original_scraped_torrent_title for PTT check: {current_title}")
        elif current_title_fallback_file:
            current_title = current_title_fallback_file
            logging.warning(f"No original_scraped_torrent_title found, using filled_by_file for PTT check: {current_title}")

        if current_title:
            # Use the original title string for PTT parsing for better accuracy
            parsed_info = parse_with_ptt(current_title)
            if parsed_info and not parsed_info.get('parsing_error'):
                # Check if it's TV type and either multiple episodes or a season pack (seasons present, episodes absent)
                if parsed_info.get('type') == 'episode':
                    if len(parsed_info.get('episodes', [])) > 1:
                        is_multi_pack = True
                        logging.info(f"PTT indicates multi-episode pack (Episodes: {parsed_info.get('episodes')})")
                    elif parsed_info.get('seasons') and not parsed_info.get('episodes'):
                        is_multi_pack = True
                        logging.info(f"PTT indicates season pack (Seasons: {parsed_info.get('seasons')})")
                    else:
                         logging.info(f"PTT indicates single episode (Seasons: {parsed_info.get('seasons', [])}, Episodes: {parsed_info.get('episodes', [])})")
                else:
                     logging.info(f"PTT indicates type '{parsed_info.get('type')}'")
            else:
                logging.warning(f"Could not parse current title '{current_title}' with PTT, assuming not multi-pack.")
        else:
             logging.error(f"No current title found for item {item_identifier}, cannot determine if multi-pack via PTT.")
             # Cannot determine, proceed with default is_multi_pack = False

        # Get unfiltered results first to ensure we can find our current item
        logging.info(f"[{item_identifier}] Calling scrape_with_fallback with is_multi_pack={is_multi_pack} to get results")
        results, filtered_out = self.scraping_queue.scrape_with_fallback(item, is_multi_pack, queue_manager or self, skip_filter=True) # Pass determined is_multi_pack

        if results:
            # Find the position of the current item's 'filled_by_magnet' in the results
            # current_title_original = item.get('original_scraped_torrent_title') # Already fetched above
            # current_title_fallback_file = item.get('filled_by_file') # Already fetched above
            # current_title = None # Already fetched and set above
            normalized_current_title = ""

            if current_title_original:
                # current_title = current_title_original # Already set
                logging.info(f"Using original_scraped_torrent_title: {current_title}")
            elif current_title_fallback_file:
                # current_title = current_title_fallback_file # Already set
                logging.warning(f"No original_scraped_torrent_title found, using filled_by_file as fallback: {current_title}")
            else:
                logging.error(f"No original_scraped_torrent_title or filled_by_file found for item {item_identifier}, skipping upgrade check")
                return

            # Normalize the chosen current title for comparison
            normalized_current_title = self._normalize_title_for_comparison(current_title)
            logging.info(f"Normalized current title for comparison: '{normalized_current_title}'")

            # Find our current position before any filtering using normalized titles
            current_position = next((index for index, result in enumerate(results)
                                     if self._normalize_title_for_comparison(result.get('title')) == normalized_current_title), None)

            if current_position is None:
                logging.warning(f"Could not find current item '{current_title}' (normalized: '{normalized_current_title}') in initial results list. Skipping filtering step for this item.")
                # We might still proceed, but filtering/comparison logic below needs to handle this

            # Get similarity threshold from settings, default to 95%
            similarity_threshold = 0.95
            try:
                threshold_value = get_setting('Scraping', 'upgrading_percentage_threshold', '0.1')
                upgrading_percentage_threshold = float(threshold_value) if threshold_value.strip() else 0.1
            except (ValueError, AttributeError):
                logging.warning("Invalid upgrading_percentage_threshold setting, using default value of 0.1")
                upgrading_percentage_threshold = 0.1

            # Apply filtering to all results except our current item (if found)
            filtered_results = []
            for result in results:
                normalized_result_title = self._normalize_title_for_comparison(result.get('title'))

                # Skip filtering if this result matches the normalized current title (our current item)
                if normalized_current_title and normalized_result_title == normalized_current_title:
                    logging.debug(f"Identified current item (normalized match: '{normalized_current_title}'), keeping: {result.get('title')}")
                    filtered_results.append(result)
                    continue
                # Also skip if original titles match exactly (handles edge cases where normalization might fail)
                elif result.get('title') == current_title:
                    logging.debug(f"Identified current item (exact match), keeping: {result.get('title')}")
                    filtered_results.append(result)
                    continue

                if not item.get('disable_not_wanted_check'):
                    if is_magnet_not_wanted(result['magnet']):
                        logging.info(f"Result '{result['title']}' filtered out by not_wanted_magnets check")
                        continue
                    if is_url_not_wanted(result['magnet']):
                        logging.info(f"Result '{result['title']}' filtered out by not_wanted_urls check")
                        continue
                filtered_results.append(result)

            # Filter out any previously failed upgrades (except our current item)
            failed_upgrades = self.failed_upgrades.get(item['id'], [])
            failed_magnets = {fu['magnet'] for fu in failed_upgrades}
            temp_filtered_results = []
            for r in filtered_results:
                normalized_result_title = self._normalize_title_for_comparison(r.get('title'))
                # Keep the current item regardless of failed status
                if normalized_current_title and normalized_result_title == normalized_current_title:
                    temp_filtered_results.append(r)
                elif r.get('title') == current_title:
                     temp_filtered_results.append(r)
                # Filter out others if they are failed magnets
                elif r.get('magnet') not in failed_magnets:
                    temp_filtered_results.append(r)
            filtered_results = temp_filtered_results

            if not filtered_results:
                logging.info(f"All results were filtered out for {item_identifier}")
                return

            # Find our new position after filtering using normalized titles
            current_position = next((index for index, result in enumerate(filtered_results)
                                     if self._normalize_title_for_comparison(result.get('title')) == normalized_current_title), None)

            if current_position is None:
                # Try finding with exact match as a fallback
                current_position = next((index for index, result in enumerate(filtered_results) if result.get('title') == current_title), None)

            if current_position is None:
                logging.warning(f"Lost track of current item '{current_title}' (normalized: '{normalized_current_title}') after filtering.")
                # Add current item to filtered results with score 0
                current_result = {
                    'title': current_title,
                    'score_breakdown': {'total_score': 0},
                    'magnet': item.get('filled_by_magnet', ''),
                    'version': item.get('version', '')
                }
                filtered_results.append(current_result)
                current_position = len(filtered_results) - 1
                logging.info(f"Added current item to filtered results with score 0")

            # Log all results with their scores for debugging
            for index, result in enumerate(filtered_results):
                similarity = SequenceMatcher(None, current_title.lower(), result['title'].lower()).ratio()
                logging.info(f"Result {index + 1}: {result['title']}")
                logging.info(f"  Similarity: {similarity:.2%}")
                if 'score_breakdown' in result:
                    total_score = result['score_breakdown'].get('total_score', 0)
                    current_score = filtered_results[current_position]['score_breakdown'].get('total_score', 0)
                    score_diff = total_score - current_score
                    
                    # Improved logging for score comparison
                    log_comparison = f"Score: {total_score:.2f} (Difference: {score_diff:+.2f} from current {current_score:.2f})"
                    is_above_threshold = False
                    
                    if current_score > 0 and total_score > current_score:
                        # Only calculate percentage if current score is positive
                        score_increase_percent = score_diff / current_score
                        log_comparison += f" ({score_increase_percent:+.2%})"
                        if score_increase_percent > upgrading_percentage_threshold:
                            is_above_threshold = True
                    elif total_score > current_score:
                        # If current score is 0 or negative, any increase is significant
                        # We'll check against the threshold conceptually later
                        log_comparison += " (Improvement from non-positive score)"
                        # Consider it potentially above threshold if the new score itself is positive enough?
                        # For now, just note it's an improvement. The filtering logic will handle the threshold application.
                        # We might implicitly consider any jump from <=0 to >0 as meeting a threshold.
                        # Or if both are negative, any increase counts.
                        
                    logging.info(f"  {log_comparison}")

                    # Check if it meets the upgrade criteria based on the refined logic below
                    meets_upgrade_criteria = False
                    if total_score > current_score:
                        if current_score <= 0:
                             # Any increase from negative or zero is considered an upgrade for now
                             # (Assuming the score itself reflects desirability)
                            meets_upgrade_criteria = True
                        elif (score_diff / current_score) > upgrading_percentage_threshold:
                             # Standard percentage check for positive scores
                            meets_upgrade_criteria = True

                    if meets_upgrade_criteria:
                        logging.info(f"  ⬆ Meets upgrade criteria (Threshold: {upgrading_percentage_threshold:.2%})")
                        
                logging.info("  ---")

            logging.info(f"Current item {item_identifier} is at position {current_position + 1} in the filtered results")
            logging.info(f"Current item title: {current_title}")
            current_score = filtered_results[current_position]['score_breakdown'].get('total_score', 0)
            logging.info(f"Current item score: {current_score:.2f}")
            
            # Only consider results that are in higher positions AND not too similar AND meet upgrade criteria
            better_results = []
            for result in filtered_results[:current_position]:
                # Check similarity first
                if SequenceMatcher(None, current_title.lower(), result['title'].lower()).ratio() >= similarity_threshold:
                    continue # Skip if too similar

                # Check upgrade criteria (score must be higher AND meet threshold logic)
                result_score = result['score_breakdown'].get('total_score', 0)
                if result_score > current_score:
                    # Check threshold condition:
                    # Upgrade if current score is non-positive OR 
                    # if current score is positive and percentage increase exceeds threshold
                    if current_score <= 0 or \
                       ((result_score - current_score) / current_score > upgrading_percentage_threshold):
                        better_results.append(result)

            if better_results:
                logging.info(f"Found {len(better_results)} potential upgrade(s) in higher positions meeting criteria (Similarity < {similarity_threshold:.2%}, Threshold: {upgrading_percentage_threshold:.2%})")
                logging.info("Better results to try:")
                for i, result in enumerate(better_results):
                    similarity = SequenceMatcher(None, current_title.lower(), result['title'].lower()).ratio()
                    score = result['score_breakdown'].get('total_score', 0)
                    if current_score != 0:
                        score_increase = (score - current_score) / current_score
                    else:
                        score_increase = float('inf')
                    logging.info(f"  {i}: {result['title']}")
                    logging.info(f"     Similarity: {similarity:.2%}")
                    logging.info(f"     Score: {score:.2f} ({'+' if score_increase > 0 else ''}{score_increase:.2%} compared to current)")

                # Save complete item state before attempting upgrade
                self.save_item_state(item)

                # Update item with scrape results in database first
                best_result = better_results[0]

                logging.info(f"[{item_identifier}] Updating item state to Adding with best result title: {best_result['title']}")
                from database import update_media_item_state, get_media_item_by_id
                update_media_item_state(item['id'], 'Adding', 
                    filled_by_title=best_result['title'], 
                    scrape_results=better_results,
                    upgrading_from=item['filled_by_file'])
                updated_item = get_media_item_by_id(item['id'])

                # Use AddingQueue to attempt the upgrade with updated item
                adding_queue = AddingQueue()
                uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None').lower()
                logging.info(f"[{item_identifier}] Adding item to adding queue for upgrade attempt")
                adding_queue.add_item(updated_item)

                # ---> EDIT: Add lock management around process call <---
                lock_acquired = False
                try:
                    # Acquire lock
                    if queue_manager and hasattr(queue_manager, 'upgrade_process_locks'):
                        queue_manager.upgrade_process_locks.add(updated_item['id'])
                        lock_acquired = True
                        logging.debug(f"[{item_identifier}] Added lock for upgrade process: {updated_item['id']}")
                    else:
                         logging.warning(f"[{item_identifier}] Could not acquire upgrade lock - QueueManager or lock set missing.")

                    logging.info(f"[{item_identifier}] Processing adding queue for upgrade attempt")
                    # ---> EDIT: Pass ignore_upgrade_lock=True <---
                    adding_queue.process(queue_manager, ignore_upgrade_lock=True) # This is the synchronous call
                finally:
                    # Release lock if acquired
                    if lock_acquired and queue_manager and hasattr(queue_manager, 'upgrade_process_locks'):
                         queue_manager.upgrade_process_locks.discard(updated_item['id'])
                         logging.debug(f"[{item_identifier}] Removed lock for upgrade process: {updated_item['id']}")
                # ---> END EDIT <---

                # Check if the item was successfully moved to Checking queue
                from database.core import get_db_connection
                conn = get_db_connection()
                cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item['id'],))
                current_state = cursor.fetchone()['state']
                conn.close()

                if current_state == 'Checking':
                    logging.info(f"Successfully initiated upgrade for item {item_identifier}")
                    
                    # Extract hash and record tracking info
                    hash_value = None
                    if best_result.get('magnet'):
                        hash_value = extract_hash_from_magnet(best_result['magnet'])
                    
                    if hash_value:
                        # Prepare item data
                        item_data = {
                            'title': item.get('title'),
                            'type': item.get('type'),
                            'version': best_result.get('version'),
                            'tmdb_id': item.get('tmdb_id'),
                            'state': 'Checking',
                            'upgrade_from': item.get('filled_by_file')
                        }
                        
                        # Check recent history for this hash
                        history = get_torrent_history(hash_value)
                        
                        # If there's a recent entry, update it instead of creating new one
                        if history:
                            update_torrent_tracking(
                                torrent_hash=hash_value,
                                item_data=item_data,
                                trigger_details={
                                    'source': 'upgrading_queue',
                                    'queue_initiated': True,
                                    'upgrade_check': True,
                                    'current_version': item.get('version'),
                                    'target_version': best_result.get('version'),
                                    'score_improvement': best_result.get('score_breakdown', {}).get('total_score', 0)
                                },
                                trigger_source='queue_upgrade',
                                rationale=f"Upgrading from version {item.get('version')} to {best_result.get('version')}"
                            )
                            logging.info(f"[{item_identifier}] Updated existing torrent tracking entry for hash {hash_value}")
                        else:
                            # Record new addition if no history exists
                            record_torrent_addition(
                                torrent_hash=hash_value,
                                trigger_source='queue_upgrade',
                                rationale=f"Upgrading from version {item.get('version')} to {best_result.get('version')}",
                                item_data=item_data,
                                trigger_details={
                                    'source': 'upgrading_queue',
                                    'queue_initiated': True,
                                    'upgrade_check': True,
                                    'current_version': item.get('version'),
                                    'target_version': best_result.get('version'),
                                    'score_improvement': best_result.get('score_breakdown', {}).get('total_score', 0)
                                }
                            )
                            logging.info(f"[{item_identifier}] Recorded new torrent addition for hash {hash_value}")
                    
                    # Ensure the upgrades_data entry is initialized
                    if item['id'] not in self.upgrades_data:
                        self.upgrades_data[item['id']] = {'count': 0, 'history': []}
                    
                    # Increment the upgrades found count
                    self.upgrades_data[item['id']]['count'] += 1
                
                    item['upgrading_from'] = item['filled_by_file'] 

                    logging.info(f"Item {item_identifier} is upgrading from {item['upgrading_from']} (Upgrades found: {self.upgrades_data[item['id']]['count']})")

                    # Log the upgrade
                    self.log_upgrade(item, adding_queue)

                    # Update the item in the database with new values from the upgrade
                    self.update_item_with_upgrade(item, adding_queue)

                    # Remove the item from the Upgrading queue
                    logging.info(f"[{item_identifier}] Removing item from upgrading queue after successful upgrade")
                    self.remove_item(item)

                    logging.info(f"Successfully upgraded item {item_identifier} to Checking state")
                else:
                    logging.info(f"Failed to upgrade item {item_identifier} - current state: {current_state}")
                    # Send failed upgrade notification
                    from routes.notifications import send_upgrade_failed_notification
                    notification_data = {
                        'title': item.get('title', 'Unknown Title'),
                        'year': item.get('year', ''),
                        'reason': 'Failed to initiate upgrade process'
                    }
                    send_upgrade_failed_notification(notification_data)
                    
                    # Log the failed upgrade
                    self.log_failed_upgrade(item, best_result['title'], 'Failed to initiate upgrade process')
                    
                    # Restore complete previous state
                    if self.restore_item_state(item):
                        # Track the failed upgrade attempt
                        self.add_failed_upgrade(item['id'], best_result)
                        logging.info(f"Restored previous state and added to failed upgrades list for {item_identifier}")
                    else:
                        logging.error(f"Failed to restore previous state for {item_identifier}, manual intervention may be needed")
            else:
                logging.info(f"No better results found for {item_identifier}")
        else:
            logging.info(f"No new results found for {item_identifier} during hourly scrape")

    def update_item_with_upgrade(self, item: Dict[str, Any], adding_queue: AddingQueue):
        # Fetch the new values from the adding queue
        new_values = adding_queue.get_new_item_values(item)

        if new_values:
            # Begin a transaction
            conn = get_db_connection()
            try:
                conn.execute('BEGIN TRANSACTION')

                # Set upgrading_from to the current filled_by_file before updating
                upgrading_from = item['filled_by_file']

                # Ensure the new version string is free of asterisks before storing
                clean_version = new_values['version'].strip('*') if new_values.get('version') else None

                # Store the current version before upgrading
                upgrading_from_version = item.get('version')

                # Update the item in the database with new values
                conn.execute('''
                    UPDATE media_items
                    SET upgrading_from = ?, filled_by_file = ?, filled_by_magnet = ?, version = ?, last_updated = ?, state = ?, upgrading_from_torrent_id = ?, upgraded = 1, upgrading_from_version = ?
                    WHERE id = ?
                ''', (
                    upgrading_from,
                    new_values['filled_by_file'],
                    new_values['filled_by_magnet'],
                    clean_version, # Use the cleaned version
                    datetime.now(),
                    'Checking',
                    item['filled_by_torrent_id'], # This is the OLD torrent ID being replaced
                    upgrading_from_version, # Store the old version
                    item['id']
                ))

                conn.commit()
                logging.info(f"Updated item in database with new values for {self.generate_identifier(item)}")

                # Update the item dictionary as well
                item['upgrading_from'] = upgrading_from
                item['filled_by_file'] = new_values['filled_by_file']
                item['filled_by_magnet'] = new_values['filled_by_magnet']
                item['upgrading_from_torrent_id'] = item['filled_by_torrent_id']
                item['version'] = clean_version # Update item dict with cleaned version
                item['last_updated'] = datetime.now()
                item['state'] = 'Checking'

                # Send notification for the upgrade
                try:
                    from routes.notifications import send_notifications
                    from routes.settings_routes import get_enabled_notifications_for_category
                    from routes.extensions import app

                    with app.app_context():
                        response = get_enabled_notifications_for_category('upgrading')
                        if response.json['success']:
                            enabled_notifications = response.json['enabled_notifications']
                            if enabled_notifications:
                                notification_data = {
                                    'id': item['id'],
                                    'title': item.get('title', 'Unknown Title'),
                                    'type': item.get('type', 'unknown'),
                                    'year': item.get('year', ''),
                                    'version': item.get('version', ''),
                                    'season_number': item.get('season_number'),
                                    'episode_number': item.get('episode_number'),
                                    'new_state': 'Upgrading',
                                    'is_upgrade': True,
                                    'upgrading_from': upgrading_from
                                }
                                send_notifications([notification_data], enabled_notifications, notification_category='collected')
                                logging.debug(f"Sent upgrade notification for item {item['id']}")
                except Exception as e:
                    logging.error(f"Failed to send upgrade notification: {str(e)}")

            except Exception as e:
                conn.rollback()
                logging.error(f"Error updating item {self.generate_identifier(item)}: {str(e)}", exc_info=True)
            finally:
                conn.close()
        else:
            logging.warning(f"No new values obtained for item {self.generate_identifier(item)}")

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
