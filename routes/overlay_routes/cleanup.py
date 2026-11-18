"""Cleanup stats, overlay removal, backups and poster reset."""

from flask import jsonify, request
from database.core import get_db_connection as _get_db_connection
from overlays.activity_logger import log_activity
from ._common import _get_overlay_manager, invalidate_poster_thumb_cache, invalidate_season_thumb_cache, logger, overlay_bp

# ============================================
# Cache Cleanup Endpoints
# ============================================

@overlay_bp.route('/api/overlays/cleanup/stats', methods=['GET'])
def get_cleanup_stats():
    """Combined cleanup stats: applied overlay count, backup dir size, and cumulative totals."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM media_overlay_state WHERE status = 'applied'")
        applied_count = cursor.fetchone()[0]

        # Cumulative all-time cleanup counters
        cursor.execute(
            "SELECT key, value FROM overlay_sync_state WHERE key IN ('cleanup_total_posters', 'cleanup_total_bytes')"
        )
        counters = {row[0]: int(row[1] or 0) for row in cursor.fetchall()}
        conn.close()

        total_posters = counters.get('cleanup_total_posters', 0)
        total_bytes   = counters.get('cleanup_total_bytes', 0)
        total_mb      = round(total_bytes / (1024 * 1024), 2)

        # Current backup directory size + orphaned count
        from overlays.cache_cleanup import PosterCacheManager
        manager = PosterCacheManager(None)
        backup_stats = manager.get_backup_stats()

        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT ms_item_id FROM media_items WHERE ms_item_id IS NOT NULL')
        valid_keys = set(row[0] for row in cursor.fetchall())
        cursor.execute('''
            SELECT DISTINCT season_ms_item_id FROM season_overlay_state
            WHERE season_ms_item_id IS NOT NULL
              AND status NOT IN ('removed', 'user_removed')
        ''')
        valid_season_keys = set(row[0] for row in cursor.fetchall())
        conn.close()

        orphaned_count = 0
        for f in manager.backup_dir.glob('*_original.jpg'):
            stem = f.stem.replace('_original', '')
            if stem.startswith('season_'):
                if stem[len('season_'):] not in valid_season_keys:
                    orphaned_count += 1
            else:
                if stem not in valid_keys:
                    orphaned_count += 1

        return jsonify({
            'success': True,
            'applied_count': applied_count,
            'backup_count': backup_stats.get('backup_count', 0),
            'backup_size_mb': backup_stats.get('total_size_mb', 0),
            'backup_orphaned': orphaned_count,
            'cleaned_total_posters': total_posters,
            'cleaned_total_mb': total_mb,
        })
    except Exception as e:
        logger.error(f"Failed to get cleanup stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/cache-size', methods=['GET'])
def get_cache_size():
    """Get backup dir size (Plex cache detection is unreliable; returns backup stats instead)."""
    try:
        from overlays.cache_cleanup import PosterCacheManager
        manager = PosterCacheManager(None)
        result = manager.get_backup_stats()
        # Map to expected field name used by frontend
        result['total_size_gb'] = result.get('total_size_mb', 0) / 1024
        result['success'] = True
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to get cache size: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/backup-stats', methods=['GET'])
def get_backup_stats():
    """Get poster backup statistics."""
    try:
        from overlays.cache_cleanup import PosterCacheManager
        manager = PosterCacheManager(None)
        result = manager.get_backup_stats()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to get backup stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/remove/<int:media_item_id>', methods=['POST'])
def remove_overlay(media_item_id):
    """Remove overlay and restore original poster for a single item."""
    try:
        from overlays.cache_cleanup import PosterCacheManager

        manager = PosterCacheManager(None)

        result = manager.remove_overlay(media_item_id)
        if result.get('success'):
            # Look up ms_item_id so we can drop the thumb cache for this item
            try:
                _c = _get_db_connection()
                _row = _c.execute('SELECT ms_item_id FROM media_items WHERE id = ?',
                                  (media_item_id,)).fetchone()
                _c.close()
                if _row and _row[0]:
                    invalidate_poster_thumb_cache(_row[0])
            except Exception:
                pass
            log_activity('remove', title=f"Overlay removed (item {media_item_id})",
                         stats={'media_item_id': media_item_id})
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to remove overlay for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/remove-all', methods=['POST'])
def remove_all_overlays():
    """Start a background Remove-All job; returns immediately with total count."""
    import threading
    from overlays.cache_cleanup import get_remove_all_status, run_remove_all_job
    try:
        status = get_remove_all_status()
        if status.get('running'):
            return jsonify({'success': False, 'error': 'A remove-all job is already running'}), 409

        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MIN(o.media_item_id) as media_item_id
            FROM media_overlay_state o
            JOIN media_items m ON m.id = o.media_item_id
            WHERE o.status = 'applied' AND m.ms_item_id IS NOT NULL
            GROUP BY m.ms_item_id
        """)
        item_ids = [row[0] for row in cursor.fetchall()]
        cursor.execute("""
            SELECT show_ms_item_id, season_ms_item_id, season_number
            FROM season_overlay_state
            WHERE status = 'applied'
        """)
        season_rows = list(cursor.fetchall())
        conn.close()

        if not item_ids and not season_rows:
            return jsonify({'success': True, 'started': False, 'total': 0,
                            'message': 'No applied overlays found'})

        overlay_manager = _get_overlay_manager()
        t = threading.Thread(
            target=run_remove_all_job,
            args=(item_ids, season_rows, overlay_manager),
            daemon=True,
        )
        t.start()

        return jsonify({'success': True, 'started': True,
                        'total': len(item_ids) + len(season_rows)})
    except Exception as e:
        logger.error(f"Failed to start remove-all job: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/remove-all/status', methods=['GET'])
def remove_all_status():
    """Poll progress of the running/last Remove-All job."""
    try:
        from overlays.cache_cleanup import get_remove_all_status
        s = get_remove_all_status()
        pct = 0
        if s['total'] > 0:
            pct = round((s['done']) / s['total'] * 100)
        return jsonify({
            'success': True,
            'running': s['running'],
            'total': s['total'],
            'done': s['done'],
            'restored': s['restored'],
            'failed': s['failed'],
            'current': s['current'],
            'errors': s['errors'],
            'percent': pct,
            'finished_at': s.get('finished_at'),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/remove-batch', methods=['POST'])
def remove_overlays_batch():
    """Remove overlays for multiple items."""
    try:
        data = request.get_json()

        if not data or 'item_ids' not in data:
            return jsonify({'success': False, 'error': 'No item_ids provided'}), 400

        item_ids = data['item_ids']
        if not isinstance(item_ids, list) or not item_ids:
            return jsonify({'success': False, 'error': 'item_ids must be a non-empty array'}), 400

        from overlays.cache_cleanup import PosterCacheManager

        cache_manager = PosterCacheManager(None)
        result = cache_manager.batch_remove_overlays(item_ids)

        # Invalidate thumb cache for all items whose poster just changed
        try:
            _ic = _get_db_connection()
            _placeholders = ','.join('?' * len(item_ids))
            _rows = _ic.execute(
                f'SELECT DISTINCT ms_item_id FROM media_items WHERE id IN ({_placeholders}) AND ms_item_id IS NOT NULL',
                item_ids
            ).fetchall()
            _ic.close()
            for _r in _rows:
                invalidate_poster_thumb_cache(_r[0])
        except Exception:
            pass

        # Also remove season overlays for any TV shows in the selection.
        # Items may be episodes (type='episode') or shows (type='show'); both share
        # the show-level ms_item_id which is what season_overlay_state indexes on.
        conn = _get_db_connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(item_ids))
        cursor.execute(
            f"SELECT DISTINCT ms_item_id FROM media_items "
            f"WHERE id IN ({placeholders}) AND type IN ('episode', 'show') AND ms_item_id IS NOT NULL",
            item_ids
        )
        show_keys = [r[0] for r in cursor.fetchall()]
        if show_keys:
            key_placeholders = ','.join('?' * len(show_keys))
            cursor.execute(
                f"SELECT show_ms_item_id, season_ms_item_id, season_number "
                f"FROM season_overlay_state "
                f"WHERE show_ms_item_id IN ({key_placeholders}) AND status='applied'",
                show_keys
            )
            season_rows = cursor.fetchall()
            conn.close()

            overlay_manager = _get_overlay_manager()

            # DB-tracked seasons first
            for sr in season_rows:
                try:
                    sr_result = overlay_manager.remove_season_overlay(
                        show_plex_rating_key=sr['show_ms_item_id'],
                        season_plex_rating_key=sr['season_ms_item_id'],
                        season_number=sr['season_number'],
                    )
                    if sr_result.get('success'):
                        invalidate_season_thumb_cache(sr['season_ms_item_id'])
                        result['restored'] = result.get('restored', 0) + 1
                    elif sr_result.get('status') != 'skipped':
                        logger.warning(f"Season overlay removal failed for {sr['season_ms_item_id']}: {sr_result.get('message')}")
                        result['failed'] = result.get('failed', 0) + 1
                except Exception as _se:
                    logger.warning(f"Failed to remove season overlay {sr['season_ms_item_id']}: {_se}")
                    result['failed'] = result.get('failed', 0) + 1

            # Fallback: scan Plex directly for untracked season overlays
            tracked_season_keys = {sr['season_ms_item_id'] for sr in season_rows}
            for show_key in show_keys:
                try:
                    seasons = overlay_manager.client.get_show_seasons(show_key)
                    for season in seasons:
                        if season['ratingKey'] in tracked_season_keys:
                            continue  # already handled above
                        sr_result = overlay_manager.remove_season_overlay(
                            show_plex_rating_key=show_key,
                            season_plex_rating_key=season['ratingKey'],
                            season_number=season.get('index', 0),
                        )
                        if sr_result.get('success'):
                            invalidate_season_thumb_cache(season['ratingKey'])
                            result['restored'] = result.get('restored', 0) + 1
                        elif sr_result.get('status') == 'error':
                            result['failed'] = result.get('failed', 0) + 1
                except Exception as _scan_err:
                    logger.debug(f"Season scan failed for show {show_key}: {_scan_err}")
        else:
            conn.close()

        log_activity('remove',
                     title=f"Remove selected overlays: {result.get('restored', 0)} restored, {result.get('failed', 0)} failed",
                     stats={'restored': result.get('restored', 0), 'failed': result.get('failed', 0),
                            'count': len(item_ids)})
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to batch remove overlays: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/orphaned-backups', methods=['POST'])
def cleanup_orphaned_backups():
    """Clean up orphaned poster backups."""
    try:
        from overlays.cache_cleanup import task_cleanup_orphaned_backups

        result = task_cleanup_orphaned_backups()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to cleanup orphaned backups: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/cleanup/delete-all-backups', methods=['POST'])
def delete_all_backups():
    """Delete all backup files and reset overlay states to pending."""
    try:
        from overlays.cache_cleanup import PosterCacheManager
        manager = PosterCacheManager(None)
        result = manager.delete_all_backups()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to delete all backups: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/stats/reset-cleanup', methods=['POST'])
def reset_cleanup_stats():
    """Reset the cumulative cleaned-up poster counters to zero."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        conn.execute(
            "UPDATE overlay_sync_state SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key IN ('cleanup_total_posters', 'cleanup_total_bytes')"
        )
        conn.commit()
        conn.close()
        logger.info("Cleanup stats reset to zero")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Failed to reset cleanup stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/stats/reset-failed', methods=['POST'])
def reset_failed_stats():
    """Reset all failed overlay items back to pending so they are retried."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE media_overlay_state SET status = 'pending', retry_count = 0, "
            "reason = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('failed', 'removal_failed')"
        )
        movie_show_count = cursor.rowcount
        cursor.execute(
            "UPDATE season_overlay_state SET status = 'pending', retry_count = 0, "
            "reason = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('failed', 'removal_failed')"
        )
        season_count = cursor.rowcount
        conn.commit()
        conn.close()
        total = movie_show_count + season_count
        logger.info(f"Failed stats reset: {total} item(s) set back to pending")
        return jsonify({'success': True, 'reset_count': total})
    except Exception as e:
        logger.error(f"Failed to reset failed stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================
# Poster Reset Routes (Phase 1 + Phase 2)
# ============================================

@overlay_bp.route('/api/overlays/reset/start', methods=['POST'])
def poster_reset_start():
    """
    Phase 1 — start a full scorched-earth reset job.
    Resets ALL library posters to clean TMDB originals.
    Body (optional JSON): {"reset_seasons": true}
    """
    try:
        from overlays.poster_reset import start_reset_job
        data = request.get_json(silent=True) or {}
        reset_seasons = data.get('reset_seasons', True)
        started = start_reset_job(ms_item_ids=None, reset_seasons=reset_seasons)
        if not started:
            return jsonify({'success': False, 'error': 'A reset job is already running'}), 409
        return jsonify({'success': True, 'message': 'Reset job started'})
    except Exception as e:
        logger.error(f"Failed to start poster reset: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/reset/selective', methods=['POST'])
def poster_reset_selective():
    """
    Phase 2 — reset only the selected items.
    Body: {"ms_item_ids": ["123", "456", ...], "reset_seasons": true}
    """
    try:
        from overlays.poster_reset import start_reset_job
        data = request.get_json(silent=True) or {}
        keys = data.get('ms_item_ids', []) or data.get('plex_rating_keys', [])
        if not keys:
            return jsonify({'success': False, 'error': 'No ms_item_ids provided'}), 400
        reset_seasons = data.get('reset_seasons', True)
        started = start_reset_job(ms_item_ids=keys, reset_seasons=reset_seasons)
        if not started:
            return jsonify({'success': False, 'error': 'A reset job is already running'}), 409
        return jsonify({'success': True, 'message': f'Reset job started for {len(keys)} item(s)'})
    except Exception as e:
        logger.error(f"Failed to start selective poster reset: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/reset/status', methods=['GET'])
def poster_reset_status():
    """Poll the status of the running/last reset job."""
    try:
        from overlays.poster_reset import get_job_status
        status = get_job_status()
        pct = 0
        if status['total'] > 0:
            pct = round((status['done'] + status['failed'] + status['skipped']) / status['total'] * 100)
        return jsonify({
            'success': True,
            'running': status['running'],
            'total': status['total'],
            'done': status['done'],
            'failed': status['failed'],
            'skipped': status['skipped'],
            'current': status['current'],
            'errors': status['errors'],
            'percent': pct,
            'cancelled': status['cancelled'],
        })
    except Exception as e:
        logger.error(f"Failed to get reset status: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/reset/cancel', methods=['POST'])
def poster_reset_cancel():
    """Cancel the running reset job."""
    try:
        from overlays.poster_reset import cancel_job, get_job_status
        status = get_job_status()
        if not status['running']:
            return jsonify({'success': False, 'error': 'No job running'}), 400
        cancel_job()
        return jsonify({'success': True, 'message': 'Cancel requested'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/reset/preview', methods=['GET'])
def poster_reset_preview():
    """
    Phase 2 — return all library items with poster thumbnail URLs for the review grid.
    """
    try:
        from overlays.poster_reset import get_preview_items
        items = get_preview_items()
        return jsonify({'success': True, 'count': len(items), 'items': items})
    except Exception as e:
        logger.error(f"Failed to get reset preview items: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
