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
from debrid.base import ProviderUnavailableError
import requests
from database.database_reading import get_media_item_by_id
from database.core import get_db_connection
import threading
import functools

# Define a constant for clarity when get_torrent_progress signals a missing torrent
PROGRESS_RESULT_MISSING = "MISSING_TORRENT"
DEFAULT_MAX_UNKNOWN_STRIKES = 5
DEFAULT_CHECKING_GRACE_PERIOD_SECONDS = 60 * 5

def with_timeout(timeout_seconds=45):
    """Decorator to add timeout to a function using threading.Timer"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = [None]
            exception = [None]
            completed = threading.Event()
            
            def target():
                try:
                    result[0] = func(*args, **kwargs)
                except Exception as e:
                    exception[0] = e
                finally:
                    completed.set()
            
            # Start the function in a separate thread
            thread = threading.Thread(target=target)
            thread.daemon = True
            thread.start()
            
            # Wait for completion or timeout
            if completed.wait(timeout=timeout_seconds):
                # Function completed
                if exception[0]:
                    raise exception[0]
                return result[0]
            else:
                # Timeout occurred
                logging.error(f"Function {func.__name__} timed out after {timeout_seconds} seconds")
                raise TimeoutError(f"Function {func.__name__} timed out after {timeout_seconds} seconds")
        
        return wrapper
    return decorator

class CheckingQueue:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(CheckingQueue, cls).__new__(cls)
            # Initialize instance attributes
            cls._instance.items = []
            cls._instance.checking_queue_times = {}
            cls._instance.progress_checks = {}
            cls._instance.debrid_provider = get_debrid_provider()  # May be None for usenet-only setups
            cls._instance.uncached_torrents = {}  # Dict of {torrent_hash: {last_check_time, item_ids[]}}
            cls._instance.unknown_strikes = {} # Tracks consecutive unknown states for torrents
        return cls._instance

    def __init__(self):
        # __init__ will be called every time, but instance is already created
        self.items = []
        self.debrid_provider = get_debrid_provider()  # May be None for usenet-only setups
        self.checking_times = {}
        self.last_check_time = datetime.now()
        self.last_report_time = datetime.now()
        self._nzb_missing_counts = {}  # tracks consecutive not-found polls per torrent_id
        self._nzb_plex_scan_triggered = set()  # torrent_ids that have already had Plex scan fired

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
            for _tid in removed_torrents:
                self._nzb_missing_counts.pop(_tid, None)
        
        # Initialize checking times for new items
        for item in self.items:
            if item['id'] not in self.checking_queue_times:
                self.checking_queue_times[item['id']] = time.time()
                logging.debug(f"Initialized checking time for item {item['id']} with torrent ID {item.get('filled_by_torrent_id')}")

    def get_contents(self, raw=False):
        # raw=True: skip live API calls, return items as-is (for page render, not SSE stream)
        if raw:
            return list(self.items)
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
        for torrent_id, items_group in torrent_groups.items(): # Renamed items to items_group to avoid conflict
            current_items_for_torrent = [item for item in self.items if item.get('filled_by_torrent_id') == torrent_id]
            if not current_items_for_torrent:
                logging.debug(f"Torrent {torrent_id} no longer has items in queue, skipping in get_contents.")
                continue

            progress = self.get_torrent_progress(torrent_id) 
            # Call with increment_strikes=False for display purposes
            state = self.get_torrent_state(torrent_id, increment_strikes=False) 
            
            if state == 'missing' or state == 'stalled':
                items_to_remove.extend(items_group)
                continue
            
            for item in current_items_for_torrent:
                item_info = dict(item)
                item_info['progress'] = progress # progress here might be stale if state changed it.
                                                 # For display, maybe get fresh progress or use what get_torrent_state determined.
                                                 # For now, using the progress fetched at the start of this loop.
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

        # NZB jobs — add NZB URL to not-wanted then move items back to Wanted
        if str(torrent_id).startswith('nzb:'):
            logging.info(f"[NZB] cli_mount job {torrent_id} failed/missing. Adding to not-wanted and moving {len(items)} item(s) back to Wanted.")
            from queues.queue_manager import QueueManager
            from database.database_writing import update_media_item_state
            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_nzb_guid
            qm = QueueManager()
            for item in items:
                try:
                    _nzb_url = item.get('filled_by_magnet', '')
                    if _nzb_url:
                        _add_nzb_guid(_nzb_url)
                    if item.get('ghostlisted'):
                        logging.warning(f"[NZB] Item {item['id']} is ghostlisted — setting Blacklisted instead of Wanted")
                        update_media_item_state(item['id'], 'Blacklisted')
                        self.remove_item(item)
                    else:
                        qm.move_to_wanted(item, "Checking")
                except Exception as _e:
                    logging.error(f"[NZB] Could not move item back to Wanted: {_e}")
            return

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
                    except Exception:
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
                    # GHOSTLIST CHECK: Don't move ghostlisted/blacklisted items back to Wanted
                    item_state = item.get('state', '')
                    is_ghostlisted = item.get('ghostlisted') == 1
                    is_blacklisted = item_state == 'Blacklisted'

                    if is_ghostlisted or is_blacklisted:
                        logging.info(f"⛔ Skipping item {item_id} - item is {'ghostlisted' if is_ghostlisted else 'blacklisted'}, not moving back to Wanted")
                        # Remove from checking queue but don't move to Wanted
                        if item in self.items:
                            self.remove_item(item)
                            logging.info(f"Removed ghostlisted/blacklisted item {item_id} from checking queue")
                    else:
                        # Original logic for non-upgrade items
                        logging.info(f"Moving item {item_id} back to Wanted state")
                        queue_manager.move_to_wanted(item, "Checking")

                        # Remove the item from the checking queue
                        if item in self.items: # Check before removing
                            self.remove_item(item) # This will also clean up unknown_strikes if needed
                            logging.info(f"Removed item {item_id} from checking queue")

            except Exception as e:
                logging.error(f"Failed to handle item {item.get('id', 'unknown')} for missing torrent {torrent_id}: {str(e)}")
        
        # Clear strike counter for this torrent as it's decisively missing
        if torrent_id in self.unknown_strikes:
            del self.unknown_strikes[torrent_id]
            logging.debug(f"Cleared unknown strikes for missing torrent {torrent_id}")

    def _resolve_nzb_file_info(self, torrent_id: str, items: list) -> None:
        """
        When an NZB download completes, query cli_mount to find the actual folder and
        video filename, then update all items with the correct filled_by_file,
        filled_by_title, debrid_folder_name, and original_scraped_torrent_title
        so that symlink creation and Plex file checks work identically to debrid adds.
        """
        from usenet import get_usenet_client
        from database.database_writing import update_media_item

        if not items:
            return

        # Use the NZB title stored as filled_by_title to search usenet provider
        sample_item = items[0]
        job_name = sample_item.get('filled_by_title') or sample_item.get('original_scraped_torrent_title') or ''
        if not job_name:
            # Fallback: look up the job name directly from usenet provider using the job_id
            try:
                _job_id_raw = torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id
                _status = get_usenet_client().get_job_status(_job_id_raw)
                if _status:
                    job_name = _status.get('name') or _status.get('raw', {}).get('name') or ''
                    if job_name:
                        logging.info(f'[NZB] Resolved job_name from usenet provider for {torrent_id}: {job_name!r}')
            except Exception:
                pass
        if not job_name:
            logging.warning(f'[NZB] Cannot resolve file info for {torrent_id}: no job_name')
            return

        # Strip virtual pack label and common appended suffixes
        import re as _re_strip
        job_name = _re_strip.sub(r'\s*\[NZB Pack[^\]]*\]', '', job_name).strip()
        job_name = _re_strip.sub(r'[-_\s]+AsRequested$', '', job_name, flags=_re_strip.IGNORECASE).strip()

        # Skip if already resolved (filled_by_file has a video extension)
        import os as _os
        _VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}
        if _os.path.splitext(sample_item.get('filled_by_file') or '')[1].lower() in _VIDEO_EXTS:
            logging.debug(f'[NZB] {torrent_id} already has video filename, skipping resolve')
            return

        try:
            client = get_usenet_client()
            is_pack = len(items) > 1  # Multiple items share same torrent_id = aggregate pack

            if is_pack:
                # Aggregate pack — get all files and match each item to its episode file
                result = client.get_nzb_folder_all_files(job_name)
                if not result:
                    logging.warning(f'[NZB] Could not find cli_mount folder for pack {job_name!r}')
                    return
                folder_name, video_files = result
                if not video_files:
                    logging.warning(f'[NZB] Found folder {folder_name!r} but no video files for pack {torrent_id}')
                    video_files = []

                # Single-file pack with multiple episodes — can't serve individual episodes.
                # Add NZB URL to not-wanted and send all items back to Wanted.
                if len(video_files) == 1 and len(items) > 1:
                    logging.warning(f'[NZB] Pack {torrent_id} has only 1 file but {len(items)} episodes — single-file pack, sending back to Wanted')
                    from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_nw
                    from queues.queue_manager import QueueManager as _QM
                    _qm = _QM()
                    for _item in items:
                        try:
                            _url = _item.get('filled_by_magnet', '')
                            if _url:
                                _add_nw(_url)
                        except Exception:
                            pass
                        try:
                            _qm.move_to_wanted(_item, 'Checking')
                        except Exception:
                            pass
                    return

                import re as _re_ep
                for item in items:
                    ep = item.get('episode_number')
                    season = item.get('season_number')
                    orig_scraped = item.get('original_scraped_torrent_title') or job_name
                    # Find file matching this episode number
                    matched_file = None
                    if ep is not None and season is not None:
                        ep_pat = _re_ep.compile(
                            rf'[Ss]{season:02d}[Ee]{ep:02d}(?![0-9])',
                            _re_ep.IGNORECASE
                        )
                        for vf in video_files:
                            if ep_pat.search(vf):
                                matched_file = vf
                                break
                    # Fallback to largest file if no episode match
                    if not matched_file and video_files:
                        matched_file = video_files[0]
                    if not matched_file:
                        matched_file = folder_name

                    update_fields = {
                        'filled_by_file': matched_file,
                        'filled_by_title': folder_name,
                        'debrid_folder_name': folder_name,
                        'location_basename': folder_name,
                        'original_scraped_torrent_title': orig_scraped,
                        'real_debrid_original_title': orig_scraped,
                    }
                    # If DB has season=0/episode=0 (misnamed pack), recover from actual filename
                    if (not season or int(season) == 0) and (not ep or int(ep) == 0):
                        _m = _re_ep.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', matched_file)
                        if _m:
                            update_fields['season_number'] = int(_m.group(1))
                            update_fields['episode_number'] = int(_m.group(2))
                            item['season_number'] = update_fields['season_number']
                            item['episode_number'] = update_fields['episode_number']
                            logging.info(f'[NZB] Recovered S{update_fields["season_number"]:02d}E{update_fields["episode_number"]:02d} from filename {matched_file!r} for item {item["id"]}')

                    update_media_item(item['id'], **update_fields)
                    item['filled_by_file'] = matched_file
                    item['filled_by_title'] = folder_name
                    item['debrid_folder_name'] = folder_name
                    item['location_basename'] = folder_name
                    item['original_scraped_torrent_title'] = orig_scraped
                    item['real_debrid_original_title'] = orig_scraped
                    logging.info(f'[NZB] Pack item {item["id"]} S{(item.get("season_number") or 0):02d}E{(item.get("episode_number") or 0):02d} → {matched_file!r}')
            else:
                # Single NZB — pass season/episode for accurate file matching
                _ep_season = sample_item.get('season_number')
                _ep_episode = sample_item.get('episode_number')
                result = client.get_nzb_file_info(job_name, season=_ep_season, episode=_ep_episode)
                if not result:
                    logging.warning(f'[NZB] Could not find cli_mount folder for job {job_name!r}')
                    return
                folder_name, video_file = result
                if not video_file:
                    logging.warning(f'[NZB] Found folder {folder_name!r} but no video file inside for {torrent_id}')
                    video_file = folder_name
                logging.info(f'[NZB] Resolved: folder={folder_name!r} file={video_file!r} for {torrent_id}')

                for item in items:
                    orig_scraped = item.get('original_scraped_torrent_title') or job_name
                    update_fields = {
                        'filled_by_file': video_file,
                        'filled_by_title': folder_name,
                        'debrid_folder_name': folder_name,
                        'location_basename': folder_name,
                        'original_scraped_torrent_title': orig_scraped,
                        'real_debrid_original_title': orig_scraped,
                    }
                    # If DB has season=0/episode=0 (misnamed pack), recover from actual filename
                    _s = item.get('season_number')
                    _e = item.get('episode_number')
                    if (not _s or int(_s) == 0) and (not _e or int(_e) == 0):
                        import re as _re_single
                        _m = _re_single.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', video_file)
                        if _m:
                            update_fields['season_number'] = int(_m.group(1))
                            update_fields['episode_number'] = int(_m.group(2))
                            item['season_number'] = update_fields['season_number']
                            item['episode_number'] = update_fields['episode_number']
                            logging.info(f'[NZB] Recovered S{update_fields["season_number"]:02d}E{update_fields["episode_number"]:02d} from filename {video_file!r} for item {item["id"]}')

                    update_media_item(item['id'], **update_fields)
                    item['filled_by_file'] = video_file
                    item['filled_by_title'] = folder_name
                    item['debrid_folder_name'] = folder_name
                    item['location_basename'] = folder_name
                    item['original_scraped_torrent_title'] = orig_scraped
                    item['real_debrid_original_title'] = orig_scraped
                    logging.info(f'[NZB] Updated item {item["id"]} with resolved file info')

        except Exception as exc:
            logging.error(f'[NZB] _resolve_nzb_file_info error for {torrent_id}: {exc}', exc_info=True)

    @timed_lru_cache(seconds=15)
    @with_timeout(45)  # 45 second timeout for the entire progress check
    def _get_nzb_progress(self, torrent_id: str) -> Union[int, str, None]:
        """Poll usenet provider for progress of an NZB job. torrent_id format: 'nzb:<job_id>'"""
        from usenet import get_usenet_client, reset_usenet_client
        reset_usenet_client()
        client = get_usenet_client()
        job_id = torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id
        try:
            status = client.get_job_status(job_id)
            if not status:
                # Job not found in cli_mount — track consecutive misses
                # After 3 consecutive misses declare it missing so handle_missing_torrent fires
                _count = self._nzb_missing_counts.get(torrent_id, 0) + 1
                if _count >= 3:
                    logging.warning(f'[NZB] Job {job_id} not found in cli_mount after {_count} checks — declaring missing')
                    self._nzb_missing_counts.pop(torrent_id, None)
                    return PROGRESS_RESULT_MISSING
                self._nzb_missing_counts[torrent_id] = _count
                logging.debug(f'[NZB] Job {job_id} not found in cli_mount (miss {_count}/3)')
                return None
            # Job found — clear any miss counter
            self._nzb_missing_counts.pop(torrent_id, None)
            raw = status.get('raw', {})
            state = str(raw.get('state', status.get('state', 'unknown'))).lower()
            inner_status = str(raw.get('status', '')).lower()
            # cli_mount progress is 0.0–1.0
            raw_progress = raw.get('progress', status.get('progress', 0))
            progress_pct = int(float(raw_progress) * 100)
            is_complete = raw.get('is_complete', False)

            # Fully complete: is_complete=True OR state is pausedUP/completed with status=downloaded
            if is_complete or inner_status == 'downloaded' and state in ('pausedup', 'completed', 'seeding'):
                logging.info(f"[NZB] cli_mount job {job_id} complete (state={state} is_complete={is_complete})")
                return 100
            # Downloaded segments, still assembling — treat as 99% so we keep polling
            if inner_status == 'downloaded' and progress_pct >= 100:
                return 99
            # Failed
            if state in ('error', 'failed', 'bad') or raw.get('bad'):
                logging.warning(f"[NZB] cli_mount job {job_id} failed (state={state})")
                return PROGRESS_RESULT_MISSING
            # Not found in queue — may have completed and been cleaned up
            if status.get('state') == 'completed':
                return 100
            # Actively downloading
            if progress_pct > 0:
                return max(1, min(99, progress_pct))
            return None  # queued/unknown — retry later
        except Exception as e:
            logging.error(f"[NZB] Error polling cli_mount for job {job_id}: {e}")
            return None

    def get_torrent_progress(self, torrent_id: str) -> Union[int, str, None]:
        """
        Get the current progress percentage for a torrent or a status string.
        Returns:
            - int: Torrent progress percentage if successful.
            - PROGRESS_RESULT_MISSING (str): If the torrent is confirmed missing (404).
            - None: If there's a temporary issue (e.g., rate limit, other recoverable error),
                    or if progress cannot be determined for other reasons that warrant a retry.
        """
        # Route NZB jobs to cli_mount instead of debrid provider
        if str(torrent_id).startswith('nzb:'):
            return self._get_nzb_progress(torrent_id)

        try:
            status_result = self.debrid_provider.get_torrent_info_with_status(torrent_id)

            if status_result.status == TorrentFetchStatus.OK:
                if isinstance(status_result.data, dict):
                    progress = status_result.data.get('progress', 0)
                    rd_status = status_result.data.get('status', '')
                    # If RD says it's actively downloading but progress hasn't ticked up yet,
                    # return a small non-zero value so get_torrent_state treats it as 'downloading'
                    # rather than 'unknown' (which accumulates strikes and prematurely stalls it).
                    if progress == 0 and rd_status == 'downloading':
                        return 0.1
                    return progress
                return 0
            elif status_result.status == TorrentFetchStatus.NOT_FOUND:
                logging.info(f"Torrent {torrent_id} confirmed NOT FOUND (404) by provider.")
                return PROGRESS_RESULT_MISSING
            # New block to catch 404s hidden in error messages
            elif status_result.message and "404" in status_result.message and \
                 status_result.status in [
                     TorrentFetchStatus.CLIENT_ERROR,
                     TorrentFetchStatus.SERVER_ERROR,
                     TorrentFetchStatus.PROVIDER_HANDLED_ERROR,
                     TorrentFetchStatus.UNKNOWN_ERROR,
                     TorrentFetchStatus.REQUEST_ERROR
                 ]:
                logging.warning(
                    f"Torrent {torrent_id} appears to be NOT FOUND (404) based on error message "
                    f"'{status_result.message}' despite status being {status_result.status.value}. "
                    f"Treating as MISSING."
                )
                return PROGRESS_RESULT_MISSING
            elif status_result.status in [
                TorrentFetchStatus.RATE_LIMITED,
                TorrentFetchStatus.PROVIDER_HANDLED_ERROR, # Error logged by provider, treat as temp
                TorrentFetchStatus.SERVER_ERROR, # Often temporary (5xx)
                TorrentFetchStatus.CLIENT_ERROR, # Other 4xx errors, treat as temp
                TorrentFetchStatus.UNKNOWN_ERROR, # Non-HTTP or unparseable errors, treat as temp
                TorrentFetchStatus.REQUEST_ERROR # Network issues, often temporary
            ]:
                logging.warning(
                    f"Temporary issue fetching torrent {torrent_id} info: {status_result.status.value} - {status_result.message}. Will retry."
                )
                return None # Signal to retry later
            else: # Should not happen if all enum values are covered
                logging.error(f"Unhandled TorrentFetchStatus {status_result.status} for torrent {torrent_id}")
                return None

        except TimeoutError:
            logging.error(f"Timeout in get_torrent_progress for {torrent_id} after 45 seconds")
            return None # Treat timeout as temporary error for retry
        except Exception as e:
            logging.error(f"Exception in get_torrent_progress for {torrent_id}: {str(e)}", exc_info=True)
            return None # General error, treat as temporary for retry

    def get_torrent_state(self, torrent_id: str, increment_strikes: bool = True) -> str:
        """Get the current state of a torrent (downloaded, downloading, missing, stalled, or unknown)"""
        max_strikes = get_setting('Debug', 'max_unknown_strikes', default=DEFAULT_MAX_UNKNOWN_STRIKES)
        grace_period = get_setting('Debug', 'checking_grace_period_seconds', default=DEFAULT_CHECKING_GRACE_PERIOD_SECONDS)
        
        from queues.queue_manager import QueueManager
        queue_manager = QueueManager()

        try:
            progress_or_status = self.get_torrent_progress(torrent_id)

            if progress_or_status == PROGRESS_RESULT_MISSING:
                logging.info(f"Torrent {torrent_id} is missing. Handling via handle_missing_torrent.")
                if torrent_id in self.unknown_strikes: 
                    del self.unknown_strikes[torrent_id]
                    logging.debug(f"Cleared unknown strikes for {torrent_id} as it's now confirmed missing.")
                try:
                    self.handle_missing_torrent(torrent_id, queue_manager)
                    return 'missing' 
                except Exception as e:
                    logging.error(f"Failed to execute handle_missing_torrent for {torrent_id} within get_torrent_state: {str(e)}")
                    if increment_strikes:
                        self.unknown_strikes[torrent_id] = self.unknown_strikes.get(torrent_id, 0) + 1
                        logging.warning(f"Torrent {torrent_id} state is 'unknown' due to handle_missing_torrent failure. Strike {self.unknown_strikes[torrent_id]}/{max_strikes}.")
                        if self.unknown_strikes[torrent_id] >= max_strikes:
                            logging.warning(f"Torrent {torrent_id} reached max unknown strikes after handle_missing_torrent failure.")
                            self.handle_stalled_torrent(torrent_id, queue_manager, f"Max unknown strikes ({max_strikes}) reached after handle_missing_torrent failure.")
                            return 'stalled' 
                    return 'unknown'

            elif isinstance(progress_or_status, (int, float)):
                current_progress = progress_or_status

                if int(current_progress) == 100:
                    if torrent_id in self.unknown_strikes: 
                        del self.unknown_strikes[torrent_id]
                        logging.debug(f"Cleared unknown strikes for {torrent_id} as it's downloaded (100%).")
                    return 'downloaded'
                
                if current_progress > 0:
                    if torrent_id in self.unknown_strikes: 
                        del self.unknown_strikes[torrent_id]
                        logging.debug(f"Cleared unknown strikes for {torrent_id} as it's downloading (>0%).")
                    return 'downloading'
                
                if torrent_id in self.progress_checks:
                    last_progress = self.progress_checks[torrent_id]['last_progress']
                    if last_progress is not None and int(last_progress) == 100: 
                        if torrent_id in self.unknown_strikes: 
                            del self.unknown_strikes[torrent_id]
                            logging.debug(f"Cleared unknown strikes for {torrent_id} as history shows downloaded (100%).")
                        return 'downloaded'

                if increment_strikes:
                    # Find the first item associated with this torrent_id to check its add time.
                    # This assumes items are added to checking_queue_times when they enter the queue.
                    item_add_time = None
                    for item_id_in_dict, t_add_time in self.checking_queue_times.items():
                        # This is a bit indirect. We need to find an item in self.items that has this torrent_id
                        # and then use its id to get the time from checking_queue_times.
                        # A more direct way would be to store torrent_add_time if a torrent can exist without items,
                        # or ensure items for a torrent are added with their add times consistently.
                        # For now, let's find the OLDEST item associated with this torrent.
                        
                        # Find an item in self.items that matches item_id_in_dict and also has the current torrent_id
                        # This is inefficient. A better approach is needed if performance becomes an issue.
                        # A simpler way: find the minimum add_time for any of these items
                        
                        # Get all items in the current checking queue associated with this torrent_id
                        current_items_for_torrent = [item_obj for item_obj in self.items if item_obj.get('filled_by_torrent_id') == torrent_id]
                        if not current_items_for_torrent:
                             # If no items are found for this torrent_id (shouldn't happen if called from process loop for an active torrent)
                             # then we can't determine a grace period based on item add time. Proceed to strike.
                             pass # Fall through to strike increment
                        else:
                            # Find the minimum add_time for any of these items
                            min_add_time_for_torrent_items = float('inf')
                            found_any_item_time = False
                            for item_obj in current_items_for_torrent:
                                if item_obj['id'] in self.checking_queue_times:
                                    min_add_time_for_torrent_items = min(min_add_time_for_torrent_items, self.checking_queue_times[item_obj['id']])
                                    found_any_item_time = True
                            
                            if found_any_item_time and (time.time() - min_add_time_for_torrent_items) < grace_period:
                                logging.debug(f"Torrent {torrent_id} is within grace period ({grace_period}s) with 0% progress. Deferring strike.")
                                return 'unknown' # Return unknown, but don't increment strike yet.
                    
                    # Grace period passed or no item time found, proceed to increment strike for 0% progress.
                    self.unknown_strikes[torrent_id] = self.unknown_strikes.get(torrent_id, 0) + 1
                    logging.warning(f"Torrent {torrent_id} state is 'unknown' (progress: {current_progress}). Strike {self.unknown_strikes[torrent_id]}/{max_strikes}.")
                    if self.unknown_strikes[torrent_id] >= max_strikes:
                        logging.warning(f"Torrent {torrent_id} reached max unknown strikes ({max_strikes}) due to persistent progress {current_progress} or errors.")
                        self.handle_stalled_torrent(torrent_id, queue_manager, f"Max unknown strikes ({max_strikes}) reached with progress {current_progress}.")
                        return 'stalled'
                return 'unknown' # Return unknown if not incrementing strikes or if grace period deferred it

            elif progress_or_status is None: 
                if increment_strikes:
                    self.unknown_strikes[torrent_id] = self.unknown_strikes.get(torrent_id, 0) + 1
                    logging.warning(f"Could not determine progress for torrent {torrent_id} in get_torrent_state (temporary error). Strike {self.unknown_strikes[torrent_id]}/{max_strikes}. Returning 'unknown' state.")
                    if self.unknown_strikes[torrent_id] >= max_strikes:
                        logging.warning(f"Torrent {torrent_id} reached max unknown strikes ({max_strikes}) due to repeated temporary errors.")
                        self.handle_stalled_torrent(torrent_id, queue_manager, f"Max unknown strikes ({max_strikes}) reached due to temporary errors.")
                        return 'stalled'
                return 'unknown'

            else: 
                logging.error(f"Unexpected return value from get_torrent_progress for {torrent_id}: {progress_or_status} in get_torrent_state.")
                if increment_strikes:
                    self.unknown_strikes[torrent_id] = self.unknown_strikes.get(torrent_id, 0) + 1
                    logging.warning(f"Torrent {torrent_id} state is 'unknown' (unexpected progress value). Strike {self.unknown_strikes[torrent_id]}/{max_strikes}.")
                    if self.unknown_strikes[torrent_id] >= max_strikes:
                        logging.warning(f"Torrent {torrent_id} reached max unknown strikes ({max_strikes}) due to unexpected progress value.")
                        self.handle_stalled_torrent(torrent_id, queue_manager, f"Max unknown strikes ({max_strikes}) reached with unexpected progress value.")
                        return 'stalled'
                return 'unknown'

        except Exception as e:
            logging.error(f"General exception in get_state for torrent {torrent_id}: {str(e)}", exc_info=True)
            if increment_strikes:
                self.unknown_strikes[torrent_id] = self.unknown_strikes.get(torrent_id, 0) + 1
                logging.warning(f"Torrent {torrent_id} state is 'unknown' (general exception). Strike {self.unknown_strikes[torrent_id]}/{max_strikes}.")
                if self.unknown_strikes[torrent_id] >= max_strikes:
                    logging.warning(f"Torrent {torrent_id} reached max unknown strikes ({max_strikes}) due to general exception.")
                    self.handle_stalled_torrent(torrent_id, queue_manager, f"Max unknown strikes ({max_strikes}) reached due to general exception.")
                    return 'stalled'
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
        item_id_to_remove = item['id']
        torrent_id = item.get('filled_by_torrent_id')
        
        # Remove item from the list
        original_item_count = len(self.items)
        self.items = [i for i in self.items if i['id'] != item_id_to_remove]
        items_removed_count = original_item_count - len(self.items)

        if items_removed_count > 0:
            logging.debug(f"Successfully removed item {item_id_to_remove} from checking queue items list.")
        else:
            logging.debug(f"Item {item_id_to_remove} not found in checking queue items list during removal, or already removed.")

        if item_id_to_remove in self.checking_queue_times:
            del self.checking_queue_times[item_id_to_remove]
            logging.debug(f"Removed item {item_id_to_remove} from checking_queue_times.")
        
        # Log removal and check if any other items still reference this torrent
        remaining_items_with_torrent = [i for i in self.items if i.get('filled_by_torrent_id') == torrent_id]
        logging.debug(f"After removing item {item_id_to_remove}, torrent ID {torrent_id} is still referenced by {len(remaining_items_with_torrent)} items in the queue.")
        
        # If no more items reference this torrent, clean up progress checks and unknown strikes
        if torrent_id and not remaining_items_with_torrent:
            if torrent_id in self.progress_checks:
                del self.progress_checks[torrent_id]
                logging.debug(f"Cleaned up progress checks for torrent {torrent_id} as it has no more associated items.")
            if torrent_id in self.unknown_strikes:
                del self.unknown_strikes[torrent_id]
                logging.debug(f"Cleaned up unknown strikes for torrent {torrent_id} as it has no more associated items.")
        
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

        # Check for items associated with torrents that are no longer in self.items (due to stalling/missing handling)
        # and ensure they are marked for removal if not already handled.
        # This is a safety net, as handle_stalled_torrent and handle_missing_torrent should manage this.
        active_torrent_ids_in_queue = {item.get('filled_by_torrent_id') for item in self.items if item.get('filled_by_torrent_id')}

        items_by_torrent_id_to_process = {}
        temp_items_to_remove_stale = []

        for item in list(self.items): # Iterate over a copy if modifying self.items indirectly
            torrent_id = item.get('filled_by_torrent_id')
            if not torrent_id: # Item has no torrent ID, process individually or skip
                # This logic might need refinement based on how items without torrent_id are handled
                logging.debug(f"Item {item['id']} has no torrent ID, skipping grouped processing in main loop.")
                continue

            if torrent_id not in active_torrent_ids_in_queue:
                 # This can happen if handle_stalled_torrent or handle_missing_torrent removed all items for this torrent_id
                 # from self.items, but the loop iterating self.items hasn't caught up.
                 logging.debug(f"Item {item['id']} belongs to torrent {torrent_id} which seems to have been fully processed/removed. Marking for local removal if still present.")
                 temp_items_to_remove_stale.append(item)
                 continue

            if torrent_id not in items_by_torrent_id_to_process:
                items_by_torrent_id_to_process[torrent_id] = []
            items_by_torrent_id_to_process[torrent_id].append(item)
        
        for stale_item in temp_items_to_remove_stale:
            if self.contains_item_id(stale_item['id']): # Check before removing
                self.remove_item(stale_item) # Ensure it is removed from self.items
                logging.debug(f"Removed stale item {stale_item['id']} from checking queue during process loop.")

        # Process items by torrent ID using the filtered list
        for torrent_id, current_items_for_torrent in items_by_torrent_id_to_process.items():
            if not current_items_for_torrent: # Should not happen due to pre-filtering but good check
                logging.debug(f"Torrent {torrent_id} has no items left after filtering, skipping.")
                continue
            try:
                logging.debug(f"Processing torrent {torrent_id} with {len(current_items_for_torrent)} associated items")
                
                # Call get_torrent_state with increment_strikes=True (default) for authoritative check
                torrent_overall_state = self.get_torrent_state(torrent_id)

                if torrent_overall_state == 'missing' or torrent_overall_state == 'stalled':
                    logging.info(f"Torrent {torrent_id} handled as '{torrent_overall_state}' by get_torrent_state. Items should have been processed.")
                    # Items are moved/removed by handle_missing_torrent or handle_stalled_torrent called within get_torrent_state.
                    # The main self.items list is modified by those handlers.
                    # No need to add to items_to_remove here as the handlers should do it.
                    continue

                # If state is not 'missing' or 'stalled', proceed with other checks
                # We need progress for further logic if not stalled/missing
                progress_or_status = self.get_torrent_progress(torrent_id)

                if progress_or_status is None: # Temporary error from get_torrent_progress, already handled by get_torrent_state for stalling
                    logging.warning(f"Temporary issue determining progress for torrent {torrent_id} in process method (state: {torrent_overall_state}). Skipping active processing this cycle.")
                    continue
                
                # If we are here, progress_or_status is an int or float (or PROGRESS_RESULT_MISSING, but that's covered by 'missing' state)
                if progress_or_status == PROGRESS_RESULT_MISSING: # Should be caught by get_torrent_state
                    logging.warning(f"Torrent {torrent_id} became missing during process loop after get_torrent_state. Re-evaluating.")
                    self.handle_missing_torrent(torrent_id, queue_manager)
                    # items_to_remove.extend(current_items_for_torrent) # Items removed by handler
                    continue

                current_progress = progress_or_status
                if not isinstance(current_progress, (int, float)):
                    logging.error(f"Unexpected type for current_progress after checks: {type(current_progress)}. Value: {current_progress}. Skipping torrent {torrent_id}.")
                    continue

                if torrent_id not in self.progress_checks:
                    logging.debug(f"Initializing progress check for torrent {torrent_id} with progress {current_progress}")
                    self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}
                
                last_check = self.progress_checks[torrent_id]['last_check']
                last_progress = self.progress_checks[torrent_id]['last_progress']
                logging.debug(f"Torrent {torrent_id} - Current progress: {current_progress}%, Last progress: {last_progress}%, Time since last check: {current_time - last_check}s")
                
                if int(current_progress) == 100:
                    # --- NZB: resolve actual folder/file names from cli_mount before symlink/Plex checks ---
                    if str(torrent_id).startswith('nzb:'):
                        # Guard: if no filled_by_file on any item in this group, we have nothing
                        # to look up in cli_mount — add to not-wanted and send back to Wanted.
                        _any_file = any(i.get('filled_by_file') for i in current_items_for_torrent)
                        if not _any_file:
                            logging.warning(f'[NZB] {torrent_id} has no filled_by_file on any item — adding to not-wanted and moving group back to Wanted')
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_nzb_guid_f
                            for _item in list(current_items_for_torrent):
                                try:
                                    _nzb_url = _item.get('filled_by_magnet', '')
                                    if _nzb_url:
                                        _add_nzb_guid_f(_nzb_url)
                                except Exception:
                                    pass
                                if self.contains_item_id(_item['id']):
                                    queue_manager.move_to_wanted(_item, 'Checking')
                            continue
                        # Version-scoped grouping: a real season pack always shares one version
                        # across its episodes. If items sharing this torrent_id span more than
                        # one version, that indicates a stale/corrupted shared job id (e.g. from
                        # a since-fixed dedup bug) rather than a genuine pack — resolving them
                        # together would collapse two different versions onto the same file.
                        # Only process the subset matching the first item's version this pass;
                        # other-version items are left for a later pass once their own torrent_id
                        # is corrected (e.g. by a fresh scrape/resubmission).
                        def _norm_ver(_v):
                            return (_v or 'Default').rstrip('*')
                        _group_ver = _norm_ver(current_items_for_torrent[0].get('version'))
                        if any(_norm_ver(_i.get('version')) != _group_ver for _i in current_items_for_torrent):
                            logging.warning(
                                f'[NZB] Torrent {torrent_id} has items spanning multiple versions '
                                f'({sorted(set(_norm_ver(_i.get("version")) for _i in current_items_for_torrent))}) — '
                                f'processing only version {_group_ver!r} this pass to avoid cross-version collapse'
                            )
                            current_items_for_torrent = [
                                _i for _i in current_items_for_torrent if _norm_ver(_i.get('version')) == _group_ver
                            ]
                        self._resolve_nzb_file_info(torrent_id, current_items_for_torrent)
                        # Reload items from DB so updated fields are visible to symlink/plex checks
                        from database import get_all_media_items
                        current_items_for_torrent = [
                            dict(item) for item in get_all_media_items(state='Checking')
                            if item.get('filled_by_torrent_id') == torrent_id
                            and _norm_ver(item.get('version')) == _group_ver
                        ]
                        # If resolve still hasn't found the file after multiple attempts, move back to Wanted
                        import os as _os
                        _sample = current_items_for_torrent[0] if current_items_for_torrent else {}
                        _fbf = _sample.get('filled_by_file') or ''
                        _VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}
                        if _fbf and _os.path.splitext(_fbf)[1].lower() not in _VIDEO_EXTS:
                            # filled_by_file has no video extension — resolve hasn't succeeded
                            if torrent_id not in self.progress_checks:
                                self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': 100, 'nzb_resolve_failures': 0}
                            self.progress_checks[torrent_id]['nzb_resolve_failures'] = self.progress_checks[torrent_id].get('nzb_resolve_failures', 0) + 1
                            failures = self.progress_checks[torrent_id]['nzb_resolve_failures']
                            logging.warning(f'[NZB] {torrent_id} resolve failure #{failures} — folder not found in cli_mount')
                            if failures >= 5:
                                logging.warning(f'[NZB] {torrent_id} failed to resolve after {failures} attempts — adding to not-wanted and moving back to Wanted')
                                self.progress_checks[torrent_id]['nzb_resolve_failures'] = 0
                                from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_nzb_guid_r
                                for item_to_move in list(current_items_for_torrent):
                                    if self.contains_item_id(item_to_move['id']):
                                        try:
                                            _nzb_url = item_to_move.get('filled_by_magnet', '')
                                            if _nzb_url:
                                                _add_nzb_guid_r(_nzb_url)
                                        except Exception:
                                            pass
                                        # If ghostlisted, can't move to Wanted — set Blacklisted and remove from queue
                                        if item_to_move.get('ghostlisted'):
                                            logging.warning(f'[NZB] {torrent_id} item {item_to_move["id"]} is ghostlisted — setting Blacklisted and removing from Checking')
                                            from database.database_writing import update_media_item_state
                                            update_media_item_state(item_to_move['id'], 'Blacklisted')
                                            self.remove_item(item_to_move)
                                        else:
                                            queue_manager.move_to_wanted(item_to_move, 'Checking')
                                continue
                        elif _fbf:
                            # Resolve succeeded — register cli_debrid IDs with cli_mount now
                            # that all episodes have their filenames. Covers ALL modes.
                            try:
                                import re as _re_ih2
                                _cli_ids2 = {}
                                # Query ALL siblings by torrent_id across all states
                                # (Checking, Collected, Upgrading) — season pack episodes
                                # move to Collected one-by-one, so current_items_for_torrent
                                # only has the episode currently in Checking.
                                try:
                                    from database import get_all_media_items as _gami2
                                    _all_sibs2 = [
                                        dict(i) for i in _gami2()
                                        if i.get('filled_by_torrent_id') == torrent_id
                                        and i.get('filled_by_file')
                                    ]
                                except Exception:
                                    _all_sibs2 = current_items_for_torrent
                                for _item2 in _all_sibs2:
                                    _fbf2 = (_item2.get('filled_by_file') or '').strip()
                                    _iid2 = _item2.get('id')
                                    if _fbf2 and _iid2:
                                        _cli_ids2[_fbf2] = _iid2
                                if _cli_ids2:
                                    _tid2 = str(torrent_id)
                                    _ih2 = _tid2[4:] if _tid2.startswith('nzb:') else ''
                                    if not _ih2:
                                        _mag2 = current_items_for_torrent[0].get('filled_by_magnet') or ''
                                        _m2 = _re_ih2.search(r'urn:btih:([0-9a-fA-F]{40})', _mag2, _re_ih2.IGNORECASE)
                                        if _m2:
                                            _ih2 = _m2.group(1).lower()
                                    if _ih2:
                                        try:
                                            from usenet.climount_client import get_climount_client as _get_dc2
                                            _dc2 = _get_dc2()
                                            if _dc2 and _dc2.is_enabled():
                                                _dc2.register_cli_ids(_ih2, _cli_ids2)
                                                logging.info(f'[CheckingQueue] Registered {len(_cli_ids2)} cli_debrid IDs for {_ih2}')
                                                _dc2.push_tags_for_item(_ih2, next(iter(_cli_ids2.values())))
                                        except Exception as _r2:
                                            logging.debug(f'[CheckingQueue] cli_ids registration error: {_r2}')
                            except Exception as _r2e:
                                logging.debug(f'[CheckingQueue] cli_ids build error: {_r2e}')

                            # Trigger targeted Plex scan once per item, async
                            if torrent_id not in self._nzb_plex_scan_triggered:
                                self._nzb_plex_scan_triggered.add(torrent_id)
                                _scan_mount = get_setting('Plex', 'mounted_file_location', '').rstrip('') or get_setting('Usenet Provider', 'mounted_file_location', '').rstrip('/')
                                _scan_folder = _sample.get('debrid_folder_name') or _sample.get('filled_by_title') or ''
                                _scan_itype = _sample.get('type', 'movie')
                                if _scan_mount and _scan_folder:
                                    import threading as _threading
                                    def _do_plex_scan(_mount, _folder, _itype):
                                        try:
                                            for _cat in _os.listdir(_mount):
                                                _candidate = _os.path.join(_mount, _cat, _folder)
                                                if _os.path.isdir(_candidate):
                                                    plex_update_item({'full_path': _candidate, 'location_on_disk': _candidate, 'type': _itype})
                                                    logging.info(f'[NZB] Triggered Plex scan for {_candidate!r}')
                                                    return
                                            logging.debug(f'[NZB] Folder {_folder!r} not found under {_mount!r} for Plex scan')
                                        except Exception as _pe:
                                            logging.debug(f'[NZB] Plex scan failed: {_pe}')
                                    _threading.Thread(target=_do_plex_scan, args=(_scan_mount, _scan_folder, _scan_itype), daemon=True).start()

                    # --- Restored Symlinked/Local processing logic ---
                    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                        items_to_scan = []
                        # Collect unique directories for batch Plex scan (prevents hammering Plex API)
                        # Store as dict: {directory: item_type} to preserve type information for section matching
                        directories_to_scan = {}
                        use_jellyfin = get_setting('Debug', 'emby_jellyfin_url', default=False)
                        use_plex = get_setting('File Management', 'plex_url_for_symlink', default=False)

                        for item_in_torrent_group in current_items_for_torrent:
                            try:
                                time_in_queue_for_item = current_time - self.checking_queue_times[item_in_torrent_group['id']]
                            except KeyError:
                                logging.warning(f"Item {item_in_torrent_group['id']} missing from checking_queue_times. Initializing.")
                                self.checking_queue_times[item_in_torrent_group['id']] = current_time
                                time_in_queue_for_item = 0

                            logging.debug(f"[CheckingQueue Process] Preparing to call check_local_file_for_item for Item ID {item_in_torrent_group.get('id')}")
                            logging.debug(f"[CheckingQueue Process] Item Data: upgrading_from='{item_in_torrent_group.get('upgrading_from')}', state='{item_in_torrent_group.get('state')}', filled_by_file='{item_in_torrent_group.get('filled_by_file')}'")

                            use_extended_search = time_in_queue_for_item > 900 and (time_in_queue_for_item % 900) < 300
                            logging.debug(f"Using extended search: {use_extended_search} (time in queue: {time_in_queue_for_item:.1f}s)")

                            processing_successful = check_local_file_for_item(
                                item_in_torrent_group,
                                extended_search=use_extended_search,
                            )

                            if processing_successful:
                                logging.info(f"Local file found and symlinked for item {item_in_torrent_group['id']} by CheckingQueue.")
                                updated_item_data = get_media_item_by_id(item_in_torrent_group['id'])
                                if not updated_item_data:
                                        logging.error(f"Failed to fetch updated data for item {item_in_torrent_group['id']} after successful local check. Skipping media server update.")
                                else:
                                        logging.debug(f"Fetched updated data for item {item_in_torrent_group['id']} before media server update.")
                                        # Collect directory for batch scan instead of scanning immediately
                                        # This prevents Plex API spam when processing season packs
                                        file_location = updated_item_data.get('full_path') or updated_item_data.get('location_on_disk') or updated_item_data.get('location')
                                        if file_location:
                                            import os
                                            directory = os.path.dirname(file_location)
                                            if directory:
                                                # Store item type with directory for proper section matching
                                                item_type = updated_item_data.get('type', 'episode')  # Default to episode for shows
                                                directories_to_scan[directory] = item_type

                                conn = get_db_connection()
                                cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item_in_torrent_group['id'],))
                                current_state_row = cursor.fetchone()
                                conn.close()
                                current_item_state = current_state_row['state'] if current_state_row else None

                                if current_item_state == 'Upgrading':
                                    logging.info(f"Item {item_in_torrent_group['id']} is marked for upgrading, keeping in Upgrading state after local check.")
                                elif current_item_state == 'Collected':
                                    logging.info(f"Item {item_in_torrent_group['id']} state confirmed as Collected after local check.")
                                    queue_manager.move_to_collected(item_in_torrent_group, "Checking", skip_notification=True)
                                    # Sync labels now that item is detected in Plex
                                    try:
                                        from utilities.plex_label_manager import sync_labels_to_plex_for_item, is_plex_labels_enabled_anywhere
                                        if is_plex_labels_enabled_anywhere():
                                            sync_labels_to_plex_for_item(item_in_torrent_group['id'])
                                    except Exception as e:
                                        logging.error(f"Error syncing labels for item {item_in_torrent_group['id']}: {e}")
                                elif current_item_state:
                                    logging.warning(f"Item {item_in_torrent_group['id']} processed locally but state is '{current_item_state}'. Moving to Collected.")
                                    queue_manager.move_to_collected(item_in_torrent_group, "Checking", skip_notification=True)
                                    # Sync labels now that item is detected in Plex
                                    try:
                                        from utilities.plex_label_manager import sync_labels_to_plex_for_item, is_plex_labels_enabled_anywhere
                                        if is_plex_labels_enabled_anywhere():
                                            sync_labels_to_plex_for_item(item_in_torrent_group['id'])
                                    except Exception as e:
                                        logging.error(f"Error syncing labels for item {item_in_torrent_group['id']}: {e}")
                                else:
                                    logging.error(f"Item {item_in_torrent_group['id']} processed locally but seems missing from DB. Cannot confirm final state.")
                            else:
                                logging.debug(f"Local file not found yet for item {item_in_torrent_group['id']}. It might appear later.")
                                items_to_scan.append(item_in_torrent_group)

                        # Batch media server update - scan each unique directory ONCE after processing all items
                        # This dramatically reduces Plex API calls for season packs (e.g., 20 episodes -> 1 scan instead of 20)
                        if directories_to_scan:
                            logging.info(f"[CheckingQueue] Batch media server update: {len(directories_to_scan)} unique directories to scan")
                            for directory, item_type in directories_to_scan.items():
                                if use_jellyfin:
                                    # For Jellyfin, create a minimal item dict with the directory and type
                                    emby_update_item({'full_path': directory, 'location_on_disk': directory, 'type': item_type})
                                elif use_plex:
                                    # For Plex, create a minimal item dict with the directory and type
                                    plex_update_item({'full_path': directory, 'location_on_disk': directory, 'type': item_type})
                            logging.info(f"[CheckingQueue] Batch media server update complete")

                        if items_to_scan:
                            logging.info("Full library scan disabled for now")

                        # Register cli_debrid IDs with cli_mount — one call per torrent group
                        # Maps each episode filename to its media_items.id for direct sync lookups
                        try:
                            import re as _re_ih
                            _cli_ids = {}
                            # Include all siblings across all states (Checking+Collected+Upgrading)
                            # since season pack episodes move to Collected one-by-one.
                            _sample = current_items_for_torrent[0] if current_items_for_torrent else {}
                            _tid_all = str(_sample.get('filled_by_torrent_id') or '')
                            try:
                                from database import get_all_media_items as _gami
                                _all_sibs = [
                                    dict(i) for i in _gami()
                                    if i.get('filled_by_torrent_id') == _tid_all
                                    and i.get('filled_by_file')
                                ] if _tid_all else current_items_for_torrent
                            except Exception:
                                _all_sibs = current_items_for_torrent
                            for _item in _all_sibs:
                                _fbf = (_item.get('filled_by_file') or '').strip()
                                _iid = _item.get('id')
                                if _fbf and _iid:
                                    _cli_ids[_fbf] = _iid
                            if _cli_ids:
                                _tid = str(_sample.get('filled_by_torrent_id') or '')
                                _infohash = ''
                                if _tid.startswith('nzb:'):
                                    _infohash = _tid[4:]
                                else:
                                    _mag = _sample.get('filled_by_magnet') or ''
                                    _m = _re_ih.search(r'urn:btih:([0-9a-fA-F]{40})', _mag, _re_ih.IGNORECASE)
                                    if _m:
                                        _infohash = _m.group(1).lower()
                                if _infohash:
                                    from usenet.climount_client import get_climount_client as _get_dc
                                    _dc = _get_dc()
                                    if _dc and _dc.is_enabled():
                                        _dc.register_cli_ids(_infohash, _cli_ids)
                                        _dc.push_tags_for_item(_infohash, next(iter(_cli_ids.values())))
                        except Exception as _cli_reg_err:
                            logging.debug(f'[CheckingQueue] cli_ids registration error: {_cli_reg_err}')
                    # --- End of restored Symlinked/Local processing logic ---

                    oldest_item_time = min((self.checking_queue_times.get(item['id'], current_time) for item in current_items_for_torrent), default=current_time)
                    time_in_queue = current_time - oldest_item_time
                    checking_queue_limit = self._calculate_dynamic_queue_period(current_items_for_torrent)
                    
                    logging.info(f"Torrent {torrent_id} has been in checking queue for {time_in_queue:.1f} seconds (dynamic limit: {checking_queue_limit} seconds for {len(current_items_for_torrent)} items)")
                    
                    if time_in_queue > checking_queue_limit:
                        if str(torrent_id).startswith('nzb:'):
                            logging.info(f"NZB {torrent_id} content not found within {checking_queue_limit}s — adding to not-wanted and moving back to Wanted")
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_nzb_guid_t
                            for _item_nw in current_items_for_torrent:
                                try:
                                    _nzb_url = _item_nw.get('filled_by_magnet', '')
                                    if _nzb_url:
                                        _add_nzb_guid_t(_nzb_url)
                                except Exception:
                                    pass
                        else:
                            logging.info(f"Removing torrent {torrent_id} from debrid service as content was not found within {checking_queue_limit} seconds (dynamic limit for {len(current_items_for_torrent)} items)")
                            try:
                                self.debrid_provider.remove_torrent(
                                    torrent_id,
                                    removal_reason=f"Content not found in checking queue after {checking_queue_limit} seconds (dynamic limit for {len(current_items_for_torrent)} items)"
                                )
                            except Exception as e:
                                logging.error(f"Failed to remove torrent {torrent_id}: {str(e)}")
                        for item_to_move in list(current_items_for_torrent):
                            if self.contains_item_id(item_to_move['id']):
                                queue_manager.move_to_wanted(item_to_move, "Checking")
                        continue

                # Skip remaining checks if the torrent is completed
                if int(current_progress) == 100:
                    continue
                
                if current_time - last_check >= 300:  # 5 minutes
                    if current_progress == last_progress:
                        logging.info(f"No progress for torrent {torrent_id} in 5 minutes. Handling failed upgrade/download.")
                        if str(torrent_id).startswith('nzb:'):
                            logging.info(f"NZB {torrent_id} stalled — skipping debrid removal")
                        else:
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

                        for item in list(current_items_for_torrent): # Iterate over a copy
                            if not self.contains_item_id(item['id']): # Check if item still exists
                                logging.debug(f"Item {item['id']} no longer in queue, skipping no-progress handling for it.")
                                continue

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
                                    else:
                                        hash_value = extract_hash_from_magnet(magnet)
                                        if hash_value: add_to_not_wanted(hash_value)
                                except Exception as e:
                                    logging.error(f"Failed to process magnet for not wanted: {str(e)}")

                        items_to_remove.extend(current_items_for_torrent) # Items are handled (moved or state restored)
                    else:
                        self.progress_checks[torrent_id] = {'last_check': current_time, 'last_progress': current_progress}

            except Exception as e:
                logging.error(f"Error processing items for torrent {torrent_id} in checking queue: {e}", exc_info=True)

        # The items_to_remove list was mainly for the old structure.
        # With handlers directly modifying self.items, this final loop might be redundant or need adjustment.
        # For now, keeping it to catch any stragglers, but ideally, handlers manage their items.
        final_items_to_remove_explicitly = [] 
        # Populate final_items_to_remove_explicitly based on any logic that flags items without direct removal in handlers.
        # This is likely empty if handlers are robust.

        for item in final_items_to_remove_explicitly:
            if self.contains_item_id(item['id']): 
                 self.remove_item(item)
            else:
                 logging.debug(f"Item {item.get('id')} already removed from checking queue, skipping final removal.")

        # After processing all torrents, check if we need to run Plex scan
        if not get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            # Only run Plex scan if we have any completed torrents
            if any(self.get_torrent_progress(torrent_id) == 100 for torrent_id in items_by_torrent_id_to_process.keys()):
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
        
        # Calculate current torrent IDs present in the queue
        current_torrent_ids = {item.get('filled_by_torrent_id') for item in self.items if item.get('filled_by_torrent_id')}

        # Clean up progress_checks for torrents no longer in queue
        self.progress_checks = {k: v for k, v in self.progress_checks.items() if k in current_torrent_ids}
        # Clean up unknown_strikes for torrents no longer in queue
        self.unknown_strikes = {k: v for k, v in self.unknown_strikes.items() if k in current_torrent_ids}
        
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

    def handle_stalled_torrent(self, torrent_id: str, queue_manager, reason: str):
        """
        Handle a torrent that has stalled or repeatedly resulted in unknown states.
        """
        logging.warning(f"Handling stalled/failed torrent {torrent_id} due to: {reason}")
        
        # Get all items in the checking queue with this torrent ID *at this moment*
        # Iterate over a copy of self.items for safety if handlers modify it.
        items_for_stalled_torrent = [item for item in list(self.items) if item.get('filled_by_torrent_id') == torrent_id]

        if not items_for_stalled_torrent:
            logging.warning(f"No items found for stalled torrent {torrent_id} at the moment of handling.")
            # Clean up strike counter if it exists, as there are no items to process
            if torrent_id in self.unknown_strikes:
                del self.unknown_strikes[torrent_id]
                logging.debug(f"Cleared unknown strikes for {torrent_id} as no items were found for stalling.")
            return
        
        logging.info(f"Found {len(items_for_stalled_torrent)} items for stalled torrent {torrent_id}: {[item['id'] for item in items_for_stalled_torrent]}")

        # Attempt to remove torrent from debrid provider
        try:
            logging.info(f"Attempting to remove torrent {torrent_id} from debrid service due to: {reason}")
            self.debrid_provider.remove_torrent(torrent_id, removal_reason=f"Stalled/Failed: {reason}")
            logging.info(f"Successfully removed torrent {torrent_id} from debrid service.")
        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code == 404:
                    logging.warning(f"Torrent {torrent_id} was already removed from debrid service (404). Continuing with stalled handling.")
                elif e.response.status_code == 429:
                    logging.warning(f"Rate limited (429) when trying to remove torrent {torrent_id} from debrid. "
                                    f"Aborting stalled handling for this torrent in this cycle. It will be retried later.")
                    return # Abort and retry later
                else:
                    logging.error(f"HTTP error ({e.response.status_code}) while removing stalled torrent {torrent_id} from debrid: {str(e)}. "
                                  f"Proceeding with item handling as stalled.")
            else:
                logging.error(f"HTTP error (no response object) while removing stalled torrent {torrent_id} from debrid: {str(e)}. "
                              f"Proceeding with item handling as stalled.")
        except ProviderUnavailableError as e:
            exc_str = str(e)  # Capture the exception string once
            # Log the captured string for better diagnostics
            logging.info(f"Handling ProviderUnavailableError for torrent {torrent_id}. Exception string: '{exc_str}'")

            # Explicitly check for 429 first
            if "429" in exc_str:
                logging.warning(
                    f"Rate limit (429) detected for torrent {torrent_id} (ProviderUnavailableError). "
                    f"Details: {exc_str}. Aborting stalled handling for this torrent in this cycle to allow for later retry."
                )
                return # This is the key retry mechanism: abort current handling

            # Then check for 404
            elif "404" in exc_str:
                logging.warning(
                    f"Not found (404) detected for torrent {torrent_id} (ProviderUnavailableError). "
                    f"Details: {exc_str}. Continuing with stalled handling as if already removed."
                )
                # For a 404, we proceed with the rest of handle_stalled_torrent (e.g., blacklisting, moving items)
                # as the torrent is effectively gone. No return here.
            else: # Not a 429 or 404 clearly identified in the ProviderUnavailableError string
                logging.error(
                    f"Provider unavailable error (neither 429 nor 404 clearly identified in message) "
                    f"while removing stalled torrent {torrent_id}. Details: {exc_str}. "
                    f"Proceeding with item handling as stalled."
                )
            # If not a 429, processing continues below (e.g. blacklisting magnets, moving items)
        except Exception as e: # Catch other potential exceptions from the provider
            logging.error(f"Unexpected error while removing stalled torrent {torrent_id} from debrid: {str(e)}. "
                          f"Proceeding with item handling as stalled.")

        # Collect unique magnets to add to not-wanted list
        magnets_to_add_to_not_wanted = []
        for item in items_for_stalled_torrent:
            magnet = item.get('filled_by_magnet')
            if magnet and magnet not in magnets_to_add_to_not_wanted:
                magnets_to_add_to_not_wanted.append(magnet)
        
        for magnet in magnets_to_add_to_not_wanted:
            try:
                logging.info(f"Adding magnet to not-wanted list due to stalled torrent: {magnet[:50]}...")
                # Re-importing here to ensure availability if called from different contexts
                from database.not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
                from debrid.common import extract_hash_from_magnet, download_and_extract_hash
                
                if magnet.startswith('http'):
                    hash_value = download_and_extract_hash(magnet) # This can raise exceptions
                    if hash_value: add_to_not_wanted(hash_value)
                    add_to_not_wanted_urls(magnet) # Add original URL as well
                else:
                    hash_value = extract_hash_from_magnet(magnet)
                    if hash_value: add_to_not_wanted(hash_value)
            except Exception as e:
                logging.error(f"Failed to add magnet to not-wanted list for stalled torrent: {str(e)}")
        
        upgrading_queue = None # Initialize for potential use

        for item in items_for_stalled_torrent: # Iterate over the collected list
            # Double-check if item is still in self.items before processing, as it might have been handled by another concurrent process
            if not self.contains_item_id(item['id']):
                logging.debug(f"Item {item['id']} (for stalled torrent {torrent_id}) no longer in self.items. Skipping.")
                continue

            try:
                item_id = item.get('id', 'N/A')
                item_identifier = queue_manager.generate_identifier(item) # item_identifier for logging
                is_upgrade = item.get('upgrading') or item.get('upgrading_from') is not None

                if is_upgrade:
                    logging.info(f"Detected failed upgrade for {item_identifier} due to stalled torrent: {reason}")
                    if upgrading_queue is None:
                        upgrading_queue = UpgradingQueue() # Lazily initialize

                    upgrading_queue.log_failed_upgrade(item, item.get('filled_by_title', 'Unknown'), f"Stalled torrent: {reason}")
                    
                    notification_data = {
                        'title': item.get('title', 'Unknown Title'),
                        'year': item.get('year', ''),
                        'reason': f"Stalled torrent: {reason}"
                    }
                    send_upgrade_failed_notification(notification_data)

                    if upgrading_queue.restore_item_state(item):
                        upgrading_queue.add_failed_upgrade(
                            item['id'],
                            {'title': item.get('filled_by_title'), 'magnet': item.get('filled_by_magnet'), 'torrent_id': torrent_id, 'reason': f'stalled_torrent: {reason}'}
                        )
                        logging.info(f"Successfully reverted failed upgrade for {item_identifier} (stalled torrent).")
                    else:
                        # GHOSTLIST CHECK: Don't move ghostlisted/blacklisted items back to Wanted
                        item_state = item.get('state', '')
                        is_ghostlisted = item.get('ghostlisted') == 1
                        is_blacklisted = item_state == 'Blacklisted'

                        if is_ghostlisted or is_blacklisted:
                            logging.info(f"⛔ Skipping item {item_id} - item is {'ghostlisted' if is_ghostlisted else 'blacklisted'}, not moving back to Wanted (stalled torrent fallback)")
                        else:
                            logging.error(f"Failed to restore previous state for {item_identifier} after stalled torrent. Moving to Wanted as fallback.")
                            queue_manager.move_to_wanted(item, "Checking")
                else:
                    # GHOSTLIST CHECK: Don't move ghostlisted/blacklisted items back to Wanted
                    item_state = item.get('state', '')
                    is_ghostlisted = item.get('ghostlisted') == 1
                    is_blacklisted = item_state == 'Blacklisted'

                    if is_ghostlisted or is_blacklisted:
                        logging.info(f"⛔ Skipping item {item_id} - item is {'ghostlisted' if is_ghostlisted else 'blacklisted'}, not moving back to Wanted (stalled torrent)")
                    else:
                        # For non-upgrade items, move back to Wanted state to trigger re-scraping
                        logging.info(f"Moving item {item_identifier} back to Wanted state due to stalled torrent: {reason}")
                        queue_manager.move_to_wanted(item, "Checking")
                
                # remove_item is called by move_to_wanted or restore_item_state indirectly through DB state change and queue update.
                # Explicitly ensure it's removed from the Python object list if not already.
                # This call to remove_item will also handle cleaning up progress_checks and unknown_strikes if this torrent_id has no more items.
                if self.contains_item_id(item_id): # Check again before explicit removal
                    self.remove_item(item) 
                    logging.info(f"Ensured item {item_id} (associated with stalled torrent {torrent_id}) is removed from checking queue object.")

            except Exception as e:
                logging.error(f"Failed to handle item {item.get('id', 'N/A')} for stalled torrent {torrent_id}: {str(e)}", exc_info=True)
        
        # Clean up strike counter for this torrent_id after all its items are processed
        if torrent_id in self.unknown_strikes:
            del self.unknown_strikes[torrent_id]
            logging.debug(f"Cleared unknown strikes for {torrent_id} after handling as stalled.")
        
        # Final check: if after all this, items for this torrent_id still exist in self.items, log it.
        # This shouldn't happen if logic is correct.
        remaining_after_stall_handling = [i for i in self.items if i.get('filled_by_torrent_id') == torrent_id]
        if remaining_after_stall_handling:
            logging.error(f"CRITICAL: {len(remaining_after_stall_handling)} items for stalled torrent {torrent_id} still in queue after handling: {[i['id'] for i in remaining_after_stall_handling]}")