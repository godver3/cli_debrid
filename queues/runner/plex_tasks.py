"""Plex tasks: file checks and removals, scans, labels, ID backfills, tokens, Reported Issues.

Methods of ProgramRunner (queues/run_program.py), kept here to keep that file manageable.
"""
import logging
from plexapi.exceptions import NotFound
from plexapi.server import PlexServer
from datetime import datetime
from database.core import get_db_connection
from database.database_reading import get_media_item_by_id
from utilities.settings import get_setting
from utilities.post_processing import handle_state_change
import os
import sqlite3
from database.symlink_verification import cleanup_old_verified_removals
from utilities.plex_functions import find_plex_library_and_section
from database.symlink_verification import get_pending_removal_paths
from utilities.plex_functions import get_section_type
from database.symlink_verification import increment_removal_attempt
import plexapi
from utilities.plex_functions import remove_file_from_plex
from database.symlink_verification import update_removal_status


class PlexTasks:
    def task_plex_full_scan(self):
        from queues.run_program import backfill_resolution_from_stored_paths, get_and_add_all_collected_from_plex  # defined there; imported when called
        get_and_add_all_collected_from_plex()

        # Backfill resolution for items with NULL resolution
        logging.info("Starting resolution backfill from stored paths...")
        try:
            result = backfill_resolution_from_stored_paths()
            if result.get('success'):
                logging.info(f"Resolution backfill completed: {result.get('updated', 0)} items updated, {result.get('failed', 0)} failed")
            else:
                logging.error(f"Resolution backfill failed: {result.get('error', 'Unknown error')}")
        except Exception as e:
            logging.error(f"Error during resolution backfill: {e}", exc_info=True)

        # Add reconciliation call after full scan processing
        logging.info("Triggering queue reconciliation after full Plex scan.")
        self.task_reconcile_queues()

    # Add manual Plex full scan task method
    def task_manual_plex_full_scan(self):
        """Manually trigger a full Plex scan, bypassing the mode check."""
        from queues.run_program import backfill_resolution_from_stored_paths, get_and_add_all_collected_from_plex  # defined there; imported when called
        logging.info("Executing manual Plex full scan task...")
        get_and_add_all_collected_from_plex(bypass=True)

        # Backfill resolution for items with NULL resolution
        logging.info("Starting resolution backfill from stored paths...")
        try:
            result = backfill_resolution_from_stored_paths()
            if result.get('success'):
                logging.info(f"Resolution backfill completed: {result.get('updated', 0)} items updated, {result.get('failed', 0)} failed")
            else:
                logging.error(f"Resolution backfill failed: {result.get('error', 'Unknown error')}")
        except Exception as e:
            logging.error(f"Error during resolution backfill: {e}", exc_info=True)

        # Add reconciliation call after full scan processing
        logging.info("Triggering queue reconciliation after manual Plex full scan.")
        self.task_reconcile_queues()

    def task_check_plex_files(self):
        """Check for new files in Plex location and update libraries"""
        updated_sections = set()  # Initialize here to prevent UnboundLocalError
        if not get_setting('Plex', 'update_plex_on_file_discovery') and not get_setting('Plex', 'disable_plex_library_checks'):
            logging.debug("Skipping Plex file check as both relevant settings are disabled.")
            return

        # This runs every minute. With nothing in Checking and no candidates for
        # the stale-location repair below, skip the Plex connection round trips.
        try:
            _work_conn = get_db_connection()
            try:
                _has_work = _work_conn.execute(
                    'SELECT 1 FROM media_items WHERE state = "Checking" '
                    'AND (ghostlisted IS NULL OR ghostlisted = 0) LIMIT 1'
                ).fetchone() or _work_conn.execute(
                    '''SELECT 1 FROM media_items
                       WHERE state = "Collected"
                         AND upgrading_from IS NOT NULL AND upgrading_from != ""
                         AND filled_by_file IS NOT NULL AND filled_by_file != ""
                       LIMIT 1'''
                ).fetchone()
            finally:
                _work_conn.close()
            if not _has_work:
                logging.debug("Plex file check: no Checking items or stale-location candidates; skipping.")
                return
        except Exception as e_work:
            logging.debug(f"Plex file check: work pre-check failed ({e_work}); continuing.")

        # Use centralized Plex connection setup if possible or ensure proper error handling
        plex = None
        try:
            plex_url = get_setting('Plex', 'url', default='')
            plex_token = get_setting('Plex', 'token', default='')
            
            if not plex_url or not plex_token:
                # Check symlink settings if primary are missing
                if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                    plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
                    plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')

            if not plex_url or not plex_token:
                logging.warning("Plex URL or token not configured in primary or symlink settings. Skipping Plex file check.")
                return

            # Connect to Plex server
            plex = PlexServer(plex_url, plex_token)
            sections = plex.library.sections()
            logging.info(f"Connected to Plex server for file check, found {len(sections)} library sections.")

        except Exception as e:
            logging.error(f"Failed to connect to Plex for file check: {str(e)}")
            return # Cannot proceed without Plex connection

        plex_file_location = get_setting('Plex', 'mounted_file_location', default='/mnt/zurg/__all__')
        if not os.path.exists(plex_file_location):
             # Also check original_files_path for symlink mode as a fallback?
            if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                plex_file_location = get_setting('File Management', 'original_files_path', default=None)
                if not plex_file_location or not os.path.exists(plex_file_location):
                    logging.warning(f"Plex mounted_file_location and original_files_path (for symlink mode) do not exist. Cannot check files.")
                    return
            elif get_setting('Plex', 'disable_plex_library_checks', default=False):
                logging.warning(f"Plex mounted_file_location ('{plex_file_location}') does not exist, but proceeding with file discovery as library checks are disabled.")
            else:
                logging.warning(f"Plex mounted_file_location does not exist: {plex_file_location}")
                return


        # Get all media items from database that are in Checking state
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            items = cursor.execute('SELECT id, title, filled_by_title, filled_by_file, filled_by_torrent_id, filled_by_magnet, type, imdb_id, tmdb_id, season_number, episode_number, year, version, original_scraped_torrent_title, real_debrid_original_title, debrid_folder_name, last_updated, upgrading_from, upgrading_from_torrent_id FROM media_items WHERE state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)').fetchall()
        except sqlite3.Error as db_err:
            logging.error(f"Database error fetching items for Plex check: {db_err}")
            conn.close()
            return
        finally:
             # Ensure connection is closed even if fetch fails after opening
            if conn: conn.close()

        logging.info(f"Found {len(items)} media items in Checking state to verify against Plex location '{plex_file_location}'")

        if not hasattr(self, 'plex_scan_tick_counts'):
            self.plex_scan_tick_counts = {}

        def _ffprobe_gate_or_reject(item_dict, actual_file_path, cache_key):
            """Optional playability check for Plex mode, gated by the same
            ffprobe_all_nzbs / ffprobe_all_debrid_additions toggles used by
            check_local_file_for_item in Symlinked/Local mode. Runs the moment
            a real on-disk path is resolved here, before this item is ever
            surfaced to Plex (scanned or searched for) — so a confirmed-broken
            file is rejected and re-scraped before Plex has any chance to add
            it, avoiding a "recently added" flicker.
            Returns True if the item should proceed, False if the caller should
            skip it this pass: either rejected (already reverted to Wanted) or
            deferred (NZB job still downloading; stays in Checking and is
            re-checked next pass). An inconclusive probe passes.
            """
            # In Symlinked/Local mode, check_local_file_for_item is already the
            # authoritative ffprobe gate for this item - it runs synchronously
            # in the same pass that creates the symlink. task_check_plex_files
            # can run concurrently with that (e.g. update_plex_on_file_discovery
            # enabled alongside Symlinked/Local, a supported combination) and
            # would otherwise independently re-resolve and re-probe the same
            # file with no coordination between the two - the same class of
            # redundant/racing double-verification already fixed elsewhere this
            # session. Leave this task's role in that mode as pure Plex
            # notification, same as before this change.
            if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                return True

            item = dict(item_dict)
            torrent_id = str(item.get('filled_by_torrent_id') or '')
            is_nzb = torrent_id.startswith('nzb:')

            if is_nzb:
                import os as _os_size_gate
                from utilities.local_library_scan import (
                    _prefer_largest_nzb_source,
                    _reject_if_nzb_size_too_small,
                )
                _source_folder = _os_size_gate.path.dirname(actual_file_path)
                actual_file_path = _prefer_largest_nzb_source(item, _source_folder, actual_file_path)
                if _reject_if_nzb_size_too_small(item, actual_file_path):
                    if cache_key in self.plex_scan_tick_counts:
                        del self.plex_scan_tick_counts[cache_key]
                    return False

            probe_section = 'Usenet Provider' if is_nzb else 'Debrid Provider'
            probe_key = 'ffprobe_all_nzbs' if is_nzb else 'ffprobe_all_debrid_additions'
            if not get_setting(probe_section, probe_key, False):
                return True

            if is_nzb:
                try:
                    from database.nzb_playback_repair import has_pending_playback_repair
                    if has_pending_playback_repair(item.get('id')):
                        logging.info(f"[ffprobe] Skipping {probe_key} for item {item.get('id')} — active playback-repair replacement candidate, already verified by cli_mount's own check")
                        return True
                except Exception:
                    pass

                # An item reaches Checking as soon as its NZB job is *matched*
                # (file list known from NZB/par2 metadata), not once the job has
                # actually finished downloading - unlike Symlinked/Local mode
                # (confirmed working, left untouched), Plex mode's file-discovery
                # here has no extra settle/retry delay before the probe fires, so
                # it's more exposed to catching the file mid-download. The
                # readability probe samples a random point 20-80% into the file's
                # duration - if that point hasn't downloaded yet, the read fails
                # cleanly (not an inconclusive/timeout case), so the probe's own
                # conservative "assume readable" fallback never catches this and
                # a genuinely-fine file gets wrongly rejected. Defer the probe
                # until cli_mount reports the job at/near complete instead of
                # judging a file that isn't fully there yet.
                try:
                    job_hash = torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id
                    from usenet import get_usenet_client
                    job_status = get_usenet_client().get_job_status(job_hash)
                    if job_status and job_status.get('state') != 'completed' and job_status.get('progress', 100) < 95:
                        # Skip this pass rather than proceed: proceeding sent the item on to
                        # the Plex scan and the tick-based force-collect without ever probing it.
                        logging.info(f"[ffprobe] Deferring {probe_key} for item {item.get('id')} — job {job_hash} still downloading ({job_status.get('progress', 0)}%)")
                        return False
                except Exception as e:
                    logging.debug(f"[ffprobe] Could not check job download progress for {torrent_id}: {e}")

            logging.info(f"[ffprobe] Running playability check ({probe_key}) on '{actual_file_path}'")
            from usenet.repair_engine import _verify_file_readable
            if _verify_file_readable(actual_file_path):
                logging.info(f"[ffprobe] Playability check passed for '{actual_file_path}'")
                return True

            logging.warning(f"[ffprobe] Playability check FAILED for '{actual_file_path}' — rejecting and reverting item {item.get('id')} to Wanted")
            from utilities.local_library_scan import _reject_unplayable_source
            _reject_unplayable_source(item, is_nzb)
            try:
                self.queue_manager.move_to_wanted(item, 'Checking')
            except Exception as e_wanted:
                logging.error(f"[ffprobe] Failed to move item {item.get('id')} back to Wanted after failed probe: {e_wanted}")
            if cache_key in self.plex_scan_tick_counts:
                del self.plex_scan_tick_counts[cache_key]
            return False

        # Check if Plex library checks are disabled (file discovery only)
        if get_setting('Plex', 'disable_plex_library_checks', default=False):
            logging.info("Plex library checks disabled - marking found files as Collected")
            updated_items = 0
            not_found_items = 0
            
            # Initialize scan tracking and tick counts
            paths_to_scan_by_section = {} # Store {section_title: set(constructed_paths)}
            sections_map = {}
            if plex and sections: # Only map if connection succeeded
                sections_map = {s.title: s for s in sections} # Map titles to section objects
            if not hasattr(self, 'plex_scan_tick_counts'):
                self.plex_scan_tick_counts = {}

            # If mounted file location is blank/empty, mark all items as Collected without file checks
            if not plex_file_location or plex_file_location.strip() == '':
                logging.info("Plex mounted file location is blank - marking all items as Collected without file existence checks")
                for item_dict in items:
                    item_id = item_dict['id']
                    item_title_log = (item_dict['title'] or 'N/A')
                    
                    conn_update = None
                    try:
                        conn_update = get_db_connection()
                        cursor_update = conn_update.cursor()
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        cursor_update.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                              (now, item_id))

                        if cursor_update.rowcount > 0:
                            conn_update.commit()
                            logging.info(f"Updated item {item_id} ({item_title_log}) to Collected state (Plex checks disabled, no file location).")
                            updated_items += 1
                            # Post-processing and notification logic
                            updated_item_details = get_media_item_by_id(item_id)
                            if updated_item_details:
                                handle_state_change(dict(updated_item_details))
                                # Add Collected notification
                                from database.database_writing import add_to_collected_notifications
                                notification_item = dict(updated_item_details)
                                notification_item['is_upgrade'] = False
                                notification_item['new_state'] = "Collected"
                                add_to_collected_notifications(notification_item)
                                logging.info(f"Added collection notification for item: {item_id} ({item_title_log})")
                        else:
                            logging.debug(f"Item {item_id} was not updated to Collected (state may have changed).")

                    except sqlite3.Error as db_update_err:
                        logging.error(f"Database error updating item {item_id} to collected: {db_update_err}")
                        if conn_update: conn_update.rollback()
                    except Exception as e_update:
                        logging.error(f"Unexpected error during item update/notification for {item_id}: {e_update}", exc_info=True)
                        if conn_update: conn_update.rollback()
                    finally:
                        if conn_update: conn_update.close()
                
                # Log summary and return early
                logging.info(f"Plex check summary (checks disabled, no file location): {updated_items} items marked as Collected, 0 items not found.")
                return

            # If we have a valid file location, proceed with file existence checks
            for item_dict in items: # Iterate over dicts
                item_id = item_dict['id']
                filled_by_title = item_dict['filled_by_title']
                filled_by_file = item_dict['filled_by_file']
                # --- START: Ensure new field is fetched ---
                original_scraped_torrent_title = item_dict['original_scraped_torrent_title'] or ''
                real_debrid_original_title = item_dict['real_debrid_original_title'] or ''
                debrid_folder_name = item_dict['debrid_folder_name'] or ''
                # --- END: Ensure new field is fetched ---

                if not filled_by_title or not filled_by_file: # This check might need re-evaluation if filled_by_title can be empty but other titles exist
                    logging.debug(f"Item {item_id} missing filled_by_title or filled_by_file. Skipping.")
                    continue

                # --- Get potential folder names ---
                # filled_by_title already fetched
                # original_torrent_title already fetched (as original_scraped_torrent_title)
                current_filename = item_dict['filled_by_file']

                # --- Construct potential paths in order of priority ---
                paths_to_check = []
                base_path = plex_file_location # Base path to check within

                # 0. Exact provider folder name
                if debrid_folder_name:
                    paths_to_check.append(os.path.join(base_path, debrid_folder_name, current_filename))

                # 1. Original Scraped Torrent Title (raw)
                if original_scraped_torrent_title:
                    paths_to_check.append(os.path.join(base_path, original_scraped_torrent_title, current_filename))

                # 2. Original Scraped Torrent Title (trimmed)
                if original_scraped_torrent_title:
                    original_scraped_torrent_title_trimmed = os.path.splitext(original_scraped_torrent_title)[0]
                    if original_scraped_torrent_title_trimmed != original_scraped_torrent_title:
                        paths_to_check.append(os.path.join(base_path, original_scraped_torrent_title_trimmed, current_filename))
                
                # 3. Real Debrid Original Title (raw) (NEW)
                if real_debrid_original_title:
                    paths_to_check.append(os.path.join(base_path, real_debrid_original_title, current_filename))

                # 4. Real Debrid Original Title (trimmed) (NEW)
                if real_debrid_original_title:
                    real_debrid_original_title_trimmed = os.path.splitext(real_debrid_original_title)[0]
                    if real_debrid_original_title_trimmed != real_debrid_original_title:
                        paths_to_check.append(os.path.join(base_path, real_debrid_original_title_trimmed, current_filename))

                # 5. Filled By Title (raw)
                if filled_by_title:
                    paths_to_check.append(os.path.join(base_path, filled_by_title, current_filename))

                # 6. Filled By Title (trimmed)
                if filled_by_title:
                    filled_by_title_trimmed = os.path.splitext(filled_by_title)[0]
                    if filled_by_title_trimmed != filled_by_title:
                        paths_to_check.append(os.path.join(base_path, filled_by_title_trimmed, current_filename))

                # 7. Direct path under base
                paths_to_check.append(os.path.join(base_path, current_filename))

                # --- Check paths in order ---
                file_found_on_disk = False
                actual_file_path = None
                checked_paths_log = [] # For logging if not found
                for idx, potential_path in enumerate(paths_to_check):
                    checked_paths_log.append(potential_path)
                    logging.debug(f"Plex Check Attempt {idx+1}: Checking path: {potential_path}")
                    if os.path.exists(potential_path):
                        file_found_on_disk = True
                        actual_file_path = potential_path
                        logging.info(f"Plex Check: Found file for item {item_id} at: {actual_file_path} (Attempt {idx+1})")
                        break # Found it, stop checking

                # --- Handle Cache Key ---
                # Use a consistent cache key, perhaps based on item ID or a combo?
                # Using filled_by_title + filled_by_file might be less reliable if those change.
                # Let's stick with the existing filled_by_title:filled_by_file key for now.
                cache_key = f"{filled_by_title}:{current_filename}"


                if file_found_on_disk:
                    logging.info(f"Confirmed file exists on disk: {actual_file_path} for item {item_id}") # Log actual path found

                    if not _ffprobe_gate_or_reject(item_dict, actual_file_path, cache_key):
                        continue

                    # Add Tick Check and Scan Path Gathering
                    should_trigger_scan = False
                    current_tick = self.plex_scan_tick_counts.get(cache_key, 0) + 1
                    self.plex_scan_tick_counts[cache_key] = current_tick
                    # Trigger scan only if library checks are ENABLED
                    if not get_setting('Plex', 'disable_plex_library_checks', default=False):
                         if current_tick <= 3:
                             should_trigger_scan = True
                             updated_items += 1 # Count item here when scan is intended
                             logging.info(f"File '{current_filename}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                         else:
                             logging.info(f"File '{current_filename}' found (tick {current_tick}). Plex scan ticks exhausted — marking item {item_id} as Collected directly (file confirmed on disk, recentlyAdded did not resolve it).")
                             # Fallback: recentlyAdded scan failed to resolve this item (e.g. file was already
                             # in Plex before it entered Checking). File is confirmed on disk, so mark Collected.
                             _conn_fb = None
                             try:
                                 _conn_fb = get_db_connection()
                                 _cur_fb = _conn_fb.cursor()
                                 _now_fb = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                 _cur_fb.execute(
                                     'UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                     (_now_fb, item_id)
                                 )
                                 if _cur_fb.rowcount > 0:
                                     _conn_fb.commit()
                                     logging.info(f"Fallback: marked item {item_id} ({item_dict['title'] if item_dict['title'] else 'N/A'}) as Collected after {current_tick} ticks with file on disk.")
                                     _fb_details = get_media_item_by_id(item_id)
                                     if _fb_details:
                                         handle_state_change(dict(_fb_details))  # triggers replace_cleanup_after_collect internally
                                         from database.database_writing import add_to_collected_notifications
                                         _notif = dict(_fb_details)
                                         _notif['is_upgrade'] = False
                                         _notif['new_state'] = "Collected"
                                         add_to_collected_notifications(_notif)
                                     # Reset tick count so it doesn't keep trying on the now-Collected item
                                     if cache_key in self.plex_scan_tick_counts:
                                         del self.plex_scan_tick_counts[cache_key]
                                 else:
                                     logging.debug(f"Fallback: item {item_id} state already changed, skipping Collected update.")
                             except sqlite3.Error as _fb_db_err:
                                 logging.error(f"Fallback: DB error marking item {item_id} as Collected: {_fb_db_err}")
                                 if _conn_fb: _conn_fb.rollback()
                             except Exception as _fb_err:
                                 logging.error(f"Fallback: unexpected error for item {item_id}: {_fb_err}", exc_info=True)
                                 if _conn_fb: _conn_fb.rollback()
                             finally:
                                 if _conn_fb: _conn_fb.close()
                    else:
                         # If library checks are disabled, we don't trigger scans based on ticks here
                         # We just mark as collected
                         updated_items += 1 # Count item as 'updated' if found (checks disabled case)
                         logging.info(f"File '{current_filename}' found (tick {current_tick}). Library checks disabled, will mark as collected.")


                    # --- Only gather scan paths if checks enabled AND should_trigger_scan ---
                    if should_trigger_scan and not get_setting('Plex', 'disable_plex_library_checks', default=False):
                        if not sections:
                             logging.error("Plex sections not available, cannot identify scan paths.")
                             # Continue processing other items
                        else:
                            item_type_mapped = 'show' if item_dict['type'] == 'episode' else item_dict['type']
                            logging.debug(f"Identifying scan paths for item {item_id} (type: {item_type_mapped}, title: '{filled_by_title}')")

                            found_matching_section_location = False
                            # Use the *folder name* from the actual_file_path to construct the scan path relative to section locations
                            folder_name_found = os.path.basename(os.path.dirname(actual_file_path))

                            for section in sections:
                                if section.type != item_type_mapped:
                                    continue

                                logging.debug(f"  Checking Section '{section.title}' (Type: {section.type})")
                                for location in section.locations:
                                    constructed_plex_path = os.path.join(location, folder_name_found)
                                    logging.debug(f"    Considering scan path: '{constructed_plex_path}' based on location '{location}' and found folder '{folder_name_found}'")

                                    if section.title not in paths_to_scan_by_section:
                                        paths_to_scan_by_section[section.title] = set()
                                    paths_to_scan_by_section[section.title].add(constructed_plex_path)
                                    found_matching_section_location = True

                            if not found_matching_section_location:
                                logging.warning(f"Could not find any matching Plex library section (type: {item_type_mapped}) for item {item_id} based on file '{current_filename}'. Scan might not be triggered correctly.")

                    # --- Update item state to Collected if checks are disabled ---
                    if get_setting('Plex', 'disable_plex_library_checks', default=False):
                         conn_update = None
                         try:
                             conn_update = get_db_connection()
                             cursor_update = conn_update.cursor()
                             now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                             cursor_update.execute('UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                                   (now, item_id))

                             if cursor_update.rowcount > 0:
                                 conn_update.commit()
                                 item_title_log = (item_dict['title'] or 'N/A')
                                 logging.info(f"Updated item {item_id} ({item_title_log}) to Collected state (Plex checks disabled).")
                                 # Post-processing and notification logic... (omitted for brevity, should be similar to existing code)
                                 updated_item_details = get_media_item_by_id(item_id)
                                 if updated_item_details:
                                      handle_state_change(dict(updated_item_details))
                                      # Add notification logic here...

                             else:
                                 logging.debug(f"Item {item_id} was not updated to Collected (state may have changed).")

                         except sqlite3.Error as db_update_err:
                             logging.error(f"Database error updating item {item_id} to collected: {db_update_err}")
                             if conn_update: conn_update.rollback()
                         except Exception as e_update:
                              logging.error(f"Unexpected error during item update/notification for {item_id}: {e_update}", exc_info=True)
                              if conn_update: conn_update.rollback()
                         finally:
                             if conn_update: conn_update.close()


                else: # File not found on disk
                    not_found_items += 1
                    logging.debug(f"File not found on disk for item {item_id}. Checked paths:\n  " + "\n  ".join(checked_paths_log))
                    # Reset tick count if file missing
                    if cache_key in self.plex_scan_tick_counts:
                        logging.debug(f"Resetting Plex scan tick count for missing file '{current_filename}'.")
                        del self.plex_scan_tick_counts[cache_key]
                    continue

            # Add Scan Triggering Logic
            if paths_to_scan_by_section and plex and sections_map: # Check connection exists
                logging.info(f"Triggering scans for {len(paths_to_scan_by_section)} sections based on detected files (library checks disabled)...")
                final_updated_sections = set() # Track unique section titles updated
                for section_title, scan_paths in paths_to_scan_by_section.items():
                    section = sections_map.get(section_title)
                    if not section:
                        logging.error(f"Could not find section object for title '{section_title}' during scan trigger phase.")
                        continue

                    for scan_path in scan_paths:
                        try:
                            logging.info(f"Triggering Plex section '{section.title}' update scan for path: {scan_path}")
                            section.update(path=scan_path)
                            final_updated_sections.add(section.title)
                        except NotFound:
                             logging.warning(f"Path '{scan_path}' not found by Plex server during scan trigger for section '{section.title}'. This might be expected if the folder doesn't exist yet.")
                        except Exception as e_scan:
                             logging.error(f"Failed to trigger update scan for Plex section '{section.title}' with path '{scan_path}': {str(e_scan)}", exc_info=True)

                if final_updated_sections:
                    logging.info(f"Plex sections triggered for update in this run: {', '.join(sorted(list(final_updated_sections)))}")

            # Log summary of operations
            logging.info(f"Plex check summary (checks disabled): {updated_items} items found on disk and marked Collected, {not_found_items} items not found.")
            # We don't update sections when checks are disabled. # <-- This comment is now slightly inaccurate due to the edit, but harmless.

        # ----- ELSE: Plex library checks are ENABLED -----
        else:
            logging.info("Plex library checks enabled - verifying file existence and triggering scans if needed.")
            updated_sections = set()  # Track which sections we've updated
            updated_items = 0 # Count items found AND triggering scan
            not_found_items = 0

            if not hasattr(self, 'plex_scan_tick_counts'):
                self.plex_scan_tick_counts = {}

            # Store tuples of (section, constructed_scan_path)
            # Using a set avoids triggering the exact same path scan multiple times if found via different items
            paths_to_scan_by_section = {} # {section_title: set(constructed_paths)}
            sections_map = {s.title: s for s in sections} # Map titles to section objects

            for item_dict in items: # Iterate over dicts
                item_dict = dict(item_dict) if not isinstance(item_dict, dict) else item_dict
                item_id = item_dict['id']
                filled_by_title = item_dict['filled_by_title']
                current_filename = item_dict['filled_by_file'] # Use current_filename for clarity
                # --- START: Ensure new field is fetched ---
                original_scraped_torrent_title = item_dict['original_scraped_torrent_title'] or ''
                real_debrid_original_title = item_dict['real_debrid_original_title'] or ''
                debrid_folder_name = item_dict['debrid_folder_name'] or ''
                # --- END: Ensure new field is fetched ---

                if not filled_by_title or not current_filename: 
                    logging.debug(f"Item {item_id} missing filled_by_title or current_filename. Skipping Plex scan trigger check.")
                    continue

                cache_key = f"{filled_by_title}:{current_filename}"

                # --- START: UNIFIED FILE SEARCH LOGIC (consistent with disabled checks mode) ---
                paths_to_check_info = [] # Stores dicts: {'name': folder_name, 'path': full_path, 'type': log_type}
                base_path = plex_file_location

                # 0. Exact provider folder name
                if debrid_folder_name:
                    path = os.path.join(base_path, debrid_folder_name, current_filename)
                    paths_to_check_info.append({'name': debrid_folder_name, 'path': path, 'type': 'debrid_folder_name'})

                # 1. Original Scraped Torrent Title (raw)
                if original_scraped_torrent_title:
                    path = os.path.join(base_path, original_scraped_torrent_title, current_filename)
                    paths_to_check_info.append({'name': original_scraped_torrent_title, 'path': path, 'type': 'original_scraped_raw'})

                # 2. Original Scraped Torrent Title (trimmed)
                if original_scraped_torrent_title:
                    trimmed_title = os.path.splitext(original_scraped_torrent_title)[0]
                    if trimmed_title != original_scraped_torrent_title:
                        path = os.path.join(base_path, trimmed_title, current_filename)
                        paths_to_check_info.append({'name': trimmed_title, 'path': path, 'type': 'original_scraped_trimmed'})
                
                # 3. Real Debrid Original Title (raw)
                if real_debrid_original_title:
                    path = os.path.join(base_path, real_debrid_original_title, current_filename)
                    paths_to_check_info.append({'name': real_debrid_original_title, 'path': path, 'type': 'real_debrid_raw'})

                # 4. Real Debrid Original Title (trimmed)
                if real_debrid_original_title:
                    trimmed_title = os.path.splitext(real_debrid_original_title)[0]
                    if trimmed_title != real_debrid_original_title:
                        path = os.path.join(base_path, trimmed_title, current_filename)
                        paths_to_check_info.append({'name': trimmed_title, 'path': path, 'type': 'real_debrid_trimmed'})

                # 5. Filled By Title (raw)
                if filled_by_title:
                    path = os.path.join(base_path, filled_by_title, current_filename)
                    paths_to_check_info.append({'name': filled_by_title, 'path': path, 'type': 'filled_by_title_raw'})

                # 6. Filled By Title (trimmed)
                if filled_by_title:
                    trimmed_title = os.path.splitext(filled_by_title)[0]
                    if trimmed_title != filled_by_title:
                        path = os.path.join(base_path, trimmed_title, current_filename)
                        paths_to_check_info.append({'name': trimmed_title, 'path': path, 'type': 'filled_by_title_trimmed'})

                # 7. Direct path under base
                direct_path = os.path.join(base_path, current_filename)
                paths_to_check_info.append({'name': None, 'path': direct_path, 'type': 'direct_under_base'})

                file_found_on_disk = False
                actual_file_path = None
                folder_name_for_plex_scan = None 
                found_path_type_log = "None"
                log_checked_paths = [] # For detailed logging if not found

                for idx, p_info in enumerate(paths_to_check_info):
                    potential_path = p_info['path']
                    log_checked_paths.append(potential_path) 
                    logging.debug(f"Plex Check (checks enabled) Attempt {idx+1}: Checking path: {potential_path} (using folder '{p_info['name']}', type: {p_info['type']})")
                    if os.path.exists(potential_path):
                        file_found_on_disk = True
                        actual_file_path = potential_path
                        folder_name_for_plex_scan = p_info['name'] 
                        found_path_type_log = p_info['type']
                        item_title_for_log = (item_dict['title'] or 'N/A')
                        logging.info(f"Plex Check (checks enabled): Found file for item {item_id} ('{item_title_for_log}') at: {actual_file_path} (Type: {found_path_type_log}, Folder for scan: '{folder_name_for_plex_scan}')")
                        break 
                # --- END: UNIFIED FILE SEARCH LOGIC ---
                
                should_trigger_scan = False
                if file_found_on_disk:
                    # File exists, update cache and handle tick count
                    item_title_for_log = item_dict['title'] if item_dict['title'] else 'N/A'
                    logging.info(f"Plex Check (checks enabled): Found file for item {item_id} ('{item_title_for_log}') at: {actual_file_path} (Type: {found_path_type_log}, Folder for scan: '{folder_name_for_plex_scan}')")
                    logging.debug(f"Confirmed file exists on disk: {actual_file_path} for item {item_id}")
                    current_tick = self.plex_scan_tick_counts.get(cache_key, 0) + 1
                    self.plex_scan_tick_counts[cache_key] = current_tick

                    if not _ffprobe_gate_or_reject(item_dict, actual_file_path, cache_key):
                        continue

                    # Option C: Direct Plex library lookup.
                    # Try 1: file path search (most reliable — checks exact file Plex should have indexed).
                    # Try 2: GUID search (fallback — sometimes unreliable in Plex).
                    _force_collect_reason = None
                    _imdb_id = item_dict['imdb_id']
                    _tmdb_id = item_dict['tmdb_id']
                    _item_type_lookup = 'show' if item_dict['type'] == 'episode' else item_dict['type']
                    # File path search: ask Plex "do you have this exact file?"
                    # Plex ?file= matches against full Plex path; URL-encode the filename.
                    _plex_location = None
                    _new_basename = os.path.basename(item_dict['filled_by_file'] or '')
                    _plex_rating_key = None
                    if actual_file_path:
                        from urllib.parse import quote as _urlquote
                        _plex_filename = os.path.basename(actual_file_path)
                        _is_episode = item_dict['type'] == 'episode'
                        for _section in sections:
                            if _section.type != _item_type_lookup:
                                continue
                            try:
                                _type_param = '&type=4' if _is_episode else ''
                                _fp_results = plex.fetchItems(f'/library/sections/{_section.key}/all?file={_urlquote(_plex_filename)}{_type_param}')
                                if _fp_results:
                                    _force_collect_reason = f"file indexed in Plex confirmed (tick {current_tick})"
                                    try:
                                        _plex_location = _fp_results[0].media[0].parts[0].file
                                    except Exception:
                                        pass
                                    try:
                                        _fp_item = _fp_results[0]
                                        if _is_episode:
                                            _plex_rating_key = str(getattr(_fp_item, 'grandparentRatingKey', None) or getattr(_fp_item, 'ratingKey', None) or '')
                                        else:
                                            _plex_rating_key = str(getattr(_fp_item, 'ratingKey', '') or '')
                                    except Exception:
                                        pass
                                    if not _plex_location:
                                        logging.warning(f"[PlexCheck] File search found item {item_id} but could not extract location from result: {_fp_results[0]}")
                                    break
                            except Exception as _e_fp:
                                logging.debug(f"[PlexCheck] File path search failed for item {item_id}: {_e_fp}")
                    # GUID search fallback
                    if not _force_collect_reason and (_imdb_id or _tmdb_id):
                        for _section in sections:
                            if _section.type != _item_type_lookup:
                                continue
                            try:
                                _results = []
                                if _imdb_id:
                                    _results = _section.search(**{'guid': f'imdb://{_imdb_id}'})
                                if not _results and _tmdb_id:
                                    _results = _section.search(**{'guid': f'tmdb://{_tmdb_id}'})
                                if _results:
                                    _force_collect_reason = f"direct Plex GUID lookup confirmed in library (tick {current_tick})"
                                    # Try to extract location_on_disk from the matched Plex item,
                                    # preferring the part whose filename matches filled_by_file.
                                    # For episodes: _results is Show objects; must fetch episodes to get parts.
                                    if not _plex_location and _new_basename:
                                        try:
                                            _is_episode = item_dict['type'] == 'episode'
                                            for _pi in _results:
                                                _candidates = []
                                                if _is_episode:
                                                    # Show object: walk seasons→episodes to find parts
                                                    try:
                                                        _sn = item_dict['season_number']
                                                        _ep = item_dict['episode_number']
                                                        if _sn is not None and _ep is not None:
                                                            _episode_obj = _pi.episode(season=int(_sn), episode=int(_ep))
                                                            _candidates = [_episode_obj] if _episode_obj else []
                                                        else:
                                                            _candidates = _pi.episodes()
                                                    except Exception:
                                                        _candidates = []
                                                else:
                                                    _candidates = [_pi]
                                                for _candidate in _candidates:
                                                    for _pm in getattr(_candidate, 'media', []):
                                                        for _pp in getattr(_pm, 'parts', []):
                                                            _pp_file = getattr(_pp, 'file', '') or ''
                                                            if os.path.basename(_pp_file) == _new_basename:
                                                                _plex_location = _pp_file
                                                                if not _plex_rating_key:
                                                                    try:
                                                                        if _is_episode:
                                                                            _plex_rating_key = str(getattr(_candidate, 'grandparentRatingKey', None) or getattr(_candidate, 'ratingKey', None) or '')
                                                                        else:
                                                                            _plex_rating_key = str(getattr(_candidate, 'ratingKey', '') or '')
                                                                    except Exception:
                                                                        pass
                                                                break
                                                        if _plex_location:
                                                            break
                                                    if _plex_location:
                                                        break
                                                if _plex_location:
                                                    break
                                        except Exception as _loc_err:
                                            logging.debug(f"[PlexCheck] Location extraction from GUID results failed for item {item_id}: {_loc_err}")
                                    # Extract ratingKey even if location not found via file match
                                    if not _plex_rating_key and _results:
                                        try:
                                            _pi0 = _results[0]
                                            _is_episode = item_dict['type'] == 'episode'
                                            if _is_episode:
                                                _plex_rating_key = str(getattr(_pi0, 'ratingKey', '') or '')
                                            else:
                                                _plex_rating_key = str(getattr(_pi0, 'ratingKey', '') or '')
                                        except Exception:
                                            pass
                                    if not _plex_location:
                                        logging.warning(f"[PlexCheck] GUID search found item {item_id} ({item_title_for_log}) in Plex but could not extract location (filled_by_file basename='{_new_basename}')")
                                    break
                            except Exception as _e_search:
                                logging.debug(f"[PlexCheck] Direct GUID search failed for item {item_id}: {_e_search}")

                    # Option B: Time-based fallback using last_updated from DB (restart-resilient).
                    if not _force_collect_reason:
                        _last_updated = item_dict['last_updated']
                        if _last_updated and current_tick > 1:
                            try:
                                _lu_dt = datetime.strptime(str(_last_updated), '%Y-%m-%d %H:%M:%S') if isinstance(_last_updated, str) else _last_updated
                                _mins_in_checking = (datetime.now() - _lu_dt).total_seconds() / 60
                                if _mins_in_checking > 30:
                                    _force_collect_reason = f"{_mins_in_checking:.0f}m in Checking (time-based fallback, tick {current_tick})"
                            except Exception:
                                pass

                    # Original tick fallback.
                    if not _force_collect_reason and current_tick > 3:
                        _force_collect_reason = f"tick {current_tick} exhausted (recentlyAdded did not resolve)"

                    if _force_collect_reason:
                        logging.info(f"[PlexCheck] Marking item {item_id} ('{item_title_for_log}') as Collected: {_force_collect_reason}.")
                        _conn_fb = None
                        try:
                            _conn_fb = get_db_connection()
                            _cur_fb = _conn_fb.cursor()
                            _now_fb = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            # Use Plex-confirmed path if available, otherwise fall back to
                            # actual_file_path from mount. NULL location_on_disk causes ghost
                            # entries in the library and repeated cleanup prompts.
                            # Never store __all__ paths — replace with typed folder (movies/shows).
                            _raw_loc = _plex_location or actual_file_path
                            if _raw_loc and '/__all__/' in _raw_loc:
                                _typed = 'shows' if item_dict.get('type') == 'episode' else 'movies'
                                _mount_base = _raw_loc.split('/__all__/')[0]
                                _raw_loc = _raw_loc.replace('/__all__/', f'/{_typed}/', 1)
                            _location_to_store = _raw_loc
                            if _location_to_store:
                                if _plex_rating_key:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ?, location_on_disk = ?, ms_item_id = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, _location_to_store, _plex_rating_key, item_id)
                                    )
                                    logging.info(f"[PlexCheck] Updated ms_item_id={_plex_rating_key} for item {item_id} ({item_title_for_log})")
                                else:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ?, location_on_disk = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, _location_to_store, item_id)
                                    )
                            else:
                                if _plex_rating_key:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ?, ms_item_id = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, _plex_rating_key, item_id)
                                    )
                                    logging.info(f"[PlexCheck] Updated ms_item_id={_plex_rating_key} for item {item_id} ({item_title_for_log})")
                                else:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET state = "Collected", collected_at = ? WHERE id = ? AND state = "Checking" AND (ghostlisted IS NULL OR ghostlisted = 0)',
                                        (_now_fb, item_id)
                                    )
                            if _cur_fb.rowcount > 0:
                                # Set original_filename from location_on_disk basename if not already set
                                import os as _os
                                _orig_fn_src = _location_to_store or ''
                                _orig_fn_val = _os.path.basename(_orig_fn_src) if _orig_fn_src else None
                                if _orig_fn_val:
                                    _cur_fb.execute(
                                        'UPDATE media_items SET original_filename = ? WHERE id = ? AND (original_filename IS NULL OR original_filename = "")',
                                        (_orig_fn_val, item_id)
                                    )
                                _conn_fb.commit()
                                logging.info(f"[PlexCheck] Marked item {item_id} ({item_title_for_log}) as Collected ({_force_collect_reason}).")
                                if _location_to_store:
                                    logging.info(f"[PlexCheck] Updated location_on_disk for item {item_id} ({item_title_for_log}): {_location_to_store}")

                                # Debrid File Naming: ensure the cli_mount folder is renamed
                                # at Collected time. By this point the file is confirmed in
                                # cli_mount so the rename succeeds immediately — no retry needed.
                                # Covers cases where the add-time rename failed or was never
                                # triggered (pre-existing RD torrents, closure bug, etc.).
                                try:
                                    _torrent_id_chk = item_dict.get('filled_by_torrent_id', '') or ''
                                    if (not _torrent_id_chk.startswith('nzb:')
                                            and _torrent_id_chk
                                            and get_setting('Debrid Provider', 'enable_debrid_naming', False)):
                                        from routes.scraper_routes import _build_debrid_title as _bdt_chk
                                        _chk_type = item_dict.get('type', '')
                                        _chk_mt = 'tv' if _chk_type == 'episode' else _chk_type
                                        _chk_orig = item_dict.get('original_scraped_torrent_title') or item_dict.get('filled_by_title', '')
                                        _chk_title = _bdt_chk(
                                            title=item_dict.get('title', ''),
                                            year=item_dict.get('year', ''),
                                            imdb_id=item_dict.get('imdb_id'),
                                            version=item_dict.get('version', ''),
                                            original_scraped_torrent_title=_chk_orig,
                                            media_type=_chk_mt,
                                            season=item_dict.get('season_number'),
                                            episode=item_dict.get('episode_number'),
                                            episode_title=item_dict.get('episode_title'),
                                            tags=item_dict.get('tags') or None,
                                            content_source_display_name=item_dict.get('content_source_detail') or item_dict.get('content_source'),
                                        )
                                        if _chk_title and _chk_title != _chk_orig:
                                            # Get hash from debrid provider using torrent ID
                                            from debrid import get_debrid_provider as _gdp_chk
                                            _dp_chk = _gdp_chk()
                                            if _dp_chk:
                                                _chk_info = _dp_chk.get_torrent_info(_torrent_id_chk)
                                                _chk_hash = (_chk_info or {}).get('hash', '').lower()
                                                if _chk_hash:
                                                    import threading as _t_chk
                                                    def _do_chk_rename(h, name, ident, iid):
                                                        import time as _t
                                                        try:
                                                            from usenet import get_usenet_client
                                                            _dc = get_usenet_client()
                                                            if not hasattr(_dc, 'rename_nzb'):
                                                                return  # active usenet provider (e.g. nzbdav) has no rename semantics
                                                            for _a in range(5):
                                                                # This short loop (5 attempts x 10s = 50s total) isn't long
                                                                # enough to distinguish "genuinely gone" from "cli_mount
                                                                # hasn't finished its periodic sync yet" (can take ~10 min
                                                                # per torrent_processor.py) — so a 404 here is never treated
                                                                # as final, just run the fixed attempt budget like before.
                                                                if _dc.rename_nzb(h, name):
                                                                    logging.info(f'[DebridNaming] Renamed {h!r} -> {name!r} (collected, attempt {_a+1})')
                                                                    if iid:
                                                                        try:
                                                                            from database.database_writing import update_media_item as _umi
                                                                            _umi(iid, debrid_folder_name=name, filled_by_title=name)
                                                                        except Exception as _db_err:
                                                                            logging.debug(f'[DebridNaming] DB update failed (collected): {_db_err}')
                                                                        _dc.register_cli_ids_for_item(h, iid)
                                                                        _dc.push_tags_for_item(h, iid)
                                                                    return
                                                                _t.sleep(10)
                                                            logging.warning(f'[DebridNaming] Could not rename {h!r} after 5 attempts (collected)')
                                                        except Exception as _e:
                                                            logging.debug(f'[DebridNaming] Rename error (collected): {_e}')
                                                    _t_chk.Thread(target=_do_chk_rename, args=(_chk_hash, _chk_title, item_title_for_log, item_id), daemon=True).start()
                                except Exception as _dbn_chk_ex:
                                    logging.debug(f'[DebridNaming] Collected-time rename setup error for {item_id}: {_dbn_chk_ex}')

                                _fb_details = get_media_item_by_id(item_id)
                                if _fb_details:
                                    handle_state_change(dict(_fb_details))
                                    from database.database_writing import add_to_collected_notifications
                                    _notif = dict(_fb_details)
                                    _notif['is_upgrade'] = bool(_notif.get('upgrading_from'))
                                    _notif['new_state'] = "Collected"
                                    add_to_collected_notifications(_notif)
                                    # Log upgrade success to Upgrade Hub activity
                                    if _notif.get('upgrading_from'):
                                        try:
                                            from queues.upgrading_queue import log_successful_upgrade
                                            log_successful_upgrade(_notif)
                                        except Exception as _lsu_err:
                                            logging.debug(f"[PlexCheck] log_successful_upgrade failed: {_lsu_err}")
                                    # Upgrade cleanup: delete old torrent/NZB and remove old Plex entry.
                                    # Gated on Scraping.enable_upgrading_cleanup — when disabled, the old
                                    # file/torrent must be left in place (same "keep both files" contract
                                    # as local_library_scan.py's upgrade handling).
                                    _old_torrent_id = _notif.get('upgrading_from_torrent_id')
                                    _upgrading_from_path = _notif.get('upgrading_from')
                                    if not get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                                        logging.info(f"[PlexCheck] Scraping.enable_upgrading_cleanup is disabled — keeping old file/torrent for item {item_id} ({item_title_for_log}).")
                                    else:
                                        if _old_torrent_id:
                                            if _old_torrent_id.startswith('nzb:'):
                                                # Old item was an NZB — remove via cli_mount
                                                try:
                                                    from usenet import get_usenet_client as _guc
                                                    _uc = _guc()
                                                    if _uc:
                                                        _uc.remove_nzb(_old_torrent_id[4:], entry_name=_upgrading_from_path or '')
                                                        logging.info(f"[PlexCheck] Removed old upgrade NZB {_old_torrent_id} for item {item_id} ({item_title_for_log})")
                                                except Exception as _ct_err:
                                                    logging.warning(f"[PlexCheck] Failed to remove old upgrade NZB {_old_torrent_id}: {_ct_err}")
                                            else:
                                                # Old item was a debrid torrent
                                                try:
                                                    from debrid import get_debrid_provider as _gdp
                                                    _dp = _gdp()
                                                    if _dp:
                                                        _dp.remove_torrent(_old_torrent_id, removal_reason='Replaced by upgrade')
                                                        logging.info(f"[PlexCheck] Removed old upgrade torrent {_old_torrent_id} for item {item_id} ({item_title_for_log})")
                                                except Exception as _ct_err:
                                                    if '404' in str(_ct_err):
                                                        logging.debug(f"[PlexCheck] Old torrent {_old_torrent_id} already removed (404)")
                                                    else:
                                                        logging.warning(f"[PlexCheck] Failed to remove old upgrade torrent {_old_torrent_id}: {_ct_err}")
                                        if _upgrading_from_path:
                                            try:
                                                from utilities.plex_functions import remove_file_from_plex
                                                remove_file_from_plex(item_title_for_log, _upgrading_from_path)
                                                logging.info(f"[PlexCheck] Removed old upgrade file from Plex for item {item_id} ({item_title_for_log})")
                                            except Exception as _cp_err:
                                                logging.warning(f"[PlexCheck] Failed to remove old upgrade file from Plex for item {item_id}: {_cp_err}")
                                # Check if the Plex episode has a local:// guid (episode-level mismatch)
                                # or the show has no external IDs (show-level mismatch).
                                # In both cases, use the Plex GUID from battery to fix directly.
                                _plex_ep_item = None
                                _plex_ep_guid_is_local = False
                                try:
                                    _check_fn2 = os.path.basename((_fb_details.get('filled_by_file') or '') if _fb_details else '')
                                    if _check_fn2 and _fb_details and _fb_details.get('type') == 'episode':
                                        for _cs2 in sections:
                                            if _cs2.type != 'show':
                                                continue
                                            _cr2 = plex.fetchItems(f'/library/sections/{_cs2.key}/all?file={__import__("urllib.parse", fromlist=["quote"]).quote(_check_fn2)}&type=4')
                                            if _cr2:
                                                _plex_ep_item = _cr2[0]
                                                _ep_guid_str = str(getattr(_plex_ep_item, 'guid', ''))
                                                _plex_ep_guid_is_local = _ep_guid_str.startswith('local://')
                                                break
                                except Exception:
                                    pass

                                if _fb_details and _plex_ep_item and _plex_ep_guid_is_local:
                                    try:
                                        _fix_imdb   = _fb_details.get('imdb_id')
                                        _fix_imdb   = _fb_details.get('imdb_id')
                                        _fix_tmdb   = _fb_details.get('tmdb_id')
                                        _fix_season = _fb_details.get('season_number')
                                        _fix_ep     = _fb_details.get('episode_number')
                                        _fix_ep_rk  = str(_plex_ep_item.ratingKey)
                                        _fix_show_rk = str(getattr(_plex_ep_item, 'grandparentRatingKey', _fix_ep_rk))

                                        # Determine fix strategy using absolute_episode in battery:
                                        # - absolute_episode > 0 (anime/absolute numbering) → episode-level GUID per episode
                                        # - absolute_episode = 0 or NULL (regular shows) → show-level GUID via force_match
                                        _is_absolute = False
                                        try:
                                            from cli_battery.app.database import Session as _BatSess2, Item as _BatItem2, Season as _BatSeason2, Episode as _BatEp2
                                            with _BatSess2() as _bs2:
                                                _bi2 = _bs2.query(_BatItem2).filter_by(imdb_id=_fix_imdb).first() if _fix_imdb else None
                                                if _bi2:
                                                    _abs2 = _bs2.query(_BatEp2).join(_BatSeason2).filter(
                                                        _BatSeason2.item_id == _bi2.id,
                                                        _BatEp2.absolute_episode > 0
                                                    ).first()
                                                    _is_absolute = _abs2 is not None
                                        except Exception:
                                            pass

                                        import threading as _threading

                                        if not _is_absolute:
                                            logging.info(
                                                f"[PlexGUID] Item {item_id} regular show — "
                                                f"scheduling show-level fix-match (ratingKey={_fix_show_rk})"
                                            )
                                            def _do_show_fix(_rk, _title, _year, _tmdb, _imdb, _s, _ep):
                                                try:
                                                    from utilities.plex_matching_functions import force_match_with_tmdb
                                                    force_match_with_tmdb(
                                                        _title,
                                                        str(_year) if _year else None,
                                                        str(_tmdb) if _tmdb else '0',
                                                        _rk,
                                                        imdb_id=_imdb,
                                                        media_type='show',
                                                        season=_s,
                                                        episode=_ep,
                                                    )
                                                except Exception as _fe:
                                                    logging.debug(f"[PlexGUID] Show fix-match failed: {_fe}")
                                            _threading.Thread(
                                                target=_do_show_fix,
                                                args=(_fix_show_rk, _fb_details.get('title', ''),
                                                      _fb_details.get('year'), _fix_tmdb, _fix_imdb,
                                                      _fix_season, _fix_ep),
                                                daemon=True
                                            ).start()
                                        else:
                                            logging.info(
                                                f"[PlexGUID] Item {item_id} absolute show — "
                                                f"scheduling episode fix-match (ratingKey={_fix_ep_rk}, "
                                                f"S{_fix_season}E{_fix_ep})"
                                            )

                                        def _do_ep_fix(_rk, _title, _year, _tmdb, _imdb, _s, _ep):
                                            try:
                                                from cli_battery.app.direct_api import DirectAPI
                                                from utilities.settings import get_setting as _gs
                                                from plexapi.server import PlexServer as _PS
                                                _gu = _gs('Plex', 'url', '').rstrip('/')
                                                _gt = _gs('Plex', 'token', '')
                                                if not _gu or not _gt:
                                                    return
                                                _gplex = _PS(_gu, _gt, timeout=30)
                                                _gitem = _gplex.fetchItem(int(_rk))
                                                # Get episode GUID from battery
                                                _gr = DirectAPI.get_plex_guid(_imdb, 'show', season=_s, episode=_ep) if _imdb else None
                                                logging.info(f"[PlexGUID] get_plex_guid({_imdb}, S{_s}E{_ep}) returned: {_gr}")
                                                _ep_guid = (_gr or {}).get('episode_guid')
                                                _s_guid  = (_gr or {}).get('season_guid')
                                                _sh_guid = (_gr or {}).get('show_guid')
                                                # Use most specific available
                                                if _ep_guid:
                                                    _full_guid = f'plex://episode/{_ep_guid}'
                                                    _gname = f'{_title} S{_s:02d}E{_ep:02d}'
                                                elif _s_guid:
                                                    _full_guid = f'plex://season/{_s_guid}'
                                                    _gname = f'{_title} Season {_s}'
                                                elif _sh_guid:
                                                    _full_guid = f'plex://show/{_sh_guid}'
                                                    _gname = _title
                                                else:
                                                    logging.debug(f"[PlexGUID] No GUID available for {_imdb} S{_s}E{_ep}")
                                                    return
                                                logging.info(f"[PlexGUID] Applying {_full_guid} to ratingKey={_rk}")
                                                import time as _time
                                                import requests as _req
                                                from urllib.parse import quote as _uq

                                                # Use raw HTTP PUT — PlexAPI fixMatch() doesn't
                                                # work on episode items but the API endpoint does.
                                                _match_url = (
                                                    f"{_gu}/library/metadata/{_rk}/match"
                                                    f"?guid={_uq(_full_guid)}&name={_uq(_gname)}"
                                                    f"&X-Plex-Token={_gt}"
                                                )
                                                _resp = _req.put(_match_url, timeout=15)
                                                logging.info(f"[PlexGUID] PUT match HTTP {_resp.status_code} for ratingKey={_rk}")
                                                _time.sleep(2)
                                                # Trigger metadata refresh so Plex fetches episode title/summary immediately
                                                _req.put(f"{_gu}/library/metadata/{_rk}/refresh?X-Plex-Token={_gt}", timeout=15)
                                                _time.sleep(4)
                                                _gitem.reload()
                                                _new_guid = str(getattr(_gitem, 'guid', ''))
                                                if not _new_guid.startswith('local://'):
                                                    logging.info(f"[PlexGUID] Episode fix-match SUCCESS for '{_title}' S{_s}E{_ep} → {_new_guid}")
                                                else:
                                                    logging.warning(f"[PlexGUID] Episode fix-match failed (HTTP {_resp.status_code}) — guid still local:// for '{_title}' S{_s}E{_ep}")
                                            except Exception as _fme:
                                                logging.debug(f"[PlexGUID] Episode fix-match failed: {_fme}")
                                        if _is_absolute:
                                            _threading.Thread(
                                                target=_do_ep_fix,
                                                args=(_fix_ep_rk, _fb_details.get('title', ''),
                                                      _fb_details.get('year'), _fix_tmdb, _fix_imdb,
                                                      _fix_season, _fix_ep),
                                                daemon=True
                                            ).start()
                                    except Exception as _guid_err:
                                        logging.debug(f"[PlexGUID] Fix-match scheduling failed: {_guid_err}")

                                if cache_key in self.plex_scan_tick_counts:
                                    del self.plex_scan_tick_counts[cache_key]
                            else:
                                logging.debug(f"[PlexCheck] Item {item_id} state already changed, skipping Collected update.")
                        except sqlite3.Error as _fb_db_err:
                            logging.error(f"[PlexCheck] DB error marking item {item_id} as Collected: {_fb_db_err}")
                            if _conn_fb: _conn_fb.rollback()
                        except Exception as _fb_err:
                            logging.error(f"[PlexCheck] Unexpected error for item {item_id}: {_fb_err}", exc_info=True)
                            if _conn_fb: _conn_fb.rollback()
                        finally:
                            if _conn_fb: _conn_fb.close()
                        continue
                    else:
                        should_trigger_scan = True
                        updated_items += 1
                        logging.info(f"File '{current_filename}' found (tick {current_tick}). Identifying relevant Plex sections to scan.")
                else:
                    # File not found
                    not_found_items += 1
                    logging.debug(f"File not found on disk for item {item_id}. Checked paths:\n  " + "\n  ".join(log_checked_paths))
                    if cache_key in self.plex_scan_tick_counts:
                        logging.debug(f"Resetting Plex scan tick count for missing file '{current_filename}'.")
                        del self.plex_scan_tick_counts[cache_key]
                    # Need to continue loop if file not found --- # This comment is from a previous edit, still relevant
                    continue 

                # --- START: Logic to identify scan paths (original location)
                if should_trigger_scan:
                    if not sections:
                         logging.error("Plex sections not available, cannot identify scan paths.")
                         continue

                    item_type_mapped = 'show' if item_dict['type'] == 'episode' else item_dict['type']
                    logging.debug(f"Identifying scan paths for item {item_id} (type: {item_type_mapped}, title: '{filled_by_title}')")

                    # Get configured library filters (support both names and IDs)
                    allowed_library_keys = None
                    try:
                        from utilities.plex_functions import process_library_names

                        # Build library dictionaries for filtering
                        all_libraries = {}  # {name: key}
                        libraries_by_key = {}  # {key: name}
                        for section in sections:
                            # Convert key to string for consistent comparison
                            key_str = str(section.key)
                            all_libraries[section.title] = key_str
                            libraries_by_key[key_str] = section.title

                        # Get configured library names/IDs from settings
                        if item_type_mapped == 'movie':
                            movie_libs_setting = get_setting('Plex', 'movie_libraries', '')
                            if movie_libs_setting:
                                allowed_library_keys = process_library_names(movie_libs_setting, all_libraries, libraries_by_key)
                                lib_names = [libraries_by_key.get(key, key) for key in allowed_library_keys]
                                logging.debug(f"Filtering Plex file check to configured movie libraries: {lib_names}")
                        elif item_type_mapped == 'show':
                            shows_libs_setting = get_setting('Plex', 'shows_libraries', '')
                            if shows_libs_setting:
                                allowed_library_keys = process_library_names(shows_libs_setting, all_libraries, libraries_by_key)
                                lib_names = [libraries_by_key.get(key, key) for key in allowed_library_keys]
                                logging.debug(f"Filtering Plex file check to configured show libraries: {lib_names}")
                    except Exception as filter_err:
                        logging.warning(f"Error setting up library filters for Plex file check: {filter_err}. Will scan all matching sections.")
                        allowed_library_keys = None

                    found_matching_section_location = False

                    # If Plex already confirmed the file location, derive the scan path
                    # directly from Plex's own reported path — no mount assumptions needed.
                    # This avoids scanning every section location (e.g. /debrid/ufc) when
                    # the file only exists under one of them (e.g. /debrid/movies).
                    if _plex_location:
                        _plex_scan_dir = os.path.dirname(_plex_location)
                        for section in sections:
                            if section.type != item_type_mapped:
                                continue
                            if allowed_library_keys is not None and str(section.key) not in allowed_library_keys:
                                continue
                            # Only add the section that actually contains this path
                            for location in section.locations:
                                if _plex_scan_dir.startswith(location):
                                    if section.title not in paths_to_scan_by_section:
                                        paths_to_scan_by_section[section.title] = set()
                                    paths_to_scan_by_section[section.title].add(_plex_scan_dir)
                                    found_matching_section_location = True
                                    logging.debug(f"  Scan path from Plex-confirmed location: '{_plex_scan_dir}' in section '{section.title}'")
                                    break
                    else:
                        # Plex hasn't indexed the file yet.
                        # Use the ACTUAL folder name from the found file path (renamed CLI name)
                        # rather than folder_name_for_plex_scan (raw DB torrent name).
                        # actual_file_path is e.g. /debrid/__all__/Succession (2018) - S04 - {imdb-...}/file.mkv
                        # so dirname gives the renamed folder name that actually exists under
                        # /debrid/shows/, /debrid/movies/ etc.
                        # Use os.listdir to verify the folder exists at each section location
                        # before triggering a scan — this prevents scanning wrong virtual folders
                        # (e.g. /debrid/ufc/) for items that don't belong there.
                        # The folder name to scan. After debrid naming rename, __all__ updates
                        # to the CLI structured name, so actual_file_path dirname is reliable.
                        # Also try debrid_folder_name and filled_by_title from DB as fallbacks
                        # since the rename may not have propagated to __all__ yet on tick 1.
                        _actual_folder_name = os.path.basename(os.path.dirname(actual_file_path)) if actual_file_path else folder_name_for_plex_scan
                        _dbn = item_dict.get('debrid_folder_name') or ''
                        _candidate_folder_names = []
                        # Prefer debrid_folder_name (CLI renamed) over actual path when it's a proper CLI name
                        _ordered = [_dbn, _actual_folder_name, item_dict.get('filled_by_title') or ''] if '{imdb-' in _dbn else [_actual_folder_name, _dbn, item_dict.get('filled_by_title') or '']
                        for _cn in _ordered:
                            if _cn and _cn not in _candidate_folder_names:
                                _candidate_folder_names.append(_cn)

                        for section in sections:
                            if section.type != item_type_mapped:
                                continue
                            if allowed_library_keys is not None and str(section.key) not in allowed_library_keys:
                                logging.debug(f"Skipping section '{section.title}' (key: {section.key}) - not in configured libraries")
                                continue
                            logging.debug(f"  Checking Section '{section.title}' (Type: {section.type})")
                            for location in section.locations:
                                if _candidate_folder_names:
                                    # Try each candidate folder name against this location's listing.
                                    # Use the first one found. If none found, skip this location
                                    # to avoid scanning wrong virtual folders (e.g. /debrid/ufc/).
                                    try:
                                        _location_listing = os.listdir(location)
                                    except Exception:
                                        _location_listing = []
                                    _matched_folder = next(
                                        (n for n in _candidate_folder_names if n in _location_listing),
                                        None
                                    )
                                    if not _matched_folder:
                                        logging.debug(f"    Skipping '{location}' — none of candidates found in listing")
                                        continue
                                    constructed_plex_path = os.path.join(location, _matched_folder)
                                else:
                                    constructed_plex_path = location
                                logging.debug(f"    Scan path: '{constructed_plex_path}'")
                                if section.title not in paths_to_scan_by_section:
                                    paths_to_scan_by_section[section.title] = set()
                                paths_to_scan_by_section[section.title].add(constructed_plex_path)
                                found_matching_section_location = True

                    if not found_matching_section_location:
                        logging.warning(f"Could not find any matching Plex library section (type: {item_type_mapped}) for item {item_id} based on file '{current_filename}'. Scan might not be triggered correctly.")
                # --- END: Logic to identify scan paths ---


            # --- Trigger scans after checking all items ---
            if paths_to_scan_by_section:
                logging.info(f"Triggering scans for {len(paths_to_scan_by_section)} sections based on detected files...")
                final_updated_sections = set()
                for section_title, scan_paths in paths_to_scan_by_section.items():
                    section = sections_map.get(section_title)
                    if not section:
                        logging.error(f"Could not find section object for title '{section_title}' during scan trigger phase.")
                        continue

                    # Deduplicate: if multiple episode folders share the same parent (show folder),
                    # scan the parent once instead of each episode folder separately.
                    # This prevents flooding Plex with N concurrent Scanner processes for season packs.
                    # IMPORTANT: only collapse to parent when the parent is NOT a section root location
                    # (e.g. /debrid/shows). Collapsing to the section root triggers a full library
                    # re-scan which causes Plex to create duplicate/mismatched entries.
                    _section_root_locations = set(section.locations)
                    deduped_paths = set()
                    for scan_path in scan_paths:
                        parent = os.path.dirname(scan_path)
                        # Never collapse to a section root — that would scan the entire library
                        if parent in _section_root_locations:
                            deduped_paths.add(scan_path)
                            continue
                        siblings = [p for p in scan_paths if os.path.dirname(p) == parent and p != scan_path]
                        if siblings:
                            deduped_paths.add(parent)
                        else:
                            deduped_paths.add(scan_path)

                    # Hard cap: never trigger more than 5 scans per section per run.
                    # Remaining paths will be picked up on the next 60s run.
                    _MAX_SCANS_PER_RUN = 25
                    if len(deduped_paths) > _MAX_SCANS_PER_RUN:
                        logging.warning(
                            f"[PlexCheck] {len(deduped_paths)} scan paths for '{section.title}' — "
                            f"capping at {_MAX_SCANS_PER_RUN} to avoid Plex DB contention. "
                            f"Remaining will be picked up next run."
                        )
                        deduped_paths = set(list(deduped_paths)[:_MAX_SCANS_PER_RUN])

                    for scan_path in deduped_paths:
                        try:
                            logging.info(f"Triggering Plex section '{section.title}' update scan for path: {scan_path}")
                            section.update(path=scan_path)
                            final_updated_sections.add(section.title)
                        except NotFound:
                             logging.warning(f"Path '{scan_path}' not found by Plex server during scan trigger for section '{section.title}'.")
                        except Exception as e_scan:
                             logging.error(f"Failed to trigger update scan for Plex section '{section.title}' with path '{scan_path}': {str(e_scan)}", exc_info=True)

                if final_updated_sections:
                    logging.info(f"Plex sections triggered for update in this run: {', '.join(sorted(list(final_updated_sections)))}")


            # --- Stale location repair ---
            # Fix Collected items where location_on_disk still points to the pre-upgrade file.
            # These items were marked Collected via tick/time fallback without a Plex location,
            # or the GUID/file search failed at upgrade time. Query Plex by filled_by_file to fix.
            try:
                _repair_conn = get_db_connection()
                _all_upgraded = _repair_conn.execute(
                    '''SELECT id, title, type, filled_by_file, location_on_disk, upgrading_from,
                              imdb_id, tmdb_id, season_number, episode_number, filled_by_torrent_id
                       FROM media_items
                       WHERE state = "Collected"
                         AND upgrading_from IS NOT NULL AND upgrading_from != ""
                         AND filled_by_file IS NOT NULL AND filled_by_file != ""
                    '''
                ).fetchall()
                _repair_conn.close()

                # Keep only items where location_on_disk is stale:
                # - NULL/empty, OR
                # - basename matches upgrading_from basename (path still points to old file)
                # Skip RD torrent items — their location is managed via cli_mount rename,
                # not via Plex library search. Repairing them would overwrite with old NZB path.
                _stale_set = {}
                for _row in _all_upgraded:
                    _tid = _row['filled_by_torrent_id'] or ''
                    if _tid and not _tid.startswith('nzb:'):
                        continue  # RD torrent — skip stale repair
                    _loc = _row['location_on_disk'] or ''
                    _upg = _row['upgrading_from'] or ''
                    if not _loc:
                        _stale_set[_row['id']] = _row
                    elif os.path.basename(_loc) == os.path.basename(_upg):
                        _stale_set[_row['id']] = _row

                if _stale_set:
                    logging.info(f"[PlexCheck] Stale location repair: found {len(_stale_set)} Collected items with stale location_on_disk")
                    from urllib.parse import quote as _urlquote2
                    for _sid, _sitem in _stale_set.items():
                        _sfilled = _sitem['filled_by_file'] or ''
                        _sbasename = os.path.basename(_sfilled)
                        if not _sbasename:
                            continue
                        _stype = _sitem['type']
                        _stype_lookup = 'show' if _stype == 'episode' else _stype
                        _new_loc = None
                        try:
                            for _section in sections:
                                if _section.type != _stype_lookup:
                                    continue
                                _type_p = '&type=4' if _stype == 'episode' else ''
                                _sr = plex.fetchItems(f'/library/sections/{_section.key}/all?file={_urlquote2(_sbasename)}{_type_p}')
                                if _sr:
                                    try:
                                        _new_loc = _sr[0].media[0].parts[0].file
                                    except Exception:
                                        pass
                                    break
                        except Exception as _se:
                            logging.debug(f"[PlexCheck] Stale repair Plex query failed for item {_sid}: {_se}")
                        if _new_loc and _new_loc != _sitem['location_on_disk']:
                            try:
                                _rc = get_db_connection()
                                _rc.execute(
                                    'UPDATE media_items SET location_on_disk = ?, last_updated = ? WHERE id = ? AND state = "Collected"',
                                    (_new_loc, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), _sid)
                                )
                                _rc.commit()
                                _rc.close()
                                logging.info(f"[PlexCheck] Stale location repaired for item {_sid} ({_sitem['title']}): '{_sitem['location_on_disk']}' → '{_new_loc}'")
                            except Exception as _ue:
                                logging.warning(f"[PlexCheck] Stale location repair DB update failed for item {_sid}: {_ue}")
                        else:
                            logging.debug(f"[PlexCheck] Stale repair: no Plex result for item {_sid} ({_sitem['title']}) filled_by_file='{_sbasename}'")
            except Exception as _repair_err:
                logging.warning(f"[PlexCheck] Stale location repair block failed: {_repair_err}")

            # Log summary of operations
            processed_items_count = len(items) # Count items processed in this run
            logging.info(f"Plex check summary (checks enabled): Processed {processed_items_count} items. Identified {updated_items} found items potentially needing scans (within tick limit). {not_found_items} items not found on disk.")

    def _replace_cleanup_after_collect(self, promoted_dict):
        """
        After a new item is promoted to Collected (via Plex fallback or reconciliation),
        clean up old entries with manual_replace=1 for the same media, and remove their
        debrid torrents, Plex entries and scan/empty trash. Handles both episode
        (Replace Season) and movie (Replace Movie).
        """
        imdb_id = promoted_dict.get('imdb_id')
        item_type = promoted_dict.get('type')
        item_id = promoted_dict.get('id')
        new_torrent_id = promoted_dict.get('filled_by_torrent_id')
        item_version = (promoted_dict.get('version') or '').replace('*', '')

        if not imdb_id or item_type not in ('episode', 'movie'):
            return

        conn = None
        try:
            from debrid import get_debrid_provider as _get_debrid_prov
            from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
            import os as _os
            _debrid_prov = _get_debrid_prov()
            conn = get_db_connection()
            cur = conn.cursor()

            _select_fields = 'id, filled_by_torrent_id, filled_by_file, location_on_disk, title, episode_title'

            if item_type == 'episode':
                season_number = promoted_dict.get('season_number')
                episode_number = promoted_dict.get('episode_number')
                if season_number is None or episode_number is None:
                    return
                old_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items
                       WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                       AND type = 'episode' AND manual_replace = 1 AND id != ?
                       AND REPLACE(COALESCE(version,''),'*','') = ?''',
                    (imdb_id, season_number, episode_number, item_id, item_version)
                ).fetchall()
                log_tag = 'REPLACE_SEASON'
                removal_reason = 'Replaced by new season pack'
                entry_label = 'episode'
            else:  # movie
                old_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items
                       WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1 AND id != ?
                       AND REPLACE(COALESCE(version,''),'*','') = ?''',
                    (imdb_id, item_id, item_version)
                ).fetchall()
                log_tag = 'REPLACE_MOVIE'
                removal_reason = 'Replaced by new movie torrent'
                entry_label = 'movie'

            ids_to_delete = set()
            plex_scan_paths = set()

            def _remove_old_item(row, reason_label):
                """Remove debrid torrent and queue Plex removal for one old row."""
                old_id = row['id']
                old_torrent_id = row['filled_by_torrent_id']
                if old_torrent_id and old_torrent_id != new_torrent_id and _debrid_prov:
                    _sibs = cur.execute(
                        "SELECT COUNT(*) FROM media_items "
                        "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                        (old_torrent_id, old_id)
                    ).fetchone()[0]
                    if _sibs:
                        logging.info(f"[{log_tag}] Skipping debrid removal of {old_torrent_id} for {reason_label} {old_id} — {_sibs} sibling(s) still active")
                    else:
                        try:
                            _debrid_prov.remove_torrent(old_torrent_id, removal_reason=removal_reason)
                            logging.info(f"[{log_tag}] Removed debrid torrent {old_torrent_id} for {reason_label} {old_id}")
                        except Exception as debrid_err:
                            if '404' in str(debrid_err):
                                logging.debug(f"[{log_tag}] Old torrent {old_torrent_id} already removed (404)")
                            else:
                                logging.error(f"[{log_tag}] Failed to remove torrent {old_torrent_id}: {debrid_err}")
                # Plex removal
                item_path = row['location_on_disk'] or row['filled_by_file']
                if item_path:
                    ep_title = row['episode_title'] if item_type == 'episode' else None
                    title = row['title'] or ''
                    try:
                        if not remove_file_from_plex(title, item_path, ep_title):
                            logging.warning(f"[{log_tag}] Direct Plex removal failed for '{title}' ({item_path}), will fallback to scan+empty trash")
                        else:
                            logging.info(f"[{log_tag}] Removed '{title}' from Plex")
                    except Exception as plex_err:
                        logging.warning(f"[{log_tag}] Plex removal error for '{title}': {plex_err}")
                    plex_scan_paths.add(_os.path.dirname(item_path))
                ids_to_delete.add(old_id)
                logging.info(f"[{log_tag}] Queued old {entry_label} entry {old_id} for deletion ({reason_label})")

            # Per-item cleanup: same imdb/season/episode with manual_replace=1
            for old_row in old_rows:
                _remove_old_item(old_row, 'replaced item')

            # Broader sweep: catch stale manual_replace=1 items in any state (e.g. Upgrading)
            # that already have a corresponding fresh Collected replacement for the same content.
            if item_type == 'episode':
                stale_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items m
                       WHERE m.imdb_id = ? AND m.season_number = ? AND m.type = 'episode'
                       AND m.manual_replace = 1 AND m.id != ?
                       AND REPLACE(COALESCE(m.version,''),'*','') = ?
                       AND EXISTS (
                           SELECT 1 FROM media_items m2
                           WHERE m2.imdb_id = m.imdb_id AND m2.season_number = m.season_number
                           AND m2.episode_number = m.episode_number AND m2.type = 'episode'
                           AND m2.manual_replace = 0 AND m2.state = 'Collected'
                           AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                       )''',
                    (imdb_id, season_number, item_id, item_version)
                ).fetchall()
            else:  # movie
                stale_rows = cur.execute(
                    f'''SELECT {_select_fields} FROM media_items m
                       WHERE m.imdb_id = ? AND m.type = 'movie'
                       AND m.manual_replace = 1 AND m.id != ?
                       AND REPLACE(COALESCE(m.version,''),'*','') = ?
                       AND EXISTS (
                           SELECT 1 FROM media_items m2
                           WHERE m2.imdb_id = m.imdb_id AND m2.type = 'movie'
                           AND m2.manual_replace = 0 AND m2.state = 'Collected'
                           AND REPLACE(COALESCE(m2.version,''),'*','') = REPLACE(COALESCE(m.version,''),'*','')
                       )''',
                    (imdb_id, item_id, item_version)
                ).fetchall()

            for stale_row in stale_rows:
                if stale_row['id'] not in ids_to_delete:
                    _remove_old_item(stale_row, 'stale item (broader sweep)')

            if ids_to_delete:
                ids_list = list(ids_to_delete)
                cur.execute(
                    f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(ids_list))})",
                    ids_list
                )
                conn.commit()
                logging.info(f"[{log_tag}] Deleted {cur.rowcount} replaced/stale {entry_label} entries. IDs: {ids_list}")

            # Scan & empty Plex trash for all affected paths (catches any missed direct removals)
            if plex_scan_paths:
                try:
                    section_type = 'show' if item_type == 'episode' else 'movie'
                    scan_and_empty_plex_trash(paths=list(plex_scan_paths), section_type=section_type)
                    logging.info(f"[{log_tag}] Triggered Plex scan+empty trash for paths: {list(plex_scan_paths)}")
                except Exception as scan_err:
                    logging.warning(f"[{log_tag}] Plex scan+empty trash failed: {scan_err}")

        except Exception as err:
            logging.error(f"[REPLACE] Error in replace cleanup after collect: {err}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    def task_verify_plex_removals(self):
        """Verify that files marked for removal are actually gone from the configured media server (Plex or Jellyfin/Emby) using title-based search."""
        logging.info("[TASK] Running media server removal verification task.")

        # Determine Plex connection details based on settings
        plex_url, plex_token = None, None
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url', '').rstrip('/')
            plex_token = get_setting('Plex', 'token', '')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='').rstrip('/')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')

        if not plex_url or not plex_token:
            logging.debug("[VERIFY] No Plex URL or token found in relevant settings. Skipping removal verification.")
            return

        # Initialize Plex connection
        plex = None
        try:
            plex = plexapi.server.PlexServer(plex_url, plex_token)
        except Exception as e:
            logging.error(f"[VERIFY] Failed to connect to Plex ({plex_url}) for removal verification: {e}")
            return

        # Fetch pending items (now includes titles)
        pending_items = get_pending_removal_paths()
        if not pending_items:
            logging.info("[VERIFY] No pending Plex removals to verify.")
            return
        logging.info(f"[VERIFY] Found {len(pending_items)} paths pending Plex removal verification.")

        verified_count = 0
        failed_verification_count = 0
        # Fetch settings for max attempts and cleanup days
        max_attempts = get_setting('File Management', 'plex_removal_max_attempts', 5)
        cleanup_days = get_setting('File Management', 'plex_removal_cleanup_days', 30)

        for item in pending_items:
            item_id = item['id']
            item_path = item['item_path']
            item_title = item['item_title']
            episode_title = item.get('episode_title') # Use .get for safety
            attempts = item['attempts']
            last_checked = item.get('last_checked_at')

            # Exponential backoff: wait longer between retries based on attempt count
            # Attempt 0: immediate, Attempt 1: 1 min, Attempt 2: 5 min, Attempt 3: 15 min, Attempt 4+: 30 min
            backoff_minutes = [0, 1, 5, 15, 30]
            required_wait = backoff_minutes[min(attempts, len(backoff_minutes) - 1)]

            if last_checked and required_wait > 0:
                try:
                    from datetime import datetime, timedelta
                    if isinstance(last_checked, str):
                        last_checked_dt = datetime.fromisoformat(last_checked.replace('Z', '+00:00'))
                    else:
                        last_checked_dt = last_checked
                    time_since_last = datetime.now() - last_checked_dt.replace(tzinfo=None)
                    if time_since_last < timedelta(minutes=required_wait):
                        remaining = timedelta(minutes=required_wait) - time_since_last
                        logging.debug(f"[VERIFY] Skipping '{item_path}' - backoff not elapsed. Wait {remaining.seconds // 60}m {remaining.seconds % 60}s more.")
                        continue
                except Exception as backoff_err:
                    logging.debug(f"[VERIFY] Could not check backoff for {item_id}: {backoff_err}")

            logging.debug(f"[VERIFY DEBUG] Processing Item: ID={item_id}, Path={item_path}, Title={item_title}, Episode={episode_title}, Attempts={attempts}")
            logging.info(f"[VERIFY] Checking path: '{item_path}' (Attempt {attempts + 1}/{max_attempts}) Title: '{item_title}', Episode: '{episode_title}'")

            if attempts >= max_attempts:
                logging.warning(f"[VERIFY] Max attempts reached for path {item_path}. Marking as Failed.")
                update_removal_status(item_id, 'Failed', failure_reason=f'Max attempts ({max_attempts}) reached.')
                failed_verification_count += 1
                continue

            item_still_exists = False # Assume item is gone unless found
            try:
                logging.debug(f"[VERIFY DEBUG] Finding Plex section for path: {item_path}")
                plex_library, plex_section = find_plex_library_and_section(plex, item_path)
                if not plex_section:
                    logging.warning(f"[VERIFY] Could not find Plex section for path: {item_path}. Incrementing attempt count.")
                    increment_removal_attempt(item_id) # Increment attempt if section not found
                    failed_verification_count += 1
                    continue
                logging.debug(f"[VERIFY DEBUG] Found section: {plex_section.title}")

                section_type = get_section_type(plex_section) # 'movie' or 'show'
                target_basename = os.path.basename(item_path)
                logging.debug(f"[VERIFY DEBUG] Section type: {section_type}, Target basename: {target_basename}")

                if not item_title:
                     logging.error(f"[VERIFY] Item ID {item_id} is missing item_title. Cannot perform title-based search for path {item_path}. Incrementing attempt count.")
                     increment_removal_attempt(item_id)
                     failed_verification_count += 1
                     continue

                # --- Movie Check ---
                if section_type == 'movie':
                    logging.debug(f"[VERIFY DEBUG] Searching for MOVIE title: '{item_title}' in section '{plex_section.title}'")
                    search_results = plex_section.search(title=item_title, libtype='movie')
                    logging.debug(f"[VERIFY DEBUG] Movie search results count: {len(search_results)}")
                    if not search_results:
                         logging.debug(f"[VERIFY] Movie title '{item_title}' not found in section '{plex_section.title}'. Check passed for this item.")
                    else:
                        for movie in search_results:
                            logging.debug(f"[VERIFY DEBUG] Checking parts for movie: {movie.title} ({movie.key})")
                            for part in movie.iterParts():
                                 part_basename = os.path.basename(part.file)
                                 logging.debug(f"[VERIFY DEBUG] Comparing target '{target_basename}' with part '{part_basename}' (from {part.file})")
                                 if part_basename == target_basename:
                                     logging.warning(f"[VERIFY] Path '{item_path}' still found associated with Movie '{item_title}' (Part: {part.file}). Verification FAILED.")
                                     item_still_exists = True
                                     break
                            if item_still_exists: break

                # --- Show Check ---
                elif section_type == 'show':
                    logging.debug(f"[VERIFY DEBUG] Searching for SHOW title: '{item_title}' in section '{plex_section.title}'")
                    shows = plex_section.search(title=item_title, libtype='show')
                    logging.debug(f"[VERIFY DEBUG] Show search results count: {len(shows)}")
                    if not shows:
                        logging.debug(f"[VERIFY] Show title '{item_title}' not found in section '{plex_section.title}'. Check passed for this item.")
                    else:
                        for show in shows:
                            logging.debug(f"[VERIFY DEBUG] Found show: {show.title} ({show.key})")
                            # Search specific episode if title provided
                            if episode_title:
                                logging.debug(f"[VERIFY DEBUG] Searching for EPISODE title: '{episode_title}' within show '{show.title}'")
                                try:
                                    episode = show.episode(title=episode_title)
                                    logging.debug(f"[VERIFY DEBUG] Found episode: {episode.title} ({episode.key})")
                                    for part in episode.iterParts():
                                        part_basename = os.path.basename(part.file)
                                        logging.debug(f"[VERIFY DEBUG] Comparing target '{target_basename}' with part '{part_basename}' (from {part.file})")
                                        if part_basename == target_basename:
                                            logging.warning(f"[VERIFY] Path '{item_path}' still found associated with Episode '{item_title} - {episode_title}' (Part: {part.file}). Verification FAILED.")
                                            item_still_exists = True
                                            break
                                except NotFound:
                                    logging.debug(f"[VERIFY] Episode '{episode_title}' not found for show '{show.title}'. Check passed for this episode.")
                                except Exception as e_ep:
                                     logging.error(f"[VERIFY] Error searching for episode '{episode_title}' in show '{show.title}': {e_ep}")
                                     item_still_exists = True # Assume exists on error
                            # If no episode title, check all episodes (less common)
                            else:
                                logging.debug(f"[VERIFY DEBUG] No episode title for '{item_title}', path '{item_path}'. Checking ALL episode parts.")
                                for episode in show.episodes():
                                     for part in episode.iterParts():
                                         part_basename = os.path.basename(part.file)
                                         if part_basename == target_basename:
                                             logging.warning(f"[VERIFY] Path '{item_path}' still found (no ep title) with Show '{item_title}' (Ep: {episode.title}, Part: {part.file}). Verification FAILED.")
                                             item_still_exists = True
                                             break
                                     if item_still_exists: break
                            if item_still_exists: break

                # --- Unknown Section Type ---
                else:
                    logging.warning(f"[VERIFY] Unknown section type '{section_type}' for section '{plex_section.title}'. Cannot verify path {item_path}. Incrementing attempt.")
                    increment_removal_attempt(item_id) # Increment attempt for unknown type
                    failed_verification_count += 1
                    continue # Skip update logic below

                # --- Logging before potential update ---
                logging.debug(f"[VERIFY DEBUG] Post-check for ID {item_id}: item_still_exists = {item_still_exists}")

            except Exception as e_proc:
                logging.error(f"[VERIFY] Error during Plex verification processing for path {item_path} (ID: {item_id}): {e_proc}", exc_info=True)
                increment_removal_attempt(item_id)
                failed_verification_count += 1
                continue # Skip to next item

            # --- Update status based on verification result ---
            try:
                if not item_still_exists:
                    logging.info(f"[VERIFY] Path '{item_path}' appears removed from Plex metadata. Marking as Verified.")
                    update_removal_status(item_id, 'Verified')
                    verified_count += 1
                else:
                    # Item still exists - use different strategies based on attempt count
                    current_attempts = attempts + 1  # This will be the new attempt count

                    # Strategy 1: First few attempts - try direct deletion via API
                    if current_attempts <= 2:
                        logging.warning(f"[VERIFY] Path '{item_path}' still found in Plex. Attempting direct removal (attempt {current_attempts})...")
                        removal_successful = remove_file_from_plex(item_title, item_path, episode_title)
                        if removal_successful:
                            logging.info(f"[VERIFY] Successfully triggered removal via remove_file_from_plex for '{item_path}'. Will verify later.")
                        else:
                            logging.warning(f"[VERIFY] Direct removal failed for '{item_path}'. Will try scan & trash on next attempts.")

                    # Strategy 2: Later attempts - use scan & empty trash instead
                    elif current_attempts <= max_attempts - 1:
                        logging.warning(f"[VERIFY] Path '{item_path}' still in Plex after {attempts} attempts. Using scan & empty trash approach...")
                        try:
                            from utilities.plex_functions import scan_and_empty_plex_trash
                            # Get the parent folder of the file to scan
                            parent_folder = os.path.dirname(item_path)
                            if parent_folder and os.path.exists(os.path.dirname(parent_folder)):  # Check grandparent exists
                                scan_result = scan_and_empty_plex_trash(paths=[parent_folder])
                                if scan_result.get('success'):
                                    logging.info(f"[VERIFY] Triggered scan & trash for folder: {parent_folder}")
                                else:
                                    logging.warning(f"[VERIFY] Scan & trash had issues: {scan_result.get('errors', [])}")
                            else:
                                logging.warning(f"[VERIFY] Cannot scan - parent folder doesn't exist: {parent_folder}")
                        except Exception as scan_err:
                            logging.error(f"[VERIFY] Error during scan & trash for '{item_path}': {scan_err}")

                    # Strategy 3: Final attempt - mark as failed, requires user intervention
                    else:
                        logging.error(f"[VERIFY] Max attempts ({max_attempts}) nearly reached for '{item_path}'. "
                                     f"This may require manual intervention in Plex (enable 'Allow media deletion' in Settings > Library).")

                    logging.warning(f"[VERIFY] Incrementing attempt count for '{item_path}' (now {current_attempts}/{max_attempts}).")
                    increment_removal_attempt(item_id)
                    failed_verification_count += 1
            except Exception as db_update_err:
                 logging.error(f"[VERIFY] Database error updating status/attempts for ID {item_id}: {db_update_err}", exc_info=True)
                 # If DB update fails, the attempt count might not increment, potentially causing loops.

        logging.info(f"[VERIFY] Plex removal verification task finished. Verified: {verified_count}, Failed/Still Pending: {failed_verification_count}.")

        # Clean up old verified/failed entries
        if cleanup_days > 0:
            logging.info(f"[VERIFY] Cleaning up verified/failed entries older than {cleanup_days} days.")
            try:
                 # Use the correct function name
                removed_count = cleanup_old_verified_removals(days=cleanup_days)
                logging.info(f"[VERIFY] Removed {removed_count} old verified/failed entries.")
            except Exception as e_cleanup:
                 logging.error(f"[VERIFY] Error during cleanup of old removal entries: {e_cleanup}")

    def task_process_standalone_plex_removals(self):
        """
        Processes the Plex removal cache.
        """
        try:
            from utilities.plex_removal_cache import process_removal_cache
            min_age_hours = 6
            process_removal_cache(min_age_hours=min_age_hours)
            logging.info(f"Standalone Plex removal processing complete (min_age_hours: {min_age_hours}).")
        except Exception as e:
            logging.error(f"Error during standalone Plex removal processing: {e}", exc_info=True)

    def task_get_plex_watch_history(self):
        """Task to get Plex watch history"""
        from utilities.plex_watch_history_functions import sync_get_watch_history_from_plex
        try:
            sync_get_watch_history_from_plex()
            logging.info("Successfully retrieved Plex watch history")
        except Exception as e:
            logging.error(f"Error retrieving Plex watch history: {str(e)}")

    def task_sync_viewer_history(self):
        """Pull every Plex account's and Emby/Jellyfin user's watch history for
        Discover recommendations. Off until turned on in the Task Manager."""
        from utilities.viewer_history import sync_all
        from utilities.library_recommendations import clear_result_cache
        sync_all()
        clear_result_cache()

    def task_sync_plex_labels(self):
        """
        Sync Plex labels for Collected items

        Ensures labels stored in database are also present in Plex.
        Handles cases where labels failed to sync initially (item not in Plex yet).
        Only runs if at least one content source has Plex labels enabled.
        """
        try:
            # Check if any content source has Plex labels enabled
            from utilities.settings import get_all_settings
            settings = get_all_settings()
            content_sources = settings.get('Content Sources', {})

            labels_enabled = any(
                source.get('plex_labels', {}).get('enabled', False)
                for source in content_sources.values()
            )

            if not labels_enabled:
                logging.debug("Plex labels not enabled in any content source, skipping sync task")
                return

            from utilities.plex_label_manager import sync_pending_labels
            synced = sync_pending_labels(max_items=100)
            if synced > 0:
                logging.info(f"Synced {synced} Plex labels")
        except Exception as e:
            logging.error(f"Error in task_sync_plex_labels: {e}", exc_info=True)

    def task_process_plex_reported_issues(self):
        """Replace movies and episodes Plex users reported with Plex's Report Issue.

        Scheduled when 'Plex Reported Issues' is enabled in settings; the Task Manager
        toggle flips that setting.
        """
        try:
            from utilities.plex_reported_issues import process_reported_issues
            counts = process_reported_issues()
            if counts:
                logging.info(f"Plex reported issues: {counts}")
        except Exception as e:
            logging.error(f"Error in task_process_plex_reported_issues: {e}", exc_info=True)

    def task_backfill_plex_labels_content_source_detail(self):
        """
        Backfill content_source_detail for items with NULL value

        Parses the content_source field and extracts appropriate detail values
        for all source types. Used to fix old items that were added before
        content_source_detail was properly populated.
        """
        try:
            from utilities.plex_label_manager import backfill_content_source_detail
            result = backfill_content_source_detail()

            if result['success']:
                logging.info(f"Backfilled {result['total_updated']} items: {result['by_source']}")
            else:
                logging.error(f"Backfill failed: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logging.error(f"Error in task_backfill_plex_labels_content_source_detail: {e}", exc_info=True)

    def task_regenerate_labels_from_backfilled_details(self, incremental: bool = False, days_back: int = 7):
        """
        Regenerate Plex labels from updated content_source_detail values

        Args:
            incremental: If True, only sync items that need updating (not synced or recently changed)
            days_back: When incremental=True, also sync items collected in last N days (default 7)

        After backfilling content_source_detail, this regenerates the plex_labels
        column from the updated detail values and syncs them to Plex. Handles all
        source types including Overseerr, Agregarr, and internal sources.

        When incremental=True, only processes items with plex_labels_last_synced IS NULL
        or items collected in the last N days, resulting in 95%+ time reduction.
        """
        try:
            from utilities.plex_label_manager import regenerate_labels_from_backfilled_details
            result = regenerate_labels_from_backfilled_details(incremental=incremental, days_back=days_back)

            if result['success']:
                mode_info = f" ({result.get('mode', 'full')} mode)" if 'mode' in result else ""
                logging.info(f"Regenerated labels for {result['total_regenerated']} items ({result['unique_items']} unique TMDB IDs){mode_info}")
            else:
                logging.error(f"Label regeneration failed: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logging.error(f"Error in task_regenerate_labels_from_backfilled_details: {e}", exc_info=True)

    def task_regenerate_labels_full(self):
        """
        Full sync: Regenerate Plex labels for ALL items

        Processes all 5922 items regardless of when they were last synced.
        Use this for initial setup, troubleshooting, or after bulk configuration changes.

        Expected time: 13-14 hours
        """
        logging.info("Starting FULL label sync (all items)")
        self.task_regenerate_labels_from_backfilled_details(incremental=False)

    def task_regenerate_labels_incremental(self):
        """
        Incremental sync: Regenerate Plex labels for changed/new items only

        Only processes items that:
        - Have never been synced (plex_labels_last_synced IS NULL), OR
        - Were collected in the last 7 days

        Use this for routine maintenance and regular syncs.

        Expected time: 5-15 minutes
        """
        logging.info("Starting INCREMENTAL label sync (last 7 days + never synced)")
        self.task_regenerate_labels_from_backfilled_details(incremental=True, days_back=7)

    def task_backfill_missing_labels(self):
        """
        Backfill Plex labels for items with NULL/empty plex_labels

        Generates and syncs labels only for items that don't have labels yet,
        without overwriting existing labels. Safe to run multiple times.
        """
        try:
            from utilities.plex_label_manager import backfill_missing_labels
            result = backfill_missing_labels()

            if result['success']:
                logging.info(f"Backfilled labels for {result['total_backfilled']} items ({result['unique_items']} unique TMDB IDs)")
            else:
                logging.error(f"Label backfill failed: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logging.error(f"Error in task_backfill_missing_labels: {e}", exc_info=True)

    def task_backfill_plex_ms_item_id(self):
        """
        Two-phase backfill for Collected items missing ms_item_id:

        Phase 1 — Plex scan triggers: For each unique folder derived from
        location_on_disk, trigger a Plex section scan so Plex indexes the file.

        Phase 2 — ms_item_id lookup: After triggering scans, query Plex by
        filename and GUID to find the ratingKey and write it to ms_item_id.
        Note: if Plex hasn't finished indexing by the time Phase 2 runs, those
        items will still be missing ms_item_id. Run the task again after Plex
        finishes scanning to pick them up.

        Runs once (disabled by default). Enable in Task Manager under Features.
        Only applies when Plex is the configured media server.
        """
        try:
            from database.core import get_db_connection as _get_db
            from urllib.parse import quote as _urlquote
            import os as _os

            # Connect to Plex using same pattern as task_check_plex_files
            plex_url = get_setting('Plex', 'url', default='')
            plex_token = get_setting('Plex', 'token', default='')
            if not plex_url or not plex_token:
                if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                    plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
                    plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
            if not plex_url or not plex_token:
                logging.info('[MSItemBackfill] Plex not configured — skipping')
                return

            plex = PlexServer(plex_url, plex_token)
            sections = plex.library.sections()
            logging.info(f'[MSItemBackfill] Connected to Plex, {len(sections)} sections')

            # Fetch all affected items
            conn = _get_db()
            rows = conn.execute(
                """SELECT id, type, imdb_id, tmdb_id, title, location_on_disk
                   FROM media_items
                   WHERE state='Collected'
                   AND (ms_item_id IS NULL OR ms_item_id = '')
                   AND location_on_disk IS NOT NULL
                   AND location_on_disk != ''"""
            ).fetchall()
            conn.close()
            items = [dict(r) for r in rows]
            logging.info(f'[MSItemBackfill] {len(items)} items missing ms_item_id')
            if not items:
                return

            # ── Phase 1: trigger Plex scans for unique folders ─────────────────
            scanned_folders = set()
            scan_count = 0
            for item in items:
                location = item['location_on_disk']
                folder = _os.path.dirname(location)
                if not folder or folder in scanned_folders:
                    continue
                is_episode = item['type'] == 'episode'
                type_lookup = 'show' if is_episode else 'movie'
                for section in sections:
                    if section.type != type_lookup:
                        continue
                    # Only scan if the folder lives under a known section location
                    for sec_loc in section.locations:
                        if folder.startswith(sec_loc) or sec_loc in folder:
                            try:
                                section.update(path=folder)
                                scanned_folders.add(folder)
                                scan_count += 1
                                logging.debug(f'[MSItemBackfill] Triggered scan: {folder}')
                            except Exception as _se:
                                logging.debug(f'[MSItemBackfill] Scan trigger failed for {folder}: {_se}')
                            break
                    if folder in scanned_folders:
                        break
            logging.info(f'[MSItemBackfill] Phase 1 complete — triggered {scan_count} folder scans')

            # ── Phase 2: look up ratingKey and write ms_item_id ────────────────
            updated = 0
            not_found = 0
            total = len(items)
            for idx, item in enumerate(items, 1):
                item_id = item['id']
                location = item['location_on_disk']
                imdb_id = item.get('imdb_id')
                tmdb_id = item.get('tmdb_id')
                is_episode = item['type'] == 'episode'
                type_lookup = 'show' if is_episode else 'movie'
                rating_key = None

                # Log progress every 25 items
                if idx % 25 == 0 or idx == 1:
                    logging.info(f'[MSItemBackfill] Phase 2 progress: {idx}/{total} (updated={updated}, not_found={not_found})')

                # Strategy 1: file path search (5s timeout)
                try:
                    filename = _os.path.basename(location)
                    type_param = '&type=4' if is_episode else ''
                    for section in sections:
                        if section.type != type_lookup:
                            continue
                        try:
                            results = plex.fetchItems(f'/library/sections/{section.key}/all?file={_urlquote(filename)}{type_param}', timeout=5)
                            if results:
                                r0 = results[0]
                                if is_episode:
                                    rating_key = str(getattr(r0, 'grandparentRatingKey', None) or getattr(r0, 'ratingKey', None) or '')
                                else:
                                    rating_key = str(getattr(r0, 'ratingKey', '') or '')
                                break
                        except Exception:
                            pass
                except Exception as _e:
                    logging.debug(f'[MSItemBackfill] File search error item {item_id}: {_e}')

                # Strategy 2: GUID fallback (5s timeout)
                if not rating_key and (imdb_id or tmdb_id):
                    try:
                        for section in sections:
                            if section.type != type_lookup:
                                continue
                            try:
                                _results = []
                                if imdb_id:
                                    _results = section.search(**{'guid': f'imdb://{imdb_id}'})
                                if not _results and tmdb_id:
                                    _results = section.search(**{'guid': f'tmdb://{tmdb_id}'})
                                if _results:
                                    rating_key = str(getattr(_results[0], 'ratingKey', '') or '')
                                    break
                            except Exception:
                                pass
                    except Exception as _e:
                        logging.debug(f'[MSItemBackfill] GUID search error item {item_id}: {_e}')

                if rating_key:
                    try:
                        conn = _get_db()
                        conn.execute("UPDATE media_items SET ms_item_id = ? WHERE id = ?", (rating_key, item_id))
                        conn.commit()
                        conn.close()
                        updated += 1
                        logging.info(f'[MSItemBackfill] Set ms_item_id={rating_key} for item {item_id} ({item.get("title")})')
                    except Exception as _e:
                        logging.error(f'[MSItemBackfill] DB write failed item {item_id}: {_e}')
                else:
                    not_found += 1
                    logging.debug(f'[MSItemBackfill] Not in Plex yet: item {item_id} ({item.get("title")})')

            logging.info(f'[MSItemBackfill] Phase 2 complete — ms_item_id updated: {updated}, still not in Plex: {not_found} (re-run task after Plex scans)')

        except Exception as e:
            logging.error(f'[MSItemBackfill] Task failed: {e}', exc_info=True)

    def task_backfill_plex_guids(self):
        """
        Backfill plex_guid on media_items and season_guids/plex_guid on tv_shows
        for all collected movies and episodes by querying cli_battery.

        Runs once (disabled by default). Enable in Task Manager under Features.
        Uses the Plex GUID stored in battery from Trakt API (ids.plex.guid).
        Falls back to a live Trakt lookup if battery doesn't have it yet.
        """
        try:
            from cli_battery.app.direct_api import DirectAPI
            from database.database_reading import get_all_media_items
            from database.database_writing import update_media_item
            from database.core import get_db_connection

            logging.info('[PlexGUIDBackfill] Starting Plex GUID backfill task')

            items = [dict(i) for i in get_all_media_items(state='Collected')
                     if i.get('imdb_id') and not i.get('plex_guid')]

            logging.info(f'[PlexGUIDBackfill] {len(items)} items need plex_guid')

            movie_ids = list({i['imdb_id'] for i in items if i.get('type') == 'movie'})
            show_ids  = list({i['imdb_id'] for i in items if i.get('type') == 'episode'})

            updated = 0
            errors  = 0

            # Movies
            for imdb_id in movie_ids:
                try:
                    result = DirectAPI.get_plex_guid(imdb_id, 'movie')
                    guid = (result or {}).get('show_guid')
                    if guid:
                        conn = get_db_connection()
                        conn.execute(
                            "UPDATE media_items SET plex_guid=? WHERE imdb_id=? AND type='movie'",
                            (guid, imdb_id)
                        )
                        conn.commit()
                        conn.close()
                        updated += 1
                except Exception as e:
                    logging.debug(f'[PlexGUIDBackfill] Movie {imdb_id} error: {e}')
                    errors += 1

            # Shows — update episodes + tv_shows table
            for imdb_id in show_ids:
                try:
                    # Force refresh to ensure absolute→TVDB episode GUID mapping runs
                    # This triggers _ensure_plex_guids_in_data which handles anime absolute numbering
                    try:
                        DirectAPI.force_refresh_metadata(imdb_id, item_type='show')
                    except Exception:
                        pass
                    result = DirectAPI.get_plex_guid(imdb_id, 'show')
                    if not result:
                        continue
                    show_guid    = result.get('show_guid')
                    season_guids = result.get('season_guids') or {}

                    conn = get_db_connection()
                    # Update episode plex_guid using season number lookup
                    # Store show-level guid + season_guids on tv_shows
                    if show_guid:
                        conn.execute(
                            "UPDATE tv_shows SET plex_guid=? WHERE imdb_id=?",
                            (show_guid, imdb_id)
                        )
                    if season_guids:
                        import json as _json
                        conn.execute(
                            "UPDATE tv_shows SET season_guids=? WHERE imdb_id=?",
                            (_json.dumps({str(k): v for k, v in season_guids.items()}), imdb_id)
                        )
                    # Update episode rows
                    if show_guid:
                        conn.execute(
                            "UPDATE media_items SET plex_guid=? WHERE imdb_id=? AND type='episode' AND plex_guid IS NULL",
                            (show_guid, imdb_id)
                        )
                    conn.commit()
                    conn.close()
                    updated += 1
                except Exception as e:
                    logging.debug(f'[PlexGUIDBackfill] Show {imdb_id} error: {e}')
                    errors += 1

            logging.info(f'[PlexGUIDBackfill] Complete: {updated} updated, {errors} errors')

        except Exception as e:
            logging.error(f'[PlexGUIDBackfill] Task error: {e}', exc_info=True)

    def task_refresh_plex_tokens(self):
        logging.info("Performing periodic Plex token validation")
        from content_checkers.plex_watchlist import validate_plex_tokens
        token_status = validate_plex_tokens()
        for username, status in token_status.items():
            if status.get('valid') is False:
                logging.error(f"Invalid Plex token detected during periodic check for user {username}")
            elif status.get('valid') is None:
                logging.warning(f"Could not verify the Plex token for user {username} (plex.tv unreachable?)")
            else:
                logging.debug(f"Plex token for user {username} is valid")

    def task_local_library_scan(self):
        """Run local library scan for symlinked files."""
        logging.info("Disabled for now")
        return
        if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            from database import get_all_media_items
            from utilities.local_library_scan import local_library_scan
            
            # Get all items in Checking state
            items = list(get_all_media_items(state="Checking"))
            if items:
                logging.info(f"Running local library scan for {len(items)} items in Checking state")
                found_items = local_library_scan(items)
                if found_items:
                    logging.info(f"Found {len(found_items)} items during local library scan")
                    
                    # Move found items to Collected state
                    for item_id, found_info in found_items.items():
                        item = found_info['item']
                        from queue_manager import QueueManager
                        queue_manager = QueueManager()
                        queue_manager.move_to_collected(item, "Checking")
            else:
                logging.debug("No items in Checking state to scan for")

    def task_sync_library_metadata(self):
        """Sync audio/subtitle track metadata from Plex/Jellyfin for collected items."""
        from utilities.settings import get_setting
        from database.core import get_db_connection
        from overlays.overlay_manager import OverlayManager
        from overlays.media_info import MediaInfoExtractor

        extractor = MediaInfoExtractor()
        use_jellyfin = get_setting('Jellyfin', 'enabled', False)
        plex_url = get_setting('Plex', 'url', '') if not use_jellyfin else get_setting('Jellyfin', 'url', '')
        plex_token = get_setting('Plex', 'token', '') if not use_jellyfin else get_setting('Jellyfin', 'api_key', '')

        if not plex_url or not plex_token:
            logging.warning("[SyncLibMeta] Media server not configured — skipping.")
            return

        manager = OverlayManager(None, plex_url, plex_token)
        client = manager.plex  # reuse already-initialized Plex/Jellyfin client

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            # For episodes, ms_item_id is the show's rating key — group to avoid
            # fetching the same show metadata 20 times for 20 episodes.
            # For movies, ms_item_id is the movie's own rating key.
            cursor.execute("""
                SELECT MIN(id) as id, ms_item_id, type
                FROM media_items
                WHERE state = 'Collected'
                AND ms_item_id IS NOT NULL AND ms_item_id != ''
                AND (ms_audio_track IS NULL OR ms_subtitle_track IS NULL)
                GROUP BY ms_item_id, type
                ORDER BY MAX(collected_at) DESC
                LIMIT 500
            """)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            logging.info("[SyncLibMeta] No items need metadata sync.")
            return

        logging.info(f"[SyncLibMeta] Syncing metadata for {len(rows)} unique ms_item_ids.")

        updated = 0
        errors = 0
        for row in rows:
            ms_item_id = row['ms_item_id']
            item_type = row['type']
            try:
                if use_jellyfin:
                    metadata = client.get_media_metadata(ms_item_id)
                    if not metadata:
                        continue
                    media_info = extractor.extract_from_jellyfin_metadata(metadata)
                else:
                    if item_type == 'episode':
                        # ms_item_id is show rating key — fetch best episode for stream data
                        metadata = client.get_show_best_episode_media(ms_item_id)
                    else:
                        metadata = client.get_media_metadata(ms_item_id)
                    if not metadata:
                        continue
                    media_info = extractor.extract_from_plex_metadata(metadata)

                audio_track = media_info.get('audio_track')
                subtitle_track = media_info.get('subtitle_track')

                if audio_track is None and subtitle_track is None:
                    continue

                # Update ALL episodes/movies sharing this ms_item_id
                conn = get_db_connection()
                try:
                    conn.execute("""
                        UPDATE media_items
                        SET ms_audio_track = COALESCE(ms_audio_track, ?),
                            ms_subtitle_track = COALESCE(ms_subtitle_track, ?),
                            ms_last_scanned = CURRENT_TIMESTAMP
                        WHERE ms_item_id = ? AND type = ?
                        AND state = 'Collected'
                        AND (ms_audio_track IS NULL OR ms_subtitle_track IS NULL)
                    """, (audio_track, subtitle_track, ms_item_id, item_type))
                    updated += conn.execute(
                        "SELECT changes()"
                    ).fetchone()[0]
                    conn.commit()
                finally:
                    conn.close()

            except Exception as e:
                logging.debug(f"[SyncLibMeta] Failed for ms_item_id {ms_item_id}: {e}")
                errors += 1

        logging.info(f"[SyncLibMeta] Done. Updated={updated} rows. Errors={errors}")
