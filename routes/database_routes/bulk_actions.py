"""Bulk queue actions on selected items."""

from flask import jsonify, request
import sqlite3
import logging
from utilities.settings import get_setting
from ..models import admin_required
from database.database_reading import get_media_item_by_id
import os
from datetime import datetime
from time import sleep
from utilities.local_library_scan import convert_item_to_symlink
from database.database_writing import update_media_item, RESET_COLLECTION_STATE_SQL
from database.symlink_verification import add_symlinked_file_for_verification
from ._common import BATCH_SIZE, database_bp, queue_plex_removal_for_item, sync_queues_after_rescrape
from utilities.paths import safe_realpath

@database_bp.route('/bulk_queue_action', methods=['POST'])
@admin_required
def bulk_queue_action():
    action = request.form.get('action')
    selected_items = request.form.getlist('selected_items')
    from routes.program_operation_routes import get_program_runner # Existing import

    target_queue = request.form.get('target_queue') 
    
    blacklist = False
    if action == 'delete':
        blacklist_str = request.form.get('blacklist', 'false')
        blacklist = blacklist_str.lower() == 'true'

    logging.info(f"Bulk action route called. Action: '{action}', Items: {selected_items[:5]}...")

    if not action or not selected_items:
        logging.warning("Bulk action returning error: Action or selected items missing.")
        return jsonify({'success': False, 'error': 'Action and selected items are required'})

    batch_size = BATCH_SIZE
    total_processed = 0
    error_count = 0
    errors = []

    from database import get_db_connection
    
    program_runner = get_program_runner()
    bulk_action_paused_queue = False # Flag to track if this function paused the queue

    try:
        if program_runner and program_runner.is_running() and hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue) and hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
            logging.info("Attempting to pause program queue for bulk DB action.")
            # Set the pause reason specifically for this bulk action
            program_runner.pause_info = { # Assuming pause_info attribute exists and is used by pause_queue
                "reason_string": "Bulk database operation in progress",
                "error_type": "SYSTEM_MAINTENANCE", 
                "service_name": "Database Bulk Action",
                "status_code": None,
                "retry_count": 0
            }
            program_runner.pause_queue() 
            bulk_action_paused_queue = True
            logging.info("Program queue paused successfully for bulk action.")
        else:
            log_message = "Program runner not found, not running, or missing pause_queue/resume_queue methods. Proceeding without pausing queue."
            if program_runner:
                if not program_runner.is_running():
                    log_message = "Program runner found but not running. Proceeding without pausing queue."
                elif not (hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue)):
                    log_message = "Program runner found and running, but 'pause_queue' method is missing. Proceeding without pausing queue."
                elif not (hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue)):
                    log_message = "Program runner found and running, but 'resume_queue' method is missing. Proceeding without pausing queue."
            logging.info(log_message)


        if action == 'delete':
            logging.info(f"Entering batched 'delete' block for {len(selected_items)} items.")
            valid_ids = []
            for raw_id in selected_items:
                try:
                    valid_ids.append(int(raw_id))
                except (TypeError, ValueError):
                    error_count += 1
                    errors.append(f"Invalid item ID: {raw_id}")

            if valid_ids:
                from utilities.deletion_manager import DeletionManager
                deletion_manager = DeletionManager()
                result = deletion_manager.delete_multiple_items(
                    item_ids=valid_ids,
                    blacklist=blacklist,
                    delete_from_media_server=True,
                    delete_files=True,
                    delete_from_debrid=False,
                    delete_symlinks=True,
                    clear_cache=False,
                    remove_from_content_source=False,
                )
                if result.get('database_locked'):
                    logging.error("Database is locked during bulk delete.")
                    return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                total_processed = result.get('deleted_count', 0)
                error_count += result.get('failed_count', 0)
                errors.extend(result.get('errors', []))

        for i in range(0, len(selected_items), batch_size):
            if action == 'delete':
                break
            batch = selected_items[i:i + batch_size]
            logging.info(f"Processing batch {i//batch_size + 1}. Action: '{action}'")

            if action == 'move' and target_queue:
                logging.info("Entering 'move' block.")
                # Keep existing move functionality
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        [target_queue, datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk move.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'change_version' and target_queue:  # target_queue contains the version in this case
                logging.info("Entering 'change_version' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET version = ?, last_updated = ? WHERE id IN ({placeholders})',
                        [target_queue, datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk change_version.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'assign_tags' and target_queue:  # target_queue contains the tag to assign in this case
                logging.info("Entering 'assign_tags' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    new_tag = target_queue.strip()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'SELECT id, tags, filled_by_torrent_id, filled_by_magnet FROM media_items WHERE id IN ({placeholders})',
                        batch
                    )
                    rows = cursor.fetchall()
                    now = datetime.now()
                    pushed_hashes = []
                    import re as _re_assign
                    for row in rows:
                        existing_tags = [t.strip() for t in (row['tags'] or '').split(',') if t.strip()]
                        if new_tag in existing_tags:
                            continue
                        existing_tags.append(new_tag)
                        updated_tags = ','.join(existing_tags)
                        cursor.execute(
                            'UPDATE media_items SET tags = ?, last_updated = ? WHERE id = ?',
                            [updated_tags, now, row['id']]
                        )
                        total_processed += 1

                        # Resolve this row's cli_mount info_hash (same convention used
                        # elsewhere: nzb: prefix stripped, or infohash parsed from magnet)
                        info_hash = ''
                        torrent_id = str(row['filled_by_torrent_id'] or '')
                        if torrent_id.startswith('nzb:'):
                            info_hash = torrent_id[4:]
                        else:
                            magnet = row['filled_by_magnet'] or ''
                            m = _re_assign.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re_assign.IGNORECASE)
                            if m:
                                info_hash = m.group(1).lower()
                        if info_hash:
                            pushed_hashes.append((info_hash, updated_tags, row['id']))
                    conn.commit()

                    if pushed_hashes:
                        try:
                            from usenet.climount_client import get_climount_client as _get_dc_assign
                            _dc_assign = _get_dc_assign()
                            if _dc_assign and _dc_assign.is_enabled():
                                for _ih, _tags, _row_id in pushed_hashes:
                                    if _dc_assign.push_tags(_ih, _tags):
                                        logging.info(f"[AssignTags] Pushed tags '{_tags}' to cli_mount for {_ih}")
                                        cursor.execute(
                                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                                            [datetime.now(), _row_id]
                                        )
                                        conn.commit()
                                    else:
                                        logging.warning(f"[AssignTags] cli_mount tag push returned false for {_ih} (tags='{_tags}')")
                            else:
                                logging.warning('[AssignTags] cli_mount client disabled/not configured — tags not pushed')
                        except Exception as _push_err:
                            logging.warning(f'[AssignTags] cli_mount tag push error: {_push_err}')
                    else:
                        logging.info('[AssignTags] No pushable info_hash resolved for any updated row — nothing sent to cli_mount')
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk assign_tags.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'update_tags' and target_queue:  # target_queue contains the tag; replaces all existing tags
                logging.info("Entering 'update_tags' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    new_tag = target_queue.strip()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'SELECT id, tags, filled_by_torrent_id, filled_by_magnet FROM media_items WHERE id IN ({placeholders})',
                        batch
                    )
                    rows = cursor.fetchall()
                    now = datetime.now()
                    pushed_hashes = []
                    import re as _re_update
                    for row in rows:
                        cursor.execute(
                            'UPDATE media_items SET tags = ?, last_updated = ? WHERE id = ?',
                            [new_tag, now, row['id']]
                        )
                        total_processed += 1

                        info_hash = ''
                        torrent_id = str(row['filled_by_torrent_id'] or '')
                        if torrent_id.startswith('nzb:'):
                            info_hash = torrent_id[4:]
                        else:
                            magnet = row['filled_by_magnet'] or ''
                            m = _re_update.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re_update.IGNORECASE)
                            if m:
                                info_hash = m.group(1).lower()
                        if info_hash:
                            pushed_hashes.append((info_hash, new_tag, row['id']))
                    conn.commit()

                    if pushed_hashes:
                        try:
                            from usenet.climount_client import get_climount_client as _get_dc_update
                            _dc_update = _get_dc_update()
                            if _dc_update and _dc_update.is_enabled():
                                for _ih, _tags, _row_id in pushed_hashes:
                                    if _dc_update.push_tags(_ih, _tags):
                                        logging.info(f"[UpdateTags] Pushed tags '{_tags}' to cli_mount for {_ih}")
                                        cursor.execute(
                                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                                            [datetime.now(), _row_id]
                                        )
                                        conn.commit()
                                    else:
                                        logging.warning(f"[UpdateTags] cli_mount tag push returned false for {_ih} (tags='{_tags}')")
                            else:
                                logging.warning('[UpdateTags] cli_mount client disabled/not configured — tags not pushed')
                        except Exception as _push_err:
                            logging.warning(f'[UpdateTags] cli_mount tag push error: {_push_err}')
                    else:
                        logging.info('[UpdateTags] No pushable info_hash resolved for any updated row — nothing sent to cli_mount')
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk update_tags.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'remove_tags' and target_queue:  # target_queue contains the tag to remove
                logging.info("Entering 'remove_tags' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    tag_to_remove = target_queue.strip()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'SELECT id, tags, filled_by_torrent_id, filled_by_magnet FROM media_items WHERE id IN ({placeholders})',
                        batch
                    )
                    rows = cursor.fetchall()
                    now = datetime.now()
                    pushed_hashes = []
                    import re as _re_remove
                    for row in rows:
                        existing_tags = [t.strip() for t in (row['tags'] or '').split(',') if t.strip()]
                        if tag_to_remove not in existing_tags:
                            continue
                        remaining_tags = [t for t in existing_tags if t != tag_to_remove]
                        updated_tags = ','.join(remaining_tags)
                        cursor.execute(
                            'UPDATE media_items SET tags = ?, last_updated = ? WHERE id = ?',
                            [updated_tags, now, row['id']]
                        )
                        total_processed += 1

                        info_hash = ''
                        torrent_id = str(row['filled_by_torrent_id'] or '')
                        if torrent_id.startswith('nzb:'):
                            info_hash = torrent_id[4:]
                        else:
                            magnet = row['filled_by_magnet'] or ''
                            m = _re_remove.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re_remove.IGNORECASE)
                            if m:
                                info_hash = m.group(1).lower()
                        if info_hash:
                            pushed_hashes.append((info_hash, tag_to_remove, row['id']))
                    conn.commit()

                    if pushed_hashes:
                        try:
                            from usenet.climount_client import get_climount_client as _get_dc_remove
                            _dc_remove = _get_dc_remove()
                            if _dc_remove and _dc_remove.is_enabled():
                                for _ih, _tag, _row_id in pushed_hashes:
                                    if _dc_remove.remove_tags(_ih, _tag):
                                        logging.info(f"[RemoveTags] Removed tag '{_tag}' from cli_mount for {_ih}")
                                        cursor.execute(
                                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                                            [datetime.now(), _row_id]
                                        )
                                        conn.commit()
                                    else:
                                        logging.warning(f"[RemoveTags] cli_mount tag removal returned false for {_ih} (tag='{_tag}')")
                            else:
                                logging.warning('[RemoveTags] cli_mount client disabled/not configured — tag not removed remotely')
                        except Exception as _push_err:
                            logging.warning(f'[RemoveTags] cli_mount tag removal error: {_push_err}')
                    else:
                        logging.info('[RemoveTags] No pushable info_hash resolved for any updated row — nothing sent to cli_mount')
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk remove_tags.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'early_release':
                logging.info("Entering 'early_release' block.")
                # Handle early release action
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET early_release = TRUE, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        ['Wanted', datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk early_release.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'rescrape':
                logging.info(f"Entering 'rescrape' block for batch: {batch}") # batch is a list of item IDs for this BATCH_SIZE chunk
                # Get file management settings (once per BATCH_SIZE chunk)
                file_management = get_setting('File Management', 'file_collection_management', 'Plex')
                mounted_location = get_setting('Plex', 'mounted_file_location', get_setting('File Management', 'original_files_path', ''))
                original_files_path = get_setting('File Management', 'original_files_path', '')
                symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')

                items_in_batch_details_raw = [] # To store raw data fetched from DB for this batch of IDs
                
                conn_rescape_batch = None 
                try:
                    from database import get_db_connection 
                    conn_rescape_batch = get_db_connection()
                    cursor_rescape_batch = conn_rescape_batch.cursor()

                    placeholders_select = ','.join('?' * len(batch)) # 'batch' here is the current chunk of item IDs
                    query_select = f"""
                        SELECT id, state, location_on_disk, original_path_for_symlink,
                               filled_by_file, title, type, episode_title, version, original_scraped_torrent_title,
                               filled_by_torrent_id, filled_by_magnet
                        FROM media_items
                        WHERE id IN ({placeholders_select})
                    """
                    cursor_rescape_batch.execute(query_select, batch)
                    db_columns = [column[0] for column in cursor_rescape_batch.description]
                    items_in_batch_details_raw = [dict(zip(db_columns, row)) for row in cursor_rescape_batch.fetchall()]
                
                except Exception as e:
                    logging.error(f"Error fetching batch details for rescrape: {str(e)}", exc_info=True)
                    errors.append(f"Error fetching details for batch {i//batch_size + 1}: {str(e)}")
                    if conn_rescape_batch: conn_rescape_batch.close()
                    continue # Skip to the next BATCH_SIZE chunk of selected_items
                
                prepared_items_for_db_update = [] 

                for item_db_data in items_in_batch_details_raw: 
                    item_id = item_db_data['id']
                    try:
                        logging.info(f"Rescrape: Processing item_id: {item_id} for file/Plex ops. Current state: {item_db_data.get('state')}, Version: {item_db_data.get('version')}")

                        # --- Start: File Deletion & Plex Removal Logic (using item_db_data) ---
                        if item_db_data['state'] in ['Collected', 'Upgrading']:
                            queue_plex_removal_for_item(item_db_data, file_management, item_id)

                        if item_db_data['state'] in ['Collected', 'Upgrading'] and \
                           (item_db_data.get('location_on_disk') or item_db_data.get('original_path_for_symlink')):
                            sleep(0.5)
                        # --- End: File Deletion & Plex Removal Logic ---

                        current_version_val = item_db_data.get('version')

                        cleaned_version_val = current_version_val # Default assignment

                        if current_version_val is None or isinstance(current_version_val, str):
                            # Never '' or None (a '**' row): that scraped with no version filters (#576).
                            from utilities.reverse_parser import resolve_scraping_version
                            cleaned_version_val = resolve_scraping_version(current_version_val)
                        else: # Not a string and not None
                            logging.warning(f"Rescrape Detail: Item ID {item_id} - Version from DB is not a string or None: '{current_version_val}' (type: {type(current_version_val)}). 'cleaned_version_val' currently is '{cleaned_version_val}'. This might cause issues if DB expects a string for version.")
                            # cleaned_version_val will hold the original non-string, non-None value here.

                        prepared_items_for_db_update.append({
                            'id': item_id,
                            'cleaned_version': cleaned_version_val,
                            'current_original_scraped_title': item_db_data.get('original_scraped_torrent_title') # Store for rescrape_original_torrent_title
                        })

                    except Exception as e_indiv_item_proc:
                        error_count += 1
                        error_msg = f"Error during file/Plex processing for item {item_id} (for rescrape): {str(e_indiv_item_proc)}"
                        errors.append(error_msg)
                        logging.error(f"Rescrape: {error_msg}", exc_info=True)

                # --- Remove cli_mount entries for torrents/NZBs no longer needed ---
                # Collect unique torrent_ids from items being rescrapped that are in active states
                batch_id_set = set(int(x) for x in batch)
                torrent_ids_to_check = {}  # torrent_id -> infohash (or '' for NZBs using torrent_id directly)
                for item_db_data in items_in_batch_details_raw:
                    if item_db_data.get('state') not in ('Collected', 'Upgrading', 'Checking'):
                        continue
                    tid = item_db_data.get('filled_by_torrent_id') or ''
                    if not tid:
                        continue
                    if tid not in torrent_ids_to_check:
                        magnet = item_db_data.get('filled_by_magnet') or ''
                        import re as _re
                        m = _re.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re.IGNORECASE)
                        torrent_ids_to_check[tid] = m.group(1).lower() if m else ''

                if torrent_ids_to_check:
                    try:
                        from usenet import get_usenet_client as _get_dc
                        _dc = _get_dc()
                        if _dc and _dc.is_enabled():
                            tid_placeholders = ','.join('?' * len(torrent_ids_to_check))
                            # Items outside this batch that still actively use these torrent IDs
                            survivors = cursor_rescape_batch.execute(
                                f"""SELECT filled_by_torrent_id FROM media_items
                                    WHERE filled_by_torrent_id IN ({tid_placeholders})
                                    AND state IN ('Collected','Upgrading','Checking')
                                    AND id NOT IN ({','.join('?' * len(batch_id_set))})""",
                                list(torrent_ids_to_check.keys()) + list(batch_id_set)
                            ).fetchall()
                            still_used = {r[0] for r in survivors}
                            for tid, infohash in torrent_ids_to_check.items():
                                if tid in still_used:
                                    logging.info(f"Rescrape: Skipping cli_mount removal for {tid!r} — still used by other items")
                                    continue
                                if tid.startswith('nzb:'):
                                    nzb_hash = tid[4:]
                                    if nzb_hash:
                                        _dc.remove_nzb(nzb_hash)
                                        logging.info(f"Rescrape: Removed NZB {nzb_hash!r} from cli_mount")
                                elif infohash:
                                    _dc.remove_nzb(infohash)
                                    logging.info(f"Rescrape: Removed debrid torrent {infohash!r} (RD id={tid!r}) from cli_mount")
                                else:
                                    logging.warning(f"Rescrape: Cannot remove {tid!r} from cli_mount — no infohash in magnet link")
                    except Exception as _cm_err:
                        logging.warning(f"Rescrape: cli_mount removal failed: {_cm_err}")
                # --- End cli_mount removal ---

                if prepared_items_for_db_update: 
                    try:
                        item_ids_for_update_clause = [item['id'] for item in prepared_items_for_db_update]
                        placeholders_for_in_clause = ','.join('?' * len(item_ids_for_update_clause))

                        version_case_sql_parts = []
                        params_for_version_case_values = []
                        for item_update_payload in prepared_items_for_db_update:
                            version_case_sql_parts.append("WHEN ? THEN ?")
                            params_for_version_case_values.extend([item_update_payload['id'], item_update_payload['cleaned_version']])

                        version_case_final_sql = "version"
                        if version_case_sql_parts:
                             version_case_final_sql = "CASE id " + " ".join(version_case_sql_parts) + " ELSE version END"

                        rescrape_title_case_sql_parts = []
                        params_for_rescrape_title_case_values = []
                        for item_update_payload in prepared_items_for_db_update:
                            rescrape_title_case_sql_parts.append("WHEN ? THEN ?")
                            params_for_rescrape_title_case_values.extend([item_update_payload['id'], item_update_payload.get('current_original_scraped_title')])

                        rescrape_title_case_final_sql = "rescrape_original_torrent_title" # Default to existing if no specific update
                        if rescrape_title_case_sql_parts:
                            rescrape_title_case_final_sql = "CASE id " + " ".join(rescrape_title_case_sql_parts) + " ELSE rescrape_original_torrent_title END"

                        # MOVED DEFINITIONS UP
                        final_db_update_query = f"""UPDATE media_items 
                               SET state = 'Wanted', 
                                   blacklisted_date = NULL,
                                   ghostlisted = 0,
                                   sleep_cycles = 0,
                                   wake_count = 0,
                                   location_on_disk = NULL, 
                                   original_path_for_symlink = NULL, 
                                   filled_by_file = NULL,
                                   filled_by_title = NULL,
                                   filled_by_magnet = NULL,
                                   filled_by_torrent_id = NULL,
                                   debrid_folder_name = NULL,
                                   scrape_results = NULL,
                                   nzb_segment_id = NULL,
                                   real_debrid_original_title = NULL,
                                   location_basename = NULL,
                                   {RESET_COLLECTION_STATE_SQL},
                                   rescrape_original_torrent_title = {rescrape_title_case_final_sql},
                                   original_scraped_torrent_title = NULL,
                                   upgrading_from = NULL,
                                   upgrading = NULL,
                                   version = {version_case_final_sql},
                                   fall_back_to_single_scraper = 0,
                                   last_updated = ? 
                               WHERE id IN ({placeholders_for_in_clause})"""

                        sql_params_for_final_db_update = params_for_rescrape_title_case_values + params_for_version_case_values + [datetime.now()] + item_ids_for_update_clause
                        
                        cursor_rescape_batch.execute(final_db_update_query, sql_params_for_final_db_update)
                        rows_affected_by_update = cursor_rescape_batch.rowcount

                        if rows_affected_by_update == len(item_ids_for_update_clause):
                            conn_rescape_batch.commit()
                            total_processed += rows_affected_by_update
                            logging.info(f"Rescrape: Successfully committed DB update for {rows_affected_by_update} items for batch {i//batch_size + 1}.")
                            sync_queues_after_rescrape(item_ids_for_update_clause)
                        else:
                            conn_rescape_batch.rollback()
                            mismatch_error_msg = f"Rescrape DB Update: Expected to affect {len(item_ids_for_update_clause)} items, but DB reported {rows_affected_by_update}. Rolled back changes for this group of items in batch {i//batch_size + 1}."
                            logging.error(mismatch_error_msg)
                            errors.append(mismatch_error_msg)
                            error_count += len(item_ids_for_update_clause) 
                    
                    except sqlite3.OperationalError as e_db_update:
                        if "database is locked" in str(e_db_update):
                            logging.error(f"Database is locked during bulk rescrape update for batch {i//batch_size + 1}.")
                            if conn_rescape_batch: conn_rescape_batch.rollback()
                            # Specific error response for database locked
                            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                        else:
                            if conn_rescape_batch: 
                                try:
                                    conn_rescape_batch.rollback() 
                                except Exception as e_rollback:
                                    logging.error(f"Rescrape: Error during rollback attempt: {e_rollback}", exc_info=True)

                            db_update_err_msg = f"Error during batch database update for rescrape (batch {i//batch_size + 1}): {str(e_db_update)}"
                            errors.append(db_update_err_msg)
                            logging.error(f"Rescrape: {db_update_err_msg}", exc_info=True)
                            error_count += len(prepared_items_for_db_update)
                    except Exception as e_db_update: 
                        if conn_rescape_batch: 
                            try:
                                conn_rescape_batch.rollback() 
                            except Exception as e_rollback:
                                logging.error(f"Rescrape: Error during rollback attempt: {e_rollback}", exc_info=True)

                        db_update_err_msg = f"Error during batch database update for rescrape (batch {i//batch_size + 1}): {str(e_db_update)}"
                        errors.append(db_update_err_msg)
                        logging.error(f"Rescrape: {db_update_err_msg}", exc_info=True)
                        error_count += len(prepared_items_for_db_update) 
                
                elif items_in_batch_details_raw: 
                    logging.info(f"Rescrape: No items from batch {i//batch_size + 1} were successfully prepared for database update (e.g., all had file/Plex processing errors).")

                if conn_rescape_batch:
                    conn_rescape_batch.close()
                # --- End New Rescrape Logic ---
            elif action == 'force_priority':
                logging.info("Entering 'force_priority' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET force_priority = TRUE, last_updated = ? WHERE id IN ({placeholders})',
                        [datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk force_priority.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'ghostlist':
                logging.info("Entering 'ghostlist' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET ghostlisted = TRUE, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        ['Blacklisted', datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk ghostlist.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'resync':
                logging.info("Entering 'resync' block.")
                for item_id in batch:
                    try:
                        item = get_media_item_by_id(item_id)
                        if not item:
                            error_count += 1
                            errors.append(f"Item {item_id} not found")
                            continue

                        old_symlink_path = item.get('location_on_disk')
                        # Determine source file path
                        source_file_path = None
                        if item.get('original_path_for_symlink') and os.path.exists(item['original_path_for_symlink']):
                            source_file_path = item['original_path_for_symlink']
                        elif old_symlink_path and os.path.islink(old_symlink_path):
                            source_file_path = safe_realpath(old_symlink_path)
                        else:
                            # Fallback – use whatever is stored if it exists
                            source_file_path = old_symlink_path if old_symlink_path and os.path.exists(old_symlink_path) else None

                        if not source_file_path or not os.path.exists(source_file_path):
                            error_count += 1
                            errors.append(f"Item {item_id}: source file not found for resync")
                            continue

                        # Prepare a copy for convert_item_to_symlink with correct source
                        item_copy = item.copy()
                        item_copy['location_on_disk'] = source_file_path

                        result = convert_item_to_symlink(item_copy, skip_verification=True)
                        if result.get('success'):
                            # Remove old symlink only if the path has changed
                            if old_symlink_path and result['new_location'] and \
                               os.path.normpath(old_symlink_path) != os.path.normpath(result['new_location']) and \
                               os.path.islink(old_symlink_path):
                                try:
                                    os.unlink(old_symlink_path)
                                except Exception as unlink_err:
                                    logging.warning(f"Failed to remove old symlink for item {item_id}: {unlink_err}")
                            # Update DB to new paths
                            update_media_item(
                                item_id,
                                location_on_disk=result['new_location'],
                                original_path_for_symlink=source_file_path
                            )
                            total_processed += 1
                        else:
                            error_count += 1
                            errors.append(f"Item {item_id}: {result.get('error')}")
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e):
                            logging.error("Database is locked during bulk resync.")
                            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                        error_count += 1
                        errors.append(f"Item {item_id}: {str(e)}")
                    except Exception as e:
                        error_count += 1
                        errors.append(f"Item {item_id}: {str(e)}")
            elif action == 'verify_symlinks':
                logging.info("Entering 'verify_symlinks' block.")
                for item_id in batch:
                    try:
                        item = get_media_item_by_id(item_id)
                        if not item:
                            error_count += 1
                            errors.append(f"Item {item_id} not found")
                            continue

                        # Check if item has a symlink path
                        symlink_path = item.get('location_on_disk')
                        if not symlink_path:
                            error_count += 1
                            errors.append(f"Item {item_id}: No symlink path found")
                            continue

                        # Check if the symlink file exists
                        if not os.path.exists(symlink_path):
                            error_count += 1
                            errors.append(f"Item {item_id}: Symlink file does not exist at {symlink_path}")
                            continue

                        # Add to verification queue
                        success = add_symlinked_file_for_verification(item_id, symlink_path)
                        if success:
                            total_processed += 1
                            logging.info(f"Added item {item_id} to symlink verification queue")
                        else:
                            error_count += 1
                            errors.append(f"Item {item_id}: Failed to add to verification queue")
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e):
                            logging.error("Database is locked during bulk verify_symlinks.")
                            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                        error_count += 1
                        errors.append(f"Item {item_id}: {str(e)}")
                    except Exception as e:
                        error_count += 1
                        errors.append(f"Item {item_id}: {str(e)}")
            else:
                logging.warning(f"Bulk action returning error: Invalid action '{action}'")
                # No need to explicitly resume here, finally block will handle it.
                return jsonify({'success': False, 'error': 'Invalid action or missing target queue'})

        if error_count > 0:
            message = f"Completed with {error_count} errors. Successfully processed {total_processed} items."
            if errors:
                message += f" First few errors: {'; '.join(errors[:3])}"
            return jsonify({'success': True, 'message': message, 'warning': True})
        else:
            action_map = {
                "delete": "deleted",
                "move": f"moved to {target_queue} queue",
                "change_version": f"changed to version {target_queue}",
                "assign_tags": f"tagged with '{target_queue}'",
                "update_tags": f"tags set to '{target_queue}'",
                "remove_tags": f"tag '{target_queue}' removed",
                "early_release": "marked as early release and moved to Wanted queue",
                "rescrape": "deleted files/Plex entries for and moved to Wanted queue", # Added rescrape message
                "force_priority": "marked for forced priority",
                "ghostlist": "ghostlisted and moved to Blacklisted queue",
                "resync": "resynchronized",
                "verify_symlinks": "added to symlink verification queue"
            }
            action_text = action_map.get(action, f"processed ({action})")
            message = f"Successfully {action_text} {total_processed} items"
            return jsonify({'success': True, 'message': message})

    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            logging.error(f"Database is locked during outer try block for bulk action '{action}'.")
            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
        else:
            logging.error(f"Outer operational error in bulk action '{action}': {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': f"An operational error occurred during bulk {action}: {str(e)}"}), 500
    except Exception as e:
        logging.error(f"Outer exception in bulk action '{action}': {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f"An unexpected error occurred during bulk {action}: {str(e)}"}), 500
    finally:
        if bulk_action_paused_queue and program_runner and hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
            logging.info("Resuming program queue in finally block after bulk DB action.")
            program_runner.resume_queue() 
        elif program_runner and not bulk_action_paused_queue:
            logging.info("Queue was not paused by this bulk operation or program_runner not available/suitable for resume.")
