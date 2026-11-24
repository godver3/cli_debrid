"""Season/episode deletion and replace marks."""

from flask import jsonify, request
from ..models import admin_required
from database.core import get_db_connection
from debrid import get_debrid_provider
import logging
from datetime import datetime
from ._common import library_bp

@library_bp.route('/delete_season/<imdb_id>/<int:season_number>', methods=['POST'])
@admin_required
def delete_season(imdb_id, season_number):
    """
    Delete entire season (all episodes in season)

    Request body:
    {
        "blacklist": bool,  # Whether to blacklist instead of permanent delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache", "content_source"]
    }
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_all_episodes_for_season
        from utilities.settings import get_setting

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)

        # Get all episodes for this season
        episodes = get_all_episodes_for_season(imdb_id, season_number)

        if not episodes:
            return jsonify({
                'success': False,
                'error': f'No episodes found for season {season_number}'
            }), 404

        episode_ids = [ep['id'] for ep in episodes]

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        # Use global setting for content source removal (ignores frontend layers)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)
        blacklist_sources = data.get('blacklist_sources', False)

        logging.info(f"[DELETE_SEASON] Starting deletion for show {imdb_id} season {season_number}")
        logging.info(f"[DELETE_SEASON] Found {len(episode_ids)} episodes to delete")
        logging.info(f"[DELETE_SEASON] Layers requested: {layers}")

        # ONLY check auto-ghostlist setting - this determines database behavior
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        #logging.info(f"[DELETE_SEASON] Auto-ghostlist setting: {auto_ghostlist} (type: {type(auto_ghostlist)})")

        # Additional debug - check what the setting value actually is
        # if auto_ghostlist:
        #     logging.info(f"[DELETE_SEASON] WILL GHOSTLIST - auto_ghostlist is truthy")
        # else:
        logging.info(f"[DELETE_SEASON] WILL DELETE - auto_ghostlist is falsy")

        # Note: Content source removal is NOT supported for season deletion
        # If user wants to remove from content source, they should delete the entire show
        if remove_from_content_source:
            logging.warning(f"[DELETE_SEASON] Content source removal requested but not supported for season deletion - use delete_show instead")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # Get show title from first episode for Plex content-level deletion
        show_title = episodes[0].get('title') if episodes else None
        # tmdb_id falls back to imdb_id if not available
        show_tmdb_id = (episodes[0].get('tmdb_id') or imdb_id) if episodes else imdb_id

        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=episode_ids,
            blacklist=False,
            blacklist_sources=False,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,  # Content source removal only in delete_show
            skip_database=True,  # We'll handle database ourselves based on auto_ghostlist
            force_delete_parent_folder=True,  # Force delete season folder even if not empty (symlink mode only, with Plex verification)
            # Plex content-level deletion params - deletes whole season in 1 API call
            plex_deletion_type='season',
            plex_content_title=show_title,
            plex_imdb_id=imdb_id,
            plex_tmdb_id=show_tmdb_id,
            plex_season_number=season_number
        )

        logging.info(f"[DELETE_SEASON] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_SEASON] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_SEASON] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_SEASON] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            response_data = {
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }
            if result.get('plex_not_found'):
                response_data['plex_not_found'] = True
            return jsonify(response_data), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        #logging.info(f"[DELETE_SEASON] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        # if auto_ghostlist:
        #     # Ghostlist: Update all episodes to ghostlisted=1 and state=Blacklisted
        #     logging.info(f"[DELETE_SEASON] Taking GHOSTLIST path")
        #     try:
        #         from database.core import get_db_connection
        #         from datetime import datetime

        #         conn = get_db_connection()
        #         cursor = conn.cursor()

        #         # Begin transaction
        #         cursor.execute('BEGIN TRANSACTION')

        #         # Ghostlist all episodes in this season (set ghostlisted=1 and state=Blacklisted)
        #         cursor.execute(
        #             'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({})'.format(','.join('?' * len(episode_ids))),
        #             ['Blacklisted', datetime.now()] + episode_ids
        #         )

        #         updated_count = cursor.rowcount
        #         conn.commit()
        #         conn.close()

        #         auto_ghostlisted = True
        #         logging.info(f"[DELETE_SEASON] Ghostlisted {updated_count} episodes for season {season_number} - ghostlisted=1, state=Blacklisted")

        #     except Exception as e:
        #         logging.error(f"[DELETE_SEASON] Failed to ghostlist: {e}")
        #         if conn:
        #             conn.rollback()
        #             conn.close()

        # else:
        # Delete: Remove all episodes from database
        logging.info(f"[DELETE_SEASON] Taking DELETE path")
        try:
            from database.database_writing import remove_from_media_items

            # Delete all episodes
            deleted_count = 0
            for episode_id in episode_ids:
                if remove_from_media_items(episode_id):
                    deleted_count += 1

            logging.info(f"[DELETE_SEASON] Deleted {deleted_count} episodes from database")
        except Exception as e:
            logging.error(f"[DELETE_SEASON] Failed to delete from database: {e}")

        # Build detailed layer execution summary (only report actual successes)
        layers_executed = []
        layers_skipped = []  # Track skipped/failed layers

        # Database layer - ghostlist or delete
        # if auto_ghostlisted:
        #     layers_executed.append('Database (Ghostlisted)')
        # else:
        layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                # Check if Plex is configured
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Filesystem - only if files actually deleted
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed', False) for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0) or sum(r.get('debrid_nzb_removed', 0) for r in result.get('results', []))
                if nzb_count == 0 and torrent_count == 0:
                    # single-item path: infer from per-item results
                    for r in result.get('results', []):
                        if r.get('debrid_removed'):
                            if r.get('debrid_nzb_removed', 0): nzb_count += 1
                            else: torrent_count += 1
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                # Check if debrid provider is available
                debrid_provider = get_debrid_provider()
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up
        symlink_count = sum(r.get('symlinks_deleted', 0) for r in result.get('results', []))
        if delete_symlinks:
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0 or result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache - only if actually cleared
        if clear_cache:
            layers_executed.append('Cache')

        # Build response with both 'error' (singular) and 'errors' (plural) for frontend compatibility
        errors_list = result.get('errors', [])
        response_data = {
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': errors_list,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'message': f"Season {season_number} deleted - {len(episode_ids)} episodes"
        }

        # Add 'error' (singular) field for frontend compatibility - uses first error if available
        if errors_list:
            response_data['error'] = errors_list[0]

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error deleting season {season_number} of show {imdb_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/mark_season_replace/<imdb_id>/<int:season_number>', methods=['POST'])
@admin_required
def mark_season_replace(imdb_id, season_number):
    """
    Mark all collected episodes in a season for replacement.
    Sets manual_replace=1 on each Collected episode so the collection hook
    can clean them up after the new season pack is collected.
    """
    try:
        conn = get_db_connection()
        cursor = conn.execute(
            """UPDATE media_items SET manual_replace = 1, last_updated = ?
               WHERE imdb_id = ? AND season_number = ? AND type = 'episode'
               AND state NOT IN ('Blacklisted') AND (ghostlisted = 0 OR ghostlisted IS NULL)""",
            (datetime.now(), imdb_id, season_number)
        )
        marked_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_SEASON] Marked {marked_count} episodes for replacement: {imdb_id} S{season_number:02d}")
        return jsonify({'success': True, 'marked_count': marked_count})
    except Exception as e:
        logging.error(f"Error marking season {season_number} of {imdb_id} for replace: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@library_bp.route('/cancel_season_replace/<imdb_id>/<int:season_number>', methods=['POST'])
@admin_required
def cancel_season_replace(imdb_id, season_number):
    """
    Cancel a pending season replacement by clearing the manual_replace flag.
    """
    try:
        conn = get_db_connection()
        cursor = conn.execute(
            """UPDATE media_items SET manual_replace = 0, last_updated = ?
               WHERE imdb_id = ? AND season_number = ? AND type = 'episode'
               AND manual_replace = 1""",
            (datetime.now(), imdb_id, season_number)
        )
        cleared_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_SEASON] Cancelled replacement for {cleared_count} episodes: {imdb_id} S{season_number:02d}")
        return jsonify({'success': True, 'cleared_count': cleared_count})
    except Exception as e:
        logging.error(f"Error cancelling season replace for {imdb_id} S{season_number}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@library_bp.route('/mark_movie_replace', methods=['POST'])
@admin_required
def mark_movie_replace():
    """
    Mark specific movie file entries for replacement.
    Sets manual_replace=1 on each provided file ID so the collection hook
    can clean them up after a new version is collected.
    """
    try:
        data = request.get_json()
        file_ids = data.get('file_ids', [])
        if not file_ids:
            return jsonify({'success': False, 'error': 'No file IDs provided'}), 400
        conn = get_db_connection()
        placeholders = ','.join(['?'] * len(file_ids))
        cursor = conn.execute(
            f"UPDATE media_items SET manual_replace = 1, last_updated = ? WHERE id IN ({placeholders}) AND type = 'movie'",
            [datetime.now()] + list(file_ids)
        )
        marked_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_MOVIE] Marked {marked_count} movie file(s) for replacement. IDs: {file_ids}")
        return jsonify({'success': True, 'marked_count': marked_count})
    except Exception as e:
        logging.error(f"Error marking movie files for replace: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@library_bp.route('/cancel_movie_replace', methods=['POST'])
@admin_required
def cancel_movie_replace():
    """
    Cancel a pending movie replacement by clearing the manual_replace flag.
    """
    try:
        data = request.get_json()
        imdb_id = data.get('imdb_id')
        if not imdb_id:
            return jsonify({'success': False, 'error': 'No imdb_id provided'}), 400
        conn = get_db_connection()
        cursor = conn.execute(
            "UPDATE media_items SET manual_replace = 0, last_updated = ? WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1",
            (datetime.now(), imdb_id)
        )
        cleared_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_MOVIE] Cancelled replacement for {cleared_count} movie file(s): {imdb_id}")
        return jsonify({'success': True, 'cleared_count': cleared_count})
    except Exception as e:
        logging.error(f"Error cancelling movie replace for {imdb_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@library_bp.route('/delete_episode/<imdb_id>/<int:season_number>/<int:episode_number>', methods=['POST'])
@admin_required
def delete_episode(imdb_id, season_number, episode_number):
    """
    Delete specific episode (one or more files for the same episode number)

    Request body:
    {
        "item_ids": [123, 456],  # Specific database IDs to delete (for multi-file episodes)
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache"]
    }
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.core import get_db_connection

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        item_ids = data.get('item_ids', [])

        # If no specific item_ids provided, get all items for this episode
        if not item_ids:
            conn = get_db_connection()
            cursor = conn.execute('''
                SELECT id FROM media_items
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ? AND type = 'episode'
            ''', (imdb_id, season_number, episode_number))
            item_ids = [row['id'] for row in cursor.fetchall()]
            conn.close()

        if not item_ids:
            return jsonify({
                'success': False,
                'error': f'No files found for episode S{season_number:02d}E{episode_number:02d}'
            }), 404

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        remove_from_content_source = 'content_source' in layers

        logging.info(f"[DELETE_EPISODE] Starting deletion for {imdb_id} S{season_number:02d}E{episode_number:02d}")
        logging.info(f"[DELETE_EPISODE] Item IDs to delete: {item_ids}")
        logging.info(f"[DELETE_EPISODE] Layers requested: {layers}")

        # Note: Content source removal is NOT supported for episode deletion
        # If user wants to remove from content source, they should delete the entire show
        if remove_from_content_source:
            logging.warning(f"[DELETE_EPISODE] Content source removal requested but not supported for episode deletion - use delete_show instead")

        # ONLY check auto-ghostlist setting - this determines database behavior
        from utilities.settings import get_setting
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        # Check file collection management mode for appropriate deletion messages
        file_management_mode = get_setting('File Management', 'file_collection_management', default='Plex')

        #logging.info(f"[DELETE_EPISODE] Auto-ghostlist setting: {auto_ghostlist}")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=item_ids,
            blacklist=False,
            blacklist_sources=False,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,
            skip_database=True  # We'll handle database ourselves based on auto_ghostlist
        )

        logging.info(f"[DELETE_EPISODE] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_EPISODE] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_EPISODE] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_EPISODE] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            return jsonify({
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        #logging.info(f"[DELETE_EPISODE] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        # if auto_ghostlist:
        #     # Ghostlist: Update episodes to ghostlisted=1 and state=Blacklisted
        #     logging.info(f"[DELETE_EPISODE] Taking GHOSTLIST path")
        #     try:
        #         from datetime import datetime

        #         conn = get_db_connection()
        #         cursor = conn.cursor()

        #         cursor.execute('BEGIN TRANSACTION')

        #         # Ghostlist specified episodes
        #         cursor.execute(
        #             'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({})'.format(','.join('?' * len(item_ids))),
        #             ['Blacklisted', datetime.now()] + item_ids
        #         )

        #         updated_count = cursor.rowcount
        #         conn.commit()
        #         conn.close()

        #         auto_ghostlisted = True
        #         logging.info(f"[DELETE_EPISODE] Ghostlisted {updated_count} file(s) for episode S{season_number:02d}E{episode_number:02d}")

        #     except Exception as e:
        #         logging.error(f"[DELETE_EPISODE] Failed to ghostlist: {e}")
        #         if conn:
        #             conn.rollback()
        #             conn.close()

        # else:
        # Delete: Remove episodes from database
        logging.info(f"[DELETE_EPISODE] Taking DELETE path")
        try:
            from database.database_writing import remove_from_media_items

            deleted_count = 0
            for item_id in item_ids:
                if remove_from_media_items(item_id):
                    deleted_count += 1

            logging.info(f"[DELETE_EPISODE] Deleted {deleted_count} file(s) from database")
        except Exception as e:
            logging.error(f"[DELETE_EPISODE] Failed to delete from database: {e}")

        # Build detailed layer execution summary
        layers_executed = []
        layers_skipped = []

        # Database layer
        # if auto_ghostlisted:
        #     layers_executed.append('Database (Ghostlisted)')
        # else:
        layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Filesystem
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                # In Plex mode, filesystem layer is not applicable since Plex manages files
                if file_management_mode == 'Plex':
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'Not applicable in Direct Mount'})
                else:
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed', False) for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0) or sum(r.get('debrid_nzb_removed', 0) for r in result.get('results', []))
                if nzb_count == 0 and torrent_count == 0:
                    # single-item path: infer from per-item results
                    for r in result.get('results', []):
                        if r.get('debrid_removed'):
                            if r.get('debrid_nzb_removed', 0): nzb_count += 1
                            else: torrent_count += 1
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                debrid_provider = get_debrid_provider()
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up (only relevant in Symlinked/Local mode)
        if file_management_mode != 'Plex':
            symlink_count = sum(r.get('symlinks_deleted', 0) for r in result.get('results', []))
            if delete_symlinks:
                # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
                if symlink_count > 0 or result['success']:
                    layers_executed.append('Symlinks')
                else:
                    layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache
        if clear_cache:
            layers_executed.append('Cache')

        return jsonify({
            'success': result['success'],
            'deleted_count': len(item_ids),
            'failed_count': result.get('failed_count', 0),
            'errors': result.get('errors', []),
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'message': f"S{season_number:02d}E{episode_number:02d} deleted - {len(item_ids)} file(s)"
        })

    except Exception as e:
        logging.error(f"Error deleting episode {imdb_id} S{season_number:02d}E{episode_number:02d}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
