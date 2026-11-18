"""Single-item rescrape and delete."""

from flask import jsonify, request
import sqlite3
import logging
from sqlalchemy import text, inspect
from routes.extensions import db
from utilities.settings import get_setting
from ..models import admin_required, user_required
from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
from database.database_reading import get_media_item_by_id
import os
from datetime import datetime
from time import sleep
from database.database_writing import RESET_COLLECTION_STATE_SQL
from ._common import database_bp, queue_plex_removal_for_item, sync_queues_after_rescrape, translate_plex_path_to_local

def rescrape_single_item(item_id):
    """Reset a single media_items row back to Wanted, cleaning up its file/Plex/
    cli_mount references first. Single-item counterpart to bulk_queue_action's
    'rescrape' action (see that function for the batched/CASE-SQL version) —
    kept in sync with the same side effects and safety checks:
      - Plex/symlink removal is only queued for Collected/Upgrading items
      - cli_mount torrent/NZB removal is skipped if another item still uses
        the same filled_by_torrent_id in an active state
      - version's '*' (primary-version marker) is stripped on rescrape

    Returns {'success': True} on success, or {'success': False, 'error': str}.
    Does not raise for expected failure modes (item not found, db locked).
    """
    from database import get_db_connection

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, state, location_on_disk, original_path_for_symlink,
                      filled_by_file, title, type, episode_title, version,
                      original_scraped_torrent_title, filled_by_torrent_id, filled_by_magnet
               FROM media_items WHERE id = ?""",
            (item_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {'success': False, 'error': f'Item {item_id} not found'}
        columns = [c[0] for c in cursor.description]
        item_db_data = dict(zip(columns, row))

        file_management = get_setting('File Management', 'file_collection_management', 'Plex')

        if item_db_data['state'] in ('Collected', 'Upgrading'):
            queue_plex_removal_for_item(item_db_data, file_management, item_id)
            if item_db_data.get('location_on_disk') or item_db_data.get('original_path_for_symlink'):
                sleep(0.5)

        current_version_val = item_db_data.get('version')
        cleaned_version_val = current_version_val
        if current_version_val is None or isinstance(current_version_val, str):
            # Never '' (a '**' row): that scraped with no version filters (#576).
            from utilities.reverse_parser import resolve_scraping_version
            cleaned_version_val = resolve_scraping_version(current_version_val)

        # cli_mount torrent/NZB removal — skip if another item still actively uses it
        if item_db_data['state'] in ('Collected', 'Upgrading', 'Checking'):
            tid = item_db_data.get('filled_by_torrent_id') or ''
            if tid:
                try:
                    from usenet import get_usenet_client as _get_dc
                    _dc = _get_dc()
                    if _dc and _dc.is_enabled():
                        still_used = cursor.execute(
                            """SELECT 1 FROM media_items
                               WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking')
                               AND id != ?""",
                            (tid, item_id)
                        ).fetchone()
                        if still_used:
                            logging.info(f"Rescrape: Skipping cli_mount removal for {tid!r} — still used by another item")
                        elif tid.startswith('nzb:'):
                            nzb_hash = tid[4:]
                            if nzb_hash:
                                _dc.remove_nzb(nzb_hash)
                                logging.info(f"Rescrape: Removed NZB {nzb_hash!r} from cli_mount")
                        else:
                            magnet = item_db_data.get('filled_by_magnet') or ''
                            import re as _re
                            m = _re.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re.IGNORECASE)
                            infohash = m.group(1).lower() if m else ''
                            if infohash:
                                _dc.remove_nzb(infohash)
                                logging.info(f"Rescrape: Removed debrid torrent {infohash!r} (RD id={tid!r}) from cli_mount")
                            else:
                                logging.warning(f"Rescrape: Cannot remove {tid!r} from cli_mount — no infohash in magnet link")
                except Exception as cm_err:
                    logging.warning(f"Rescrape: cli_mount removal failed for item {item_id}: {cm_err}")

        cursor.execute(
            f"""UPDATE media_items
               SET state = 'Wanted',
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
                   rescrape_original_torrent_title = ?,
                   original_scraped_torrent_title = NULL,
                   upgrading_from = NULL,
                   upgrading = NULL,
                   version = ?,
                   fall_back_to_single_scraper = 0,
                   last_updated = ?
               WHERE id = ?""",
            (item_db_data.get('original_scraped_torrent_title'), cleaned_version_val, datetime.now(), item_id)
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return {'success': False, 'error': f'Expected to update 1 row for item {item_id}, DB reported {cursor.rowcount}'}

        conn.commit()
        logging.info(f"Rescrape: Successfully reset item {item_id} to Wanted.")
        sync_queues_after_rescrape([item_id])
        return {'success': True}

    except sqlite3.OperationalError as e:
        if conn:
            conn.rollback()
        if "database is locked" in str(e):
            logging.error(f"Database is locked during rescrape of item {item_id}.")
            return {'success': False, 'error': 'database is locked', 'database_locked': True}
        logging.error(f"Operational error rescraping item {item_id}: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}
    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"Error rescraping item {item_id}: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()

def _find_rescrape_candidates(ms_item_id, item_type, season_number=None, episode_number=None, filename=None):
    """Resolve an external client's (ms_item_id, type[, season/episode], filename)
    tuple down to matching media_items rows. ms_item_id alone cannot uniquely
    identify a single version/file (see column comment in schema_management.py —
    it's shared across every version of a movie, and for episodes it's the
    *show's* ratingKey/ItemId, not the episode's), so this narrows by
    ms_item_id + type [+ season/episode for episodes], then disambiguates by
    filename (matched against location_basename, falling back to filled_by_file)
    when more than one candidate remains.

    Returns a list of dicts (each: id, title, version, state, filename/basename)
    — empty if no match, one item if unambiguous, multiple if the caller needs
    to prompt the user (e.g. multiple versions with no filename to disambiguate,
    or filename didn't narrow it down).
    """
    from database import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        query = """SELECT id, title, type, version, state, season_number, episode_number,
                          location_basename, filled_by_file
                   FROM media_items
                   WHERE ms_item_id = ? AND type = ?"""
        params = [ms_item_id, item_type]
        if item_type == 'episode':
            query += " AND season_number = ? AND episode_number = ?"
            params += [season_number, episode_number]
        cursor.execute(query, params)
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()]

        if len(rows) <= 1 or not filename:
            return rows

        wanted_basename = os.path.basename(filename)
        by_location = [r for r in rows if r.get('location_basename') == wanted_basename]
        if by_location:
            return by_location

        by_filled = [r for r in rows if r.get('filled_by_file') and os.path.basename(r['filled_by_file']) == wanted_basename]
        if by_filled:
            return by_filled

        return rows
    finally:
        conn.close()

@database_bp.route('/rescrape_item', methods=['POST'])
@user_required
def rescrape_item():
    """Move a single Collected/Upgrading item back to Wanted for re-request,
    for use by external clients (e.g. the Plezy companion app) that only know
    a media-server item id (Plex ratingKey / Jellyfin item id) and, optionally,
    the on-disk filename of the specific version they want re-requested.

    Request JSON:
      item_id: int            — optional; if given, skips lookup and rescrapes
                                 this media_items row directly (e.g. the row the
                                 client already resolved via a version picker).
      ms_item_id: str          — Plex ratingKey or Jellyfin item id.
      type: 'movie'|'episode'
      season_number, episode_number: int — required when type == 'episode'.
      filename: str            — basename of the specific file/version to
                                  disambiguate when ms_item_id matches more
                                  than one version row.

    Either item_id, or (ms_item_id + type [+ season/episode]), is required.

    Response:
      200 {"success": true} on success.
      200 {"success": false, "error": "ambiguous", "candidates": [...]} when
          more than one version matches and filename didn't disambiguate —
          the client should prompt the user and retry with item_id set.
      404 {"success": false, "error": "..."} when nothing matches.
      400 {"success": false, "error": "..."} on a malformed request.
    """
    data = request.get_json(silent=True) or {}

    item_id = data.get('item_id')
    if item_id:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'item_id must be an integer'}), 400
        result = rescrape_single_item(item_id)
        status = 200 if result.get('success') else (503 if result.get('database_locked') else 400)
        return jsonify(result), status

    ms_item_id = data.get('ms_item_id')
    item_type = data.get('type')
    if not ms_item_id or item_type not in ('movie', 'episode'):
        return jsonify({'success': False, 'error': 'ms_item_id and type (movie|episode) are required when item_id is not provided'}), 400

    season_number = data.get('season_number')
    episode_number = data.get('episode_number')
    if item_type == 'episode' and (season_number is None or episode_number is None):
        return jsonify({'success': False, 'error': 'season_number and episode_number are required when type is episode'}), 400

    filename = data.get('filename')

    candidates = _find_rescrape_candidates(ms_item_id, item_type, season_number, episode_number, filename)

    if not candidates:
        return jsonify({'success': False, 'error': 'No matching item found for the given ms_item_id'}), 404

    if len(candidates) > 1:
        return jsonify({
            'success': False,
            'error': 'ambiguous',
            'message': 'Multiple versions match — retry with item_id set to the chosen candidate',
            'candidates': candidates,
        }), 200

    result = rescrape_single_item(candidates[0]['id'])
    status = 200 if result.get('success') else (503 if result.get('database_locked') else 400)
    return jsonify(result), status

@database_bp.route('/delete_item', methods=['POST'])
@admin_required
def delete_item():
    data = request.get_json()
    item_id = data.get('item_id')
    blacklist = data.get('blacklist', False)
    
    if not item_id:
        return jsonify({'success': False, 'error': 'No item ID provided'}), 400

    try:
        item = get_media_item_by_id(item_id)
        if not item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404

        # Get file management settings
        file_management = get_setting('File Management', 'file_collection_management', 'Plex')
        mounted_location = get_setting('Plex', 'mounted_file_location', get_setting('File Management', 'original_files_path', ''))
        original_files_path = get_setting('File Management', 'original_files_path', '')
        symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')

        if item['state'] == 'Collected' or item['state'] == 'Upgrading':
            # Check if we're in limited environment mode
            from utilities.set_supervisor_env import is_limited_environment
            limited_env = is_limited_environment()
            
            if file_management == 'Plex':
                if mounted_location and item.get('location_on_disk'):
                    if not limited_env:
                        try:
                            if os.path.exists(item['location_on_disk']):
                                os.remove(item['location_on_disk'])
                                logging.info(f"Delete item: Removed file from disk {item['location_on_disk']} (Plex mode).")
                        except Exception as e:
                            logging.error(f"Error deleting file at {item['location_on_disk']}: {str(e)}")
                    else:
                        logging.info(f"Delete item: Skipped file deletion for {item['location_on_disk']} due to limited environment mode (Plex mode).")

                # Allow time for file system operations to complete
                sleep(1)

                # Immediate Plex removal
                path_to_remove_from_plex = item.get('filled_by_file')
                if path_to_remove_from_plex:
                    try:
                        logging.info(f"Delete item: Attempting immediate Plex removal for {item['title']} ({path_to_remove_from_plex}).")
                        plex_removal_result = remove_file_from_plex(item['title'], path_to_remove_from_plex, item.get('episode_title'))
                        if plex_removal_result:
                            logging.info(f"Delete item: Successfully removed {item['title']} from Plex ({path_to_remove_from_plex}).")
                        else:
                            # Direct removal failed - try scan & empty trash as fallback
                            logging.warning(f"Delete item: Direct Plex removal failed for {item['title']}. Trying scan & empty trash...")
                            try:
                                # Determine section type based on item type
                                section_type = 'movie' if item.get('type') == 'movie' else 'show'
                                scan_paths = [os.path.dirname(path_to_remove_from_plex)] if path_to_remove_from_plex else None
                                scan_and_empty_plex_trash(paths=scan_paths, section_type=section_type)
                                logging.info(f"Delete item: Triggered library scan and trash empty for {item['title']} (section_type={section_type}).")
                            except Exception as scan_err:
                                logging.warning(f"Delete item: Scan & empty trash also failed for {item['title']}: {scan_err}. Item may need manual removal from Plex.")
                    except Exception as e:
                        logging.error(f"Delete item: Error during immediate Plex removal for {item['title']} ({path_to_remove_from_plex}): {str(e)}.")
                else:
                    logging.warning(f"Delete item: No 'filled_by_file' path for item {item_id} ({item['title']}). Skipping Plex removal.")

            elif file_management == 'Symlinked/Local':
                symlink_path_from_db = item.get('location_on_disk')
                original_file_path_to_remove_disk = item.get('original_path_for_symlink')

                # Translate the Plex path to local path for symlink deletion
                # location_on_disk may contain Plex's view of the path (e.g., /movies/...)
                # but the actual symlink is at the local path (e.g., /mnt/symlinked/movies/...)
                symlink_path_to_remove_disk = translate_plex_path_to_local(symlink_path_from_db) if symlink_path_from_db else None

                if symlink_path_to_remove_disk and symlink_path_to_remove_disk != symlink_path_from_db:
                    logging.info(f"Delete item: Translated path '{symlink_path_from_db}' to local path '{symlink_path_to_remove_disk}'")

                # Determine the path Plex uses (keep the original Plex path for API call)
                path_for_plex_api_call = symlink_path_from_db

                if symlink_path_to_remove_disk:
                    # Always remove symlinks (they're just pointers)
                    try:
                        if os.path.exists(symlink_path_to_remove_disk) and os.path.islink(symlink_path_to_remove_disk):
                            os.unlink(symlink_path_to_remove_disk)
                            logging.info(f"Delete item: Removed symlink {symlink_path_to_remove_disk} (Symlinked/Local mode).")
                        elif os.path.islink(symlink_path_to_remove_disk):
                            # Broken symlink - still remove it
                            os.unlink(symlink_path_to_remove_disk)
                            logging.info(f"Delete item: Removed broken symlink {symlink_path_to_remove_disk} (Symlinked/Local mode).")
                        else:
                            logging.warning(f"Delete item: Path {symlink_path_to_remove_disk} is not a symlink or doesn't exist. Skipping symlink removal.")
                    except Exception as e:
                        logging.error(f"Error removing symlink at {symlink_path_to_remove_disk}: {str(e)}")
                
                if original_file_path_to_remove_disk:
                    if not path_for_plex_api_call: # Fallback if symlink path wasn't set
                        path_for_plex_api_call = original_file_path_to_remove_disk
                    # Only delete original files if not in limited environment mode
                    if not limited_env:
                        try:
                            if os.path.exists(original_file_path_to_remove_disk):
                                os.remove(original_file_path_to_remove_disk)
                                logging.info(f"Delete item: Removed original file {original_file_path_to_remove_disk} (Symlinked/Local mode).")
                        except Exception as e:
                            logging.error(f"Error deleting original file at {original_file_path_to_remove_disk}: {str(e)}")
                    else:
                        logging.info(f"Delete item: Skipped original file deletion for {original_file_path_to_remove_disk} due to limited environment mode (Symlinked/Local mode).")

                # Allow time for file system operations to complete
                sleep(1)

                # Immediate Plex removal using the determined path
                if path_for_plex_api_call:
                    try:
                        logging.info(f"Delete item: Attempting immediate Plex removal for {item['title']} using path {path_for_plex_api_call} (Symlinked/Local mode).")
                        plex_removal_result = remove_file_from_plex(item['title'], path_for_plex_api_call, item.get('episode_title'))
                        if plex_removal_result:
                            logging.info(f"Delete item: Successfully removed {item['title']} from Plex ({path_for_plex_api_call}).")
                        else:
                            # Direct removal failed - try scan & empty trash as fallback
                            logging.warning(f"Delete item: Direct Plex removal failed for {item['title']}. Trying scan & empty trash...")
                            try:
                                # Determine section type based on item type
                                section_type = 'movie' if item.get('type') == 'movie' else 'show'
                                scan_paths = [os.path.dirname(path_for_plex_api_call)] if path_for_plex_api_call else None
                                scan_and_empty_plex_trash(paths=scan_paths, section_type=section_type)
                                logging.info(f"Delete item: Triggered library scan and trash empty for {item['title']} (section_type={section_type}).")
                            except Exception as scan_err:
                                logging.warning(f"Delete item: Scan & empty trash also failed for {item['title']}: {scan_err}. Item may need manual removal from Plex.")
                    except Exception as e:
                        logging.error(f"Delete item: Error during immediate Plex removal for {item['title']} ({path_for_plex_api_call}): {str(e)}.")
                else:
                    logging.warning(f"Delete item: No suitable path found for Plex removal for item {item_id} ({item['title']}) (Symlinked/Local mode). Skipping Plex removal.")

        # --- cli_mount removal ---
        # Remove the entry from cli_mount (and RD) if no other live items share the same torrent.
        # Must happen BEFORE DB deletion so we can still check siblings.
        _torrent_id = item.get('filled_by_torrent_id') or ''
        _magnet = item.get('filled_by_magnet') or ''
        if _torrent_id and item.get('state') in ('Collected', 'Upgrading', 'Checking'):
            try:
                import re as _re_dh
                from database import get_db_connection as _gdb_del
                _conn_del = _gdb_del()
                try:
                    _sibs = _conn_del.execute(
                        "SELECT COUNT(*) FROM media_items "
                        "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                        (_torrent_id, item_id)
                    ).fetchone()[0]
                finally:
                    _conn_del.close()
                if _sibs == 0:
                    from usenet import get_usenet_client as _get_dc_del
                    _dc_del = _get_dc_del()
                    if _dc_del and _dc_del.is_enabled():
                        if _torrent_id.startswith('nzb:'):
                            _nzb_hash = _torrent_id[4:]
                            if _nzb_hash:
                                _dc_del.remove_nzb(_nzb_hash)
                                logging.info(f"Delete item: Removed NZB {_nzb_hash!r} from cli_mount for item {item_id}")
                        else:
                            _m = _re_dh.search(r'urn:btih:([0-9a-fA-F]{40})', _magnet, _re_dh.IGNORECASE)
                            _infohash = _m.group(1).lower() if _m else ''
                            if _infohash:
                                _dc_del.remove_nzb(_infohash)
                                logging.info(f"Delete item: Removed debrid torrent {_infohash!r} from cli_mount for item {item_id}")
                else:
                    logging.info(f"Delete item: Skipping cli_mount removal for {_torrent_id!r} — {_sibs} sibling(s) still active")
            except Exception as _cm_del_err:
                logging.warning(f"Delete item: cli_mount removal failed for item {item_id}: {_cm_del_err}")
        # --- End cli_mount removal ---

        # Handle database operation based on blacklist flag
        if blacklist:
            from database import update_media_item_state
            update_media_item_state(item_id, 'Blacklisted')
        else:
            from database import remove_from_media_items
            remove_from_media_items(item_id)

        return jsonify({'success': True})
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            logging.error(f"Database is locked during delete_item for item_id {item_id}.")
            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
        else:
            logging.error(f"Operational error processing delete request for item_id {item_id}: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        logging.error(f"Error processing delete request: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def perform_database_migration():
    # logging.info("Performing database migration...")
    inspector = inspect(db.engine)
    if not inspector.has_table("user"):
        # If the user table doesn't exist, create all tables
        db.create_all()
    else:
        # Check if onboarding_complete column exists
        columns = [c['name'] for c in inspector.get_columns('user')]
        if 'onboarding_complete' not in columns:
            # Add onboarding_complete column
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE user ADD COLUMN onboarding_complete BOOLEAN DEFAULT FALSE"))
                conn.commit()
    
    # Commit the changes
    db.session.commit()
