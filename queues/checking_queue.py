import logging
import time
from typing import Dict, Any, Optional, Union
from queues.run_program import get_and_add_recent_collected_from_plex, run_recent_local_library_scan
from utilities.local_library_scan import check_local_file_for_item
from utilities.plex_functions import plex_update_item
from utilities.emby_functions import emby_update_item
from database.not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
from queues.adding_queue import AddingQueue
from debrid import get_debrid_provider
from utilities.settings import get_setting
from debrid.common import timed_lru_cache, extract_hash_from_magnet, download_and_extract_hash
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
from pathlib import Path
import os
from datetime import datetime
from queues.upgrading_queue import UpgradingQueue
from routes.notifications import send_upgrade_failed_notification
from debrid.status import TorrentFetchStatus

# Define a constant for clarity when get_torrent_progress signals a missing torrent
PROGRESS_RESULT_MISSING = "MISSING_TORRENT"

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
            cls._instance.uncached_torrents = {}  # Dict of {torrent_hash: {last_check_time, item_ids[]}}
        return cls._instance

    def __init__(self):
        # __init__ will be called every time, but instance is already created
        self.items = []
        self.debrid_provider = get_debrid_provider()
        self.checking_times = {}
        self.last_check_time = datetime.now()
        self.last_report_time = datetime.now()

    def update(self):
        from database import get_all_media_items
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
                from database.not_wanted_magnets import add_to_not_wanted
                from debrid.common import extract_hash_from_magnet
                
                # Check if magnet is actually an HTTP link
                if magnet.startswith('http'):
                    hash_value = download_and_extract_hash(magnet)
                    add_to_not_wanted(hash_value)
                    from database.not_wanted_magnets import add_to_not_wanted_urls
                    add_to_not_wanted_urls(magnet)
                else:
                    # Extract hash from magnet link
                    try:
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
        
        # Move items back to Wanted state or handle failed upgrade
        upgrading_queue = None # Initialize outside the loop

        for item in items:
            try:
                item_id = item.get('id', 'unknown')
                item_identifier = queue_manager.generate_identifier(item)
                is_upgrade = item.get('upgrading') or item.get('upgrading_from') is not None

                if is_upgrade:
                    logging.info(f"Detected failed upgrade for {item_identifier} due to missing torrent {torrent_id}")
                    if upgrading_queue is None:
                        upgrading_queue = UpgradingQueue()

                    # Log the failed upgrade
                    upgrading_queue.log_failed_upgrade(
                        item,
                        item.get('filled_by_title', 'Unknown'),
                        'Torrent not found on Debrid (404)'
                    )

                    # Send failed upgrade notification
                    notification_data = {
                        'title': item.get('title', 'Unknown Title'),
                        'year': item.get('year', ''),
                        'reason': 'Torrent not found on Debrid (404)'
                    }
                    send_upgrade_failed_notification(notification_data)

                    # Restore the previous state
                    if upgrading_queue.restore_item_state(item):
                        # Add the failed attempt to tracking
                        upgrading_queue.add_failed_upgrade(
                            item['id'],
                            {
                                'title': item.get('filled_by_title'),
                                'magnet': item.get('filled_by_magnet'),
                                'torrent_id': torrent_id,
                                'reason': 'torrent_404'
                            }
                        )
                        logging.info(f"Successfully reverted failed upgrade for {item_identifier}")
                    else:
                        logging.error(f"Failed to restore previous state for {item_identifier} after missing torrent detection")
                        # Fallback: Maybe move to wanted or keep in checking? For now, just log.

                    # Remove the item from the checking queue since state is restored
                    if item in self.items:
                        self.remove_item(item)
                        logging.info(f"Removed reverted upgrade item {item_id} from checking queue")

                else:
                    # Original logic for non-upgrade items
                    logging.info(f"Moving item {item_id} back to Wanted state")
                    queue_manager.move_to_wanted(item, "Checking")

                    # Remove the item from the checking queue
                    if item in self.items:
                        self.remove_item(item)
                        logging.info(f"Removed item {item_id} from checking queue")

            except Exception as e:
                logging.error(f"Failed to handle item {item.get('id', 'unknown')} for missing torrent {torrent_id}: {str(e)}")

    @timed_lru_cache(seconds=60)
    def get_torrent_progress(self, torrent_id: str) -> Union[int, str, None]:
        """
        Get the current progress percentage for a torrent or a status string.
        Returns:
            - int: Torrent progress percentage if successful.
            - PROGRESS_RESULT_MISSING (str): If the torrent is confirmed missing (404).
            - None: If there's a temporary issue (e.g., rate limit, other recoverable error),
                    or if progress cannot be determined for other reasons that warrant a retry.
        """
        try:
            status_result = self.debrid_provider.get_torrent_info_with_status(torrent_id)

            if status_result.status == TorrentFetchStatus.OK:
                return status_result.data.get('progress', 0) if status_result.data else 0
            elif status_result.status == TorrentFetchStatus.NOT_FOUND:
                logging.info(f"Torrent {torrent_id} confirmed NOT FOUND (404) by provider.")
                return PROGRESS_RESULT_MISSING
            elif status_result.status in [
                TorrentFetchStatus.RATE_LIMITED,
                TorrentFetchStatus.PROVIDER_HANDLED_ERROR, # Error logged by provider, treat as temp
                TorrentFetchStatus.SERVER_ERROR, # Often temporary
                TorrentFetchStatus.REQUEST_ERROR # Network issues, often temporary
            ]:
                logging.warning(
                    f"Temporary issue fetching torrent {torrent_id} info: {status_result.status.value} - {status_result.message}. Will retry."
                )
                return None # Signal to retry later
            elif status_result.status in [
                TorrentFetchStatus.CLIENT_ERROR, # Non-404/429 client errors
                TorrentFetchStatus.UNKNOWN_ERROR
            ]:
                logging.error(
                    f"Error fetching torrent {torrent_id} info: {status_result.status.value} - {status_result.message}. Treating as temporary for now to allow retries."
                )
                # For now, to be safe and avoid incorrect blacklisting for potentially recoverable client/unknown errors,
                # treat as temporary (None). Specific permanent client errors would need explicit handling if they shouldn't be retried.
                return None
            else: # Should not happen if all enum values are covered
                logging.error(f"Unhandled TorrentFetchStatus {status_result.status} for torrent {torrent_id}")
                return None

        except Exception as e:
            logging.error(f"Exception in get_torrent_progress for {torrent_id}: {str(e)}", exc_info=True)
            return None # General error, treat as temporary for retry

    def get_torrent_state(self, torrent_id: str) -> str:
        """Get the current state of a torrent (downloaded, downloading, missing, or unknown)"""
        try:
            progress_or_status = self.get_torrent_progress(torrent_id)

            if progress_or_status == PROGRESS_RESULT_MISSING:
                logging.info(f"Torrent {torrent_id} is missing. Handling via handle_missing_torrent.")
                try:
                    # Import QueueManager here to avoid circular imports
                    from queues.queue_manager import QueueManager
                    queue_manager = QueueManager()
                    # This method will move items to Wanted and add to not-wanted list
                    self.handle_missing_torrent(torrent_id, queue_manager)
                    return 'missing' # State to indicate it was handled as missing
                except Exception as e:
                    logging.error(f"Failed to execute handle_missing_torrent for {torrent_id} within get_torrent_state: {str(e)}")
                    return 'unknown' # Fallback if handling fails

            elif isinstance(progress_or_status, (int, float)):
                current_progress = progress_or_status
                # Cast to int for 100% check to handle 100 or 100.0
                if int(current_progress) == 100:
                    return 'downloaded'
                # For > 0 check, float or int works fine
                if current_progress > 0:
                    return 'downloading'
                
                # Check progress history if available for 0% or 0.0 progress
                if torrent_id in self.progress_checks:
                    last_progress = self.progress_checks[torrent_id]['last_progress']
                    # Cast to int for 100% check for last_progress
                    if last_progress is not None and int(last_progress) == 100: 
                        return 'downloaded'
                    # Cast both to float for reliable comparison if last_progress might be int/float
                    if last_progress is not None and float(current_progress) > float(last_progress):
                         return 'downloading'
                
                return 'unknown' # Progress is 0, 0.0 or not clearly downloading yet

            elif progress_or_status is None: # Temporary error, progress unknown for this cycle
                logging.warning(f"Could not determine progress for torrent {torrent_id} in get_torrent_state (temporary error). Returning 'unknown' state.")
                return 'unknown'

            else: # Should ideally not be reached if all return types from get_torrent_progress are handled
                logging.error(f"Unexpected return value from get_torrent_progress for {torrent_id}: {progress_or_status} in get_torrent_state.")
                return 'unknown'

        except Exception as e:
            logging.error(f"General exception in get_state for torrent {torrent_id}: {str(e)}", exc_info=True)
            return 'unknown'

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)
        self.checking_queue_times[item['id']] = time.time()
        logging.debug(f"Added item {item['id']} to checking queue")
        
        # Check if this might be an uncached torrent based on initial progress
        torrent_id = item.get('filled_by_torrent_id')
        if torrent_id:
            try:
                initial_progress = self.get_torrent_progress(torrent_id)
                logging.debug(f"Initial progress for torrent {torrent_id}: {initial_progress}")
                # If progress is 0%, likely uncached and needs tracking
                if initial_progress == 0:
                    self.register_uncached_torrent(item)
                    logging.debug(f"Registered item {item['id']} for cache status tracking (initial progress: 0%)")
            except Exception as e:
                logging.error(f"Failed to check initial progress for item {item['id']}: {str(e)}")

        from routes.notifications import send_notifications
        from routes.settings_routes import get_enabled_notifications, get_enabled_notifications_for_category
        from routes.extensions import app
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
                                # Convert season_number and episode_number to strings to avoid type comparison issues
                                'season_number': str(item.get('season_number', '')) if item.get('season_number') is not None else None,
                                'episode_number': str(item.get('episode_number', '')) if item.get('episode_number') is not None else None,
                                'new_state': 'Downloading' if item.get('downloading') else 'Checking',
                                'is_upgrade': False,
                                'upgrading_from': None
                            }
                            send_notifications([notification_data], enabled_notifications, notification_category='state_change')
                            logging.debug(f"Sent notification for item {item['id']}")
        except Exception as e:
            logging.error(f"Failed to send state change notification: {str(e)}")

    def register_uncached_torrent(self, item):
        """Register a torrent for cache status tracking"""
        if not item.get('filled_by_magnet'):
            return
            
        try:
            # Extract hash from magnet or URL
            hash_value = None
            magnet = item.get('filled_by_magnet')
            
            if magnet.startswith('http'):
                hash_value = download_and_extract_hash(magnet)
            else:
                hash_value = extract_hash_from_magnet(magnet)
                
            if hash_value:
                if hash_value not in self.uncached_torrents:
                    self.uncached_torrents[hash_value] = {
                        'last_check_time': time.time(),
                        'item_ids': [item['id']]
                    }
                    logging.debug(f"Added new uncached torrent with hash {hash_value[:8]}... for tracking")
                else:
                    # Add the item ID if not already tracked
                    if item['id'] not in self.uncached_torrents[hash_value]['item_ids']:
                        self.uncached_torrents[hash_value]['item_ids'].append(item['id'])
                        logging.debug(f"Added item {item['id']} to existing tracked hash {hash_value[:8]}...")
        except Exception as e:
            logging.error(f"Failed to register uncached torrent: {str(e)}")

    def check_uncached_torrents(self, phalanx_db_manager):
        """Periodically check if uncached torrents have become cached"""
        current_time = time.time()
        cache_check_interval = get_setting('Debug', 'cache_check_interval', default=900)  # 15 minutes default
        
        hashes_to_remove = []
        
        # Check if phalanx db is enabled using settings
        phalanx_enabled = get_setting('UI Settings', 'enable_phalanx_db', default=False)
        
        for hash_value, data in list(self.uncached_torrents.items()):
            # Only check if enough time has passed since last check
            if current_time - data['last_check_time'] >= cache_check_interval:
                try:
                    # Create a magnet link from the hash for direct cache check
                    magnet_link = f"magnet:?xt=urn:btih:{hash_value}"
                    
                    # Always do a direct check with the debrid provider
                    # Skip phalanx db check since we're verifying cache status
                    is_cached = self.debrid_provider.is_cached_sync(
                        magnet_link,
                        skip_phalanx_db=True,  # Always skip PhalanxDB for direct verification
                        remove_uncached=False,   # <-- Changed from True to False
                        remove_cached=False     # Keep cached torrents
                    )
                    
                    # Update last check time
                    data['last_check_time'] = current_time
                    
                    if is_cached:
                        logging.info(f"Previously uncached torrent {hash_value} is now cached")
                        hashes_to_remove.append(hash_value)
                        
                        # Only update phalanx db if enabled
                        if phalanx_enabled and phalanx_db_manager:
                            try:
                                update_result = phalanx_db_manager.update_cache_status(hash_value, True)
                                if update_result:
                                    logging.info(f"Updated PhalanxDB cache status for {hash_value}")
                                else:
                                    logging.warning(f"Failed to update PhalanxDB cache status for {hash_value}")
                            except Exception as e:
                                logging.error(f"Failed to update PhalanxDB cache status for {hash_value}: {str(e)}")
                    else:
                        logging.debug(f"Hash {hash_value} still not cached according to direct check")
                        
                except Exception as e:
                    logging.error(f"Error checking uncached torrent {hash_value}: {str(e)}")
                    continue
        
        # Remove cached hashes from tracking
        for hash_value in hashes_to_remove:
            if hash_value in self.uncached_torrents:
                del self.uncached_torrents[hash_value]
                logging.info(f"Removed hash {hash_value} from uncached tracking as it's now cached")

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
        
        # Also clean up any uncached torrent tracking if this was the last item
        try:
            if item.get('filled_by_magnet'):
                magnet = item.get('filled_by_magnet')
                hash_value = None
                if magnet.startswith('http'):
                    hash_value = download_and_extract_hash(magnet)
                else:
                    hash_value = extract_hash_from_magnet(magnet)
                
                if hash_value and hash_value in self.uncached_torrents:
                    # Remove this item ID from tracking
                    if item['id'] in self.uncached_torrents[hash_value]['item_ids']:
                        self.uncached_torrents[hash_value]['item_ids'].remove(item['id'])
                        logging.debug(f"Removed item {item['id']} from uncached torrent tracking for hash {hash_value[:8]}...")
                    
                    # If no more items with this hash, remove the hash from tracking
                    if not self.uncached_torrents[hash_value]['item_ids']:
                        del self.uncached_torrents[hash_value]
                        logging.debug(f"Removed hash {hash_value[:8]}... from uncached tracking as it has no more items")
        except Exception as e:
            logging.error(f"Error cleaning up uncached torrent tracking for item {item['id']}: {str(e)}")

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

    def process(self, queue_manager, program_runner):
        """
        Process items in the checking queue by checking torrent progress
        and triggering local file checks for completed torrents in
        Symlinked/Local mode.
        """
        current_time = time.time()
        items_to_remove = []

        # Ensure these are initialized here if they were inside the removed block
        adding_queue = AddingQueue()
        phalanx_db_manager = PhalanxDBClassManager()

        # Periodically check cache status of tracked torrents (keep this)
        if self.uncached_torrents:
            logging.debug(f"Checking cache status for {len(self.uncached_torrents)} tracked torrents")
            self.check_uncached_torrents(phalanx_db_manager)

        # Group items by torrent ID
        items_by_torrent = {}

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
                
                progress_or_status = self.get_torrent_progress(torrent_id)
                
                if progress_or_status == PROGRESS_RESULT_MISSING:
                    logging.info(f"Torrent {torrent_id} is missing (confirmed during process loop). Handling missing torrent.")
                    try:
                        self.handle_missing_torrent(torrent_id, queue_manager)
                        # Items are moved by handle_missing_torrent. Mark them for removal from Python queue object.
                        items_to_remove.extend(items)
                    except Exception as e:
                        logging.error(f"Failed to handle missing torrent {torrent_id} in process loop: {str(e)}")
                    continue # Move to the next torrent

                elif progress_or_status is None: # Temporary error from get_torrent_progress
                    logging.warning(f"Temporary issue determining progress for torrent {torrent_id} in process method. Skipping this cycle.")
                    continue # Move to the next torrent
                
                # If we are here, progress_or_status is an int or float
                current_progress = progress_or_status
                # Ensure it's an int or float, as provider can return float percentages like 0.2 for 0.2%
                if not isinstance(current_progress, (int, float)):
                    logging.error(f"Unexpected type for current_progress after checks: {type(current_progress)}. Value: {current_progress}. Skipping torrent {torrent_id}.")
                    continue

                if torrent_id not in self.progress_checks:
                    logging.debug(f"Initializing progress check for torrent {torrent_id} with progress {current_progress}")
                    self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}
                
                last_check = self.progress_checks[torrent_id]['last_check']
                last_progress = self.progress_checks[torrent_id]['last_progress']
                logging.debug(f"Torrent {torrent_id} - Current progress: {current_progress}%, Last progress: {last_progress}%, Time since last check: {current_time - last_check}s")
                
                # Check if progress just hit 100% (transition from downloading to downloaded)
                # Use int(current_progress) to robustly compare with 100, handles floats like 100.0
                if int(current_progress) == 100 and last_progress is not None and last_progress < 100:
                    logging.info(f"Torrent {torrent_id} just finished downloading. Updating cache status.")
                    
                    # Get hash from any item's magnet link
                    for item in items:
                        magnet = item.get('filled_by_magnet')
                        if magnet:
                            try:
                                # Extract hash from magnet link
                                hash_value = None
                                if magnet.startswith('http'):
                                    hash_value = download_and_extract_hash(magnet)
                                else:
                                    hash_value = extract_hash_from_magnet(magnet)
                                
                                if hash_value:
                                    # Update cache status synchronously
                                    logging.info(f"Updating cache status for hash {hash_value} to cached=True")
                                    try:
                                        phalanx_db_manager.update_cache_status(hash_value, True)
                                    except Exception as e:
                                        logging.error(f"Failed to update cache status for hash {hash_value}: {str(e)}")
                                    break  # We only need to update once per torrent
                            except Exception as e:
                                logging.error(f"Failed to update cache status: {str(e)}")
                
                if int(current_progress) == 100:
                    # Process completed torrents in Symlinked/Local mode
                    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                        items_to_scan = [] # Keep track of items not found yet
                        for item in items:
                            try:
                                time_in_queue = current_time - self.checking_queue_times[item['id']]
                            except KeyError:
                                logging.warning(f"Item {item['id']} missing from checking_queue_times. Initializing.")
                                self.checking_queue_times[item['id']] = current_time
                                time_in_queue = 0

                            # Log item details just before calling check_local_file_for_item
                            logging.debug(f"[CheckingQueue Process] Preparing to call check_local_file_for_item for Item ID {item.get('id')}")
                            logging.debug(f"[CheckingQueue Process] Item Data: upgrading_from='{item.get('upgrading_from')}', state='{item.get('state')}', filled_by_file='{item.get('filled_by_file')}'")
                            # Optional: Log the full item dict for very detailed debugging
                            # logging.debug(f"[CheckingQueue Process] Full item data for {item.get('id')}: {item}")

                            # --- EDIT: Modify call to pass callback and handle boolean result ---
                            logging.info(f"Checking for local file for item {item['id']}...")
                            use_extended_search = time_in_queue > 900 and (time_in_queue % 900) < 300
                            logging.debug(f"Using extended search: {use_extended_search} (time in queue: {time_in_queue:.1f}s)")

                            processing_successful = check_local_file_for_item(
                                item,
                                extended_search=use_extended_search,
                            )

                            if processing_successful:
                                logging.info(f"Local file found and symlinked for item {item['id']} by CheckingQueue.")

                                # --- EDIT: Fetch updated item data ---
                                from database import get_media_item_by_id # Ensure import is available
                                updated_item_data = get_media_item_by_id(item['id'])
                                if not updated_item_data:
                                     logging.error(f"Failed to fetch updated data for item {item['id']} after successful local check. Skipping media server update.")
                                     # Decide how to handle this - maybe continue to state check?
                                else:
                                     # Use the fresh data for media server updates
                                     logging.debug(f"Fetched updated data for item {item['id']} before media server update.")
                                     if get_setting('Debug', 'emby_jellyfin_url', default=False):
                                         emby_update_item(dict(updated_item_data)) # Pass updated data
                                     elif get_setting('File Management', 'plex_url_for_symlink', default=False):
                                         plex_update_item(dict(updated_item_data)) # Pass updated data
                                # --- END EDIT ---

                                # Re-check state after processing, as check_local_file_for_item might change it
                                from database.core import get_db_connection
                                conn = get_db_connection()
                                cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item['id'],))
                                current_state_row = cursor.fetchone()
                                conn.close()

                                current_state = current_state_row['state'] if current_state_row else None

                                if current_state == 'Upgrading':
                                    logging.info(f"Item {item['id']} is marked for upgrading, keeping in Upgrading state after local check.")
                                    # handle_state_change is called within check_local_file_for_item
                                elif current_state == 'Collected':
                                    logging.info(f"Item {item['id']} state confirmed as Collected after local check.")
                                    # Ensure it's removed from the Python queue object
                                    queue_manager.move_to_collected(item, "Checking", skip_notification=True)
                                elif current_state: # e.g., if it somehow reverted to 'Wanted' or stayed 'Checking' despite success?
                                    logging.warning(f"Item {item['id']} processed locally but state is '{current_state}'. Moving to Collected.")
                                    queue_manager.move_to_collected(item, "Checking", skip_notification=True)
                                else:
                                    logging.error(f"Item {item['id']} processed locally but seems missing from DB. Cannot confirm final state.")
                                    items_to_remove.append(item) # Ensure removed from memory queue

                            else: # check_local_file_for_item returned False
                                logging.debug(f"Local file not found yet for item {item['id']}. It might appear later.")
                                items_to_scan.append(item)
                            # --- END EDIT ---

                        # If we have items that weren't found directly, do a full scan
                        if items_to_scan:
                            logging.info("Full library scan disabled for now")

                # Check if we've exceeded the checking queue period for non-actively-downloading items
                if int(current_progress) == 100:
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
                        items_to_remove.extend(items)
                        continue

                # Skip remaining checks if the torrent is completed
                if int(current_progress) == 100:
                    continue
                
                if current_time - last_check >= 300:  # 5 minutes
                    if current_progress == last_progress:
                        logging.info(f"No progress for torrent {torrent_id} in 5 minutes. Handling failed upgrade/download.")
                        try:
                            self.debrid_provider.remove_torrent(
                                torrent_id,
                                removal_reason=f"No download progress after 5 minutes (stalled at {current_progress}%)"
                            )
                            logging.info(f"Removed failed torrent {torrent_id} from debrid service")
                        except Exception as e:
                            logging.error(f"Failed to remove torrent {torrent_id}: {str(e)}")

                        # Add upgrade check here
                        upgrading_queue = None # Initialize for potential use

                        for item in items:
                            item_identifier = queue_manager.generate_identifier(item)
                            is_upgrade = item.get('upgrading') or item.get('upgrading_from') is not None

                            if is_upgrade:
                                logging.info(f"Failed upgrade detected for {item_identifier} due to no progress")
                                if upgrading_queue is None:
                                    from queues.upgrading_queue import UpgradingQueue
                                    from routes.notifications import send_upgrade_failed_notification
                                    upgrading_queue = UpgradingQueue()

                                # Send failed upgrade notification
                                notification_data = {
                                    'title': item.get('title', 'Unknown Title'),
                                    'year': item.get('year', ''),
                                    'reason': f'No download progress after 5 minutes (stalled at {current_progress}%)'
                                }
                                send_upgrade_failed_notification(notification_data)

                                # Log the failed upgrade
                                upgrading_queue.log_failed_upgrade(
                                    item,
                                    item.get('filled_by_title', 'Unknown'),
                                    f'No download progress after 5 minutes (stalled at {current_progress}%)'
                                )

                                # Restore the previous state
                                if upgrading_queue.restore_item_state(item):
                                    # Add the failed attempt to tracking
                                    upgrading_queue.add_failed_upgrade(
                                        item['id'],
                                        {
                                            'title': item.get('filled_by_title'),
                                            'magnet': item.get('filled_by_magnet'),
                                            'torrent_id': torrent_id,
                                            'reason': 'no_progress'
                                        }
                                    )
                                    logging.info(f"Successfully reverted failed upgrade for {item_identifier}")
                                else:
                                    logging.error(f"Failed to restore previous state for {item_identifier} after no progress detection")
                                    # Fallback? Move to wanted?
                                    queue_manager.move_to_wanted(item, "Checking")
                            else:
                                # Original logic for non-upgrade items
                                logging.info(f"Moving non-upgrade item back to Wanted due to no progress: {item_identifier}")
                                queue_manager.move_to_wanted(item, "Checking")

                            # Add magnet to not wanted list (for both upgrade and non-upgrade failures)
                            magnet = item.get('filled_by_magnet')
                            if magnet:
                                try:
                                    hash_value = None
                                    if magnet.startswith('http'):
                                        hash_value = download_and_extract_hash(magnet)
                                        if hash_value: add_to_not_wanted(hash_value)
                                        add_to_not_wanted_urls(magnet)
                                        logging.info(f"Added hash {hash_value} and URL to not wanted lists for {item_identifier}")
                                    else:
                                        hash_value = extract_hash_from_magnet(magnet)
                                        if hash_value: add_to_not_wanted(hash_value)
                                        logging.info(f"Added hash {hash_value} to not wanted list for {item_identifier}")
                                except Exception as e:
                                    logging.error(f"Failed to process magnet for not wanted: {str(e)}")

                        items_to_remove.extend(items)
                    else:
                        self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}

            except Exception as e:
                logging.error(f"Error processing items for torrent {torrent_id} in checking queue: {e}", exc_info=True)

        # Remove processed items from the Checking queue (use items_to_remove accumulated during normal processing)
        for item in items_to_remove:
            if self.contains_item_id(item['id']): # Double check item is still present before removing
                 self.remove_item(item)
            else:
                 logging.debug(f"Item {item.get('id')} already removed from checking queue, skipping removal.")

        # After processing all torrents, check if we need to run Plex scan
        if not get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            # Only run Plex scan if we have any completed torrents
            if any(self.get_torrent_progress(torrent_id) == 100 for torrent_id in items_by_torrent.keys()):
                get_and_add_recent_collected_from_plex()
                # Add reconciliation call after recent scan processing
                logging.info("Triggering queue reconciliation after recent Plex scan.")
                program_runner.task_reconcile_queues()

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
                    hash_value = download_and_extract_hash(magnet)
                    from database.not_wanted_magnets import add_to_not_wanted
                    add_to_not_wanted(hash_value)
                    from database.not_wanted_magnets import add_to_not_wanted_urls
                    add_to_not_wanted_urls(magnet)
                    logging.info(f"Added hash {hash_value} and URL to not wanted lists for {item_identifier}")
                else:
                    hash_value = extract_hash_from_magnet(magnet)
                    from database.not_wanted_magnets import add_to_not_wanted
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
        
        # Clean up uncached torrents tracking
        all_tracking_item_ids = set()
        for hash_data in self.uncached_torrents.values():
            all_tracking_item_ids.update(hash_data['item_ids'])
        
        valid_tracking_item_ids = all_tracking_item_ids.intersection(current_item_ids)
        invalid_tracking_item_ids = all_tracking_item_ids - valid_tracking_item_ids
        
        if invalid_tracking_item_ids:
            # Remove invalid item IDs from uncached torrents tracking
            for hash_value in list(self.uncached_torrents.keys()):
                self.uncached_torrents[hash_value]['item_ids'] = [
                    item_id for item_id in self.uncached_torrents[hash_value]['item_ids'] 
                    if item_id in valid_tracking_item_ids
                ]
                
                # If no valid item IDs left, remove this hash from tracking
                if not self.uncached_torrents[hash_value]['item_ids']:
                    del self.uncached_torrents[hash_value]
            
            logging.debug(f"Cleaned up {len(invalid_tracking_item_ids)} invalid item IDs from uncached torrents tracking")

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)