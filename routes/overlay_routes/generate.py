"""Overlay generation: single, poster choice, batch, all, status."""

import sqlite3
from flask import jsonify, request, send_file, abort
from database.core import get_db_connection as _get_db_connection
from overlays.activity_logger import log_activity
from overlays.utils import is_jellyfin_mode
from utilities.settings import get_setting
from ._common import _get_layout_manager, _get_overlay_manager, invalidate_poster_thumb_cache, logger, overlay_bp

# ============================================
# Overlay Generation Endpoints
# ============================================

@overlay_bp.route('/api/overlays/generate/<int:media_item_id>', methods=['POST'])
def generate_overlay(media_item_id):
    """
    Generate overlay for a specific media item.

    Request body (optional):
        layout_id: Template ID to use (optional)
        force: Force regeneration (optional, default: false)
    """
    try:
        data = request.get_json() or {}

        manager = _get_overlay_manager()
        result = manager.generate_overlay_for_item(
            media_item_id,
            force=data.get('force', False),
            layout_id=data.get('layout_id')
        )

        _st = result.get('status', 'error')
        if _st not in ('skipped',):
            log_activity('generate', result='success' if result.get('success') else 'failed',
                         title=f"Generate overlay: {result.get('message', _st)} (item {media_item_id})",
                         stats={'media_item_id': media_item_id, 'status': _st})
        status_code = 200 if result['success'] else 400
        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"Failed to generate overlay for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/generate/regenerate/<int:media_item_id>', methods=['POST'])
def regenerate_single_overlay(media_item_id):
    """
    Regenerate overlay for a single item using the currently-selected Plex poster as the base.
    This deletes the existing backup so a fresh poster is downloaded from Plex (the user's
    current selection), then re-applies the overlay on top of it.
    """
    try:
        data = request.get_json() or {}
        manager = _get_overlay_manager()
        result = manager.generate_overlay_for_item(
            media_item_id,
            force=True,
            layout_id=data.get('layout_id'),
            force_fresh_poster=True
        )
        _st = result.get('status', 'error')
        log_activity('regenerate', result='success' if result.get('success') else 'failed',
                     title=f"Regenerate overlay: {result.get('message', _st)} (item {media_item_id})",
                     stats={'media_item_id': media_item_id, 'status': _st})
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code
    except Exception as e:
        logger.error(f"Failed to regenerate overlay for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/poster_list/<int:media_item_id>', methods=['GET'])
def get_item_poster_list(media_item_id):
    """Return list of available clean (non-overlay) Plex posters for a media item."""
    try:
        if is_jellyfin_mode():
            return jsonify({'success': False, 'error': 'Poster picker only available for Plex'}), 400
        conn = _get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT ms_item_id, title FROM media_items WHERE id = ?', (media_item_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row['ms_item_id']:
            return jsonify({'success': False, 'error': 'Item not found or no Plex ID'}), 404
        ms_item_id = row['ms_item_id']
        plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        from overlays.plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)
        all_posters = client.get_poster_list(ms_item_id)
        # Filter out upload:// (our overlays) to prevent overlay-on-overlay
        clean = [p for p in all_posters if not p.get('ratingKey', '').startswith('upload://')]
        posters = [
            {
                'index': idx,
                'rating_key': p.get('ratingKey', ''),
                'selected': bool(p.get('selected')),
                'provider': p.get('provider', ''),
                'thumb_url': f"/api/overlays/poster_thumb/{ms_item_id}/{idx}",
            }
            for idx, p in enumerate(clean)
        ]
        return jsonify({'success': True, 'posters': posters, 'ms_item_id': ms_item_id})
    except Exception as e:
        logger.error(f"Failed to get poster list for item {media_item_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/poster_thumb/<ms_item_id>/<int:index>', methods=['GET'])
def proxy_poster_thumb(ms_item_id, index):
    """Proxy a specific Plex poster thumbnail to the browser."""
    try:
        if is_jellyfin_mode():
            abort(400)
        from io import BytesIO
        plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        from overlays.plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)
        all_posters = client.get_poster_list(ms_item_id)
        clean = [p for p in all_posters if not p.get('ratingKey', '').startswith('upload://')]
        logger.debug(f"poster_thumb {ms_item_id}/{index}: {len(clean)} clean posters available")
        if index >= len(clean):
            logger.warning(f"poster_thumb: index {index} out of range ({len(clean)} posters)")
            abort(404)
        poster = clean[index]
        rating_key = poster.get('ratingKey', '')
        logger.debug(f"poster_thumb: fetching ratingKey={rating_key!r} thumb={poster.get('thumb','')!r}")
        # Use download_poster_by_rating_key which handles all ratingKey formats and auth
        img_bytes = client.download_poster_by_rating_key(ms_item_id, rating_key)
        if not img_bytes:
            logger.warning(f"poster_thumb: download_poster_by_rating_key returned empty for {rating_key!r}")
            abort(404)
        return send_file(BytesIO(img_bytes), mimetype='image/jpeg')
    except Exception as e:
        logger.warning(f"Failed to proxy poster thumb {ms_item_id}/{index}: {e}")
        abort(404)

@overlay_bp.route('/api/overlays/apply_poster/<int:media_item_id>', methods=['POST'])
def apply_specific_poster(media_item_id):
    """Download a chosen Plex poster, save as backup, and apply overlay on top."""
    try:
        if is_jellyfin_mode():
            return jsonify({'success': False, 'error': 'Poster picker only available for Plex'}), 400
        data = request.get_json() or {}
        poster_rating_key = data.get('rating_key')
        if not poster_rating_key:
            return jsonify({'success': False, 'error': 'rating_key required'}), 400
        # Safety: never allow using an uploaded (overlay) poster as source
        if poster_rating_key.startswith('upload://'):
            return jsonify({'success': False, 'error': 'Cannot use an uploaded poster as source'}), 400
        conn = _get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT ms_item_id, title FROM media_items WHERE id = ?', (media_item_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row['ms_item_id']:
            return jsonify({'success': False, 'error': 'Item not found or no Plex ID'}), 404
        ms_item_id = row['ms_item_id']
        plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        from overlays.plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)
        # Download the specific clean poster
        poster_bytes = client.download_poster_by_rating_key(ms_item_id, poster_rating_key)
        if not poster_bytes:
            return jsonify({'success': False, 'error': 'Failed to download poster from Plex'}), 500
        # Save as backup so generate_overlay_for_item picks it up as the source
        from overlays.cache_cleanup import PosterCacheManager
        PosterCacheManager(None).backup_poster(ms_item_id, poster_bytes)
        # Reset overlay state to 'pending' so the generate path uses the backup directly
        # (avoids the 'removed' branch which would try to compare with current Plex poster)
        _conn = _get_db_connection()
        _conn.execute(
            "UPDATE media_overlay_state SET status='pending', reason='poster pick',"
            " updated_at=CURRENT_TIMESTAMP WHERE media_item_id=?",
            (media_item_id,)
        )
        _conn.commit()
        _conn.close()
        # Apply overlay on top of the chosen poster
        manager = _get_overlay_manager()
        result = manager.generate_overlay_for_item(media_item_id, force=True)
        if result.get('success'):
            invalidate_poster_thumb_cache(ms_item_id)
        log_activity('poster_pick',
                     result='success' if result.get('success') else 'failed',
                     title=f"Poster picked: {row['title']} (item {media_item_id})",
                     stats={'media_item_id': media_item_id})
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.error(f"Failed to apply specific poster for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/generate/batch', methods=['POST'])
def batch_generate_overlays():
    """Start a background generate job for selected items."""
    import threading
    from overlays.overlay_manager import get_generate_status, _update_gen
    from datetime import datetime as _dt
    try:
        data = request.get_json()
        if not data or 'item_ids' not in data:
            return jsonify({'success': False, 'error': 'No item_ids provided'}), 400
        item_ids = data['item_ids']
        if not isinstance(item_ids, list) or not item_ids:
            return jsonify({'success': False, 'error': 'item_ids must be a non-empty array'}), 400
        if get_generate_status().get('running'):
            return jsonify({'success': False, 'error': 'A generate job is already running'}), 409

        label = f"Generate selected ({len(item_ids)} item{'s' if len(item_ids) != 1 else ''})"
        _update_gen(running=True, total=len(item_ids), done=0, applied=0, failed=0, skipped=0,
                    current='', errors=[], label=label,
                    started_at=_dt.utcnow().isoformat(), finished_at=None)

        def _bg():
            from overlays.overlay_manager import _update_gen
            from datetime import datetime as _dt2
            try:
                layout_id = data.get('layout_id')
                force = data.get('force', False)
                layout_media_type = None
                if layout_id:
                    lm = _get_layout_manager()
                    layout_obj = lm.get_layout(layout_id)
                    if layout_obj:
                        layout_media_type = layout_obj.get('media_type')

                manager = _get_overlay_manager()

                if layout_media_type == 'season':
                    # Resolve the selected item_ids → ms_item_ids (show-level Plex keys)
                    _sel_conn = _get_db_connection()
                    _sel_cur  = _sel_conn.cursor()
                    _sel_cur.execute(
                        f"SELECT DISTINCT ms_item_id FROM media_items "
                        f"WHERE type='episode' AND ms_item_id IS NOT NULL "
                        f"AND id IN ({','.join('?' * len(item_ids))})",
                        item_ids)
                    _selected_show_keys = {r[0] for r in _sel_cur.fetchall()}
                    _sel_conn.close()

                    all_seasons = []
                    try:
                        if is_jellyfin_mode():
                            for _sk in _selected_show_keys:
                                all_seasons.extend(manager.client.get_show_seasons(_sk) or [])
                        else:
                            # Fetch all sections, collect seasons, filter to selected shows only
                            sections = manager.plex.get_all_sections()
                            for section in sections:
                                if section.get('type') == 'show':
                                    for _s in manager.plex.get_all_seasons_for_section(section['key']):
                                        if _s.get('parentRatingKey', '') in _selected_show_keys:
                                            all_seasons.append(_s)
                    except Exception as _sec_exc:
                        logger.warning(f"Failed to fetch seasons from media server: {_sec_exc}")

                    # Correct the total so the progress bar reflects seasons, not shows
                    from overlays.overlay_manager import get_generate_status as _ggs
                    if _ggs().get('running'):
                        _update_gen(total=len(all_seasons), done=0)

                    # Build show-title lookup for progress display and failure names
                    _show_title_map = {}
                    try:
                        _stm_conn = _get_db_connection()
                        for _stm_row in _stm_conn.execute(
                                "SELECT DISTINCT ms_item_id, title FROM media_items "
                                "WHERE type='episode' AND ms_item_id IS NOT NULL AND ms_item_id != ''"):
                            if _stm_row['ms_item_id']:
                                _show_title_map[_stm_row['ms_item_id']] = _stm_row['title']
                        _stm_conn.close()
                    except Exception:
                        pass

                    season_results = []
                    for s in all_seasons:
                        show_key = s.get('parentRatingKey', '')
                        season_key = s.get('ratingKey', '')
                        season_num = s.get('index', 0)
                        season_title = s.get('title', f'Season {season_num}')
                        if not show_key or not season_key:
                            continue
                        _st = 'failed'
                        _show_name = _show_title_map.get(show_key, show_key)
                        # Pre-check: skip already-applied seasons without hitting the media server
                        if not force:
                            _pre_state = manager._get_season_overlay_state(season_key)
                            if _pre_state and _pre_state.get('status') in ('applied', 'no_poster', 'removed'):
                                _st = 'skipped'
                                season_results.append({'show_key': show_key, 'season_number': season_num,
                                                       'status': _st, 'label': f"{_show_name} - {season_title}"})
                                _gs2 = _ggs()
                                if _gs2.get('running'):
                                    _update_gen(done=_gs2['done'] + 1,
                                                skipped=_gs2['skipped'] + 1,
                                                current=f"{_show_name} - {season_title}")
                                continue
                        try:
                            res = manager.generate_season_overlay(
                                show_plex_rating_key=show_key,
                                season_plex_rating_key=season_key,
                                season_number=season_num,
                                force=force,
                                layout_id=layout_id,
                            )
                            _st = res.get('status', 'failed')
                            season_results.append({'show_key': show_key, 'season_number': season_num,
                                                   'status': _st, 'label': f"{_show_name} - {season_title}"})
                        except Exception as _se:
                            season_results.append({'show_key': show_key, 'season_number': season_num,
                                                   'status': 'failed', 'label': f"{_show_name} - {season_title}"})
                            logger.warning(f"Season overlay generation failed for show {show_key} season {season_num}: {_se}")

                        # Per-item progress update
                        _gs2 = _ggs()
                        if _gs2.get('running'):
                            _update_gen(
                                done=_gs2['done'] + 1,
                                applied=_gs2['applied'] + (1 if _st == 'applied' else 0),
                                failed=_gs2['failed'] + (1 if _st not in ('applied', 'skipped', 'analyzing') else 0),
                                skipped=_gs2['skipped'] + (1 if _st in ('skipped', 'analyzing') else 0),
                                current=f"{_show_name} - {season_title}",
                            )

                    _s_applied = sum(1 for r in season_results if r.get('status') == 'applied')
                    _s_failed  = sum(1 for r in season_results if r.get('status') not in ('applied', 'skipped', 'analyzing'))
                    _s_fail_names = [r['label'] for r in season_results
                                     if r.get('status') not in ('applied', 'skipped', 'analyzing')]
                    _s_fail_stats = {'failures': _s_fail_names} if 0 < _s_failed <= 10 else {}
                    log_activity('generate',
                                 title=f"Generate selected (season layout): {_s_applied} applied, {_s_failed} failed",
                                 stats={'applied': _s_applied, 'failed': _s_failed,
                                        'total': len(season_results), **_s_fail_stats})
                else:
                    results = manager.batch_generate_overlays(item_ids, force=force, layout_id=layout_id)
                    _applied = results.get('applied', 0) if isinstance(results, dict) else 0
                    _failed  = results.get('failed', 0)  if isinstance(results, dict) else 0
                    log_activity('generate',
                                 title=f"Generate selected: {_applied} applied, {_failed} failed",
                                 stats={'applied': _applied, 'failed': _failed, 'total': len(item_ids)})
            except Exception as _e:
                logger.error(f"Batch generate background job failed: {_e}", exc_info=True)
                from overlays.overlay_manager import get_generate_status
                s = get_generate_status()
                _update_gen(errors=list(s.get('errors', [])) + [str(_e)])
            finally:
                _update_gen(running=False, current='', finished_at=_dt2.utcnow().isoformat())

        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({'success': True, 'started': True, 'total': len(item_ids), 'label': label})

    except Exception as e:
        logger.error(f"Failed to batch generate overlays: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def _estimate_generate_total(data: dict) -> int:
    """Quickly estimate how many items a generate-all job will process."""
    try:
        media_type = data.get('media_type', 'movie')
        db_type = 'episode' if media_type == 'tv' else media_type
        conn = _get_db_connection()
        cursor = conn.cursor()
        if db_type == 'episode':
            cursor.execute(
                "SELECT COUNT(DISTINCT ms_item_id) FROM media_items "
                "WHERE type='episode' AND state IN ('Collected','Upgrading') AND ms_item_id IS NOT NULL"
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM media_items "
                "WHERE type=? AND state IN ('Collected','Upgrading') AND ms_item_id IS NOT NULL",
                (db_type,)
            )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0

def _run_generate_all_bg(data: dict):
    """Background worker for generate/regenerate-all. No Flask context needed."""
    from overlays.overlay_manager import _update_gen, get_generate_status
    from datetime import datetime as _dt
    try:
        _run_generate_all_work(data)
    except Exception as _e:
        logger.error(f"Background generate-all job failed: {_e}", exc_info=True)
        s = get_generate_status()
        _update_gen(errors=list(s.get('errors', [])) + [str(_e)])
    finally:
        _update_gen(running=False, current='',
                    finished_at=_dt.utcnow().isoformat())

def _run_generate_all_work(data: dict):
    """
    Core worker for generate_all / regenerate_all — no Flask context required.
    Updates the shared _gen_job state as items are processed.
    """
    media_type = data.get('media_type')
    layout_id = data.get('layout_id')
    force = data.get('force', False)
    force_fresh_poster = data.get('force_fresh_poster', False)
    seasons_only = data.get('seasons_only', False)

    if not media_type:
        return

    # Determine what the selected layout is for, so we don't apply the wrong
    # layout type to the wrong items (e.g. a season layout to show posters).
    layout_media_type = None
    if layout_id:
        lm = _get_layout_manager()
        layout_obj = lm.get_layout(layout_id)
        if layout_obj:
            layout_media_type = layout_obj.get('media_type')  # 'movie', 'tv', 'season', 'both'

    # If a season layout is selected on the TV tab, only generate season overlays.
    season_layout_only = (layout_media_type == 'season') or seasons_only
    # If a tv/movie layout is selected, skip season generation (use None to let it auto-select).
    skip_seasons = (layout_media_type in ('tv', 'movie'))

    manager = _get_overlay_manager()
    results = []
    item_ids = []

    if not season_layout_only:
        conn = _get_db_connection()
        cursor = conn.cursor()
        # Map UI 'tv' to DB 'episode'; 'movie' stays 'movie'
        db_type = 'episode' if media_type == 'tv' else media_type
        # In Jellyfin mode, exclude legacy Plex integer IDs (they haven't been synced yet)
        if is_jellyfin_mode():
            _has_valid_id = "(ms_item_id IS NOT NULL AND LENGTH(ms_item_id) >= 20)"
        else:
            _has_valid_id = "ms_item_id IS NOT NULL"
        # For shows: use one representative episode per show (min id), deduplicated by ms_item_id
        # For movies: just all movies with a ms_item_id
        if db_type == 'episode':
            cursor.execute(
                f"""SELECT MIN(id) as id FROM media_items
                   WHERE type = 'episode' AND state IN ('Collected', 'Upgrading') AND {_has_valid_id}
                   GROUP BY ms_item_id""",
            )
        else:
            cursor.execute(
                f"SELECT id FROM media_items WHERE type = ? AND state IN ('Collected', 'Upgrading') AND {_has_valid_id}",
                (db_type,)
            )
        item_ids = [row[0] for row in cursor.fetchall()]
        conn.close()

        if item_ids:
            results = manager.batch_generate_overlays(
                item_ids, force=force, layout_id=layout_id,
                force_fresh_poster=force_fresh_poster)

    # For TV: also generate season overlays for all processed shows
    # Skip if a non-season layout was explicitly selected (user wants shows only).
    season_results = []
    if media_type == 'tv' and not skip_seasons:
        # Use season layout_id if that's what was selected; otherwise pass None (auto-select)
        season_layout_id = layout_id if season_layout_only else None

        # Get all seasons for all tracked shows.
        # For Plex: use the batch sections API.
        # For Jellyfin: get unique show keys from DB, fetch seasons per show.
        all_seasons = []
        try:
            if is_jellyfin_mode():
                _conn_s2 = _get_db_connection()
                _cur_s2  = _conn_s2.cursor()
                _cur_s2.execute(
                    "SELECT DISTINCT ms_item_id FROM media_items "
                    "WHERE type='episode' AND ms_item_id IS NOT NULL AND ms_item_id != '' "
                    "AND LENGTH(ms_item_id) >= 20 "
                    "AND state IN ('Collected', 'Upgrading')"
                )
                _all_show_keys = [r[0] for r in _cur_s2.fetchall()]
                _conn_s2.close()
                for _sk in _all_show_keys:
                    for _season in (manager.client.get_show_seasons(_sk) or []):
                        # Inject parentRatingKey so the loop below can read show_key
                        _season.setdefault('parentRatingKey', _sk)
                        all_seasons.append(_season)
            else:
                sections = manager.plex.get_all_sections()
                for section in sections:
                    if section.get('type') == 'show':
                        all_seasons.extend(
                            manager.plex.get_all_seasons_for_section(section['key'])
                        )
        except Exception as _sec_exc:
            logger.warning(f"Failed to fetch seasons from media server: {_sec_exc}")

        # Build show-title lookup for progress display and failure names
        _show_title_map = {}
        try:
            _stm_conn = _get_db_connection()
            for _stm_row in _stm_conn.execute(
                    "SELECT DISTINCT ms_item_id, title FROM media_items "
                    "WHERE type='episode' AND ms_item_id IS NOT NULL AND ms_item_id != ''"):
                if _stm_row['ms_item_id']:
                    _show_title_map[_stm_row['ms_item_id']] = _stm_row['title']
            _stm_conn.close()
        except Exception:
            pass

        # Reset progress for the season phase so the bar doesn't stay frozen at
        # 100% (shows done) while seasons are processing.  Always do this when
        # there are seasons to process — not just for season-only jobs.
        if all_seasons:
            from overlays.overlay_manager import _update_gen, get_generate_status as _ggs
            if _ggs().get('running'):
                _update_gen(
                    total=len(all_seasons), done=0,
                    label=f"Applying season overlays\u2026 ({len(all_seasons)} seasons)",
                    current='',
                )

        for s in all_seasons:
            show_key = s.get('parentRatingKey', '')
            season_key = s.get('ratingKey', '')
            season_num = s.get('index', 0)
            season_title = s.get('title', f'Season {season_num}')
            if not show_key or not season_key:
                continue
            _show_name = _show_title_map.get(show_key, show_key)
            # Pre-check: skip already-applied seasons without hitting the media server
            if not force:
                _pre_state = manager._get_season_overlay_state(season_key)
                if _pre_state and _pre_state.get('status') == 'applied':
                    _st = 'skipped'
                    season_results.append({'show_key': show_key, 'season_number': season_num,
                                           'status': _st, 'label': f"{_show_name} - {season_title}"})
                    from overlays.overlay_manager import _update_gen, get_generate_status as _ggs2
                    _s2 = _ggs2()
                    if _s2.get('running'):
                        _update_gen(done=_s2['done'] + 1, skipped=_s2['skipped'] + 1,
                                    current=f"{_show_name} - {season_title}")
                    continue
            try:
                res = manager.generate_season_overlay(
                    show_plex_rating_key=show_key,
                    season_plex_rating_key=season_key,
                    season_number=season_num,
                    force=force,
                    layout_id=season_layout_id,
                    force_fresh_poster=force_fresh_poster,
                )
                _st = res.get('status')
                season_results.append({
                    'show_key': show_key,
                    'season_number': season_num,
                    'status': _st,
                    'label': f"{_show_name} - {season_title}",
                })
            except Exception as _se:
                _st = 'failed'
                season_results.append({'show_key': show_key, 'season_number': season_num,
                                       'status': _st, 'label': f"{_show_name} - {season_title}"})
                logger.warning(f"Season overlay generation failed for show {show_key} season {season_num}: {_se}")

            # Update progress counter so the frontend shows real-time season progress
            from overlays.overlay_manager import _update_gen, get_generate_status as _ggs2
            _s2 = _ggs2()
            if _s2.get('running'):
                _update_gen(
                    done=_s2['done'] + 1,
                    applied=_s2['applied'] + (1 if _st == 'applied' else 0),
                    failed=_s2['failed'] + (1 if _st not in ('applied', 'skipped', 'analyzing') else 0),
                    skipped=_s2['skipped'] + (1 if _st in ('skipped', 'analyzing') else 0),
                    current=f"{_show_name} - {season_title}",
                )

    _applied   = results.get('applied', 0) if isinstance(results, dict) else 0
    _failed    = results.get('failed', 0)  if isinstance(results, dict) else 0
    _s_applied = sum(1 for r in season_results if r.get('status') == 'applied') if season_results else 0
    _s_failed  = sum(1 for r in season_results if r.get('status') not in ('applied', 'skipped', 'analyzing')) if season_results else 0
    _action = 'regenerate_all' if force else 'generate_all'
    if season_layout_only:
        _title = (f"{'Regenerate' if force else 'Generate'} all seasons ({media_type}): "
                  f"{_s_applied} applied, {_s_failed} failed")
    else:
        _title = (f"{'Regenerate' if force else 'Generate'} all ({media_type}): "
                  f"{_applied} applied, {_failed} failed"
                  + (f", {_s_applied} seasons applied" if _s_applied else ""))
    _s_fail_names = [r['label'] for r in season_results
                     if r.get('status') not in ('applied', 'skipped', 'analyzing') and r.get('label')]
    _s_fail_stats = {'failures': _s_fail_names} if 0 < _s_failed <= 10 else {}
    log_activity(_action,
                 title=_title,
                 stats={'media_type': media_type, 'applied': _applied + _s_applied,
                        'failed': _failed + _s_failed,
                        'seasons_applied': _s_applied, 'total': len(item_ids),
                        **_s_fail_stats})

@overlay_bp.route('/api/overlays/generate/all', methods=['POST'])
def generate_all_overlays():
    """Start background generate-all job for a given media type."""
    import threading
    from overlays.overlay_manager import get_generate_status, _update_gen
    try:
        data = request.get_json() or {}
        if get_generate_status().get('running'):
            return jsonify({'success': False, 'error': 'A generate job is already running'}), 409
        media_type = data.get('media_type', 'movie')
        label = f"Generate all {'movies' if media_type == 'movie' else 'TV shows'}"
        total = _estimate_generate_total(data)
        _update_gen(running=True, total=total, done=0, applied=0, failed=0, skipped=0,
                    current='', errors=[], label=label,
                    started_at=__import__('datetime').datetime.utcnow().isoformat(),
                    finished_at=None)
        t = threading.Thread(target=_run_generate_all_bg, args=(data,), daemon=True)
        t.start()
        return jsonify({'success': True, 'started': True, 'total': total, 'label': label})
    except Exception as e:
        logger.error(f"Failed to start generate-all job: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/generate/regenerate-all', methods=['POST'])
def regenerate_all_overlays():
    """Start background regenerate-all job for a given media type."""
    import threading
    from overlays.overlay_manager import get_generate_status, _update_gen
    try:
        data = request.get_json() or {}
        data['force'] = True
        data['force_fresh_poster'] = True
        if get_generate_status().get('running'):
            return jsonify({'success': False, 'error': 'A generate job is already running'}), 409
        media_type = data.get('media_type', 'movie')
        label = f"Regenerate all {'movies' if media_type == 'movie' else 'TV shows'}"
        total = _estimate_generate_total(data)
        _update_gen(running=True, total=total, done=0, applied=0, failed=0, skipped=0,
                    current='', errors=[], label=label,
                    started_at=__import__('datetime').datetime.utcnow().isoformat(),
                    finished_at=None)
        t = threading.Thread(target=_run_generate_all_bg, args=(data,), daemon=True)
        t.start()
        return jsonify({'success': True, 'started': True, 'total': total, 'label': label})
    except Exception as e:
        logger.error(f"Failed to start regenerate-all job: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/generate/status', methods=['GET'])
def generate_status():
    """Poll the progress of the running/last generate job."""
    try:
        from overlays.overlay_manager import get_generate_status
        s = get_generate_status()
        pct = 0
        if s['total'] > 0:
            pct = round(s['done'] / s['total'] * 100)
        return jsonify({
            'success': True,
            'running':  s['running'],
            'total':    s['total'],
            'done':     s['done'],
            'applied':  s['applied'],
            'failed':   s['failed'],
            'skipped':  s['skipped'],
            'current':  s['current'],
            'errors':   s['errors'],
            'percent':  pct,
            'label':    s.get('label', ''),
            'finished_at': s.get('finished_at'),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
