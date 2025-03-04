import logging
import time
from typing import Dict, Any, Optional
from database import get_all_media_items
from run_program import get_and_add_recent_collected_from_plex, run_recent_local_library_scan
from utilities.local_library_scan import check_local_file_for_item, local_library_scan
from utilities.plex_functions import plex_update_item
from utilities.emby_functions import emby_update_item
from not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
from queues.adding_queue import AddingQueue
from debrid import get_debrid_provider
from settings import get_setting
from debrid.common import timed_lru_cache
from pathlib import Path
import os

class CheckingQueue:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(CheckingQueue, cls).__new__(cls)
            # Initialize instance attributes
            cls._instance.items = []
            cls._instance.checking_queue_times = {}
            cls._instance.progress_checks = {}
            cls._instance.debrid_provider = get_debrid_provider()
        return cls._instance

    def __init__(self):
        # __init__ will be called every time, but instance is already created
        pass

    def update(self):
        db_items = get_all_media_items(state="Checking")
        old_item_ids = {item['id'] for item in self.items}
        old_torrent_ids = {item.get('filled_by_torrent_id') for item in self.items if item.get('filled_by_torrent_id')}
        
        self.items = [dict(row) for row in db_items]
        new_item_ids = {item['id'] for item in self.items}
        new_torrent_ids = {item.get('filled_by_torrent_id') for item in self.items if item.get('filled_by_torrent_id')}
        
        # Log changes in items and torrent IDs
        added_items = new_item_ids - old_item_ids
        removed_items = old_item_ids - new_item_ids
        added_torrents = new_torrent_ids - old_torrent_ids
        removed_torrents = old_torrent_ids - new_torrent_ids
        
        if added_items:
            logging.debug(f"New items added to checking queue: {added_items}")
        if removed_items:
            logging.debug(f"Items removed from checking queue: {removed_items}")
        if added_torrents:
            logging.debug(f"New torrent IDs added: {added_torrents}")
        if removed_torrents:
            logging.debug(f"Torrent IDs no longer present: {removed_torrents}")
        
        # Initialize checking times for new items
        for item in self.items:
            if item['id'] not in self.checking_queue_times:
                self.checking_queue_times[item['id']] = time.time()
                logging.debug(f"Initialized checking time for item {item['id']} with torrent ID {item.get('filled_by_torrent_id')}")

    def get_contents(self):
        # Add progress and state information to each item
        items_with_info = []
        items_to_remove = []
        
        # Group items by torrent ID to reduce API calls
        torrent_groups = {}
        for item in self.items:
            torrent_id = item.get('filled_by_torrent_id')
            if torrent_id:
                if torrent_id not in torrent_groups:
                    torrent_groups[torrent_id] = []
                torrent_groups[torrent_id].append(item)
        
        # Process items in batches by torrent ID
        for torrent_id, items in torrent_groups.items():
            progress = self.get_torrent_progress(torrent_id)
            state = self.get_torrent_state(torrent_id)
            
            # If state is 'missing', these items will be moved to Wanted by get_torrent_state
            # so we should skip them here and mark them for removal
            if state == 'missing':
                items_to_remove.extend(items)
                continue
            
            for item in items:
                item_info = dict(item)
                item_info['progress'] = progress
                item_info['state'] = state
                items_with_info.append(item_info)
        
        # Handle items without torrent IDs
        for item in self.items:
            if not item.get('filled_by_torrent_id'):
                item_info = dict(item)
                item_info['progress'] = None
                item_info['state'] = 'unknown'
                items_with_info.append(item_info)
        
        # Remove items that are being moved to Wanted
        for item in items_to_remove:
            if item in self.items:
                self.remove_item(item)
                
        return items_with_info

    def handle_missing_torrent(self, torrent_id, queue_manager):
        """
        Handle a torrent that is no longer on Real-Debrid (404 error).
        
        This method:
        1. Finds all items in the checking queue with this torrent ID
        2. Adds the magnet to the not-wanted list
        3. Moves all items back to the Wanted state
        
        Args:
            torrent_id (str): The ID of the missing torrent
            queue_manager (QueueManager): The queue manager instance
        """
        logging.info(f"Handling missing torrent {torrent_id}")
        
        # Get all items in the checking queue with this torrent ID
        items = [item for item in self.items if item.get('filled_by_torrent_id') == torrent_id]
        if not items:
            logging.warning(f"No items found for missing torrent {torrent_id}")
            return
        
        logging.info(f"Found {len(items)} items for missing torrent {torrent_id}")
        
        # Get the magnet link from the first item
        magnets_to_add = []
        for item in items:
            magnet = item.get('filled_by_magnet')
            if magnet and magnet not in magnets_to_add:
                magnets_to_add.append(magnet)
        
        # Add magnets to not-wanted list
        for magnet in magnets_to_add:
            try:
                logging.info(f"Adding magnet to not-wanted list: {magnet[:50]}...")
                from not_wanted_magnets import add_to_not_wanted
                from debrid.common import extract_hash_from_magnet
                
                # Check if magnet is actually an HTTP link
                if magnet.startswith('http'):
                    from debrid.common import download_and_extract_hash
                    hash_value = download_and_extract_hash(magnet)
                    add_to_not_wanted(hash_value)
                    from not_wanted_magnets import add_to_not_wanted_urls
                    add_to_not_wanted_urls(magnet)
                else:
                    # Extract hash from magnet link
                    try:
                        from debrid.common import extract_hash_from_magnet
                        hash_value = extract_hash_from_magnet(magnet)
                        add_to_not_wanted(hash_value)
                    except:
                        # If extract_hash_from_magnet is not available in debrid.common
                        import re
                        hash_match = re.search(r'btih:([a-zA-Z0-9]+)', magnet)
                        if hash_match:
                            hash_value = hash_match.group(1).lower()
                            add_to_not_wanted(hash_value)
            except Exception as e:
                logging.error(f"Failed to add magnet to not-wanted list: {str(e)}")
        
        # Move items back to Wanted state
        
        for item in items:
            try:
                item_id = item.get('id', 'unknown')
                logging.info(f"Moving item {item_id} back to Wanted state")
                queue_manager.move_to_wanted(item, "Checking")
                
                # Remove the item from the checking queue
                if item in self.items:
                    self.remove_item(item)
                    logging.info(f"Removed item {item_id} from checking queue")
            except Exception as e:
                logging.error(f"Failed to move item to Wanted state: {str(e)}")

    @timed_lru_cache(seconds=60)
    def get_torrent_progress(self, torrent_id: str) -> Optional[int]:
        """Get the current progress percentage for a torrent"""
        try:
            torrent_info = self.debrid_provider.get_torrent_info(torrent_id)
            if torrent_info:
                return torrent_info.get('progress', 0)
            else:
                logging.info(f"Torrent {torrent_id} not found on Real-Debrid (404)")
                return None
        except Exception as e:
            logging.error(f"Failed to get progress for torrent {torrent_id}: {str(e)}")
            return None

    def get_torrent_state(self, torrent_id: str) -> str:
        """Get the current state of a torrent (downloaded or downloading)"""
        try:
            current_progress = self.get_torrent_progress(torrent_id)
            
            # Handle case where progress couldn't be retrieved (404 error)
            if current_progress is None:
                logging.info(f"Could not get progress for torrent {torrent_id}, returning unknown state")
                # This is likely a 404 error, so we should handle the missing torrent
                try:
                    # Import QueueManager here to avoid circular imports
                    from queue_manager import QueueManager
                    queue_manager = QueueManager()
                    self.handle_missing_torrent(torrent_id, queue_manager)
                    return 'missing'
                except Exception as e:
                    logging.error(f"Failed to handle missing torrent: {str(e)}")
                    return 'unknown'
            
            # If progress is 100%, it's downloaded
            if current_progress == 100:
                return 'downloaded'
            
            # If we have any progress > 0, it's downloading
            if current_progress > 0:
                return 'downloading'
            
            # Check progress history if available
            if torrent_id in self.progress_checks:
                last_progress = self.progress_checks[torrent_id]['last_progress']
                if last_progress == 100:
                    return 'downloaded'
                if last_progress is not None and current_progress > last_progress:
                    return 'downloading'
            
            return 'unknown'
        except Exception as e:
            logging.error(f"Failed to get state for torrent {torrent_id}: {str(e)}")
            return 'unknown'

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.checking_queue_times[item['id']] = time.time()
        logging.debug(f"Added item {item['id']} to checking queue")

        from notifications import send_notifications
        from routes.settings_routes import get_enabled_notifications, get_enabled_notifications_for_category
        from extensions import app
        from database.database_reading import get_media_item_by_id

        item['upgrading'] = get_media_item_by_id(item['id'])['upgrading']

        logging.debug(f"Sending notification for item {item['id']} - upgrading: {item.get('upgrading')}")

        # Send notification for the state change
        try:
            # Only send notification if this is not an upgrade
            if not item.get('upgrading'):
                with app.app_context():
                    response = get_enabled_notifications_for_category('checking')
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
                                'new_state': 'Downloading' if item.get('downloading') else 'Checking',
                                'is_upgrade': False,
                                'upgrading_from': None
                            }
                            send_notifications([notification_data], enabled_notifications, notification_category='state_change')
                            logging.debug(f"Sent notification for item {item['id']}")
        except Exception as e:
            logging.error(f"Failed to send state change notification: {str(e)}")

    def remove_item(self, item: Dict[str, Any]):
        torrent_id = item.get('filled_by_torrent_id')
        self.items = [i for i in self.items if i['id'] != item['id']]
        if item['id'] in self.checking_queue_times:
            del self.checking_queue_times[item['id']]
        
        # Log removal and check if any other items still reference this torrent
        remaining_items_with_torrent = [i for i in self.items if i.get('filled_by_torrent_id') == torrent_id]
        logging.debug(f"Removed item {item['id']} from checking queue. Torrent ID {torrent_id} still referenced by {len(remaining_items_with_torrent)} items")
        
        # If no more items reference this torrent, clean up progress checks
        if not remaining_items_with_torrent and torrent_id in self.progress_checks:
            del self.progress_checks[torrent_id]
            logging.debug(f"Cleaned up progress checks for torrent {torrent_id} as it has no more associated items")

    def _calculate_dynamic_queue_period(self, items):
        """Calculate a dynamic queue period based on the number of items.
        Base period from settings + 1 minute per item in the checking queue.
        Only applies dynamic timing when not using Symlinked/Local file management.
        For items added in batches, considers when each item was added."""
        base_period = get_setting('Debug', 'checking_queue_period', default=3600)
        
        # Only use dynamic timing if NOT using Symlinked/Local file management
        if get_setting('File Management', 'file_collection_management') != 'Symlinked/Local':
            items_count = len(items)
            # Get the time each item has been in the queue
            current_time = time.time()
            item_times = []
            for item in items:
                item_add_time = self.checking_queue_times.get(item['id'], current_time)
                time_in_queue = current_time - item_add_time
                item_times.append(time_in_queue)
            
            # Sort times to find the newest item
            item_times.sort()
            newest_item_time = item_times[0] if item_times else 0
            
            # Calculate remaining items that still need processing time
            # Only count items that have been in queue for less than base_period
            remaining_items = sum(1 for t in item_times if t < base_period)
            
            # Add 60 seconds per remaining item, measured from the newest item's add time
            dynamic_period = base_period + (remaining_items * 60)
            
            # Adjust the period based on the newest item's time in queue
            # This ensures newer items get their full processing time
            if newest_item_time < base_period:
                dynamic_period = max(dynamic_period - newest_item_time, base_period)
            
            logging.debug(f"Using dynamic queue period: {dynamic_period}s (base: {base_period}s + {remaining_items} remaining items * 60s, newest item age: {newest_item_time:.1f}s)")
            return dynamic_period
        else:
            logging.debug(f"Using static queue period: {base_period}s (Symlinked/Local file management)")
            return base_period

    def process(self, queue_manager):
        if self.items:
            item = self.items[0]
            item_identifier = queue_manager.generate_identifier(item)
            logging.debug(f"Checking Queue - Processing item with resolution: {item.get('resolution', 'Not found')} for {item_identifier}")
        #logging.debug(f"Starting to process checking queue with {len(self.items)} items")
        current_time = time.time()

        adding_queue = AddingQueue()

        # Group items by torrent ID
        items_by_torrent = {}
        items_to_remove = []
        current_time = time.time()

        # Group all items by their torrent ID and log the grouping
        for item in self.items:
            torrent_id = item['filled_by_torrent_id']
            if torrent_id not in items_by_torrent:
                items_by_torrent[torrent_id] = []
            items_by_torrent[torrent_id].append(item)
            logging.debug(f"Grouped item {item['id']} under torrent ID {torrent_id}")
            # Ensure checking_queue_times is initialized for this item
            if item['id'] not in self.checking_queue_times:
                logging.warning(f"Item {item['id']} missing from checking_queue_times. Initializing with current time.")
                self.checking_queue_times[item['id']] = current_time

        # Process items by torrent ID
        for torrent_id, items in items_by_torrent.items():
            try:
                logging.debug(f"Processing torrent {torrent_id} with {len(items)} associated items")
                
                # Use the cached get_torrent_progress which includes error handling for 404s
                current_progress = self.get_torrent_progress(torrent_id)
                
                # If current_progress is None, the torrent was not found (404)
                if current_progress is None:
                    logging.info(f"Torrent {torrent_id} not found (404), moving items back to Wanted")
                    try:
                        # Call handle_missing_torrent directly to ensure items are moved back to Wanted
                        self.handle_missing_torrent(torrent_id, queue_manager)
                        # Mark items for removal from checking queue
                        items_to_remove.extend(items)
                    except Exception as e:
                        logging.error(f"Failed to handle missing torrent {torrent_id}: {str(e)}")
                    continue
                
                if torrent_id not in self.progress_checks:
                    logging.debug(f"Initializing progress check for torrent {torrent_id} with progress {current_progress}")
                    self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}
                
                last_check = self.progress_checks[torrent_id]['last_check']
                last_progress = self.progress_checks[torrent_id]['last_progress']
                logging.debug(f"Torrent {torrent_id} - Current progress: {current_progress}%, Last progress: {last_progress}%, Time since last check: {current_time - last_check}s")
                            
                if current_progress == 100:
                    # Process collected content based on symlink setting
                    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                        # First check all items for local files directly
                        items_to_scan = []
                        for item in items:
                            try:
                                time_in_queue = current_time - self.checking_queue_times[item['id']]
                            except KeyError:
                                logging.warning(f"Item {item['id']} missing from checking_queue_times. Initializing with current time.")
                                self.checking_queue_times[item['id']] = current_time
                                time_in_queue = 0
                            
                            # Run extended search after 15 minutes and every 15 minutes thereafter
                            # Use a wider 5-minute window to ensure we don't miss checks due to queue timing
                            if time_in_queue > 900 and (time_in_queue % 900) < 300:  # Check within a 5-minute window every 15 minutes
                                logging.info(f"Checking for local file for item {item['id']} with extended search")
                                file_found = check_local_file_for_item(item, extended_search=True)
                            else:
                                logging.info(f"Checking for local file for item {item['id']} without extended search")
                                file_found = check_local_file_for_item(item)
                            if file_found:
                                logging.info(f"Local file found and symlinked for item {item['id']}")

                                # Check for Plex or Emby configuration and update accordingly
                                if get_setting('Debug', 'emby_url', default=False):
                                    # Call Emby update for the item if we have an Emby URL
                                    emby_update_item(item)
                                elif get_setting('File Management', 'plex_url_for_symlink', default=False):
                                    # Call Plex update for the item if we have a Plex URL
                                    plex_update_item(item)
                                    
                                # Check if the item was marked for upgrading by check_local_file_for_item
                                from database.core import get_db_connection
                                conn = get_db_connection()
                                cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item['id'],))
                                current_state = cursor.fetchone()['state']
                                conn.close()

                                if current_state == 'Upgrading':
                                    logging.info(f"Item {item['id']} is marked for upgrading, keeping in Upgrading state")
                                else:
                                    queue_manager.move_to_collected(item, "Checking", skip_notification=True)
                            else:
                                items_to_scan.append(item)
                        
                        # If we have items that weren't found directly, do a full scan
                        if items_to_scan:
                            logging.info("Full library scan disabled for now")

                # Check if we've exceeded the checking queue period for non-actively-downloading items
                if current_progress == 100:
                    oldest_item_time = min(self.checking_queue_times.get(item['id'], current_time) for item in items)
                    time_in_queue = current_time - oldest_item_time
                    checking_queue_limit = self._calculate_dynamic_queue_period(items)
                    
                    logging.info(f"Torrent {torrent_id} has been in checking queue for {time_in_queue:.1f} seconds (dynamic limit: {checking_queue_limit} seconds for {len(items)} items)")
                    
                    if time_in_queue > checking_queue_limit:
                        logging.info(f"Removing torrent {torrent_id} from debrid service as content was not found within {checking_queue_limit} seconds (dynamic limit for {len(items)} items)")
                        try:
                            self.debrid_provider.remove_torrent(
                                torrent_id,
                                removal_reason=f"Content not found in checking queue after {checking_queue_limit} seconds (dynamic limit for {len(items)} items)"
                            )
                        except Exception as e:
                            logging.error(f"Failed to remove torrent {torrent_id}: {str(e)}")
                        # Move all items for this torrent back to Wanted
                        for item in items:
                            queue_manager.move_to_wanted(item, "Checking")
                            items_to_remove.append(item)
                        continue

                # Skip remaining checks if the torrent is completed
                if current_progress == 100:
                    continue
                
                if current_time - last_check >= 300:  # 5 minutes
                    if current_progress == last_progress:
                        logging.info(f"No progress for torrent {torrent_id} in 5 minutes. Handling failed upgrade/download.")
                        try:
                            # Remove the failed torrent from debrid service
                            self.debrid_provider.remove_torrent(
                                torrent_id,
                                removal_reason=f"No download progress after 5 minutes (stalled at {current_progress}%)"
                            )
                            logging.info(f"Removed failed torrent {torrent_id} from debrid service")
                        except Exception as e:
                            logging.error(f"Failed to remove torrent {torrent_id}: {str(e)}")

                        for item in items:
                            # Check if this was an upgrade attempt
                            if item.get('upgrading'):
                                logging.info(f"Failed upgrade detected for {queue_manager.generate_identifier(item)}")
                                # Get the UpgradingQueue instance to handle the reversion
                                from queues.upgrading_queue import UpgradingQueue
                                upgrading_queue = UpgradingQueue()
                                
                                # Send failed upgrade notification
                                from notifications import send_upgrade_failed_notification
                                notification_data = {
                                    'title': item.get('title', 'Unknown Title'),
                                    'year': item.get('year', ''),
                                    'reason': 'No download progress after 5 minutes'
                                }
                                send_upgrade_failed_notification(notification_data)
                                
                                # Log the failed upgrade
                                upgrading_queue.log_failed_upgrade(
                                    item, 
                                    item.get('filled_by_title', 'Unknown'), 
                                    'No download progress after 5 minutes'
                                )
                                
                                # First restore the previous state
                                if upgrading_queue.restore_item_state(item):
                                    # Add the failed attempt to tracking
                                    upgrading_queue.add_failed_upgrade(
                                        item['id'], 
                                        {
                                            'title': item.get('filled_by_title'),
                                            'magnet': item.get('filled_by_magnet'),
                                            'torrent_id': torrent_id
                                        }
                                    )
                                    logging.info(f"Successfully reverted failed upgrade for {queue_manager.generate_identifier(item)}")
                                else:
                                    logging.error(f"Failed to restore previous state for {queue_manager.generate_identifier(item)}, moving to Wanted")
                                    queue_manager.move_to_wanted(item, "Checking")
                            else:
                                # Regular download failure, move to Wanted
                                queue_manager.move_to_wanted(item, "Checking")
                                logging.info(f"Moving item back to Wanted: {queue_manager.generate_identifier(item)}")

                            # Add magnet to not wanted list
                            magnet = item.get('filled_by_magnet')
                            if magnet:
                                try:
                                    if magnet.startswith('http'):
                                        from debrid.common import download_and_extract_hash
                                        hash_value = download_and_extract_hash(magnet)
                                        add_to_not_wanted(hash_value)
                                        add_to_not_wanted_urls(magnet)
                                        logging.info(f"Added hash {hash_value} and URL to not wanted lists")
                                    else:
                                        from debrid.common import extract_hash_from_magnet
                                        hash_value = extract_hash_from_magnet(magnet)
                                        add_to_not_wanted(hash_value)
                                        logging.info(f"Added hash {hash_value} to not wanted list")
                                except Exception as e:
                                    logging.error(f"Failed to process magnet for not wanted: {str(e)}")

                        items_to_remove.extend(items)
                    else:
                        self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}

            except Exception as e:
                logging.error(f"Error processing items for torrent {torrent_id} in checking queue: {e}", exc_info=True)

        # Remove processed items from the Checking queue
        for item in items_to_remove:
            self.remove_item(item)

        # After processing all torrents, check if we need to run Plex scan
        if not get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            # Only run Plex scan if we have any completed torrents
            if any(self.get_torrent_progress(torrent_id) == 100 for torrent_id in items_by_torrent.keys()):
                get_and_add_recent_collected_from_plex()

        #logging.debug(f"Finished processing checking queue. Remaining items: {len(self.items)}")

    def move_items_to_wanted(self, items, queue_manager, adding_queue=None, torrent_id=None):
        """
        Move items from the checking queue back to the wanted queue.
        
        This is typically used when a torrent is no longer available on Real-Debrid.
        
        Args:
            items (list): List of items to move back to wanted
            queue_manager (QueueManager): Queue manager instance
            adding_queue (AddingQueue, optional): Adding queue instance
            torrent_id (str, optional): ID of the torrent that was removed
        """
        if not items:
            logging.debug("No items to move back to Wanted")
            return
        
        logging.info(f"Moving {len(items)} items back to Wanted state")
        
        # Process magnets first
        magnets_to_add = []
        for item in items:
            magnet = item.get('filled_by_magnet')
            if magnet and magnet not in magnets_to_add:
                magnets_to_add.append(magnet)
        
        # Add magnets to not-wanted list
        for magnet in magnets_to_add:
            try:
                item_identifier = queue_manager.generate_identifier(items[0]) if items else "unknown"
                
                # Check if magnet is actually an HTTP link
                if magnet.startswith('http'):
                    logging.debug(f"Magnet is HTTP link, downloading torrent first for {item_identifier}")
                    from debrid.common import download_and_extract_hash
                    hash_value = download_and_extract_hash(magnet)
                    from not_wanted_magnets import add_to_not_wanted
                    add_to_not_wanted(hash_value)
                    from not_wanted_magnets import add_to_not_wanted_urls
                    add_to_not_wanted_urls(magnet)
                    logging.info(f"Added hash {hash_value} and URL to not wanted lists for {item_identifier}")
                else:
                    from debrid.common import extract_hash_from_magnet
                    hash_value = extract_hash_from_magnet(magnet)
                    from not_wanted_magnets import add_to_not_wanted
                    add_to_not_wanted(hash_value)
                    logging.info(f"Added hash {hash_value} to not wanted list for {item_identifier}")
            except Exception as e:
                logging.error(f"Failed to process magnet for not wanted: {str(e)}")
        
        # Move items back to Wanted state
        for item in items:
            try:
                item_identifier = queue_manager.generate_identifier(item)
                queue_manager.move_to_wanted(item, "Checking")
                logging.info(f"Successfully moved item back to Wanted: {item_identifier}")
            except Exception as e:
                logging.error(f"Failed to move item {item_identifier} back to Wanted: {str(e)}")
            
            # Remove from checking queue
            self.remove_item(item)

    def clean_up_checking_times(self):
        """Clean up old entries from checking times and progress cache"""
        # Clean up checking times for removed items
        current_item_ids = {item['id'] for item in self.items}
        self.checking_queue_times = {k: v for k, v in self.checking_queue_times.items() if k in current_item_ids}
        
        # Clean up progress cache for removed torrents
        current_torrent_ids = {item.get('filled_by_torrent_id') for item in self.items if item.get('filled_by_torrent_id')}
        logging.debug(f"Cleaned up checking time for item ID: {current_item_ids}")