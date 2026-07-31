import logging
from typing import Dict, Any, Optional
from datetime import datetime
import os
import subprocess
from utilities.settings import get_setting


def replace_cleanup_after_collect(item_dict):
    """
    Called after a new item is promoted to Collected state.
    Cleans up old entries with manual_replace=1 for the same media,
    removes their debrid torrents and Plex entries. No-op when no
    manual_replace items exist for the same imdb_id.
    """
    imdb_id = item_dict.get('imdb_id')
    item_type = item_dict.get('type')
    item_id = item_dict.get('id')
    new_torrent_id = item_dict.get('filled_by_torrent_id')
    # Matches SQL's REPLACE(COALESCE(version,''),'*','') exactly.
    item_version = (item_dict.get('version') or '').rstrip('*')

    if not imdb_id or item_type not in ('episode', 'movie'):
        return

    conn = None
    try:
        from database import get_db_connection
        from debrid import get_debrid_provider as _get_debrid_prov
        from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
        import os as _os

        try:
            _debrid_prov = _get_debrid_prov()
        except Exception:
            _debrid_prov = None
        conn = get_db_connection()
        cur = conn.cursor()

        _fields = 'id, filled_by_torrent_id, filled_by_file, location_on_disk, title, episode_title'

        if item_type == 'episode':
            season_number = item_dict.get('season_number')
            episode_number = item_dict.get('episode_number')
            if season_number is None or episode_number is None:
                return
            old_rows = cur.execute(
                f'''SELECT {_fields} FROM media_items
                   WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                   AND type = 'episode' AND manual_replace = 1 AND id != ?
                   AND REPLACE(COALESCE(version,''),'*','') = ?''',
                (imdb_id, season_number, episode_number, item_id, item_version)
            ).fetchall()
            stale_rows = cur.execute(
                f'''SELECT {_fields} FROM media_items m
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
            log_tag = 'REPLACE_SEASON'
            removal_reason = 'Replaced by new season pack'
            entry_label = 'episode'
        else:  # movie
            old_rows = cur.execute(
                f'''SELECT {_fields} FROM media_items
                   WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1 AND id != ?
                   AND REPLACE(COALESCE(version,''),'*','') = ?''',
                (imdb_id, item_id, item_version)
            ).fetchall()
            stale_rows = cur.execute(
                f'''SELECT {_fields} FROM media_items m
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
            log_tag = 'REPLACE_MOVIE'
            removal_reason = 'Replaced by new movie torrent'
            entry_label = 'movie'

        # Merge, deduplicating by id
        all_rows = {row['id']: row for row in list(old_rows) + list(stale_rows)}
        if not all_rows:
            return  # Nothing to clean up

        ids_to_delete = set()
        plex_scan_paths = set()

        for old_row in all_rows.values():
            old_id = old_row['id']
            old_torrent_id = old_row['filled_by_torrent_id']
            if old_torrent_id and old_torrent_id != new_torrent_id and _debrid_prov:
                # Sibling guard: never remove a torrent still referenced by another
                # live media_items row (e.g. a different version sharing this job id
                # due to a since-fixed dedup bug) — that would delete the file out
                # from under the surviving row, leaving it with a broken symlink.
                try:
                    _sib_count = cur.execute(
                        "SELECT COUNT(*) FROM media_items "
                        "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                        (old_torrent_id, old_id)
                    ).fetchone()[0]
                except Exception as sib_err:
                    logging.warning(f"[{log_tag}] Sibling check failed for {old_torrent_id}, skipping removal to be safe: {sib_err}")
                    _sib_count = 1
                if _sib_count > 0:
                    logging.info(f"[{log_tag}] Skipping debrid removal for {old_torrent_id} — still referenced by {_sib_count} other item(s)")
                else:
                    try:
                        _debrid_prov.remove_torrent(old_torrent_id, removal_reason=removal_reason)
                        logging.info(f"[{log_tag}] Removed debrid torrent {old_torrent_id} for old entry {old_id}")
                    except Exception as debrid_err:
                        if '404' in str(debrid_err):
                            logging.debug(f"[{log_tag}] Old torrent {old_torrent_id} already removed (404)")
                        else:
                            logging.error(f"[{log_tag}] Failed to remove torrent {old_torrent_id}: {debrid_err}")
            item_path = old_row['location_on_disk'] or old_row['filled_by_file']
            if item_path:
                ep_title = old_row['episode_title'] if item_type == 'episode' else None
                title = old_row['title'] or ''
                try:
                    if not remove_file_from_plex(title, item_path, ep_title):
                        logging.warning(f"[{log_tag}] Direct Plex removal failed for '{title}' ({item_path}), will fallback to scan+empty trash")
                    else:
                        logging.info(f"[{log_tag}] Removed '{title}' from Plex")
                except Exception as plex_err:
                    logging.warning(f"[{log_tag}] Plex removal error for '{title}': {plex_err}")
                plex_scan_paths.add(_os.path.dirname(item_path))
            ids_to_delete.add(old_id)

        if ids_to_delete:
            ids_list = list(ids_to_delete)
            cur.execute(
                f"DELETE FROM media_items WHERE id IN ({','.join(['?']*len(ids_list))})",
                ids_list
            )
            conn.commit()
            logging.info(f"[{log_tag}] Deleted {cur.rowcount} replaced {entry_label} entries. IDs: {ids_list}")

        if plex_scan_paths:
            try:
                section_type = 'show' if item_type == 'episode' else 'movie'
                scan_and_empty_plex_trash(paths=list(plex_scan_paths), section_type=section_type)
                logging.info(f"[{log_tag}] Triggered Plex scan+empty trash for paths: {list(plex_scan_paths)}")
            except Exception as scan_err:
                logging.warning(f"[{log_tag}] Plex scan+empty trash failed: {scan_err}")

    except Exception as err:
        logging.error(f"[REPLACE] Error in replace cleanup after collect: {err}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def validate_cinesync_path(path: str) -> bool:
    """
    Validate that the CineSync path is properly configured.
    
    Args:
        path (str): Path to validate
        
    Returns:
        bool: True if path is valid, False otherwise
    """
    if not path:
        return False
        
    if not path.endswith('/main.py'):
        logging.warning("CineSync path must end with /main.py")
        return False
        
    if not os.path.isfile(path):
        logging.warning(f"CineSync main.py not found at: {path}")
        return False
        
    return True

def run_cinesync(item: Dict[str, Any]) -> None:
    """
    Run the CineSync MediaHub if configured.
    
    Args:
        item (Dict[str, Any]): The media item that triggered the state change
    """
    cinesync_path = get_setting('Debug', 'cinesync_path', '')
    
    if not validate_cinesync_path(cinesync_path):
        return
        
    try:
        # Build command with arguments
        cmd = ['python', cinesync_path]
        
        # Add file path based on collection management setting as positional argument
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            if item.get('location_on_disk'):
                cmd.append(item['location_on_disk'])
        else:
            if item.get('original_path_for_symlink'):
                cmd.append(item['original_path_for_symlink'])
                
        # Add IMDb ID if present
        if item.get('imdb_id'):
            cmd.extend(['--imdb', item['imdb_id']])
            
        logging.info(f"Running CineSync with args: {' '.join(cmd)}")
            
        # Run CineSync with arguments
        subprocess.Popen(cmd, 
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
    except Exception as e:
        logging.error(f"Failed to start CineSync MediaHub: {str(e)}")

def run_custom_script(item: Dict[str, Any]) -> None:
    """
    Run custom post-processing script if configured.
    
    Args:
        item (Dict[str, Any]): The media item that triggered the state change
    """
    if not get_setting('Custom Post-Processing', 'enable_custom_script', False):
        logging.debug("Custom script disabled or not configured — skipping")
        return

    script_path = get_setting('Custom Post-Processing', 'custom_script_path', '')
    if not script_path:
        logging.warning("Custom script enabled but no script path configured")
        return
    if not os.path.isfile(script_path):
        logging.warning(f"Custom script not found or not accessible inside container at: {script_path} — check volume mounts")
        return
        
    try:
        # Get argument template
        args_template = get_setting('Custom Post-Processing', 'custom_script_args', '{title} {imdb_id}')
        
        # Format arguments with item data
        formatted_args = args_template.format(
            title=item.get('title', ''),
            year=item.get('year', ''),
            type=item.get('type', ''),
            imdb_id=item.get('imdb_id', ''),
            location_on_disk=item.get('location_on_disk', ''),
            original_path_for_symlink=item.get('original_path_for_symlink', ''),
            state=item.get('state', ''),
            version=item.get('version', '')
        )
        
        # Build command
        cmd = [script_path] + formatted_args.split()
        
        logging.info(f"Running custom script with args: {' '.join(cmd)}")
        
        # Run script
        subprocess.Popen(cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
    except Exception as e:
        logging.error(f"Failed to run custom script: {str(e)}")

def _handle_plex_watchlist_removal(item: Dict[str, Any]) -> None:
    """Remove item from My Plex Watchlist or Other Plex Watchlist based on its content_source."""
    if not get_setting('Debug', 'plex_watchlist_removal', False):
        return

    # Respect the keep_series setting: skip episodes/shows if enabled
    keep_series = get_setting('Debug', 'plex_watchlist_keep_series', False)
    if keep_series and item.get('type') in ('episode', 'show'):
        logging.debug(f"[WATCHLIST_REMOVE] Skipping series '{item.get('title')}' (plex_watchlist_keep_series enabled)")
        return

    content_source = item.get('content_source', '')
    if not content_source:
        return

    # Resolve source type from source config (e.g. "My Plex Watchlist_1" → type "My Plex Watchlist")
    from queues.config_manager import load_config
    config = load_config()
    source_config = config.get('Content Sources', {}).get(content_source, {})
    source_type = source_config.get('type', '')

    if source_type == 'My Plex Watchlist':
        from content_checkers.plex_watchlist import remove_from_plex_watchlist_by_item
        result = remove_from_plex_watchlist_by_item(item)
        if result.get('success'):
            logging.info(f"[WATCHLIST_REMOVE] Removed '{item.get('title')}' from My Plex Watchlist")
        elif not result.get('not_found'):
            logging.warning(f"[WATCHLIST_REMOVE] Could not remove '{item.get('title')}' from My Plex Watchlist: {result.get('message')}")

    elif source_type == 'Other Plex Watchlist':
        token = source_config.get('token', '')
        if not token:
            logging.warning(f"[WATCHLIST_REMOVE] No token found for source '{content_source}', cannot remove from watchlist")
            return
        from content_checkers.plex_watchlist import remove_from_other_plex_watchlist_by_item
        result = remove_from_other_plex_watchlist_by_item(item, token)
        if result.get('success'):
            logging.info(f"[WATCHLIST_REMOVE] Removed '{item.get('title')}' from Other Plex Watchlist ({content_source})")
        elif not result.get('not_found'):
            logging.warning(f"[WATCHLIST_REMOVE] Could not remove '{item.get('title')}' from Other Plex Watchlist ({content_source}): {result.get('message')}")


def handle_state_change(item: Dict[str, Any]) -> None:
    """
    Handle any post-processing needed when an item enters a new state.
    Currently handles 'Collected' and 'Upgrading' states.
    
    Args:
        item (Dict[str, Any]): The media item that has entered a new state
    """
    try:
        item_id = item.get('id')
        if not item_id:
            logging.error("No item ID provided for state post-processing")
            return

        state = item.get('state')
        if not state:
            logging.error("No state provided for post-processing")
            return

        #logging.info(f"Running post-processing for {state} state - Item ID: {item_id}")
        
        # Get fresh item data from database to ensure we have latest state
        # Lazy import to avoid circular dependency
        if state == 'Collected' or state == 'Upgrading':
            from database import get_media_item_by_id
            fresh_item = get_media_item_by_id(item_id)
            if not fresh_item:
                logging.error(f"Could not find item {item_id} in database for post-processing")
                return
                
            # Run CineSync for items
            run_cinesync(dict(fresh_item))
            
            # Run subtitle downloader
            try:
                file_path = fresh_item.get('location_on_disk')
                if not file_path:
                    logging.warning("No location_on_disk found for item, skipping subtitle download")
                elif get_setting('File Management', 'file_collection_management') == 'Plex':
                    # Plex mode: media is on a read-only debrid/usenet mount, so a
                    # sidecar .srt can't be written next to it. Download subtitles
                    # and upload them to the Plex item via API (keyed on the stored
                    # ratingKey). Storage-agnostic — works for zurg/nzbdav/climount.
                    rk = fresh_item.get('ms_item_id')
                    if rk:
                        # The on-disk file is often obfuscated (e.g. tGcr.mkv) on
                        # debrid/usenet mounts; the release folder name carries the
                        # real title, so use it as the subtitle-matching hint.
                        import os as _os_sub
                        name_hint = (fresh_item.get('debrid_folder_name')
                                     or fresh_item.get('filled_by_title')
                                     or _os_sub.path.basename(_os_sub.path.dirname(file_path)) or None)
                        logging.info("Running subtitle downloader (Plex API upload mode).")
                        from .downsub import main as downsub_main
                        downsub_main(file_path, rating_key=str(rk), name_hint=name_hint)
                    else:
                        logging.info("Plex-mode subtitle upload skipped: item has no Plex ratingKey (ms_item_id) yet")
                else:
                    logging.info("Running subtitle downloader - this may take some time if it has never been run.")
                    from .downsub import main as downsub_main
                    downsub_main(file_path)
            except Exception as e:
                logging.error(f"Failed to run subtitle downloader: {str(e)}")
                logging.exception("Subtitle downloader traceback:")
                
            # Run custom script if enabled
            run_custom_script(dict(fresh_item))

            # Apply Plex labels based on content source configuration
            try:
                from utilities.plex_label_manager import apply_labels_for_item, is_plex_labels_enabled_anywhere

                # Only process labels if at least one content source has them enabled
                if is_plex_labels_enabled_anywhere():
                    logging.info(f"POST-PROCESSING: About to apply Plex labels for item {fresh_item.get('id')} ({fresh_item.get('title')})")
                    result = apply_labels_for_item(dict(fresh_item))
                    logging.info(f"POST-PROCESSING: Plex labels application returned {result} for item {fresh_item.get('id')}")
                else:
                    logging.debug(f"POST-PROCESSING: Plex labels disabled globally, skipping label application")
            except Exception as e:
                logging.error(f"Failed to apply Plex labels: {str(e)}")
                logging.exception("Plex labels traceback:")

            # Update item with size and resolution info
            # OPTIMIZATION: Skip slow Plex API search (35-40s per episode) during checking queue
            # Use fast filesystem check only. Plex search can be done later if needed.
            try:
                item_id = fresh_item.get('id')
                if not item_id:
                    logging.warning(f"POST-PROCESSING: No item ID available for size/resolution update")
                else:
                    logging.info(f"POST-PROCESSING: About to update size/resolution for item {item_id} ({fresh_item.get('title')})")
                    from utilities.plex_functions import update_item_with_plex_info
                    file_path = fresh_item.get('location_on_disk') or fresh_item.get('filled_by_file')
                    if file_path:
                        logging.info(f"POST-PROCESSING: Calling update_item_with_plex_info for {item_id} with path: {file_path} (skipping slow Plex search)")
                        # skip_plex_search=True prevents 35-40s delay per episode
                        result = update_item_with_plex_info(item_id, file_path, skip_plex_search=True)
                        logging.info(f"POST-PROCESSING: Size/resolution update returned {result} for item {item_id}")
                    else:
                        logging.info(f"POST-PROCESSING: No file path available for item {item_id} to update size/resolution")
            except Exception as e:
                logging.error(f"Failed to update item size/resolution info: {str(e)}")
                logging.exception("Size/resolution update traceback:")

            # Trigger replace cleanup if this item replaced a manual_replace=1 item
            if state in ('Collected', 'Upgrading'):
                try:
                    replace_cleanup_after_collect(dict(fresh_item))
                except Exception as e:
                    logging.error(f"Failed to run replace cleanup after collect: {str(e)}")

            # Remove from Plex Watchlist if the setting is enabled and the item
            # came from a My Plex Watchlist or Other Plex Watchlist source.
            if state == 'Collected':
                try:
                    _handle_plex_watchlist_removal(dict(fresh_item))
                except Exception as e:
                    logging.error(f"Failed to handle Plex Watchlist removal: {e}")

            # Apply poster overlay in a background thread (non-blocking).
            # Duplicate-run protection is handled inside apply_overlay_for_new_item
            # via the _overlay_in_flight set (atomic, lock-protected).
            try:
                if get_setting('Overlay Settings', 'overlays_enabled', False):
                    import threading
                    from overlays.scheduled_tasks import apply_overlay_for_new_item
                    t = threading.Thread(
                        target=apply_overlay_for_new_item,
                        args=(item_id,),
                        daemon=True,
                        name=f'overlay-new-{item_id}'
                    )
                    t.start()
                    logging.debug(f"POST-PROCESSING: Overlay thread started for item {item_id}")
            except Exception as e:
                logging.error(f"POST-PROCESSING: Failed to start overlay thread for item {item_id}: {e}")
        else:
            logging.warning(f"Unhandled state {state} in post-processing")

    except Exception as e:
        logging.error(f"Error in state post-processing for item {item.get('id')}: {str(e)}")
        logging.exception("Traceback:") 
