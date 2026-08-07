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
        self.upgrade_states_file = Path(db_content_dir) / "upgrade_states.pkl"
        self.upgrade_times_file = Path(db_content_dir) / "upgrade_times.pkl"
        self.upgrades_data = self.load_upgrades_data()
        self.failed_upgrades = self.load_failed_upgrades()
        self.upgrade_states = self.load_upgrade_states()
        self.upgrade_times = self.load_upgrade_times()
        self.currently_processing_item_id: Optional[str] = None
        # Track last run date for the daily delayed-upgrade pass
        self._last_delayed_upgrade_run_date = None

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
        try:
            with open(self.upgrade_states_file, 'wb') as f:
                pickle.dump(self.upgrade_states, f)
        except (IOError, pickle.PicklingError, EOFError) as e:
            logging.error(f"Failed to save upgrade states to {self.upgrade_states_file}: {str(e)}", exc_info=True)

    def load_upgrade_times(self):
        """Load persisted upgrade_times from disk so start_time survives restarts."""
        try:
            if self.upgrade_times_file.exists() and self.upgrade_times_file.stat().st_size > 0:
                with open(self.upgrade_times_file, 'rb') as f:
                    return pickle.load(f)
        except (EOFError, pickle.UnpicklingError, Exception) as e:
            logging.error(f"Error loading upgrade_times, starting fresh: {e}")
        return {}

    def save_upgrade_times(self):
        """Persist upgrade_times to disk so start_time survives restarts."""
        try:
            with open(self.upgrade_times_file, 'wb') as f:
                pickle.dump(self.upgrade_times, f)
        except (IOError, pickle.PicklingError, EOFError) as e:
            logging.error(f"Failed to save upgrade_times: {str(e)}", exc_info=True)

    def save_item_state(self, item: Dict[str, Any]):
        """Save complete item state before attempting an upgrade"""
        item_id = item['id']
        if item_id not in self.upgrade_states:
            self.upgrade_states[item_id] = []

        # Save complete item state with timestamp
        current_state_copy = item.copy() # Ensure we are saving a distinct copy
        self.upgrade_states[item_id].append({
            'timestamp': datetime.now(),
            'state': current_state_copy
        })

        # --- Call the save function ---
        self.save_upgrade_states()
        # --- Log SUCCESS *after* saving ---
        logging.info(f"Saved state snapshot for item {item_id} (Total states stored for item: {len(self.upgrade_states[item_id])})")

    def get_last_stable_state(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent stable state for an item"""
        if item_id not in self.upgrade_states or not self.upgrade_states[item_id]:
            return None
        
        return self.upgrade_states[item_id][-1]['state']

    def restore_item_state(self, item: Dict[str, Any]) -> bool:
        """Restore item to its last stable state"""
        item_id = item['id']
        last_state_entry = None # Initialize

        # --- Enhanced Check ---
        if item_id not in self.upgrade_states or not self.upgrade_states[item_id]:
            logging.warning(f"No previous state found for item {item_id} in upgrade_states dictionary.")
            return False
        # --- End Enhanced Check ---

        # Get the state entry (dictionary containing timestamp and state)
        last_state_entry = self.upgrade_states[item_id][-1]
        last_state = last_state_entry.get('state') # Extract the actual state dictionary

        if not last_state:
            logging.error(f"Found state entry for item {item_id}, but the 'state' key is missing or empty. Cannot restore.")
            # Optional: Maybe remove the corrupted entry?
            # self.upgrade_states[item_id].pop()
            # self.save_upgrade_states()
            return False

        conn = None
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
            
            # Remove the used state from history *after* successful DB commit
            if self.upgrade_states.get(item_id): # Check if key still exists
                try:
                    self.upgrade_states[item_id].pop()
                    logging.info(f"Popped used state for item {item_id}. Remaining states: {len(self.upgrade_states[item_id])}")
                    self.save_upgrade_states() # Save after popping
                except IndexError:
                     logging.warning(f"Attempted to pop state for item {item_id}, but the list was already empty (concurrent modification?).")

            logging.info(f"Successfully restored previous state for item {item_id} from snapshot taken at {last_state_entry.get('timestamp')}")
            return True
            
        except Exception as e:
            conn.rollback()
            logging.error(f"Failed to restore previous state for item {item_id} using snapshot from {last_state_entry.get('timestamp') if last_state_entry else 'N/A'}: {str(e)}")
            return False
        finally:
            if conn: # Ensure conn exists before closing
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
        # Sort hub pre-seeded items first (they have a magnet ready — no scraping needed)
        try:
            from database.zilean_upgrade import _queued_magnets, _ensure_cache_initialized
            _ensure_cache_initialized()
            self.items.sort(key=lambda x: 0 if x['id'] in _queued_magnets else 1)
        except Exception:
            pass
        changed = False
        for item in self.items:
            if item['id'] not in self.upgrade_times:
                # Use last_updated (when item was moved to Upgrading) as the queue-entry
                # timestamp. Captured here on first registration and persisted to disk so
                # it survives restarts without relying on the DB field (which gets
                # overwritten by hourly update_media_item() calls).
                _last_updated = item.get('last_updated')
                if _last_updated:
                    start_time = datetime.fromisoformat(_last_updated) if isinstance(_last_updated, str) else _last_updated
                else:
                    start_time = datetime.now()
                self.upgrade_times[item['id']] = {
                    'start_time': start_time,
                    'time_added': start_time.strftime('%Y-%m-%d %H:%M:%S')
                }
                changed = True
        if changed:
            self.save_upgrade_times()

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
        # Capture last_updated as the queue-entry timestamp before any subsequent
        # update_media_item() calls reset it during hourly scrapes.
        _last_updated = item.get('last_updated')
        if _last_updated:
            start_time = datetime.fromisoformat(_last_updated) if isinstance(_last_updated, str) else _last_updated
        else:
            start_time = datetime.now()
        logging.info(f"upgrading queue start_time: {start_time}")
        self.upgrade_times[item['id']] = {
            'start_time': start_time,
            'time_added': start_time.strftime('%Y-%m-%d %H:%M:%S')
        }
        self.save_upgrade_times()
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
        active_ids = {item['id'] for item in self.items}
        changed = False
        for item_id in list(self.upgrade_times.keys()):
            if item_id not in active_ids:
                del self.upgrade_times[item_id]
                if item_id in self.last_scrape_times:
                    del self.last_scrape_times[item_id]
                logging.debug(f"Cleaned up upgrade time for item ID: {item_id}")
                changed = True
        if changed:
            self.save_upgrade_times()
        for item_id in list(self.upgrades_found.keys()):
            if item_id not in active_ids:
                del self.upgrades_found[item_id]
        for item_id in list(self.upgrades_data.keys()):
            if item_id not in active_ids:
                del self.upgrades_data[item_id]
        self.save_upgrades_data()

    def process(self, queue_manager=None):
        current_time = datetime.now()
        # Run delayed upgrade scrape once per day based on setting Scraping.delayed_upgrade_scrape_days
        try:
            days_setting = get_setting('Scraping', 'delayed_upgrade_scrape_days', '0')
            delayed_days = int(days_setting) if str(days_setting).strip() else 0
        except Exception:
            delayed_days = 0
        if delayed_days > 0:
            today = current_time.date()
            if self._last_delayed_upgrade_run_date != today:
                try:
                    self._run_daily_delayed_upgrade_scrape(delayed_days)
                finally:
                    self._last_delayed_upgrade_run_date = today
        for item in self.items[:]:  # Create a copy of the list to iterate over
            try:
                item_id = item['id']

                # Eject items that are no longer in Upgrading state in the DB
                # (e.g. Blacklisted, Collected, or otherwise changed externally)
                try:
                    from database.core import get_db_connection as _get_conn
                    _chk = _get_conn()
                    _row = _chk.execute("SELECT state FROM media_items WHERE id = ?", (item_id,)).fetchone()
                    _chk.close()
                    if _row and _row['state'] != 'Upgrading':
                        logging.warning(f"[UpgradingQueue] Item {item_id} has state '{_row['state']}' in DB — removing from Upgrading queue.")
                        self.remove_item(item)
                        continue
                except Exception as _chk_err:
                    logging.debug(f"[UpgradingQueue] State check failed for item {item_id}: {_chk_err}")

                upgrade_info = self.upgrade_times.get(item_id)

                # upgrade_times is now persisted to disk, so a missing entry here means
                # the pkl was lost or this is a brand-new item that somehow bypassed
                # add_item/update. Use current_time as a safe fallback — the item gets
                # a fresh window which is acceptable for this rare edge case.
                if not upgrade_info:
                    logging.warning(f"[UpgradingQueue] No upgrade_times entry for {item_id} — synthesizing with current time.")
                    self.upgrade_times[item_id] = {
                        'start_time': current_time,
                        'time_added': current_time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self.save_upgrade_times()
                    upgrade_info = self.upgrade_times[item_id]

                if upgrade_info:
                    # Use start_time from upgrade_times — captured once when the item
                    # first entered the queue (from last_updated at that moment).
                    # We intentionally do NOT re-read last_updated from the item here
                    # because update_media_item() resets last_updated on every hourly
                    # scrape, which would make time_in_queue never exceed the timeout.
                    collected_at = upgrade_info.get('start_time', current_time)

                    time_in_queue = current_time - collected_at

                    logging.info(f"Item {item_id} has been in the Upgrading queue for {time_in_queue}.")

                    # Get the configured duration from settings, default to 24 hours if blank or invalid
                    try:
                        setting_value = get_setting('Debug', 'upgrade_queue_duration_hours', '24')
                        queue_duration_hours = int(setting_value) if setting_value.strip() else 24
                    except (ValueError, AttributeError):
                        queue_duration_hours = 24
                    max_duration = timedelta(hours=queue_duration_hours)

                    # Check for hub pre-seeded magnet BEFORE scraping (hourly_scrape pops it)
                    _has_hub_magnet = False
                    try:
                        from database.zilean_upgrade import _queued_magnets, _ensure_cache_initialized
                        _ensure_cache_initialized()
                        _has_hub_magnet = item_id in _queued_magnets
                    except Exception:
                        pass

                    # Process immediately if hub magnet is ready; otherwise respect hourly timer
                    if _has_hub_magnet:
                        logging.info(f"Item {item_id} has hub magnet ready — processing immediately (bypassing scrape timer).")
                        self.hourly_scrape(item, queue_manager)
                        self.last_scrape_times[item_id] = current_time
                        if not any(i['id'] == item_id for i in self.items):
                            logging.info(f"Item {item_id} was removed during hub-magnet processing (likely upgraded).")
                        elif time_in_queue > max_duration:
                            logging.info(f"Item {item_id} timed out after hub-magnet attempt (in queue > {queue_duration_hours} hours).")
                            self.remove_item(item)
                            from database import update_media_item_state
                            update_media_item_state(item_id, state="Collected")
                            logging.info(f"Moved item {item_id} to Collected state due to timeout.")
                    elif not get_setting("Scraping", "enable_upgrading", default=False):
                        logging.info(f"Item {item_id} is in the Upgrading queue but Scraping.enable_upgrading is disabled — reverting to Collected without scraping.")
                        self.remove_item(item)
                        from database import update_media_item_state
                        update_media_item_state(item_id, state="Collected")
                    elif self.should_perform_hourly_scrape(item_id, current_time):
                        logging.info(f"Performing hourly scrape for item {item_id} which has been in queue for {time_in_queue}.")
                        self.hourly_scrape(item, queue_manager) # This might remove the item if upgraded
                        self.last_scrape_times[item_id] = current_time

                        # Nested Check: After scrape, check if item still exists AND has timed out
                        if any(i['id'] == item_id for i in self.items):
                            if time_in_queue > max_duration:
                                logging.info(f"Item {item_id} timed out after scrape attempt (in queue > {queue_duration_hours} hours).")
                                self.remove_item(item)
                                from database import update_media_item_state
                                update_media_item_state(item_id, state="Collected")
                                logging.info(f"Moved item {item_id} to Collected state due to timeout.")
                        else:
                            logging.info(f"Item {item_id} was removed during hourly scrape (likely upgraded). Skipping timeout check.")
                    else:
                        # Still check timeout even when skipping the hourly scrape
                        if time_in_queue > max_duration:
                            logging.info(f"Item {item_id} timed out (in queue > {queue_duration_hours} hours) while waiting for next scrape window.")
                            self.remove_item(item)
                            from database import update_media_item_state
                            update_media_item_state(item_id, state="Collected")
                            logging.info(f"Moved item {item_id} to Collected state due to timeout.")
                        else:
                            logging.debug(f"Skipping scrape for item {item_id} - not time yet.")

            except Exception as e:
                logging.error(f"Error processing item {item.get('id', 'unknown')}: {str(e)}")
                logging.exception("Traceback:")

        # Clean up upgrade times for items no longer in the queue
        self.clean_up_upgrade_times()

    def _run_daily_delayed_upgrade_scrape(self, delayed_days: int):
        """Perform a one-time daily upgrade scrape for items released exactly delayed_days ago.

        Eligibility is controlled by media_items.delayed_upgrade_eligible flag. Each item is
        scraped at most once by this routine; after scraping we disable the flag.
        """
        try:
            from database.database_writing import (
                get_delayed_upgrade_eligible_items,
                update_delayed_upgrade_eligibility,
            )
        except Exception as e:
            logging.error(f"Unable to import delayed-upgrade DB helpers: {e}")
            return

        try:
            candidates = get_delayed_upgrade_eligible_items(delayed_days) or []
        except Exception as e:
            logging.error(f"Failed to load delayed-upgrade candidates: {e}")
            return

        if not candidates:
            logging.info("No delayed-upgrade candidates found today")
            return

        logging.info(f"Delayed-upgrade daily pass: {len(candidates)} candidate(s) at {delayed_days} days since release")

        for item in candidates:
            try:
                item_id = item.get('id')
                if not item_id:
                    continue
                # Mark as consumed before attempting (ensures single run)
                update_delayed_upgrade_eligibility(item_id, False)
                # Perform single scrape attempt
                self.hourly_scrape(item, queue_manager=None)
            except Exception as e:
                logging.error(f"Delayed-upgrade scrape failed for item {item.get('id')}: {e}")

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

    def get_currently_processing_item_id(self) -> Optional[str]:
        """Returns the ID of the item currently being processed, or None."""
        return self.currently_processing_item_id

    def calculate_current_item_score(self, item: Dict[str, Any], version_settings: Dict[str, Any]) -> float:
        """Calculate a score for the current item when current_score is 0"""
        from scraper.functions.rank_results import rank_result_key
        from scraper.functions.file_processing import parse_torrent_info
        from utilities.settings import get_setting
        
        # Get the current item's title for scoring
        current_title = item.get('original_scraped_torrent_title') or item.get('filled_by_file', '')
        if not current_title:
            logging.warning(f"Cannot calculate score for item {item['id']} - no title available")
            return 0.0
        
        # Parse the current item's title using PTT
        try:
            parsed_info = parse_torrent_info(current_title)
            if not parsed_info or parsed_info.get('parsing_error'):
                logging.warning(f"Failed to parse current item title '{current_title}' for scoring")
                return 0.0
        except Exception as e:
            logging.error(f"Error parsing current item title '{current_title}': {e}")
            return 0.0
        
        # Create a synthetic result object for the current item
        current_result = {
            'title': current_title,
            'size': item.get('size', 0.0),  # Use item's size if available
            'source': 'Current Item',
            'magnet': '',  # Not needed for scoring
            'seeders': 0,  # Not needed for scoring
            'parsed_info': parsed_info,
            'additional_metadata': {
                'filename': item.get('filled_by_file', '')
            }
        }
        
        # Get version settings for scoring
        if not version_settings:
            from utilities.settings import get_setting
            version_name = (item.get('version', 'default') or 'default').rstrip('*').strip()
            try:
                from queues.config_manager import load_config
                config = load_config()
                version_settings = config.get('Scraping', {}).get('versions', {}).get(version_name, {})
            except Exception as e:
                logging.warning(f"Could not load version settings for '{version_name}': {e}")
                version_settings = {}
        
        # Get additional parameters for scoring
        query_title = item.get('title', '')
        query_year = item.get('year')
        query_season = item.get('season_number')
        query_episode = item.get('episode_number')
        content_type = item.get('type', 'movie')
        multi = False  # Current item is not a multi-pack
        
        # Get preferred language and translated title if available
        preferred_language = get_setting('Scraping', 'preferred_language', None)
        translated_title = None  # Could be enhanced to get from item if available
        
        # Get show season episode counts for TV shows
        show_season_episode_counts = None
        if content_type == 'episode' and query_season:
            try:
                from metadata.metadata import get_show_season_episode_counts
                show_season_episode_counts = get_show_season_episode_counts(item.get('tmdb_id'))
            except Exception as e:
                logging.debug(f"Could not get season episode counts for scoring: {e}")
        
        # Calculate score using rank_result_key
        try:
            score_key = rank_result_key(
                current_result,
                [current_result],  # Single item list
                query_title,
                query_year,
                query_season,
                query_episode,
                multi,
                content_type,
                version_settings,
                preferred_language=preferred_language,
                translated_title=translated_title,
                show_season_episode_counts=show_season_episode_counts,
                upgrade_mode=True,
            )
            
            # Extract the total score from the result
            total_score = current_result.get('score_breakdown', {}).get('total_score', 0.0)
            logging.info(f"Calculated score for current item '{current_title}': {total_score:.2f}")
            return total_score
            
        except Exception as e:
            logging.error(f"Error calculating score for current item '{current_title}': {e}")
            return 0.0

    def hourly_scrape(self, item: Dict[str, Any], queue_manager=None):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Starting hourly scrape for {item_identifier}")
        self.currently_processing_item_id = item['id']
        try:
            # --- Max Upgrading Score Check ---
            max_upgrading_score = 0.0
            try:
                max_upgrading_score = float(get_setting('Debug', 'max_upgrading_score', 0.0))
            except Exception as e:
                logging.warning(f"Could not parse max_upgrading_score setting: {e}. Defaulting to 0.0 (disabled)")
            
            # Get current score for max upgrading score check
            current_score_for_check = item.get('current_score', 0)
            if max_upgrading_score > 0 and current_score_for_check >= max_upgrading_score:
                logging.info(f"Skipping upgrade for {item_identifier}: current_score {current_score_for_check} >= max_upgrading_score {max_upgrading_score}")
                return
            # --- End Max Upgrading Score Check ---

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

            # Check for a pre-seeded Upgrade Hub candidate (skip scraping)
            # Use .get() not .pop() — only consume the magnet after confirmed upgrade success
            try:
                from database.zilean_upgrade import _queued_magnets, delete_queued_magnet_from_db, _ensure_cache_initialized
                _ensure_cache_initialized()  # ensure DB-persisted magnets are loaded after restarts
                pre_candidate = _queued_magnets.get(item['id'], None)
            except Exception:
                pre_candidate = None

            if pre_candidate and pre_candidate.get('new_magnet'):
                logging.info(f"[{item_identifier}] Using pre-selected Upgrade Hub candidate "
                             f"'{pre_candidate.get('new_title')}', skipping scrape")
                # Use the best available score estimate for the hub candidate.
                # stored new_score: calculated with full context during scan (reliable, may be stale).
                # live recalc: same ranker as current_score (apples-to-apples, less context).
                # Taking max() ensures the better estimate wins.
                _hub_new_score = pre_candidate.get('new_score', 9999.0) or 9999.0
                try:
                    _mock = dict(item)
                    _mock['filled_by_file'] = pre_candidate.get('new_title', '')
                    _mock['original_scraped_torrent_title'] = None
                    _recalc = self.calculate_current_item_score(_mock, {})
                    if _recalc > 0:
                        _hub_new_score = max(_hub_new_score, _recalc)
                        logging.info(f"[{item_identifier}] Hub candidate score: stored={pre_candidate.get('new_score', 0):.2f} recalc={_recalc:.2f} using={_hub_new_score:.2f}")
                except Exception as _e:
                    logging.debug(f"[{item_identifier}] Hub candidate score recalc failed: {_e}")
                _cand_protocol = pre_candidate.get('protocol', 'torrent')
                _cand_magnet   = pre_candidate.get('new_magnet', '')
                _cand_result = {
                    'title': pre_candidate.get('new_title', ''),
                    'magnet': _cand_magnet,
                    'score_breakdown': {'total_score': _hub_new_score},
                }
                if _cand_protocol == 'nzb':
                    _cand_result['protocol'] = 'nzb'
                    _cand_result['nzb_url']  = _cand_magnet
                results = [_cand_result]
                filtered_out = []
                # Hub-queued items bypass not_wanted check — user explicitly chose this torrent
                _skip_not_wanted = True
            else:
                # Normal path: scrape for candidates
                _skip_not_wanted = False
                logging.info(f"[{item_identifier}] Calling scrape_with_fallback with is_multi_pack={is_multi_pack} to get results")
                results, filtered_out = self.scraping_queue.scrape_with_fallback(item, is_multi_pack, queue_manager or self, skip_filter=False)

            if not results:
                 logging.info(f"No results returned from scrape_with_fallback for {item_identifier}")
                 # Potentially reset upgrading flag if no results consistently? Or just wait.
                 # update_media_item(item['id'], upgrading=False) # Optional: Reset if no results?
                 return

            # --- Calculate Current Item Score if needed ---
            current_score = item.get('current_score', 0)
            if current_score <= 0:
                # Get version settings for scoring
                version_name = (item.get('version', 'default') or 'default').rstrip('*').strip()
                try:
                    from queues.config_manager import load_config
                    config = load_config()
                    version_settings = config.get('Scraping', {}).get('versions', {}).get(version_name, {})
                except Exception as e:
                    logging.warning(f"Could not load version settings for '{version_name}': {e}")
                    version_settings = {}
                
                calculated_score = self.calculate_current_item_score(item, version_settings)
                if calculated_score > 0:
                    current_score = calculated_score
                    logging.info(f"Updated current score for {item_identifier} from 0 to {current_score:.2f}")
                    # Update the item's current_score in the database
                    update_media_item(item['id'], current_score=current_score)
                    # Update local item dict
                    item['current_score'] = current_score

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
                if isinstance(threshold_value, str):
                    upgrading_score_percentage_threshold = float(threshold_value.strip()) if threshold_value.strip() else 0.1
                elif isinstance(threshold_value, (int, float)):
                    upgrading_score_percentage_threshold = float(threshold_value)
                else:
                    # If it's neither string, int, nor float, use default and log a warning for unexpected type
                    logging.warning(f"Unexpected type for upgrading_percentage_threshold: {type(threshold_value)}. Using default 0.1.")
                    upgrading_score_percentage_threshold = 0.1
            except ValueError: # Catches errors from float() conversion if string is invalid
                logging.warning("Invalid upgrading_percentage_threshold setting (ValueError after type check), using default value of 0.1 for score increase.")
                upgrading_score_percentage_threshold = 0.1
            except AttributeError: # This should ideally not be hit with the new checks, but kept for safety
                logging.warning("Invalid upgrading_percentage_threshold setting (AttributeError), using default value of 0.1 for score increase.")
                upgrading_score_percentage_threshold = 0.1

            # Apply filtering: not wanted, failed upgrades
            filtered_results = []
            failed_upgrades = self.failed_upgrades.get(item['id'], [])
            failed_magnets = {fu['magnet'] for fu in failed_upgrades}

            for result in results:
                # 1. Check Not Wanted (unless disabled or hub pre-seeded — user explicitly chose it)
                if not item.get('disable_not_wanted_check') and not _skip_not_wanted:
                    _result_id = result.get('magnet') or result.get('nzb_url')
                    if is_magnet_not_wanted(_result_id):
                        logging.info(f"Result '{result.get('title', 'N/A')}' filtered out by not_wanted_magnets check")
                        continue
                    if is_url_not_wanted(_result_id):
                        logging.info(f"Result '{result.get('title', 'N/A')}' filtered out by not_wanted_urls check")
                        continue

                # 2. Check Failed Upgrades (hub pre-candidates bypass — user explicitly re-queued)
                if not _skip_not_wanted and result.get('magnet') in failed_magnets:
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

            logging.info(f"[{item_identifier}] Comparing {len(filtered_results)} filtered results against current score {current_score:.2f}")

            better_results = []
            for result in filtered_results:
                result_score = result.get('score_breakdown', {}).get('total_score', 0)

                # Check if the result score is actually better than the stored score
                is_better_score = False
                if result_score > current_score:
                    if current_score <= 0:
                        # Any positive score is better than non-positive
                        is_better_score = True
                        logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) is better than non-positive current score ({current_score:.2f}).")
                    else:
                        # Hub pre-candidates bypass the % threshold — user explicitly chose the upgrade
                        if _skip_not_wanted:
                            is_better_score = True
                            logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) accepted (hub pre-candidate bypasses % threshold).")
                        else:
                            # Check percentage increase threshold for positive scores
                            score_increase_percent = (result_score - current_score) / current_score
                            if score_increase_percent > upgrading_score_percentage_threshold:
                                is_better_score = True
                                logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) meets score threshold ({score_increase_percent:+.2%} > {upgrading_score_percentage_threshold:.2%}) compared to current ({current_score:.2f}).")
                            else:
                                logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) score increase ({score_increase_percent:+.2%}) does NOT meet threshold ({upgrading_score_percentage_threshold:.2%}) compared to current ({current_score:.2f}).")

                if is_better_score:
                    better_results.append(result)
                else:
                    # Log why it wasn't considered better if score wasn't higher
                    if result_score <= current_score:
                         logging.debug(f"  -> Result '{result.get('title', 'N/A')}' ({result_score:.2f}) score is not higher than current ({current_score:.2f}).")


            # Sort better_results by score descending to pick the best
            better_results.sort(key=lambda r: r.get('score_breakdown', {}).get('total_score', 0), reverse=True)

            if not better_results and pre_candidate is not None:
                logging.info(f"[{item_identifier}] Hub candidate did not score better than current — clearing stale hub magnet.")
                try:
                    from database.zilean_upgrade import _queued_magnets, delete_queued_magnet_from_db
                    _queued_magnets.pop(item['id'], None)
                    delete_queued_magnet_from_db(item['id'])
                except Exception:
                    pass

            if better_results:
                best_result = better_results[0]
                best_score = best_result.get('score_breakdown', {}).get('total_score', 0)
                logging.info(f"Found {len(better_results)} potential upgrade(s) for {item_identifier}.")
                logging.info(f"Best candidate: '{best_result.get('title', 'N/A')}' with score {best_score:.2f} (Current score: {current_score:.2f})")

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

                # If the current item was collected via NZB, clear filled_by_torrent_id
                # before passing to AddingQueue. Otherwise the health check loop in
                # adding_queue sees the old nzb:<job_id>, finds it complete in cli_mount,
                # and moves straight to Checking without ever submitting the new NZB.
                _original_torrent_id = str(item.get('filled_by_torrent_id') or '')
                if _original_torrent_id.startswith('nzb:'):
                    from database.database_writing import update_media_item as _umi
                    _umi(item['id'], filled_by_torrent_id=None)

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
                row = conn.execute('SELECT state, filled_by_torrent_id FROM media_items WHERE id = ?', (item['id'],)).fetchone()
                current_state_after_add = row['state']
                _new_torrent_id = str(row['filled_by_torrent_id'] or '')
                # A NEW NZB job was submitted if the torrent ID changed to a new nzb: value
                # (covers both: previously torrent item now upgrading via NZB, and
                #  previously NZB item upgrading to a different NZB job)
                _nzb_submitted = (
                    _new_torrent_id.startswith('nzb:') and
                    _new_torrent_id != _original_torrent_id
                )
                conn.close()

                # Success conditions:
                #   Debrid: state moves to 'Checking' (torrent added to debrid service)
                #   NZB:    state stays 'Adding' while cli_mount polls, but filled_by_torrent_id
                #           changed to a new 'nzb:<job_id>' confirming the job was submitted
                _upgrade_succeeded = (
                    current_state_after_add == 'Checking' or
                    (current_state_after_add == 'Adding' and _nzb_submitted)
                )
                if _upgrade_succeeded:
                    logging.info(f"Successfully initiated upgrade for item {item_identifier}. Item moved to {current_state_after_add}.")

                    # For NZB upgrades the adding_queue already set the correct state ('Adding'),
                    # filled_by_torrent_id ('nzb:<job_id>'), and filled_by_file (nzb job name).
                    # update_item_with_upgrade would overwrite state to 'Checking' and clobber
                    # those values — skip it and just record upgrading_from + score directly.
                    if _nzb_submitted:
                        try:
                            new_score = best_result.get('score_breakdown', {}).get('total_score', 0)
                            upgrading_from = item.get('filled_by_file') or ''
                            conn2 = get_db_connection()
                            conn2.execute(
                                "UPDATE media_items SET upgrading_from=?, current_score=?, upgraded=1, upgrading=0, upgrading_from_torrent_id=?, upgrading_from_version=? WHERE id=?",
                                (upgrading_from, new_score, item.get('filled_by_torrent_id'), item.get('version'), item['id'])
                            )
                            conn2.commit()
                            conn2.close()
                            item['upgrading_from'] = upgrading_from
                            item['current_score'] = new_score
                        except Exception as _ue:
                            logging.warning(f"[{item_identifier}] NZB upgrade DB update failed: {_ue}")
                    else:
                        # Update item data with the successful upgrade details, including the NEW score
                        self.update_item_with_upgrade(item, adding_queue, best_result)

                    # Log success, record tracking etc. (combine logic from original code)
                    self.log_upgrade(item, adding_queue) # Needs updated item dict after update_item_with_upgrade?

                    # Log upgrade initiated to Upgrade Hub activity.
                    # Result is 'initiated' — true success is logged in collected_items.py
                    # when the file is confirmed collected and the old version removed.
                    try:
                        from database.upgrade_hub_activity import log_hub_activity
                        _hub_triggered_by = pre_candidate.get('triggered_by', 'manual') if pre_candidate else 'automatic'
                        _hub_from_file = item.get('upgrading_from', '')
                        _hub_to_file = best_result.get('title', '')
                        _hub_label = item.get('title', str(item.get('id', '')))
                        log_hub_activity(
                            'upgrade_processed',
                            triggered_by=_hub_triggered_by,
                            result='initiated',
                            title=f"{_hub_label} \u2192 {_hub_to_file}",
                            stats={
                                'item_id': item.get('id'),
                                'title': _hub_label,
                                'type': item.get('type'),
                                'from_file': _hub_from_file,
                                'to_file': _hub_to_file,
                                'imdb_id': item.get('imdb_id'),
                            },
                        )
                    except Exception:
                        pass
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
                            'score_improvement': best_score - current_score # Calculate diff
                        }
                        rationale = f"Upgrading from version {item.get('version')} (score {current_score:.2f}) to {best_result.get('version')} (score {best_score:.2f})"

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
                    # Consume the hub magnet now that upgrade is confirmed
                    if pre_candidate is not None:
                        try:
                            from database.zilean_upgrade import _queued_magnets, delete_queued_magnet_from_db
                            _queued_magnets.pop(item['id'], None)
                            delete_queued_magnet_from_db(item['id'])
                        except Exception:
                            pass
                    self.remove_item(item)

                else:
                    _protocol_label = 'NZB' if best_result.get('protocol') == 'nzb' else 'Torrent'
                    _failure_reason = (updated_item.get('_upgrade_failure_reason') or
                                       item.get('_upgrade_failure_reason') or
                                       f'{_protocol_label} not added — tried: {best_result.get("title", "?")} (state: {current_state_after_add})')
                    logging.warning(f"Failed to upgrade item {item_identifier} - {_failure_reason}")
                    from routes.notifications import send_upgrade_failed_notification
                    notification_data = {
                        'title': item.get('title', 'Unknown Title'),
                        'year': item.get('year', ''),
                        'reason': f'Adding Queue Failure: {_failure_reason}'
                    }
                    send_upgrade_failed_notification(notification_data)

                    self.log_failed_upgrade(item, best_result.get('title', 'N/A'), _failure_reason)

                    # ── collect side-effect info so it can be reflected in the activity log ──
                    _hub_purged_count = 0
                    _hub_not_wanted_added = False

                    # Remove the failed hub magnet so the item falls back to normal scraping.
                    # Also purge ALL other queued candidates sharing the same info hash —
                    # multiple episodes pointing to the same season pack would otherwise
                    # each retry the identical failing magnet in rapid succession.
                    if pre_candidate is not None:
                        try:
                            from database.zilean_upgrade import _queued_magnets, delete_queued_magnet_from_db
                            _failed_hash = pre_candidate.get('new_info_hash', '')
                            _stale_ids = [item['id']]
                            if _failed_hash:
                                _stale_ids += [
                                    _iid for _iid, _c in list(_queued_magnets.items())
                                    if _iid != item['id'] and _c.get('new_info_hash') == _failed_hash
                                ]
                            for _sid in _stale_ids:
                                _queued_magnets.pop(_sid, None)
                                delete_queued_magnet_from_db(_sid)
                            _hub_purged_count = len(_stale_ids) - 1  # exclude self
                            if _hub_purged_count > 0:
                                logging.info(f"[{item_identifier}] Purged {len(_stale_ids)} hub candidates sharing the same failed magnet hash.")
                            else:
                                logging.info(f"[{item_identifier}] Removed failed hub magnet — item will fall back to normal scraping.")
                        except Exception:
                            pass

                    # Clean up the newly submitted job before restoring state.
                    # If a new NZB or debrid torrent was submitted during the failed upgrade
                    # attempt, remove it so it doesn't linger in cli_mount/RD.
                    if _nzb_submitted and _new_torrent_id.startswith('nzb:'):
                        try:
                            from usenet import get_usenet_client as _guc_fail
                            _uc_fail = _guc_fail()
                            if _uc_fail:
                                _uc_fail.remove_nzb(_new_torrent_id[4:], entry_name=best_result.get('title', ''))
                                logging.info(f"[{item_identifier}] Removed failed new NZB job {_new_torrent_id} from cli_mount")
                        except Exception as _nzb_cleanup_err:
                            logging.warning(f"[{item_identifier}] Could not clean up failed NZB job {_new_torrent_id}: {_nzb_cleanup_err}")
                    elif current_state_after_add == 'Checking' and _new_torrent_id and not _new_torrent_id.startswith('nzb:'):
                        try:
                            from debrid import get_debrid_provider as _gdp_fail
                            _dp_fail = _gdp_fail()
                            if _dp_fail:
                                _dp_fail.remove_torrent(_new_torrent_id, removal_reason='Upgrade failed — rolling back')
                                logging.info(f"[{item_identifier}] Removed failed new debrid torrent {_new_torrent_id} from RD")
                        except Exception as _dt_cleanup_err:
                            logging.warning(f"[{item_identifier}] Could not clean up failed debrid torrent {_new_torrent_id}: {_dt_cleanup_err}")

                    # Restore complete previous state
                    if self.restore_item_state(item):
                        # Track the failed upgrade attempt
                        self.add_failed_upgrade(item['id'], best_result) # Log the one we tried
                        logging.info(f"Restored previous state and added to failed upgrades list for {item_identifier}")
                        # Add the failed magnet to not_wanted so it's filtered from future scans
                        _failed_magnet = (
                            (pre_candidate.get('new_magnet') if pre_candidate else None)
                            or best_result.get('magnet', '')
                        )
                        if _failed_magnet:
                            try:
                                from database.not_wanted_magnets import add_to_not_wanted
                                add_to_not_wanted(_failed_magnet)
                                _hub_not_wanted_added = True
                                logging.info(f"[{item_identifier}] Added failed magnet to not_wanted for future scan filtering.")
                            except Exception as _nw_e:
                                logging.warning(f"[{item_identifier}] Could not add failed magnet to not_wanted: {_nw_e}")
                        # Item remains in Upgrading queue, but state reset in DB
                        # We might need to update the 'upgrading' flag back to False if restore_item_state doesn't
                        update_media_item(item['id'], upgrading=False)
                    else:
                        # No snapshot to restore; revert to Collected instead of leaving it stuck mid-upgrade.
                        logging.error(f"Failed to restore previous state for {item_identifier}; reverting to Collected")
                        update_media_item(item['id'], state='Collected', upgrading=False, upgrading_from=None)

                    # Log failure to Upgrade Hub activity (after purge/not_wanted so we can include outcomes)
                    if pre_candidate is not None:
                        try:
                            from database.upgrade_hub_activity import log_hub_activity
                            _hub_label = item.get('title', str(item.get('id', '')))
                            _hub_actions: list = []
                            if _hub_not_wanted_added:
                                _hub_actions.append('Added to not wanted — will be excluded from future scans')
                            if _hub_purged_count > 0:
                                _hub_actions.append(
                                    f'Purged {_hub_purged_count} other queued candidate'
                                    f'{"s" if _hub_purged_count != 1 else ""} sharing the same torrent'
                                )
                            log_hub_activity(
                                'upgrade_processed',
                                triggered_by=pre_candidate.get('triggered_by', 'manual'),
                                result='failed',
                                title=f"{_hub_label} \u2014 upgrade failed",
                                stats={
                                    'item_id': item.get('id'),
                                    'title': _hub_label,
                                    'type': item.get('type'),
                                    'from_file': item.get('upgrading_from', ''),
                                    'error': _failure_reason,
                                    'imdb_id': item.get('imdb_id'),
                                    'actions_taken': _hub_actions,
                                },
                            )
                        except Exception:
                            pass

        except Exception as e:
            logging.error(f"Error during hourly scrape for {item_identifier}: {e}", exc_info=True)
            # Consider if any specific cleanup or state change is needed on error

        finally:
            # Ensure the processing ID is cleared regardless of success, failure, or removal
            logging.debug(f"Finished hourly scrape processing for {item_identifier}. Clearing processing flag.")
            self.currently_processing_item_id = None

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
                _upg_seg_id = new_values.get('nzb_segment_id', '') or ''
                _upg_seg_sql = ', nzb_segment_id = ?' if _upg_seg_id else ''
                _upg_seg_vals = [_upg_seg_id] if _upg_seg_id else []
                conn.execute(f'''
                    UPDATE media_items
                    SET upgrading_from = ?,
                        filled_by_file = ?,
                        filled_by_magnet = ?,
                        version = ?,
                        current_score = ?,
                        last_updated = ?,
                        state = ?,
                        upgrading_from_torrent_id = ?,
                        upgraded = 1,
                        upgrading_from_version = ?,
                        upgrading = 0
                        {_upg_seg_sql}
                    WHERE id = ?
                ''', (
                    upgrading_from,
                    new_values.get('filled_by_file'),
                    new_values.get('filled_by_magnet'),
                    clean_version,
                    new_score,
                    datetime.now(),
                    'Checking',
                    item['filled_by_torrent_id'],
                    upgrading_from_version,
                    *_upg_seg_vals,
                    item['id']
                ))

                conn.commit()
                logging.info(f"Updated item in database with new values (including score {new_score:.2f}) for {self.generate_identifier(item)}")

                # Update the local item dictionary (important if used further)
                item['upgrading_from'] = upgrading_from
                item['filled_by_file'] = new_values.get('filled_by_file')
                item['filled_by_magnet'] = new_values.get('filled_by_magnet')
                item['upgrading_from_torrent_id'] = item.get('filled_by_torrent_id')
                if _upg_seg_id:
                    item['nzb_segment_id'] = _upg_seg_id
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
                            'upgrading_from': item['upgrading_from'],
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
            return f"movie_{item['title']}_{item['imdb_id']}_{'_'.join((item['version'] or '').split())}"
        elif item['type'] == 'episode':
            s = item.get('season_number') or 0
            e = item.get('episode_number') or 0
            return f"episode_{item['title']}_{item['imdb_id']}_S{s:02d}E{e:02d}_{'_'.join((item['version'] or '').split())}"
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

    # Log true completion to Upgrade Hub activity (old file confirmed removed, new file collected)
    try:
        from database.upgrade_hub_activity import log_hub_activity
        _label = item.get('title', item_identifier)
        _from  = item.get('upgrading_from', '')
        _to    = item.get('filled_by_file', '')
        log_hub_activity(
            'upgrade_processed',
            triggered_by='system',
            result='success',
            title=f"{_label} \u2713 upgrade confirmed",
            stats={
                'title': _label,
                'type': item.get('type'),
                'from_file': _from,
                'to_file': _to,
                'imdb_id': item.get('imdb_id'),
            },
        )
    except Exception:
        pass
