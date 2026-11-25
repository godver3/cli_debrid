"""Movie and movie-file deletion."""

from flask import jsonify, request
from ..models import admin_required
from debrid import get_debrid_provider
import logging
from ._common import library_bp

@library_bp.route('/delete_movie/<imdb_id>', methods=['POST'])
@admin_required
def delete_movie(imdb_id):
    """
    Delete entire movie (all video files)

    Request body:
    {
        "blacklist": bool,  # Whether to blacklist instead of permanent delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache", "content_source"]
    }
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.core import get_db_connection
        from utilities.settings import get_setting

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)

        # Get ALL movie files from database (movies can have multiple files/versions)
        conn = get_db_connection()
        movie_query = """
            SELECT id, title, year, tmdb_id, imdb_id, type
            FROM media_items
            WHERE (tmdb_id = ? OR imdb_id = ?) AND type = 'movie'
        """
        movie_rows = conn.execute(movie_query, (str(imdb_id), str(imdb_id))).fetchall()
        conn.close()

        if not movie_rows:
            return jsonify({
                'success': False,
                'error': 'No movie found with this ID'
            }), 404

        # Collect all movie file IDs
        movie_ids = [row['id'] for row in movie_rows]
        movie = dict(movie_rows[0])  # Use first row for metadata
        movie_title = movie['title']

        logging.info(f"[DELETE_MOVIE] Found {len(movie_ids)} file(s) for movie {imdb_id} ({movie_title}): {movie_ids}")

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        # Use global setting for content source removal (ignores frontend layers)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)
        blacklist_sources = data.get('blacklist_sources', False)

        logging.info(f"[DELETE_MOVIE] Starting deletion for movie {imdb_id} ({movie_title})")
        logging.info(f"[DELETE_MOVIE] Layers requested: {layers}")

        # Check auto-ghostlist setting
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        logging.info(f"[DELETE_MOVIE] Auto-ghostlist setting: {auto_ghostlist}")

        # Handle content source removal
        content_source_result = None
        if remove_from_content_source:
            logging.info(f"[DELETE_MOVIE] Removing movie from content sources")
            debrid_provider = get_debrid_provider()
            deletion_manager = DeletionManager(debrid_provider=debrid_provider)
            movie['type'] = 'movie'
            content_source_result = deletion_manager.remove_from_content_source(movie)
            logging.info(f"[DELETE_MOVIE] Content source removal result: {content_source_result.get('message')}")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # tmdb_id falls back to imdb_id if not available
        movie_tmdb_id = movie.get('tmdb_id') or imdb_id

        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=movie_ids,  # Delete ALL files for this movie
            blacklist=blacklist,
            blacklist_sources=blacklist_sources,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,  # Already handled above
            force_delete_parent_folder=True,  # Whole movie delete - remove movie folder
            skip_database=True,  # We'll handle database ourselves based on auto_ghostlist
            # Plex content-level deletion params - deletes whole movie in 1 API call
            plex_deletion_type='movie',
            plex_content_title=movie_title,
            plex_imdb_id=imdb_id,
            plex_tmdb_id=movie_tmdb_id
        )

        logging.info(f"[DELETE_MOVIE] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_MOVIE] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_MOVIE] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_MOVIE] Errors: {result.get('errors', [])}")

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

        # Handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_MOVIE] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        if auto_ghostlist:
            # Ghostlist: Update movie to ghostlisted=1 and state=Blacklisted
            logging.info(f"[DELETE_MOVIE] Taking GHOSTLIST path")
            try:
                from datetime import datetime

                conn = get_db_connection()
                cursor = conn.cursor()

                # Begin transaction
                cursor.execute('BEGIN TRANSACTION')

                # Ghostlist all movie files
                placeholders = ','.join('?' * len(movie_ids))
                cursor.execute(
                    f'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                    ['Blacklisted', datetime.now()] + movie_ids
                )

                updated_count = cursor.rowcount
                conn.commit()
                conn.close()

                auto_ghostlisted = True
                logging.info(f"[DELETE_MOVIE] Ghostlisted movie {imdb_id} - ghostlisted=1, state=Blacklisted")

                # Also add to manual blacklist to prevent re-addition
                try:
                    from database.manual_blacklist import add_to_manual_blacklist
                    add_to_manual_blacklist(
                        imdb_id=imdb_id,
                        media_type='movie',
                        title=movie.get('title', ''),
                        year=str(movie.get('year', ''))
                    )
                    logging.info(f"[DELETE_MOVIE] Added {imdb_id} to manual blacklist")
                except Exception as mb_err:
                    logging.warning(f"[DELETE_MOVIE] Could not add to manual blacklist: {mb_err}")

            except Exception as e:
                logging.error(f"[DELETE_MOVIE] Failed to ghostlist: {e}")
                if conn:
                    conn.rollback()
                    conn.close()

        else:
            # Delete: Remove all movie files from database
            logging.info(f"[DELETE_MOVIE] Taking DELETE path")
            try:
                from database.database_writing import remove_from_media_items

                # Delete all movie files
                deleted_count = 0
                for mid in movie_ids:
                    if remove_from_media_items(mid):
                        deleted_count += 1

                logging.info(f"[DELETE_MOVIE] Deleted {deleted_count}/{len(movie_ids)} movie file(s) from database")
                if deleted_count < len(movie_ids):
                    logging.warning(f"[DELETE_MOVIE] Failed to delete {len(movie_ids) - deleted_count} movie file(s) from database")

            except Exception as e:
                logging.error(f"[DELETE_MOVIE] Failed to delete from database: {e}")

        # Build detailed layer execution summary
        layers_executed = []
        layers_skipped = []

        # Database layer - ghostlist or delete
        if auto_ghostlisted:
            layers_executed.append('Database (Ghostlisted)')
        else:
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

        # Check file collection management mode for appropriate deletion messages
        file_management = get_setting('File Management', 'file_collection_management', default='Plex')

        # Filesystem - only if files actually deleted
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                # In Plex mode, filesystem layer is not applicable since Plex manages files
                if file_management == 'Plex':
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

        # Symlinks - check if deleted or cleaned up (only show in Symlink mode)
        symlink_count = sum(len(r.get('deleted_symlinks', [])) for r in result.get('results', []))
        if delete_symlinks and file_management != 'Plex':
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0 or result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache - always succeeds if requested
        if clear_cache:
            layers_executed.append('Cache')

        # Content Source - report based on actual result
        if remove_from_content_source and content_source_result:
            if content_source_result.get('success'):
                layers_executed.append(f"Content Source ({', '.join(content_source_result.get('sources_succeeded', []))})")
            else:
                layers_executed.append('Content Source (Failed)')

        # Build response with both 'error' (singular) and 'errors' (plural) for frontend compatibility
        errors_list = result.get('errors', [])
        response_data = {
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': errors_list,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'content_source_removal': content_source_result,
            'auto_ghostlisted': auto_ghostlisted,
            'message': f"{'Ghostlisted' if auto_ghostlisted else 'Deleted'} movie: {movie_title}"
        }

        # Add 'error' (singular) field for frontend compatibility - uses first error if available
        if errors_list:
            response_data['error'] = errors_list[0]

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error deleting movie {imdb_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/delete_movie_files', methods=['POST'])
@admin_required
def delete_movie_files():
    """
    Delete specific movie files (for movies with multiple versions/files)

    Request body:
    {
        "file_ids": [int],  # List of media_items IDs to delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks"]
    }

    Note: Does NOT include content_source removal - movie still exists, just fewer files
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.core import get_db_connection

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks'])
        item_ids = data.get('file_ids', [])

        if not item_ids:
            return jsonify({
                'success': False,
                'error': 'No file IDs provided'
            }), 400

        # Verify all files exist and are movies
        conn = get_db_connection()
        for item_id in item_ids:
            file_row = conn.execute('SELECT id, title, type FROM media_items WHERE id = ?', (item_id,)).fetchone()
            if not file_row:
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'File ID {item_id} not found'
                }), 404
            if file_row['type'] != 'movie':
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'File ID {item_id} is not a movie'
                }), 400
        conn.close()

        # Parse layers
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        remove_from_content_source = 'content_source' in layers

        logging.info(f"[DELETE_MOVIE_FILES] Starting deletion for {len(item_ids)} file(s)")
        logging.info(f"[DELETE_MOVIE_FILES] Item IDs to delete: {item_ids}")
        logging.info(f"[DELETE_MOVIE_FILES] Layers requested: {layers}")

        # Note: Content source removal is NOT supported for movie file deletion
        # If user wants to remove from content source, they should delete the entire movie
        if remove_from_content_source:
            logging.warning(f"[DELETE_MOVIE_FILES] Content source removal requested but not supported for file deletion - use delete_movie instead")

        # Check auto-ghostlist setting
        from utilities.settings import get_setting
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        #logging.info(f"[DELETE_MOVIE_FILES] Auto-ghostlist setting: {auto_ghostlist}")

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
            remove_from_content_source=False,  # Never remove content source for file deletion
            skip_database=True  # Handle database ourselves based on auto_ghostlist
        )

        logging.info(f"[DELETE_MOVIE_FILES] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_MOVIE_FILES] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_MOVIE_FILES] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_MOVIE_FILES] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            return jsonify({
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }), 500

        # Handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_MOVIE_FILES] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        # if auto_ghostlist:
        #     # Ghostlist: Update files to ghostlisted=1 and state=Blacklisted
        #     logging.info(f"[DELETE_MOVIE_FILES] Taking GHOSTLIST path")
        #     try:
        #         from datetime import datetime

        #         conn = get_db_connection()
        #         cursor = conn.cursor()

        #         cursor.execute('BEGIN TRANSACTION')

        #         # Ghostlist specified files
        #         placeholders = ','.join('?' * len(item_ids))
        #         cursor.execute(
        #             f'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({placeholders})',
        #             ['Blacklisted', datetime.now()] + item_ids
        #         )

        #         updated_count = cursor.rowcount
        #         conn.commit()
        #         conn.close()

        #         auto_ghostlisted = True
        #         logging.info(f"[DELETE_MOVIE_FILES] Ghostlisted {updated_count} file(s)")

        #     except Exception as e:
        #         logging.error(f"[DELETE_MOVIE_FILES] Failed to ghostlist: {e}")
        #         if conn:
        #             conn.rollback()
        #             conn.close()

        # else:
        # Delete: Remove files from database
        logging.info(f"[DELETE_MOVIE_FILES] Taking DELETE path")
        try:
            from database.database_writing import remove_from_media_items

            deleted_count = 0
            for item_id in item_ids:
                if remove_from_media_items(item_id):
                    deleted_count += 1

            logging.info(f"[DELETE_MOVIE_FILES] Deleted {deleted_count}/{len(item_ids)} file(s) from database")
            if deleted_count < len(item_ids):
                logging.warning(f"[DELETE_MOVIE_FILES] Failed to delete {len(item_ids) - deleted_count} file(s) from database")

        except Exception as e:
            logging.error(f"[DELETE_MOVIE_FILES] Failed to delete from database: {e}")

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
                layers_executed.append(f'Filesystem ({file_count} file{"s" if file_count != 1 else ""})')
            else:
                layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed') for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                # Show count if available from batch removal
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0)
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up
        symlink_count = sum(len(r.get('deleted_symlinks', [])) for r in result.get('results', []))
        if delete_symlinks:
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0:
                layers_executed.append(f'Symlinks ({symlink_count} symlink{"s" if symlink_count != 1 else ""})')
            elif result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache
        cache_cleared = any(r.get('cache_cleared') for r in result.get('results', []))
        if clear_cache:
            if cache_cleared:
                layers_executed.append('Cache')
            else:
                layers_skipped.append({'layer': 'Cache', 'reason': 'No cache files found'})

        return jsonify({
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': result.get('errors', []),
            'auto_ghostlisted': auto_ghostlisted,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'content_source_removal': None  # File deletion doesn't remove from content sources
        })

    except Exception as e:
        logging.error(f"Error deleting movie files: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
