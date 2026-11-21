"""Library sync with progress state and per-item overlay status."""

from flask import jsonify
from database.core import get_db_connection as _get_db_connection
from overlays.activity_logger import log_activity
from overlays.utils import is_jellyfin_mode, get_jellyfin_token
from utilities.settings import get_setting
from ._common import _get_media_client, _get_overlay_manager, logger, overlay_bp

# Track background sync state
_library_sync_state = {
    'running': False,
    'progress': '',
    'updated_movies': 0,
    'updated_episodes': 0,
    'errors': [],
    'done': False,
}

def _run_library_sync():
    """Background thread: fetch Plex items and populate ms_item_id in DB."""
    import time as _time
    global _library_sync_state

    _library_sync_state.update({
        'running': True, 'done': False, 'progress': 'Starting…',
        'updated_movies': 0, 'updated_episodes': 0, 'errors': [],
    })

    try:
        client = _get_media_client()
        conn = _get_db_connection()
        cursor = conn.cursor()
        _jf_mode = is_jellyfin_mode()
        # In Jellyfin mode also overwrite legacy Plex integer IDs; in Plex mode only fill NULLs
        if _jf_mode:
            _needs_id = "(ms_item_id IS NULL OR ms_item_id = '' OR LENGTH(ms_item_id) < 20)"
        else:
            _needs_id = "ms_item_id IS NULL"

        # --- Sync movies ---
        _library_sync_state['progress'] = 'Fetching movies from media server…'
        logger.info("sync_library: fetching movies from media server...")
        try:
            if _jf_mode:
                ms_movies = client.get_all_items_with_guids(media_type='Movie')
            else:
                ms_movies = client.get_all_items_with_guids(plex_type=1)
            logger.info(f"sync_library: got {len(ms_movies)} movies from media server")
            _library_sync_state['progress'] = f'Matching {len(ms_movies)} movies…'

            for item in ms_movies:
                rk = item['ratingKey']
                imdb_id = item.get('imdb_id')
                tmdb_id = item.get('tmdb_id')
                if not rk:
                    continue
                updated = 0
                if imdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='movie' AND imdb_id=? AND {_needs_id}",
                        (rk, imdb_id))
                    updated = cursor.rowcount
                if not updated and tmdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='movie' AND tmdb_id=? AND {_needs_id}",
                        (rk, tmdb_id))
                    updated = cursor.rowcount
                # Fallback: match by title + year, only for Plex items with no external IDs at all
                if not updated and not imdb_id and not tmdb_id and item.get('title') and item.get('year'):
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='movie' AND LOWER(title)=LOWER(?) AND year=? AND {_needs_id}",
                        (rk, item['title'], item['year']))
                    updated = cursor.rowcount
                _library_sync_state['updated_movies'] += updated

        except Exception as e:
            msg = f"Movie sync error: {e}"
            logger.error(f"sync_library: {msg}", exc_info=True)
            _library_sync_state['errors'].append(msg)

        # Small pause between movie and show fetch
        _time.sleep(1)

        # --- Sync TV shows ---
        _library_sync_state['progress'] = 'Fetching TV shows from media server…'
        logger.info("sync_library: fetching TV shows from media server...")
        try:
            if _jf_mode:
                ms_shows = client.get_all_items_with_guids(media_type='Series')
            else:
                ms_shows = client.get_all_items_with_guids(plex_type=2)
            logger.info(f"sync_library: got {len(ms_shows)} shows from media server")
            _library_sync_state['progress'] = f'Matching {len(ms_shows)} shows…'

            for item in ms_shows:
                rk = item['ratingKey']
                imdb_id = item.get('imdb_id')
                tmdb_id = item.get('tmdb_id')
                if not rk:
                    continue
                updated = 0
                if imdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='episode' AND imdb_id=? AND {_needs_id}",
                        (rk, imdb_id))
                    updated = cursor.rowcount
                if not updated and tmdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='episode' AND tmdb_id=? AND {_needs_id}",
                        (rk, tmdb_id))
                    updated = cursor.rowcount
                # Fallback: match by title + year, only for Plex items with no external IDs at all
                if not updated and not imdb_id and not tmdb_id and item.get('title') and item.get('year'):
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='episode' AND LOWER(title)=LOWER(?) AND year=? AND {_needs_id}",
                        (rk, item['title'], item['year']))
                    updated = cursor.rowcount
                _library_sync_state['updated_episodes'] += updated

        except Exception as e:
            msg = f"TV show sync error: {e}"
            logger.error(f"sync_library: {msg}", exc_info=True)
            _library_sync_state['errors'].append(msg)

        conn.commit()
        conn.close()

        # Clean up season_overlay_state rows whose show ratingKey is no longer valid.
        # This happens when Plex rescans and assigns new ratingKeys to shows — the old
        # season rows become orphaned and would 404 on the next overlay sync. Removing
        # them here lets the overlay sync re-register the seasons with fresh ratingKeys.
        try:
            _conn2 = _get_db_connection()
            _conn2.execute('''
                DELETE FROM season_overlay_state
                WHERE show_ms_item_id NOT IN (
                    SELECT DISTINCT ms_item_id FROM media_items
                    WHERE ms_item_id IS NOT NULL
                )
            ''')
            _stale = _conn2.total_changes
            _conn2.commit()
            _conn2.close()
            if _stale:
                logger.info(f"sync_library: removed {_stale} stale season_overlay_state row(s)")
        except Exception as _se:
            logger.warning(f"sync_library: could not clean stale season rows: {_se}")

        m = _library_sync_state['updated_movies']
        ep = _library_sync_state['updated_episodes']
        logger.info(f"sync_library complete: {m} movies, {ep} episodes updated")
        _library_sync_state['progress'] = f'Done — {m} movies, {ep} shows matched'
        log_activity('sync_library',
                     title=f"Sync Library: {m} movies, {ep} shows matched",
                     stats={'movies_updated': m, 'episodes_updated': ep})

    except Exception as e:
        logger.error(f"sync_library background error: {e}", exc_info=True)
        _library_sync_state['errors'].append(str(e))
        _library_sync_state['progress'] = f'Error: {e}'

    finally:
        _library_sync_state['running'] = False
        _library_sync_state['done'] = True

@overlay_bp.route('/api/overlays/sync_library', methods=['POST'])
def sync_library():
    """Start background media-server key sync (returns immediately)."""
    global _library_sync_state

    if _library_sync_state['running']:
        return jsonify({'success': True, 'status': 'already_running',
                        'progress': _library_sync_state['progress']})

    if is_jellyfin_mode():
        jf_token = get_jellyfin_token()
        if not jf_token:
            return jsonify({'success': False, 'error': 'Jellyfin token not configured in Settings'}), 400
    else:
        plex_url = get_setting('Plex', 'url', default='')
        plex_token = get_setting('Plex', 'token', default='')
        if not plex_url or not plex_token:
            return jsonify({'success': False, 'error': 'Plex URL / token not configured in Settings'}), 400

    import threading
    t = threading.Thread(target=_run_library_sync, daemon=True)
    t.start()

    return jsonify({'success': True, 'status': 'started',
                    'message': 'Sync started in background — check /api/overlays/sync_library/status'})

@overlay_bp.route('/api/overlays/sync_library/status', methods=['GET'])
def sync_library_status():
    """Return current state of the background Plex key sync."""
    return jsonify({
        'success': True,
        'running': _library_sync_state['running'],
        'done': _library_sync_state['done'],
        'progress': _library_sync_state['progress'],
        'updated_movies': _library_sync_state['updated_movies'],
        'updated_episodes': _library_sync_state['updated_episodes'],
        'errors': _library_sync_state['errors'],
    })

@overlay_bp.route('/api/overlays/status/<int:media_item_id>', methods=['GET'])
def get_overlay_status(media_item_id):
    """Get overlay generation status for a media item."""
    try:
        manager = _get_overlay_manager()
        state = manager._get_overlay_state(media_item_id)

        if not state:
            return jsonify({
                'success': True,
                'status': 'not_started',
                'message': 'No overlay generation attempted yet'
            })

        return jsonify({
            'success': True,
            'status': state['status'],
            'reason': state.get('reason'),
            'retry_count': state.get('retry_count', 0),
            'last_retry': state.get('last_retry'),
            'overlay_applied_at': state.get('overlay_applied_at')
        })

    except Exception as e:
        logger.error(f"Failed to get overlay status for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
