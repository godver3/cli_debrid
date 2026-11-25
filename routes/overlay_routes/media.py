"""System status, movie/show/season listings and season overlays."""

from flask import jsonify, request, abort
from database.core import get_db_connection as _get_db_connection
from overlays.activity_logger import log_activity
from ._common import _get_overlay_manager, _serve_from_thumb_cache, _thumb_cache_path, _write_thumb_cache, invalidate_season_thumb_cache, logger, overlay_bp

# ============================================
# System Status Endpoints
# ============================================

@overlay_bp.route('/api/overlays/status', methods=['GET'])
def system_status():
    """Get overlay system status and statistics."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # ── Layout counts (tiny table, fast) ──────────────────────────────────
        cursor.execute('''
            SELECT
                COUNT(*)                                             AS total,
                SUM(CASE WHEN is_default = 1 THEN 1 ELSE 0 END)    AS active,
                SUM(CASE WHEN media_type = 'movie'  THEN 1 ELSE 0 END) AS movies,
                SUM(CASE WHEN media_type = 'tv'     THEN 1 ELSE 0 END) AS shows,
                SUM(CASE WHEN media_type = 'season' THEN 1 ELSE 0 END) AS seasons
            FROM overlay_layouts
        ''')
        lr = cursor.fetchone()
        total_layouts   = lr[0] or 0
        active_layouts  = lr[1] or 0
        layouts_movies  = lr[2] or 0
        layouts_shows   = lr[3] or 0
        layouts_seasons = lr[4] or 0

        # ── media_overlay_state aggregated in one pass ────────────────────────
        # One JOIN query that counts applied/failed per type in a single scan.
        cursor.execute('''
            SELECT
                m.type,
                o.status,
                COUNT(DISTINCT m.ms_item_id) AS cnt
            FROM media_overlay_state o
            JOIN media_items m ON m.id = o.media_item_id
            WHERE m.ms_item_id IS NOT NULL
              AND o.status IN ('applied', 'failed', 'removal_failed')
            GROUP BY m.type, o.status
        ''')
        _mos = {}  # (type, status) -> cnt
        for row in cursor.fetchall():
            _mos[(row[0], row[1])] = row[2]

        applied_movies = _mos.get(('movie',   'applied'), 0)
        applied_shows  = _mos.get(('episode', 'applied'), 0)
        failed_movies  = _mos.get(('movie',   'failed'),  0) + _mos.get(('movie',   'removal_failed'), 0)
        failed_shows   = _mos.get(('episode', 'failed'),  0) + _mos.get(('episode', 'removal_failed'), 0)

        # ── season_overlay_state aggregated in one pass ───────────────────────
        cursor.execute('''
            SELECT status, COUNT(*) FROM season_overlay_state GROUP BY status
        ''')
        _sos = dict(cursor.fetchall())
        applied_seasons = _sos.get('applied', 0)
        failed_seasons  = _sos.get('failed', 0) + _sos.get('removal_failed', 0)
        skipped_seasons = _sos.get('skipped', 0)

        applied_count = applied_movies + applied_shows + applied_seasons

        # ── Unprocessed movies ────────────────────────────────────────────────
        # Items with no overlay row at all, or in pending/removed/user_removed state
        cursor.execute('''
            SELECT COUNT(DISTINCT m.id)
            FROM media_items m
            LEFT JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE m.ms_item_id IS NOT NULL
              AND m.ms_item_id != ''
              AND m.state IN ('Collected', 'Upgrading')
              AND m.type = 'movie'
              AND (o.media_item_id IS NULL OR o.status IN ('pending', 'removed', 'user_removed'))
        ''')
        unprocessed_movies = cursor.fetchone()[0]

        # ── Unprocessed shows ─────────────────────────────────────────────────
        # A show is "unprocessed" when none of its episodes have an 'applied' row.
        # Use a subquery of applied show keys rather than a correlated NOT EXISTS.
        cursor.execute('''
            SELECT COUNT(DISTINCT m.ms_item_id)
            FROM media_items m
            WHERE m.ms_item_id IS NOT NULL
              AND m.ms_item_id != ''
              AND m.state IN ('Collected', 'Upgrading')
              AND m.type = 'episode'
              AND m.ms_item_id NOT IN (
                  SELECT DISTINCT m2.ms_item_id
                  FROM media_overlay_state o2
                  JOIN media_items m2 ON m2.id = o2.media_item_id
                  WHERE m2.type = 'episode'
                    AND o2.status = 'applied'
                    AND m2.ms_item_id IS NOT NULL
              )
        ''')
        unprocessed_shows = cursor.fetchone()[0]

        # ── Unprocessed seasons ───────────────────────────────────────────────
        # Count seasons registered in season_overlay_state that are pending/failed.
        # Using tv_shows.total_seasons (metadata count) was inaccurate because it
        # includes seasons from metadata that don't exist in the media server yet
        # (future seasons, uncollected seasons), inflating the "unprocessed" count.
        unprocessed_seasons = _sos.get('pending', 0) + _sos.get('failed', 0)

        unprocessed_count = unprocessed_movies + unprocessed_shows

        conn.close()

        return jsonify({
            'success': True,
            'layouts': {
                'total': total_layouts,
                'active': active_layouts,
                'layouts_movies': layouts_movies,
                'layouts_shows': layouts_shows,
                'layouts_seasons': layouts_seasons,
            },
            'overlays': {
                'applied': applied_count,
                'applied_movies': applied_movies,
                'applied_shows': applied_shows,
                'applied_seasons': applied_seasons,
                'unprocessed': unprocessed_count,
                'unprocessed_movies': unprocessed_movies,
                'unprocessed_shows': unprocessed_shows,
                'unprocessed_seasons': unprocessed_seasons,
                'failed': failed_movies + failed_shows + failed_seasons,
                'failed_movies': failed_movies,
                'failed_shows': failed_shows,
                'failed_seasons': failed_seasons,
                'skipped': _sos.get('skipped', 0)
            }
        })

    except Exception as e:
        logger.error(f"Failed to get system status: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================
# Media List Endpoints
# ============================================

def _cached_poster_path(imdb_id, tmdb_id, media_type='movie'):
    """Return TMDB poster path from cache (e.g. '/abc.jpg') or None."""
    try:
        from routes.poster_cache import get_cached_poster_url
        cached = (get_cached_poster_url(imdb_id, media_type) if imdb_id else None) or \
                 (get_cached_poster_url(tmdb_id, media_type) if tmdb_id else None)
        if cached and 'image.tmdb.org' in cached:
            parts = cached.split('/t/p/')
            if len(parts) > 1:
                full_path = parts[1]
                return ('/' + full_path.split('/', 1)[1]) if '/' in full_path else None
    except Exception:
        pass
    return None

@overlay_bp.route('/api/overlays/media/movies', methods=['GET'])
def list_movies():
    """Get list of movies with overlay status."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Get movies with overlay status.
        # Group by ms_item_id when available so split Plex items (each with a unique
        # ratingKey) appear as separate cards with their own overlays. Fall back to
        # imdb_id/tmdb_id grouping only for items not yet synced (ms_item_id is NULL).
        # Merged Plex items (multiple DB rows sharing the same ms_item_id) are still
        # collapsed to one card and show a version_count badge.
        cursor.execute('''
            SELECT
                MIN(m.id) AS id,
                m.title,
                m.year,
                m.imdb_id,
                m.tmdb_id,
                m.ms_item_id,
                COUNT(*) AS version_count,
                CASE
                    WHEN MAX(CASE WHEN o.status = 'applied'         THEN 1 ELSE 0 END) = 1 THEN 'applied'
                    WHEN MAX(CASE WHEN o.status = 'pending'         THEN 1 ELSE 0 END) = 1 THEN 'pending'
                    WHEN MAX(CASE WHEN o.status = 'analyzing'       THEN 1 ELSE 0 END) = 1 THEN 'analyzing'
                    WHEN MAX(CASE WHEN o.status = 'failed'          THEN 1 ELSE 0 END) = 1 THEN 'failed'
                    WHEN MAX(CASE WHEN o.status = 'removal_failed'  THEN 1 ELSE 0 END) = 1 THEN 'failed'
                    ELSE 'not_started'
                END AS overlay_status,
                MAX(CASE WHEN o.status IN ('failed', 'removal_failed') THEN o.reason ELSE NULL END) AS overlay_reason
            FROM media_items m
            LEFT JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE m.type = 'movie'
              AND m.state IN ('Collected', 'Upgrading')
            GROUP BY
                CASE
                    WHEN m.ms_item_id IS NOT NULL AND m.ms_item_id != ''
                    THEN m.ms_item_id
                    ELSE COALESCE(NULLIF(m.imdb_id, ''), NULLIF(m.tmdb_id, ''), m.title || CAST(m.year AS TEXT))
                END
            ORDER BY m.title
        ''')

        items = []
        for row in cursor.fetchall():
            items.append({
                'id': row['id'],
                'title': row['title'],
                'year': row['year'],
                'imdb_id': row['imdb_id'],
                'tmdb_id': row['tmdb_id'],
                'ms_item_id': row['ms_item_id'],
                'overlay_status': row['overlay_status'],
                'overlay_reason': row['overlay_reason'],
                'version_count': row['version_count'],
                'poster_path': _cached_poster_path(row['imdb_id'], row['tmdb_id'], 'movie'),
            })

        conn.close()

        return jsonify({
            'success': True,
            'count': len(items),
            'items': items
        })

    except Exception as e:
        logger.error(f"Failed to list movies: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/media/shows', methods=['GET'])
def list_shows():
    """Get list of TV shows with overlay status."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Get TV shows with overlay status — driven by media_items so newly-added
        # shows appear immediately without waiting for the tv_shows background task.
        # tv_shows is LEFT JOINed for metadata (title/year fallback) only.
        cursor.execute('''
            SELECT
                ep.imdb_id,
                ep.tmdb_id,
                COALESCE(ts.title, ep.title)           AS title,
                COALESCE(ts.year,  ep.year)            AS year,
                ep.ms_item_id,
                ep.collected_episodes,
                ep.total_episodes,
                ep.rep_id,
                CASE WHEN o.status = 'removal_failed' THEN 'failed'
                     ELSE COALESCE(o.status, 'not_started') END AS overlay_status,
                CASE WHEN o.status IN ('failed', 'removal_failed') THEN o.reason ELSE NULL END AS overlay_reason,
                ss.unprocessed_season_nums
            FROM (
                SELECT
                    COALESCE(NULLIF(imdb_id, ''), NULLIF(tmdb_id, ''), title || CAST(year AS TEXT)) AS show_key,
                    MAX(ms_item_id)        AS ms_item_id,
                    MIN(id)                AS rep_id,
                    MAX(imdb_id)           AS imdb_id,
                    MAX(tmdb_id)           AS tmdb_id,
                    MAX(title)             AS title,
                    MAX(year)              AS year,
                    COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading')
                          THEN season_number || '-' || episode_number END) AS collected_episodes,
                    COUNT(DISTINCT season_number || '-' || episode_number) AS total_episodes
                FROM media_items
                WHERE type = 'episode'
                  AND (ghostlisted = 0 OR ghostlisted IS NULL)
                GROUP BY show_key
            ) ep
            LEFT JOIN tv_shows ts ON ts.imdb_id = ep.imdb_id
                                  OR ts.tmdb_id = ep.tmdb_id
            LEFT JOIN media_overlay_state o ON ep.rep_id = o.media_item_id
            LEFT JOIN (
                SELECT f.show_ms_item_id,
                       GROUP_CONCAT(f.season_number) AS unprocessed_season_nums
                FROM season_overlay_state f
                WHERE f.status IN ('not_started', 'pending', 'failed', 'removal_failed')
                  AND NOT EXISTS (
                      SELECT 1 FROM season_overlay_state a
                      WHERE a.show_ms_item_id = f.show_ms_item_id
                        AND a.season_number = f.season_number
                        AND a.status = 'applied'
                  )
                GROUP BY f.show_ms_item_id
            ) ss ON ss.show_ms_item_id = ep.ms_item_id
            WHERE ep.collected_episodes > 0
            ORDER BY COALESCE(ts.title, ep.title)
        ''')

        items = []
        for row in cursor.fetchall():
            raw = row['unprocessed_season_nums']
            if raw:
                seasons_unprocessed = sorted(set(
                    int(x) for x in raw.split(',') if x.strip().lstrip('-').isdigit()
                ))
            else:
                seasons_unprocessed = []
            items.append({
                'id': row['rep_id'],
                'title': row['title'],
                'year': row['year'],
                'imdb_id': row['imdb_id'],
                'tmdb_id': row['tmdb_id'],
                'ms_item_id': row['ms_item_id'],
                'overlay_status': row['overlay_status'],
                'overlay_reason': row['overlay_reason'],
                'collected_episodes': row['collected_episodes'] or 0,
                'total_episodes': row['total_episodes'] or 0,
                'poster_path': _cached_poster_path(row['imdb_id'], row['tmdb_id'], 'show'),
                'seasons_unprocessed': seasons_unprocessed,
            })

        conn.close()

        return jsonify({
            'success': True,
            'count': len(items),
            'items': items
        })

    except Exception as e:
        logger.error(f"Failed to list shows: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================
# Season Overlay Endpoints
# ============================================

@overlay_bp.route('/api/overlays/poster_applied/<ms_item_id>', methods=['GET'])
def poster_applied(ms_item_id):
    """
    Proxy the currently-selected (overlay-applied) Plex poster for a movie or show.
    Returns a 300px-wide JPEG thumbnail. Results are cached to disk for 24 hours so
    that the overlay page never floods Plex with hundreds of simultaneous downloads.
    """
    from io import BytesIO
    from flask import Response
    cache_path = _thumb_cache_path(ms_item_id)
    cached = _serve_from_thumb_cache(cache_path)
    if cached:
        return cached
    try:
        manager = _get_overlay_manager()
        img = manager.client.download_poster(ms_item_id)
        if not img:
            abort(404)
        thumb_w = 300
        w, h = img.size
        if w > thumb_w:
            thumb_h = int(h * thumb_w / w)
            img = img.resize((thumb_w, thumb_h))
        buf = BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=85)
        jpeg_bytes = buf.getvalue()
        _write_thumb_cache(cache_path, jpeg_bytes)
        return Response(jpeg_bytes, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as e:
        logger.debug(f"poster_applied {ms_item_id}: {e}")
        abort(404)

@overlay_bp.route('/api/overlays/seasons/<season_key>/thumb', methods=['GET'])
def season_thumb(season_key):
    """Proxy season thumbnail from the media server to the browser (disk-cached 24 h)."""
    from io import BytesIO
    from flask import Response
    cache_path = _thumb_cache_path(season_key, prefix='season_')
    cached = _serve_from_thumb_cache(cache_path)
    if cached:
        return cached
    try:
        manager = _get_overlay_manager()
        img = manager.client.download_poster(season_key)
        if not img:
            abort(404)
        buf = BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=85)
        jpeg_bytes = buf.getvalue()
        _write_thumb_cache(cache_path, jpeg_bytes)
        return Response(jpeg_bytes, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as e:
        logger.debug(f"season_thumb {season_key}: {e}")
        abort(404)

@overlay_bp.route('/api/overlays/seasons/<show_plex_rating_key>', methods=['GET'])
def list_seasons(show_plex_rating_key):
    """
    Fetch all Plex seasons for a show and return them with their overlay status.

    Query params:
        layout_id (optional): layout to pre-select in the UI
    """
    try:
        manager = _get_overlay_manager()
        seasons = manager.client.get_show_seasons(show_plex_rating_key)

        if not seasons:
            return jsonify({'success': True, 'seasons': []}), 200

        # Attach stored overlay status for each season
        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT season_ms_item_id, status, reason, overlay_applied_at '
            'FROM season_overlay_state WHERE show_ms_item_id = ?',
            (show_plex_rating_key,)
        )
        state_map = {r['season_ms_item_id']: dict(r) for r in cursor.fetchall()}
        conn.close()

        result = []
        for s in seasons:
            rk = s['ratingKey']
            state = state_map.get(rk, {})
            result.append({
                'season_ms_item_id': rk,
                'title': s['title'],
                'season_number': s['index'],
                'thumb_url': s.get('thumb_url'),
                'overlay_status': state.get('status', 'not_started'),
                'overlay_applied_at': state.get('overlay_applied_at'),
                'reason': state.get('reason'),
            })

        return jsonify({'success': True, 'seasons': result})

    except Exception as e:
        logger.error(f"Failed to list seasons for show {show_plex_rating_key}: {e}",
                     exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/seasons/<show_plex_rating_key>/generate', methods=['POST'])
def generate_season_overlays(show_plex_rating_key):
    """
    Generate overlays for all (or selected) seasons of a show.

    Body JSON:
        season_keys (list, optional): specific season rating keys; if absent → all seasons
        layout_id (int, optional): explicit layout ID
        force (bool, optional): force regeneration even if already applied
    """
    try:
        data = request.get_json() or {}
        season_keys = data.get('season_keys')   # None → all seasons
        layout_id = data.get('layout_id')
        force = bool(data.get('force', False))

        manager = _get_overlay_manager()
        seasons = manager.client.get_show_seasons(show_plex_rating_key)

        if season_keys:
            seasons = [s for s in seasons if s['ratingKey'] in season_keys]

        if not seasons:
            return jsonify({'success': True, 'message': 'No seasons found', 'results': []})

        # Look up show title for failure labels in activity log
        _show_title = show_plex_rating_key
        try:
            _st_conn = _get_db_connection()
            _st_row = _st_conn.execute(
                "SELECT title FROM media_items WHERE ms_item_id=? AND type='episode' LIMIT 1",
                (show_plex_rating_key,)).fetchone()
            _st_conn.close()
            if _st_row:
                _show_title = _st_row['title']
        except Exception:
            pass

        results = []
        applied = failed = skipped = 0

        for s in seasons:
            res = manager.generate_season_overlay(
                show_plex_rating_key=show_plex_rating_key,
                season_plex_rating_key=s['ratingKey'],
                season_number=s['index'],
                force=force,
                layout_id=layout_id,
            )
            results.append({
                'season_number': s['index'],
                'title': s['title'],
                'status': res.get('status'),
                'message': res.get('message'),
            })
            status = res.get('status', 'error')
            if status == 'applied':
                applied += 1
            elif status == 'skipped':
                skipped += 1
            else:
                failed += 1

        _fail_names = [f"{_show_title} - {r['title']}" for r in results
                       if r.get('status') not in ('applied', 'skipped', 'analyzing')]
        _fail_stats = {'failures': _fail_names} if 0 < failed <= 10 else {}
        log_activity('season_generate',
                     title=f"Season overlays (show {show_plex_rating_key}): {applied} applied, {failed} failed, {skipped} skipped",
                     stats={'show_key': show_plex_rating_key, 'total': len(seasons),
                            'applied': applied, 'failed': failed, 'skipped': skipped,
                            **_fail_stats})
        return jsonify({
            'success': True,
            'total': len(seasons),
            'applied': applied,
            'skipped': skipped,
            'failed': failed,
            'results': results,
        })

    except Exception as e:
        logger.error(f"Failed to generate season overlays for show {show_plex_rating_key}: {e}",
                     exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route(
    '/api/overlays/seasons/<show_plex_rating_key>/<season_plex_rating_key>/regenerate',
    methods=['POST'])
def regenerate_season_overlay(show_plex_rating_key, season_plex_rating_key):
    """
    Force-regenerate the overlay for a single season using the current Plex poster.

    Body JSON:
        season_number (int, required)
        layout_id (int, optional)
    """
    try:
        data = request.get_json() or {}
        season_number = data.get('season_number', 0)
        layout_id = data.get('layout_id')

        manager = _get_overlay_manager()
        res = manager.generate_season_overlay(
            show_plex_rating_key=show_plex_rating_key,
            season_plex_rating_key=season_plex_rating_key,
            season_number=season_number,
            force=True,
            layout_id=layout_id,
            force_fresh_poster=True,
        )
        if res.get('success'):
            log_activity('season_regenerate',
                         title=f"Season {season_number} regenerated (show {show_plex_rating_key})",
                         stats={'show_key': show_plex_rating_key,
                                'season_key': season_plex_rating_key, 'season_number': season_number})
        return jsonify({
            'success': res.get('success', False),
            'status': res.get('status'),
            'message': res.get('message'),
        })

    except Exception as e:
        logger.error(
            f"Failed to regenerate season overlay {season_plex_rating_key}: {e}",
            exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route(
    '/api/overlays/seasons/<show_plex_rating_key>/<season_plex_rating_key>/remove',
    methods=['POST'])
def remove_season_overlay(show_plex_rating_key, season_plex_rating_key):
    """
    Remove overlay from a single season and restore its original poster.

    Body JSON:
        season_number (int, required)
    """
    try:
        data = request.get_json() or {}
        season_number = data.get('season_number', 0)

        manager = _get_overlay_manager()
        res = manager.remove_season_overlay(
            show_plex_rating_key=show_plex_rating_key,
            season_plex_rating_key=season_plex_rating_key,
            season_number=season_number,
            user_initiated=True,
        )
        if res.get('success'):
            invalidate_season_thumb_cache(season_plex_rating_key)
            log_activity('season_remove',
                         title=f"Season {season_number} overlay removed (show {show_plex_rating_key})",
                         stats={'show_key': show_plex_rating_key,
                                'season_key': season_plex_rating_key, 'season_number': season_number})
        return jsonify({
            'success': res.get('success', False),
            'status': res.get('status'),
            'message': res.get('message'),
        })

    except Exception as e:
        logger.error(
            f"Failed to remove season overlay {season_plex_rating_key}: {e}",
            exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
