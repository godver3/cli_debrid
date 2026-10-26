"""Housekeeping: purges, backups, notifications, time sync, queue reconciliation, stats, database health, Upgrade Hub.

Methods of ProgramRunner (queues/run_program.py), kept here to keep that file manageable.
"""
from database.not_wanted_magnets import purge_not_wanted_magnets_file
import logging
import time
from pathlib import Path
import json
import os
import pickle
import requests
from routes.notifications import send_notifications
import uuid
import ntplib
from utilities.log_redaction import RedactingFormatter
from utilities.settings import get_setting
from debrid import ProviderUnavailableError
import asyncio
from debrid import get_debrid_provider


class MaintenanceTasks:
    def task_purge_not_wanted_magnets_file(self):
        purge_not_wanted_magnets_file()

    # Manual-only (Debug -> Run task manually); never scheduled.
    def task_purge_not_wanted_urls_file(self):
        from database.not_wanted_magnets import purge_not_wanted_urls_file
        purge_not_wanted_urls_file()

    def task_purge_not_wanted_nzbs_files(self):
        from database.not_wanted_magnets import purge_not_wanted_nzbs_files
        purge_not_wanted_nzbs_files()

    def task_purge_all_not_wanted_files(self):
        from database.not_wanted_magnets import purge_all_not_wanted_files
        purge_all_not_wanted_files()

    def task_reset_trakt_auth(self):
        from utilities.trakt_auth_cleanup import reset_trakt_auth
        reset_trakt_auth()

    def task_debug_log(self):
        current_time = time.time()
        debug_info = []
        for task, interval in self.task_intervals.items():
            time_until_next_run = interval - (current_time - self.last_run_times[task])
            minutes, seconds = divmod(int(time_until_next_run), 60)
            hours, minutes = divmod(minutes, 60)
            debug_info.append(f"{task}: {hours:02d}:{minutes:02d}:{seconds:02d}")

        logging.info("Time until next task run:\n" + "\n".join(debug_info))

    def task_send_notifications(self):
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content/')
        notifications_file = Path(db_content_dir) / "collected_notifications.pkl"
        
        if not notifications_file.exists():
            return

        # Generate a unique temporary filename in the same directory
        unique_suffix = f".{uuid.uuid4()}.tmp"
        temp_notifications_file = notifications_file.with_suffix(unique_suffix)

        try:
            # Atomically move the file for processing
            notifications_file.rename(temp_notifications_file)
        except FileNotFoundError:
            logging.debug("Notifications file disappeared before processing, another worker likely picked it up.")
            return

        notifications = []
        try:
            with open(temp_notifications_file, "rb") as f:
                notifications = pickle.load(f)
        except (pickle.UnpicklingError, EOFError, FileNotFoundError) as pe:
            logging.error(f"Error reading notifications pickle file ({temp_notifications_file}): {pe}. Discarding file.")
        except Exception as e_read:
            logging.error(f"Error processing unique temp file read ({temp_notifications_file}): {e_read}", exc_info=True)
        finally:
            # Always attempt to clean up the temporary file
            try:
                temp_notifications_file.unlink()
            except OSError as e_unlink:
                logging.error(f"Failed to remove processed temp notification file {temp_notifications_file}: {e_unlink}")

        if notifications:
            # Fetch enabled notifications using CLI_DEBRID_PORT
            port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
            try:
                response = requests.get(f'http://localhost:{port}/settings/notifications/enabled', timeout=10) # Add timeout
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                enabled_notifications = response.json().get('enabled_notifications', {})
                
                # Send notifications
                send_notifications(notifications, enabled_notifications)
                
                logging.info(f"Sent {len(notifications)} notifications.")

            except requests.exceptions.RequestException as req_err:
                logging.error(f"Failed to fetch enabled notifications: {req_err}")
                # Re-queue notifications if fetching config fails?
                # For simplicity now, we log error and notifications are lost for this cycle.
                # Could re-pickle 'notifications' back to the original file path.
            except json.JSONDecodeError as json_err:
                 logging.error(f"Failed to parse enabled notifications response: {json_err}")
            except Exception as e_send:
                logging.error(f"Error sending notifications: {str(e_send)}", exc_info=True)

    def task_sync_time(self):
        """Check system clock against NTP and log any significant offset.

        Tries multiple NTP servers in order so that a single unresponsive
        server does not cause the task to always fail.
        """
        ntp_servers = [
            'pool.ntp.org',
            'time.cloudflare.com',
            'time.google.com',
        ]
        ntp_client = ntplib.NTPClient()
        last_error = None

        for server in ntp_servers:
            try:
                response = ntp_client.request(server, version=3, timeout=5)
                offset = response.tx_time - time.time()
                logging.info(f"Time sync check via {server}: offset = {offset:.3f}s")
                if abs(offset) > 60:
                    logging.warning(f"System time offset is significant ({offset:.2f}s). Consider synchronising the system clock.")
                return  # success — no need to try further servers
            except ntplib.NTPException as e:
                last_error = e
                logging.debug(f"NTP error from {server}: {e}")
            except Exception as e:
                last_error = e
                logging.debug(f"Could not reach NTP server {server}: {e}")

        logging.error(f"Failed to synchronize time: all NTP servers unreachable. Last error: {last_error}")

    def task_backup_database(self):
        """
        Scheduled task to backup the database daily.
        cli_mount DB backup always runs (lightweight file copy).
        CLI media_items.db backup only runs when system is idle.
        """
        try:
            # cli_mount DB backup: always run — just a file copy, no performance impact
            try:
                from main import backup_climount_databases
                backup_climount_databases()
            except Exception as _dcy_err:
                logging.warning(f"[DATABASE_BACKUP] cli_mount backup skipped: {_dcy_err}")

            # CLI DB backup: only run when system is idle to avoid performance impact
            if not self._is_system_idle_for_backup():
                logging.info("[DATABASE_BACKUP] System is busy, skipping CLI database backup")
                return

            logging.info("[DATABASE_BACKUP] Starting scheduled database backup (system is idle)")

            from main import backup_database
            success = backup_database(skip_if_recent=False)

            if success:
                logging.info("[DATABASE_BACKUP] Scheduled database backup completed successfully")
            else:
                logging.warning("[DATABASE_BACKUP] Scheduled database backup failed")

        except Exception as e:
            logging.error(f"[DATABASE_BACKUP] Error in scheduled backup task: {e}", exc_info=True)

    def task_backup_debrid(self):
        """Scheduled task to backup the debrid torrent library (1d/3d/7d rotating slots)."""
        try:
            from utilities.debrid_backup import run_backup, _get_settings
            settings = _get_settings()
            if not settings.get('enabled'):
                logging.debug('[DEBRID_BACKUP] Task ran but backup is disabled — skipping')
                return
            result = run_backup(force=False)
            if result.get('success'):
                logging.info(f"[DEBRID_BACKUP] Backup complete: {result.get('count', 0)} torrents ({result.get('provider', '?')})")
            elif result.get('skipped'):
                logging.debug('[DEBRID_BACKUP] Backup skipped (disabled)')
            else:
                logging.warning(f"[DEBRID_BACKUP] Backup failed: {result.get('message', 'unknown')}")
        except Exception as e:
            logging.error(f'[DEBRID_BACKUP] Scheduled task error: {e}', exc_info=True)

    def task_cleanup_debrid(self):
        """Scheduled task to clean up errored and duplicate debrid torrents."""
        try:
            from utilities.debrid_backup import run_cleanup
            result = run_cleanup(force=True)
            if result.get('success'):
                logging.info(f"[DEBRID_CLEANUP] Complete: {result.get('total_deleted', 0)} removed "
                             f"(errors={result.get('deleted_errors',0)}, "
                             f"dupes={result.get('deleted_dupes',0)}, "
                             f"stalled={result.get('deleted_stalled',0)})")
            elif result.get('skipped'):
                logging.debug('[DEBRID_CLEANUP] Skipped (disabled)')
            else:
                logging.warning(f"[DEBRID_CLEANUP] Failed: {result.get('message', 'unknown')}")
        except Exception as e:
            logging.error(f'[DEBRID_CLEANUP] Scheduled task error: {e}', exc_info=True)

    def _is_system_idle_for_backup(self):
        """
        Check if the system is idle enough to run a database backup.

        Returns:
            bool: True if system is idle, False if busy
        """
        try:
            # Check 1: No active queue processing (checking if any tasks are currently executing)
            if self.currently_executing_tasks:
                logging.debug(f"[DATABASE_BACKUP] System busy: {len(self.currently_executing_tasks)} task(s) executing")
                return False

            # Check 2: Queue sizes are not growing rapidly (system is stable)
            from queues.queue_manager import QueueManager
            queue_manager = QueueManager()

            # If scraping or adding queues have items, system is actively working
            scraping_size = queue_manager.get_queue_size('Scraping')
            adding_size = queue_manager.get_queue_size('Adding')

            if scraping_size > 0 or adding_size > 0:
                logging.debug(f"[DATABASE_BACKUP] System busy: Scraping={scraping_size}, Adding={adding_size}")
                return False

            # Check 3: Not in a paused state (avoid backing up during maintenance)
            if queue_manager.is_paused():
                logging.debug("[DATABASE_BACKUP] System paused, waiting for resume")
                return False

            logging.debug("[DATABASE_BACKUP] System is idle, backup can proceed")
            return True

        except Exception as e:
            logging.warning(f"[DATABASE_BACKUP] Error checking idle state: {e}, assuming not idle")
            return False

    def task_reconcile_queues(self):
        """Task to reconcile items in Checking state with matching filled_by_file items,
           and deduplicate items in Wanted, Scraping, or Unreleased states."""
        import sqlite3
        import logging
        import os
        from datetime import datetime

        # Setup specific logging for reconciliations
        reconciliation_logger = logging.getLogger('reconciliations')
        if not reconciliation_logger.handlers:
            log_dir = os.environ.get('USER_LOGS', '/user/logs/')
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, 'reconciliations.log')
            handler = logging.FileHandler(log_file)
            handler.setFormatter(RedactingFormatter('%(asctime)s - %(message)s'))
            reconciliation_logger.addHandler(handler)
            reconciliation_logger.setLevel(logging.INFO)

        from database.core import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        reconciled_count = 0
        deleted_count_filepath = 0 # Renamed for clarity
        deleted_count_semantic = 0 # For the new deduplication step
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            # Step 1: Original reconciliation for 'Checking' items based on filled_by_file
            reconciliation_logger.info("Starting reconciliation for 'Checking' items based on shared file paths...")
            cursor.execute("""
                SELECT
                    c.id as checking_id, c.title as checking_title, c.type as checking_type, c.filled_by_file,
                    m.id as matching_id, m.title as matching_title, m.state as matching_state, m.type as matching_type
                FROM media_items c
                JOIN media_items m ON c.filled_by_file = m.filled_by_file AND c.id != m.id
                WHERE c.state = 'Checking'
                  AND c.filled_by_file IS NOT NULL
                  AND m.state != 'Checking'
                  AND (m.ghostlisted IS NULL OR m.ghostlisted = 0)
                  AND (c.ghostlisted IS NULL OR c.ghostlisted = 0)
            """)
            reconciliation_pairs = cursor.fetchall()

            items_to_update = []
            items_to_delete_filepath = set()

            for pair in reconciliation_pairs:
                reconciliation_logger.info(
                    f"File-based Reconciliation Found: shared file '{pair['filled_by_file']}'\n"
                    f"  - Keeping (was Checking, now Collected): ID={pair['checking_id']}, Title='{pair['checking_title']}', Type={pair['checking_type']}\n"
                    f"  - Deleting (Matching entry): ID={pair['matching_id']}, Title='{pair['matching_title']}', State={pair['matching_state']}, Type={pair['matching_type']}"
                )
                items_to_update.append(pair['checking_id'])
                items_to_delete_filepath.add(pair['matching_id'])

            if items_to_update:
                update_sql = f"UPDATE media_items SET state = 'Collected', collected_at = ? WHERE id IN ({','.join(['?']*len(items_to_update))})"
                params = [now_str] + items_to_update
                cursor.execute(update_sql, params)
                reconciled_count = cursor.rowcount
                reconciliation_logger.info(f"Updated {reconciled_count} 'Checking' items to 'Collected' (file-based reconciliation). IDs: {items_to_update}")

            if items_to_delete_filepath:
                delete_ids_filepath = list(items_to_delete_filepath - set(items_to_update))
                if delete_ids_filepath:
                    # Only delete items that are true duplicates (same imdb_id+season+episode).
                    # Items that share filled_by_file but are different episodes (season pack coalescing)
                    # should be marked Collected, not deleted.
                    rows = cursor.execute(
                        f"SELECT id, imdb_id, season_number, episode_number FROM media_items "
                        f"WHERE id IN ({','.join(['?']*len(delete_ids_filepath))})",
                        delete_ids_filepath
                    ).fetchall()
                    # Build set of (imdb_id, season, episode) for already-updating items
                    updating_eps = set()
                    if items_to_update:
                        for _ur in cursor.execute(
                            f"SELECT imdb_id, season_number, episode_number FROM media_items "
                            f"WHERE id IN ({','.join(['?']*len(items_to_update))})",
                            items_to_update
                        ).fetchall():
                            updating_eps.add((_ur['imdb_id'], _ur['season_number'], _ur['episode_number']))
                    true_dupes = []
                    collect_instead = []
                    for r in rows:
                        key = (r['imdb_id'], r['season_number'], r['episode_number'])
                        if key in updating_eps:
                            true_dupes.append(r['id'])
                        else:
                            collect_instead.append(r['id'])
                    if collect_instead:
                        collect_sql = f"UPDATE media_items SET state = 'Collected', collected_at = ? WHERE id IN ({','.join(['?']*len(collect_instead))})"
                        cursor.execute(collect_sql, [now_str] + collect_instead)
                        reconciliation_logger.info(f"Marked {cursor.rowcount} season pack coalesced items as Collected (shared file). IDs: {collect_instead}")
                    if true_dupes:
                        delete_sql = f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(true_dupes))})"
                        cursor.execute(delete_sql, true_dupes)
                        deleted_count_filepath = cursor.rowcount
                        reconciliation_logger.info(f"Deleted {deleted_count_filepath} true duplicate items (file-based reconciliation). IDs: {true_dupes}")

            # --- Step 1b: Replace Season/Movie cleanup ---
            # Periodic backstop for the same manual_replace=1 cleanup that
            # handle_state_change() -> replace_cleanup_after_collect() already runs
            # right after a collect event (utilities/post_processing.py) - this sweep
            # additionally catches stale items left behind across restarts or races.
            # When new items are promoted to Collected, remove old entries flagged with
            # manual_replace=1 (from a different torrent/NZB job), remove the old
            # debrid/usenet entry, unlink the old symlink and original file (Symlinked/
            # Local mode), and remove old items from Plex (with scan+empty trash fallback).
            deleted_count_replace = 0
            if items_to_update:
                try:
                    promoted_rows = cursor.execute(
                        f"SELECT id, type, imdb_id, season_number, episode_number, filled_by_torrent_id, version "
                        f"FROM media_items WHERE id IN ({','.join(['?']*len(items_to_update))})",
                        items_to_update
                    ).fetchall()
                    from debrid import get_debrid_provider as _get_debrid_prov
                    from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
                    import os as _os_recon
                    _debrid_prov = _get_debrid_prov()
                    ids_to_delete_replace = set()
                    _recon_plex_paths = set()
                    _recon_section_types = set()
                    _recon_select = ('id, filled_by_torrent_id, filled_by_file, location_on_disk, '
                                      'original_path_for_symlink, title, episode_title')
                    _recon_is_symlink_mode = get_setting('File Management', 'file_collection_management') == 'Symlinked/Local'
                    _recon_mount_location = get_setting('Plex', 'mounted_file_location', '')

                    def _recon_remove_old_provider_entry(old_torrent_id, removal_reason, log_tag, old_id):
                        """Remove the old torrent/NZB job via whichever provider actually owns it -
                        an NZB job id 404s against the debrid provider's remove_torrent()."""
                        if str(old_torrent_id).startswith('nzb:'):
                            try:
                                from usenet import get_usenet_client
                                get_usenet_client().remove_nzb(old_torrent_id[4:])
                                logging.info(f"[{log_tag}] Removed old NZB job {old_torrent_id} for replaced item {old_id}")
                            except Exception as _nzb_err:
                                if '404' in str(_nzb_err):
                                    logging.debug(f"[{log_tag}] Old NZB job {old_torrent_id} already removed (404)")
                                else:
                                    logging.error(f"[{log_tag}] Failed to remove old NZB job {old_torrent_id}: {_nzb_err}")
                        elif _debrid_prov:
                            try:
                                _debrid_prov.remove_torrent(old_torrent_id, removal_reason=removal_reason)
                                logging.info(f"[{log_tag}] Removed debrid torrent {old_torrent_id} for replaced item {old_id}")
                            except Exception as _debrid_err:
                                if '404' in str(_debrid_err):
                                    logging.debug(f"[{log_tag}] Old torrent {old_torrent_id} already removed (404)")
                                else:
                                    logging.error(f"[{log_tag}] Failed to remove torrent {old_torrent_id}: {_debrid_err}")

                    def _recon_unlink_old_files(old_row, log_tag, old_id):
                        """Directly unlink the old symlink and original mount file - the DB-row
                        deletion below never touched the filesystem on its own, and provider
                        removal only clears the debrid/NZB side, not a Symlinked/Local mode
                        symlink or (for a non-debrid-mount source) the file behind it."""
                        if not _recon_is_symlink_mode:
                            return
                        location_on_disk = old_row['location_on_disk']
                        original_path = old_row['original_path_for_symlink']
                        try:
                            if location_on_disk and _os_recon.path.islink(location_on_disk):
                                _os_recon.unlink(location_on_disk)
                                logging.info(f"[{log_tag}] Removed old symlink {location_on_disk} for replaced item {old_id}")
                        except Exception as _sym_err:
                            logging.warning(f"[{log_tag}] Failed to remove old symlink {location_on_disk}: {_sym_err}")
                        try:
                            if (original_path and _os_recon.path.exists(original_path)
                                    and not (_recon_mount_location and original_path.startswith(_recon_mount_location))):
                                _os_recon.remove(original_path)
                                logging.info(f"[{log_tag}] Removed old original file {original_path} for replaced item {old_id}")
                        except Exception as _orig_err:
                            logging.warning(f"[{log_tag}] Failed to remove old original file {original_path}: {_orig_err}")

                    # Per-episode/movie cleanup: find old entries with manual_replace=1 for each promoted item
                    for promoted in promoted_rows:
                        if not promoted['imdb_id']:
                            continue
                        if promoted['type'] == 'episode':
                            if promoted['season_number'] is None or promoted['episode_number'] is None:
                                continue
                            old_replace_rows = cursor.execute(
                                f'''SELECT {_recon_select} FROM media_items
                                   WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                                   AND type = 'episode' AND manual_replace = 1 AND id != ?
                                   AND REPLACE(COALESCE(version,''),'*','') = ?''',
                                (promoted['imdb_id'], promoted['season_number'],
                                 promoted['episode_number'], promoted['id'],
                                 (promoted['version'] or '').replace('*', ''))
                            ).fetchall()
                            log_tag = 'REPLACE_SEASON'
                            removal_reason = 'Replaced by new season pack'
                            entry_label = 'episode'
                            _recon_section_types.add('show')
                        elif promoted['type'] == 'movie':
                            old_replace_rows = cursor.execute(
                                f'''SELECT {_recon_select} FROM media_items
                                   WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1 AND id != ?
                                   AND REPLACE(COALESCE(version,''),'*','') = ?''',
                                (promoted['imdb_id'], promoted['id'],
                                 (promoted['version'] or '').replace('*', ''))
                            ).fetchall()
                            log_tag = 'REPLACE_MOVIE'
                            removal_reason = 'Replaced by new movie torrent'
                            entry_label = 'movie'
                            _recon_section_types.add('movie')
                        else:
                            continue
                        new_torrent_id = promoted['filled_by_torrent_id']
                        for old_item in old_replace_rows:
                            old_id = old_item['id']
                            old_torrent_id = old_item['filled_by_torrent_id']
                            if old_torrent_id and old_torrent_id != new_torrent_id:
                                _sibs = cursor.execute(
                                    "SELECT COUNT(*) FROM media_items "
                                    "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                    (old_torrent_id, old_id)
                                ).fetchone()[0]
                                if _sibs:
                                    logging.info(f"[{log_tag}] Skipping debrid removal of {old_torrent_id} for replaced item {old_id} — {_sibs} sibling(s) still active")
                                else:
                                    _recon_remove_old_provider_entry(old_torrent_id, removal_reason, log_tag, old_id)
                            if old_id not in items_to_delete_filepath and old_id not in items_to_update:
                                # Plex removal
                                _old_path = old_item['location_on_disk'] or old_item['filled_by_file']
                                if _old_path:
                                    _ep_title = old_item['episode_title'] if promoted['type'] == 'episode' else None
                                    try:
                                        if not remove_file_from_plex(old_item['title'] or '', _old_path, _ep_title):
                                            logging.warning(f"[{log_tag}] Direct Plex removal failed for item {old_id}, will scan+empty trash")
                                        else:
                                            logging.info(f"[{log_tag}] Removed item {old_id} from Plex")
                                    except Exception as _plex_err:
                                        logging.warning(f"[{log_tag}] Plex removal error for item {old_id}: {_plex_err}")
                                    _recon_plex_paths.add(_os_recon.path.dirname(_old_path))
                                _recon_unlink_old_files(old_item, log_tag, old_id)
                                ids_to_delete_replace.add(old_id)
                                logging.info(f"[{log_tag}] Queued old {entry_label} entry {old_id} for deletion after replacement")

                    # Broader sweep: catch stale manual_replace=1 items in any state (e.g. Upgrading)
                    # that already have a corresponding Collected replacement.
                    # Also handles items in items_to_update that had manual_replace=1 (old Upgrading
                    # items promoted to Collected by reconciliation) by clearing the flag.
                    _imdb_sweep = {}
                    for _p in promoted_rows:
                        if _p['imdb_id'] and _p['type'] in ('episode', 'movie'):
                            _sw_key = (_p['imdb_id'], (_p['version'] or '').replace('*', ''))
                            if _sw_key not in _imdb_sweep:
                                _imdb_sweep[_sw_key] = {'type': _p['type'], 'seasons': set()}
                            if _p['type'] == 'episode' and _p['season_number'] is not None:
                                _imdb_sweep[_sw_key]['seasons'].add(_p['season_number'])
                    for (_sw_imdb, _sw_version), _sw_info in _imdb_sweep.items():
                        if _sw_info['type'] == 'episode':
                            for _sw_season in _sw_info['seasons']:
                                _sw_rows = cursor.execute(
                                    f'''SELECT {_recon_select} FROM media_items m
                                       WHERE m.imdb_id = ? AND m.season_number = ? AND m.type = 'episode'
                                       AND m.manual_replace = 1
                                       AND REPLACE(COALESCE(m.version,''),'*','') = ?
                                       AND EXISTS (
                                           SELECT 1 FROM media_items m2
                                           WHERE m2.imdb_id = m.imdb_id AND m2.season_number = m.season_number
                                           AND m2.episode_number = m.episode_number AND m2.type = 'episode'
                                           AND m2.manual_replace = 0 AND m2.state = 'Collected'
                                           AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                                       )''',
                                    (_sw_imdb, _sw_season, _sw_version)
                                ).fetchall()
                                for _sr in _sw_rows:
                                    _sid = _sr['id']
                                    if _sid in items_to_update:
                                        cursor.execute('UPDATE media_items SET manual_replace = 0 WHERE id = ?', (_sid,))
                                        logging.info(f"[REPLACE_SEASON] Cleared manual_replace for promoted stale item {_sid}")
                                    elif _sid not in items_to_delete_filepath and _sid not in ids_to_delete_replace:
                                        _st = _sr['filled_by_torrent_id']
                                        if _st:
                                            _sw_sibs = cursor.execute(
                                                "SELECT COUNT(*) FROM media_items "
                                                "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                                (_st, _sid)
                                            ).fetchone()[0]
                                            if _sw_sibs:
                                                logging.info(f"[REPLACE_SEASON] Skipping debrid removal of {_st} for stale item {_sid} — {_sw_sibs} sibling(s) still active")
                                            else:
                                                _recon_remove_old_provider_entry(_st, 'Replaced by new season pack', 'REPLACE_SEASON', _sid)
                                        _sw_path = _sr['location_on_disk'] or _sr['filled_by_file']
                                        if _sw_path:
                                            try:
                                                if not remove_file_from_plex(_sr['title'] or '', _sw_path, _sr['episode_title']):
                                                    logging.warning(f"[REPLACE_SEASON] Direct Plex removal failed for stale item {_sid}")
                                                else:
                                                    logging.info(f"[REPLACE_SEASON] Removed stale item {_sid} from Plex")
                                            except Exception as _pe:
                                                logging.warning(f"[REPLACE_SEASON] Plex removal error for stale item {_sid}: {_pe}")
                                            _recon_plex_paths.add(_os_recon.path.dirname(_sw_path))
                                            _recon_section_types.add('show')
                                        _recon_unlink_old_files(_sr, 'REPLACE_SEASON', _sid)
                                        ids_to_delete_replace.add(_sid)
                                        logging.info(f"[REPLACE_SEASON] Queued stale episode {_sid} for deletion (broader sweep)")
                        elif _sw_info['type'] == 'movie':
                            _sw_rows = cursor.execute(
                                f'''SELECT {_recon_select} FROM media_items m
                                   WHERE m.imdb_id = ? AND m.type = 'movie'
                                   AND m.manual_replace = 1
                                   AND REPLACE(COALESCE(m.version,''),'*','') = ?
                                   AND EXISTS (
                                       SELECT 1 FROM media_items m2
                                       WHERE m2.imdb_id = m.imdb_id AND m2.type = 'movie'
                                       AND m2.manual_replace = 0 AND m2.state = 'Collected'
                                       AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                                   )''',
                                (_sw_imdb, _sw_version)
                            ).fetchall()
                            for _sr in _sw_rows:
                                _sid = _sr['id']
                                if _sid in items_to_update:
                                    cursor.execute('UPDATE media_items SET manual_replace = 0 WHERE id = ?', (_sid,))
                                    logging.info(f"[REPLACE_MOVIE] Cleared manual_replace for promoted stale item {_sid}")
                                elif _sid not in items_to_delete_filepath and _sid not in ids_to_delete_replace:
                                    _st = _sr['filled_by_torrent_id']
                                    if _st:
                                        _sw_sibs = cursor.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                            (_st, _sid)
                                        ).fetchone()[0]
                                        if _sw_sibs:
                                            logging.info(f"[REPLACE_MOVIE] Skipping debrid removal of {_st} for stale item {_sid} — {_sw_sibs} sibling(s) still active")
                                        else:
                                            _recon_remove_old_provider_entry(_st, 'Replaced by new movie torrent', 'REPLACE_MOVIE', _sid)
                                    _sw_path = _sr['location_on_disk'] or _sr['filled_by_file']
                                    if _sw_path:
                                        try:
                                            if not remove_file_from_plex(_sr['title'] or '', _sw_path, None):
                                                logging.warning(f"[REPLACE_MOVIE] Direct Plex removal failed for stale item {_sid}")
                                            else:
                                                logging.info(f"[REPLACE_MOVIE] Removed stale item {_sid} from Plex")
                                        except Exception as _pe:
                                            logging.warning(f"[REPLACE_MOVIE] Plex removal error for stale item {_sid}: {_pe}")
                                        _recon_plex_paths.add(_os_recon.path.dirname(_sw_path))
                                        _recon_section_types.add('movie')
                                    _recon_unlink_old_files(_sr, 'REPLACE_MOVIE', _sid)
                                    ids_to_delete_replace.add(_sid)
                                    logging.info(f"[REPLACE_MOVIE] Queued stale movie {_sid} for deletion (broader sweep)")

                    if ids_to_delete_replace:
                        _ids_list = list(ids_to_delete_replace)
                        cursor.execute(
                            f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(_ids_list))})",
                            _ids_list
                        )
                        deleted_count_replace = cursor.rowcount
                        logging.info(f"[REPLACE] Deleted {deleted_count_replace} replaced/stale entries. IDs: {_ids_list}")

                    # Scan & empty Plex trash for all affected paths
                    if _recon_plex_paths:
                        try:
                            _sec_type = 'show' if 'show' in _recon_section_types and 'movie' not in _recon_section_types else \
                                        'movie' if 'movie' in _recon_section_types and 'show' not in _recon_section_types else None
                            scan_and_empty_plex_trash(paths=list(_recon_plex_paths), section_type=_sec_type)
                            logging.info(f"[REPLACE] Triggered Plex scan+empty trash for paths: {list(_recon_plex_paths)}")
                        except Exception as _scan_err:
                            logging.warning(f"[REPLACE] Plex scan+empty trash failed: {_scan_err}")

                except Exception as _replace_err:
                    logging.error(f"[REPLACE] Error in reconciliation replace hook: {_replace_err}")
            # --- End Step 1b ---

            # --- Step 1c: Resolve stranded NZB coalesced items in Adding state ---
            # When a season pack NZB is submitted, only the initiator episode goes through
            # health check → Checking → _resolve_nzb_file_info → Collected.
            # The coalesced siblings sit in Adding with filled_by_torrent_id=nzb:xxx but no
            # filled_by_file. If a sibling is already Collected, look up the pack's actual
            # file listing on cli_mount and match each stranded item to its own episode file
            # by SxxEyy (same matching checking_queue._resolve_nzb_file_info uses) before
            # moving it into Checking - never straight to Collected, since that would mark
            # an item done with no real file/symlink ever created for it.
            try:
                stranded_rows = cursor.execute("""
                    SELECT a.id, a.imdb_id, a.season_number, a.episode_number, a.title
                    FROM media_items a
                    WHERE a.state = 'Adding'
                      AND a.filled_by_torrent_id LIKE 'nzb:%'
                      AND a.filled_by_file IS NULL
                      AND a.imdb_id IS NOT NULL
                      AND (a.ghostlisted IS NULL OR a.ghostlisted = 0)
                """).fetchall()

                if stranded_rows:
                    stranded_matches = []
                    for row in stranded_rows:
                        # Prefer the initiator row (has nzb torrent_id + folder info),
                        # fall back to any Collected sibling with a filled_by_file.
                        sibling = cursor.execute("""
                            SELECT id, filled_by_file, filled_by_title, debrid_folder_name,
                                   location_basename, original_scraped_torrent_title,
                                   real_debrid_original_title, filled_by_torrent_id
                            FROM media_items
                            WHERE imdb_id = ? AND season_number = ? AND type = 'episode'
                              AND state = 'Collected'
                              AND filled_by_file IS NOT NULL
                              AND id != ?
                            ORDER BY (filled_by_torrent_id LIKE 'nzb:%') DESC
                            LIMIT 1
                        """, (row['imdb_id'], row['season_number'], row['id'])).fetchone()
                        if sibling:
                            stranded_matches.append((row['id'], dict(sibling), dict(row)))

                    if stranded_matches:
                        from usenet import get_usenet_client as _get_dc_sc
                        _dc_sc = _get_dc_sc()
                        _folder_files_cache = {}  # folder_name -> video_files list, avoid repeat lookups

                        for item_id, sib, item in stranded_matches:
                            folder_name = sib['filled_by_title'] or sib['debrid_folder_name'] or sib['location_basename']
                            if not folder_name:
                                continue

                            if folder_name not in _folder_files_cache:
                                try:
                                    _result = _dc_sc.get_nzb_folder_all_files(folder_name)
                                    _folder_files_cache[folder_name] = _result[1] if _result else []
                                except Exception as _lookup_err:
                                    logging.warning(f"[NZBCoalesce] Folder lookup failed for {folder_name!r}: {_lookup_err}")
                                    _folder_files_cache[folder_name] = []
                            video_files = _folder_files_cache[folder_name]

                            season = item['season_number']
                            episode = item['episode_number']
                            matched_file = None
                            try:
                                _file_result = _dc_sc.get_nzb_file_info(
                                    folder_name, season=season, episode=episode,
                                )
                                if _file_result:
                                    matched_file = _file_result[1]
                            except Exception as _resolve_err:
                                logging.debug(
                                    f"[NZBCoalesce] get_nzb_file_info failed for {folder_name!r}: {_resolve_err}"
                                )

                            if not matched_file:
                                # Can't confirm this item's own file exists in the pack folder yet -
                                # leave it in Adding rather than falsely marking it done.
                                reconciliation_logger.debug(
                                    f"[NZBCoalesce] Stranded item {item_id} '{item['title']}' "
                                    f"S{item['season_number']}E{item['episode_number']} — no matching file "
                                    f"found yet in folder {folder_name!r}, leaving in Adding"
                                )
                                continue

                            cursor.execute("""
                                UPDATE media_items SET
                                    state = 'Checking',
                                    filled_by_file = ?,
                                    filled_by_torrent_id = ?,
                                    filled_by_title = ?,
                                    debrid_folder_name = ?,
                                    location_basename = ?,
                                    original_scraped_torrent_title = ?,
                                    real_debrid_original_title = ?
                                WHERE id = ?
                            """, (
                                matched_file,
                                sib['filled_by_torrent_id'],
                                folder_name,
                                folder_name,
                                folder_name,
                                sib['original_scraped_torrent_title'],
                                sib['real_debrid_original_title'],
                                item_id,
                            ))
                            reconciliation_logger.info(
                                f"[NZBCoalesce] Resolved stranded Adding item {item_id} "
                                f"'{item['title']}' S{item['season_number']}E{item['episode_number']} "
                                f"→ {matched_file!r}, moved to Checking (sibling {sib['id']} already Collected)"
                            )
            except Exception as _sc_err:
                logging.error(f"[NZBCoalesce] Step 1c error: {_sc_err}", exc_info=True)
            # --- End Step 1c ---

            # --- Step 2: New deduplication for Wanted, Scraping, Unreleased states ---
            reconciliation_logger.info("Starting semantic deduplication for 'Wanted', 'Scraping', 'Unreleased' items (IMDB ID, S/E, Version - with '*' trimmed from version)...")
            
            cursor.execute("""
                SELECT id, imdb_id, season_number, episode_number, version, state, type, title
                FROM media_items
                WHERE state IN ('Wanted', 'Scraping', 'Unreleased')
                  AND imdb_id IS NOT NULL
                  AND (ghostlisted IS NULL OR ghostlisted = 0)
                ORDER BY imdb_id, type, season_number, episode_number, version, id
            """)
            candidate_semantic_duplicates = cursor.fetchall()

            items_to_delete_semantic_set = set()
            processed_groups = {}

            for item_row in candidate_semantic_duplicates:
                item = dict(item_row)
                
                s_num_key = item['season_number'] if item['type'] == 'episode' else None
                e_num_key = item['episode_number'] if item['type'] == 'episode' else None
                
                # Trim asterisks from version for grouping key
                raw_version = item['version']
                version_key = raw_version.replace('*', '') if isinstance(raw_version, str) else raw_version

                group_key = (item['imdb_id'], version_key, s_num_key, e_num_key)

                if group_key not in processed_groups:
                    processed_groups[group_key] = []
                processed_groups[group_key].append(item) # Store original item for logging/details

            state_priority = {'Scraping': 0, 'Wanted': 1, 'Unreleased': 2}

            for group_key, items_in_group in processed_groups.items():
                if len(items_in_group) > 1:
                    # Sort items: by state priority, then by ID (smallest ID is older)
                    items_in_group.sort(key=lambda x: (state_priority.get(x['state'], 99), x['id']))
                    
                    item_to_keep = items_in_group[0]
                    ids_in_group_to_delete = [i['id'] for i in items_in_group[1:]]

                    if ids_in_group_to_delete:
                        # Log with original version for clarity, but mention grouping logic
                        deleted_titles_log = [f"ID:{i['id']} '{i['title']}' (State:{i['state']}, OrigV:'{i['version']}')" for i in items_in_group[1:]]
                        group_key_log = (group_key[0], group_key[1], group_key[2], group_key[3]) # imdb, trimmed_version, s, e
                        reconciliation_logger.info(
                            f"Semantic Deduplication for group (key: {group_key_log}):\n"
                            f"  - Keeping: ID={item_to_keep['id']}, Title='{item_to_keep['title']}', State='{item_to_keep['state']}', Type='{item_to_keep['type']}', OrigV:'{item_to_keep['version']}'\n"
                            f"  - Deleting: {'; '.join(deleted_titles_log)}"
                        )
                        for del_id in ids_in_group_to_delete:
                            items_to_delete_semantic_set.add(del_id)
            
            if items_to_delete_semantic_set:
                final_semantic_delete_ids = list(items_to_delete_semantic_set - items_to_delete_filepath - set(items_to_update))
                                
                if final_semantic_delete_ids:
                    delete_sql_semantic = f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(final_semantic_delete_ids))})"
                    cursor.execute(delete_sql_semantic, final_semantic_delete_ids)
                    deleted_count_semantic = cursor.rowcount
                    reconciliation_logger.info(f"Deleted {deleted_count_semantic} items based on semantic duplication (IMDB ID, S/E, Version - with '*' trimmed). IDs: {final_semantic_delete_ids}")

            # Step 3: Delete Wanted/Scraping/Unreleased items that have a Collected counterpart
            # with the same imdb_id + season + episode + version (ignoring * suffix).
            # This cleans up duplicates created by Plex scan inserting Default* Collected rows
            # while the original Default Wanted row still exists.
            deleted_count_collected_dup = 0
            try:
                cursor.execute("""
                    DELETE FROM media_items
                    WHERE state IN ('Wanted', 'Scraping', 'Unreleased')
                      AND imdb_id IS NOT NULL
                      AND (ghostlisted IS NULL OR ghostlisted = 0)
                      AND EXISTS (
                          SELECT 1 FROM media_items c
                          WHERE c.state = 'Collected'
                            AND c.imdb_id = media_items.imdb_id
                            AND c.type = media_items.type
                            AND (c.season_number = media_items.season_number OR (c.season_number IS NULL AND media_items.season_number IS NULL))
                            AND (c.episode_number = media_items.episode_number OR (c.episode_number IS NULL AND media_items.episode_number IS NULL))
                            AND REPLACE(COALESCE(c.version,''), '*', '') = REPLACE(COALESCE(media_items.version,''), '*', '')
                            AND (c.ghostlisted IS NULL OR c.ghostlisted = 0)
                      )
                """)
                deleted_count_collected_dup = cursor.rowcount
                if deleted_count_collected_dup:
                    reconciliation_logger.info(f"Deleted {deleted_count_collected_dup} Wanted/Scraping items that have a Collected counterpart (version star mismatch cleanup).")
            except Exception as _cd_err:
                reconciliation_logger.error(f"Collected-dup cleanup error: {_cd_err}")

            conn.commit()

            log_parts = []
            if reconciled_count > 0:
                log_parts.append(f"{reconciled_count} items updated to 'Collected'")
            if deleted_count_filepath > 0:
                log_parts.append(f"{deleted_count_filepath} duplicates deleted (shared file paths)")
            if deleted_count_semantic > 0:
                log_parts.append(f"{deleted_count_semantic} duplicates deleted (content/version with '*' trimmed)")
            if deleted_count_replace > 0:
                log_parts.append(f"{deleted_count_replace} replaced entries deleted")

            if log_parts:
                 logging.info(f"Queue reconciliation completed: {', '.join(log_parts)}.")
            else: 
                 logging.debug("Queue reconciliation found no items needing update or deletion in this cycle.")

        except sqlite3.Error as e:
            logging.error(f"Database error during queue reconciliation: {str(e)}")
            if conn: conn.rollback() # Rollback on error
        finally:
            if conn: conn.close() # Ensure connection is closed

    def task_refresh_download_stats(self):
        """Task to refresh the download stats cache"""
        import threading
        from database.statistics import get_cached_download_stats
        
        result = [None]
        exception = [None]
        
        def run_with_timeout():
            try:
                get_cached_download_stats()
                result[0] = "success"
                logging.debug("Download stats cache refreshed")
            except Exception as e:
                exception[0] = e
                logging.error(f"Error refreshing download stats cache: {str(e)}")
        
        # Start the operation in a separate thread
        thread = threading.Thread(target=run_with_timeout)
        thread.daemon = True
        thread.start()
        
        # Wait for up to 30 seconds
        thread.join(timeout=30.0)
        
        if thread.is_alive():
            logging.error("Download stats refresh timed out after 30 seconds")
        elif exception[0]:
            logging.error(f"Download stats refresh failed: {str(exception[0])}")

    def task_retry_content_source_removals(self):
        """Retry content-source removals that failed during a delete (see content_source_retry)."""
        from database.content_source_retry import process_retry_queue
        attempted = process_retry_queue()
        if attempted:
            logging.info(f"[RETRY_QUEUE] Retried {attempted} content source removal(s)")

    def task_check_database_health(self):
        """Periodic task to verify database health and handle any corruption."""
        from main import verify_database_health

        try:
            paused_for_db = self.queue_paused and (self.pause_info or {}).get("error_type") == "DB_HEALTH"
            if not verify_database_health():
                if paused_for_db:
                    # Already paused and notified; keep holding until a check passes
                    logging.error("Database health check still failing - queue stays paused")
                    return
                logging.error("Database health check failed during periodic check")
                # Update pause_info for DB health
                self.pause_info = {
                    "reason_string": "Database corruption detected - check logs for details",
                    "error_type": "DB_HEALTH",
                    "service_name": "System Database",
                    "status_code": None,
                    "retry_count": 0
                }
                self.pause_queue()

                # Send notification about database corruption
                try:
                    from routes.notifications import send_program_crash_notification
                    send_program_crash_notification(
                        "Database corruption detected (see logs for which database) - queue paused. "
                        "Restore the database from a backup; the queue resumes when the hourly check passes.")

                except Exception as e:
                    logging.error(f"Failed to send database corruption notification: {str(e)}")
            elif paused_for_db:
                logging.info("Database health check passed - resuming queue paused for DB_HEALTH")
                self.resume_queue()
            else:
                logging.info("Periodic database health check passed")
        except Exception as e:
            logging.error(f"Error during periodic database health check: {str(e)}")

    # Add task method to update queue views
    def task_update_queue_views(self):
        """Periodically updates the in-memory queue views from the database."""
        if not hasattr(self, 'queue_manager') or not self.queue_manager:
            logging.warning("Queue manager not available, cannot update queue views.")
            return

        # logging.debug("Running task_update_queue_views to sync queues with DB.")
        updated_count = 0
        start_time = time.time()
        try:
            # It's important to iterate over a copy of the items in case the dict changes
            queues_to_update = list(self.queue_manager.queues.values())
            for queue in queues_to_update:
                if hasattr(queue, 'update'):
                    try:
                        queue.update()
                        updated_count += 1
                    except Exception as e:
                        logging.error(f"Error updating queue '{getattr(queue, 'name', 'Unknown')}': {e}", exc_info=True)
                else:
                     # This might happen if initialization failed partially
                    logging.warning(f"Queue object '{getattr(queue, 'name', 'Unknown')}' lacks an update method.")

            duration = time.time() - start_time
            # logging.debug(f"Finished task_update_queue_views in {duration:.3f}s. Updated {updated_count} queues.")
        except Exception as e:
            logging.error(f"Unexpected error in task_update_queue_views: {e}", exc_info=True)

    # Modified task for library size cache refresh (now fully synchronous structure)
    def task_refresh_library_size_cache(self):
        """Scheduled task to refresh the Debrid library size cache."""
        logging.info("Initiating scheduled library size cache refresh task.")
        try:
            provider = get_debrid_provider()
            # Use provider capability or method presence instead of concrete type
            if hasattr(provider, 'get_total_library_size'):
                logging.info("Background task: Refreshing library size cache via Debrid provider...")
                # The provider's get_total_library_size is async, so we run it in a new event loop.
                # This call is expected to fetch the size and update the cache file itself.
                calculated_size = asyncio.run(provider.get_total_library_size())
                if calculated_size is not None and not str(calculated_size).startswith("Error"):
                    logging.info(f"Background task: Library size cache refresh successful. Provider reported size: {calculated_size}")
                else:
                    logging.warning(f"Background task: Library size cache refresh via provider failed or returned error. Result: {calculated_size}")
            else:
                logging.info("Background task: Library size cache refresh skipped (provider does not implement total size API or not configured).")
        except ProviderUnavailableError:
            logging.warning("Background task: Debrid provider unavailable during library size cache refresh.")
        except RuntimeError as e_runtime:
            # This might happen if asyncio.run() is called from an already running loop,
            # though less likely with BackgroundScheduler's default thread-based execution.
             logging.error(f"Background task: Runtime error during library size cache refresh: {e_runtime}", exc_info=True)
        except Exception as e:
            logging.error(f"Background task: General error during library size cache refresh: {e}", exc_info=True)

    def task_upgrade_hub_scan(self):
        """Scheduled nightly scan for better-quality releases via Zilean."""
        try:
            if 'task_upgrade_hub_scan' not in self.enabled_tasks:
                logging.debug("[UPGRADE_HUB] Scheduled scan skipped — task disabled in Task Manager")
                return
            from database.zilean_upgrade import scan_for_upgrades, get_scan_status
            from utilities.settings import get_setting
            if get_scan_status()['in_progress']:
                logging.info("[UPGRADE_HUB] Scheduled scan skipped — scan already in progress")
                return
            scan_limit = get_setting('Upgrade Hub', 'scan_limit', None)
            if scan_limit not in (None, '', 'null'):
                try:
                    scan_limit = int(scan_limit)
                except (TypeError, ValueError):
                    scan_limit = None
            else:
                scan_limit = None
            logging.info("[UPGRADE_HUB] Starting scheduled upgrade scan")
            result = scan_for_upgrades(scan_limit=scan_limit, triggered_by='scheduled')
            if 'error' in result:
                logging.warning(f"[UPGRADE_HUB] Scheduled scan error: {result['error']}")
            else:
                upgrades = len(result.get('upgrade_candidates', []))
                packs    = len(result.get('pack_candidates', []))
                logging.info(f"[UPGRADE_HUB] Scheduled scan complete: {upgrades} upgrades, {packs} packs")
        except Exception as e:
            logging.error(f"[UPGRADE_HUB] Scheduled scan failed: {e}", exc_info=True)

    def task_upgrade_hub_auto_queue(self):
        """Auto-queue upgrade candidates found in the most recent scan."""
        try:
            if 'task_upgrade_hub_auto_queue' not in self.enabled_tasks:
                logging.debug("[UPGRADE_HUB] Auto-queue skipped — task disabled in Task Manager")
                return
            from utilities.settings import get_setting
            from database.zilean_upgrade import (
                get_last_results, scan_for_upgrades, get_scan_status, queue_upgrade_candidates
            )
            results = get_last_results()
            if not results or 'error' in results:
                if get_scan_status()['in_progress']:
                    logging.info("[UPGRADE_HUB] Auto-queue skipped — scan in progress")
                    return
                scan_limit = get_setting('Upgrade Hub', 'scan_limit', None)
                if scan_limit not in (None, '', 'null'):
                    try:
                        scan_limit = int(scan_limit)
                    except (TypeError, ValueError):
                        scan_limit = None
                else:
                    scan_limit = None
                results = scan_for_upgrades(scan_limit=scan_limit, triggered_by='scheduled')
            if not results or 'error' in results:
                return
            threshold = float(get_setting('Upgrade Hub', 'min_improvement_threshold', 0) or 0)
            show_recent_only = str(get_setting('Upgrade Hub', 'show_recent_only', False)).lower() == 'true'
            recent_threshold_days = int(get_setting('Upgrade Hub', 'recent_threshold_days', 90) or 90)
            max_per_run = int(get_setting('Upgrade Hub', 'max_upgrades_per_run', 10) or 10)

            import datetime as _dt
            cutoff_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=recent_threshold_days)

            from dateutil import parser as _dtparser

            # Fetch all item_ids already in Upgrading state so we skip them and
            # queue the next batch instead (allows multiple button presses to work).
            try:
                from database.core import get_db_connection as _get_db
                _conn = _get_db()
                _already_upgrading = {
                    r[0] for r in _conn.execute(
                        "SELECT id FROM media_items WHERE state='Upgrading'"
                    ).fetchall()
                }
                _conn.close()
            except Exception:
                _already_upgrading = set()

            # Collect season pack candidates first (to build dedup set for upgrades)
            pack_slots = []
            pack_covered_ids = set()
            for p in results.get('pack_candidates', []):
                if p.get('improvement_pct', 0) < threshold:
                    continue
                if show_recent_only:
                    pack_ep_ids = p.get('item_ids', [])
                    if pack_ep_ids:
                        try:
                            from database.core import get_db_connection as _get_db
                            _conn = _get_db()
                            placeholders = ','.join('?' * len(pack_ep_ids))
                            row = _conn.execute(
                                f"SELECT MAX(ingested_at) FROM media_items WHERE id IN ({placeholders})",
                                pack_ep_ids,
                            ).fetchone()
                            _conn.close()
                            if row and row[0]:
                                iat = _dtparser.parse(row[0])
                                if iat.tzinfo is None:
                                    iat = iat.replace(tzinfo=_dt.timezone.utc)
                                if iat < cutoff_dt:
                                    continue
                        except Exception:
                            pass
                ep_ids = [i for i in p.get('item_ids', []) if i not in _already_upgrading]
                if ep_ids:
                    pack_slots.append(ep_ids)
                    pack_covered_ids.update(ep_ids)

            # Collect individual upgrade candidates — skip any episode covered by a pack slot
            # or already in Upgrading state (so repeated button presses queue the next batch)
            upgrade_item_ids = []
            for c in results.get('upgrade_candidates', []):
                if c.get('improvement_pct', 0) < threshold:
                    continue
                if c['item_id'] in pack_covered_ids:
                    continue
                if c['item_id'] in _already_upgrading:
                    continue
                if show_recent_only:
                    iat_str = c.get('new_ingested_at', '')
                    if not iat_str:
                        continue  # no date — exclude when recent-only is on
                    try:
                        iat = _dtparser.parse(iat_str)
                        if iat.tzinfo is None:
                            iat = iat.replace(tzinfo=_dt.timezone.utc)
                        if iat < cutoff_dt:
                            continue
                    except Exception:
                        continue  # unparseable date — exclude when recent-only is on
                upgrade_item_ids.append(c['item_id'])

            # Cap each side independently by max_per_run, then combine
            upgrade_slots = [([iid], 'upgrade') for iid in upgrade_item_ids][:max_per_run]
            pack_slots_q  = [(ids, 'pack') for ids in pack_slots][:max_per_run]
            all_slots     = upgrade_slots + pack_slots_q

            if not all_slots:
                logging.info("[UPGRADE_HUB] Auto-queue: no candidates matching settings filters")
                try:
                    from database.upgrade_hub_activity import log_hub_activity
                    log_hub_activity('queue', triggered_by='scheduled', result='success',
                                     title='Auto-queue: no candidates to queue',
                                     stats={'queued': 0, 'failed': 0})
                except Exception:
                    pass
                return

            flat_item_ids = [iid for ids, _ in all_slots for iid in ids]
            n_upgrades = sum(1 for _, t in all_slots if t == 'upgrade')
            n_packs    = sum(1 for _, t in all_slots if t == 'pack')
            queue_upgrade_candidates(flat_item_ids, triggered_by='scheduled')
            logging.info(
                f"[UPGRADE_HUB] Auto-queued {n_upgrades} upgrade(s) + {n_packs} pack(s) "
                f"= {len(flat_item_ids)} item(s) total (limit: {max_per_run} per side)"
            )
        except Exception as e:
            logging.error(f"[UPGRADE_HUB] Auto-queue task failed: {e}", exc_info=True)
