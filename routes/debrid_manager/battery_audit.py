"""Battery (metadata store) audit: orphaned or mismatched entries and IMDb re-identification.

Part of the Debrid Manager blueprint (routes/debrid_manager_routes.py).
"""
from routes.models import admin_required
from flask import jsonify
import logging
from flask import request
import time
import threading
import os
import re
from concurrent.futures import ThreadPoolExecutor
from routes.debrid_manager_routes import (
    debrid_manager_bp,
)


_battery_audit_cache = {'data': None, 'ts': 0}


_BATTERY_AUDIT_TTL = 300  # 5 minutes


def _get_battery_db_path():
    import os
    return os.path.join(
        os.environ.get('USER_DB_CONTENT', '/user/db_content'),
        'cli_battery.db'
    )


def _run_battery_audit():
    """Run all battery audit checks. Returns dict of issues."""
    import sqlite3
    import os
    from datetime import datetime, timezone

    battery_db = _get_battery_db_path()
    if not os.path.exists(battery_db):
        return {'error': f'Battery DB not found: {battery_db}'}

    from database.core import get_db_connection

    main_conn = get_db_connection()
    batt_conn = sqlite3.connect(battery_db)
    batt_conn.row_factory = sqlite3.Row

    try:
        # All IMDB IDs in battery
        batt_imdb_ids = {r[0] for r in batt_conn.execute(
            "SELECT imdb_id FROM items WHERE imdb_id IS NOT NULL"
        ).fetchall()}

        # All IMDB IDs in main DB (any state)
        main_imdb_ids = {r[0] for r in main_conn.execute(
            "SELECT DISTINCT imdb_id FROM media_items WHERE imdb_id IS NOT NULL"
        ).fetchall()}

        # Collected items in main DB
        collected_imdb_ids = {r[0] for r in main_conn.execute(
            "SELECT DISTINCT imdb_id FROM media_items WHERE imdb_id IS NOT NULL AND state = 'Collected'"
        ).fetchall()}

        # ── Check 1: Orphaned Battery Items ────────────────────────────────
        # Battery has them; main DB doesn't know about them at all
        orphaned_ids = batt_imdb_ids - main_imdb_ids
        orphaned_battery = []
        if orphaned_ids:
            ph = ','.join('?' * len(orphaned_ids))
            rows = batt_conn.execute(
                f"SELECT imdb_id, title, year, type, media_status, last_trakt_fetch "
                f"FROM items WHERE imdb_id IN ({ph})",
                list(orphaned_ids)
            ).fetchall()
            orphaned_battery = [dict(r) for r in rows]

        # ── Check 2: Missing Battery Items ─────────────────────────────────
        # Collected in main DB, but battery has no entry
        missing_ids = collected_imdb_ids - batt_imdb_ids
        missing_battery = []
        if missing_ids:
            ph = ','.join('?' * len(missing_ids))
            rows = main_conn.execute(
                f"SELECT DISTINCT imdb_id, title, year, type FROM media_items "
                f"WHERE imdb_id IN ({ph}) AND state='Collected' GROUP BY imdb_id",
                list(missing_ids)
            ).fetchall()
            missing_battery = [dict(r) for r in rows]

        # ── Check 3: Stale Metadata ─────────────────────────────────────────
        # Battery items (that ARE in main DB) with stale last_trakt_fetch
        from cli_battery.app.staleness import is_stale
        relevant_ids = batt_imdb_ids & main_imdb_ids  # only care about tracked items
        stale_metadata = []
        if relevant_ids:
            ph = ','.join('?' * len(relevant_ids))
            all_items = batt_conn.execute(
                f"SELECT imdb_id, title, year, type, media_status, last_trakt_fetch "
                f"FROM items WHERE imdb_id IN ({ph})",
                list(relevant_ids)
            ).fetchall()
            now_utc = datetime.now(timezone.utc)
            for row in all_items:
                last_fetch_str = row['last_trakt_fetch']
                last_fetch = None
                if last_fetch_str:
                    try:
                        last_fetch = datetime.fromisoformat(
                            str(last_fetch_str).replace('Z', '+00:00')
                        )
                        if last_fetch.tzinfo is None:
                            last_fetch = last_fetch.replace(tzinfo=timezone.utc)
                    except Exception:
                        last_fetch = None
                if is_stale(row['type'] or 'movie', row['media_status'], last_fetch):
                    age_days = int((now_utc - last_fetch).days) if last_fetch else None
                    stale_metadata.append({
                        'imdb_id': row['imdb_id'],
                        'title': row['title'],
                        'year': row['year'],
                        'type': row['type'],
                        'media_status': row['media_status'],
                        'age_days': age_days,
                    })

        # ── Check 4: Orphaned TVDB Mappings ───────────────────────────────
        tvdb_rows = batt_conn.execute(
            "SELECT tvdb_id, imdb_id, media_type FROM tvdb_to_imdb_mapping"
        ).fetchall()
        orphaned_tvdb = [dict(r) for r in tvdb_rows if r['imdb_id'] and r['imdb_id'] not in batt_imdb_ids]

        # ── Check 5: Orphaned TMDB Mappings ───────────────────────────────
        tmdb_rows = batt_conn.execute(
            "SELECT tmdb_id, imdb_id, media_type FROM tmdb_to_imdb_mapping"
        ).fetchall()
        orphaned_tmdb = [dict(r) for r in tmdb_rows if r['imdb_id'] and r['imdb_id'] not in batt_imdb_ids]

        # Enrich orphaned mapping rows with title/year from media_items first
        all_orphaned_mapping_ids = list({r['imdb_id'] for r in orphaned_tvdb + orphaned_tmdb if r.get('imdb_id')})
        main_title_map = {}
        if all_orphaned_mapping_ids:
            ph = ','.join('?' * len(all_orphaned_mapping_ids))
            for row in main_conn.execute(
                f"SELECT imdb_id, title, year, type FROM media_items "
                f"WHERE imdb_id IN ({ph}) GROUP BY imdb_id",
                all_orphaned_mapping_ids
            ).fetchall():
                main_title_map[row['imdb_id']] = {'title': row['title'], 'year': row['year'], 'type': row['type']}
        for r in orphaned_tvdb + orphaned_tmdb:
            info = main_title_map.get(r.get('imdb_id'), {})
            r['title'] = info.get('title')
            r['year'] = info.get('year')

        # For orphaned TMDB rows still missing title, try TMDB API
        # Falls back to alternate type if NULL media_type causes a 404 (e.g. stored as tv but is movie)
        try:
            from metadata.metadata import get_tmdb_metadata
            for r in orphaned_tmdb:
                if not r.get('title') and r.get('tmdb_id'):
                    primary_type = r.get('media_type') or 'tv'
                    fallback_type = 'movie' if primary_type != 'movie' else 'tv'
                    tmdb_data = get_tmdb_metadata(str(r['tmdb_id']), primary_type)
                    if not tmdb_data:
                        tmdb_data = get_tmdb_metadata(str(r['tmdb_id']), fallback_type)
                        if tmdb_data:
                            r['media_type'] = fallback_type  # correct the stored type label
                    if tmdb_data:
                        r['title'] = tmdb_data.get('title')
                        r['year'] = tmdb_data.get('year')
        except Exception:
            pass

        # ── Check 6: No IMDB ID ────────────────────────────────────────────
        # Collected items in media_items with no IMDB ID — can't get metadata or upgrade scoring
        no_imdb_rows = main_conn.execute(
            "SELECT title, year, type, filled_by_file "
            "FROM media_items "
            "WHERE state = 'Collected' AND (imdb_id IS NULL OR imdb_id = '') "
            "AND title NOT IN ('Unknown Title', '') "
            "GROUP BY title, year, type "
            "ORDER BY title"
        ).fetchall()
        no_imdb_id = [dict(r) for r in no_imdb_rows]

        return {
            'orphaned_battery': orphaned_battery,
            'missing_battery':  missing_battery,
            'stale_metadata':   stale_metadata[:200],  # cap to prevent UI overload
            'orphaned_tvdb':    orphaned_tvdb,
            'orphaned_tmdb':    orphaned_tmdb,
            'no_imdb_id':       no_imdb_id[:200],
            'summary': {
                'orphaned_battery': len(orphaned_battery),
                'missing_battery':  len(missing_battery),
                'stale_metadata':   len(stale_metadata),
                'orphaned_tvdb':    len(orphaned_tvdb),
                'orphaned_tmdb':    len(orphaned_tmdb),
                'no_imdb_id':       len(no_imdb_id),
            },
        }
    finally:
        main_conn.close()
        batt_conn.close()


@debrid_manager_bp.route('/api/battery_audit')
@admin_required
def api_battery_audit():
    """Run battery audit checks (cached 5 min)."""
    force = request.args.get('force') == '1'
    now = time.time()
    if (not force
            and _battery_audit_cache['data'] is not None
            and (now - _battery_audit_cache['ts']) < _BATTERY_AUDIT_TTL):
        return jsonify({'success': True, 'cached': True, **_battery_audit_cache['data']})
    try:
        result = _run_battery_audit()
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 500
        _battery_audit_cache['data'] = result
        _battery_audit_cache['ts'] = now
        return jsonify({'success': True, 'cached': False, **result})
    except Exception as e:
        logging.error(f"[BatteryAudit] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/battery_audit/refresh', methods=['POST'])
@admin_required
def api_battery_audit_refresh():
    """Force-refresh battery metadata for given IMDB IDs (fix stale/missing)."""
    data = request.get_json(silent=True) or {}
    # Support {items: [{imdb_id, type}]} (type-aware) or legacy {imdb_ids: [...]}
    raw_items = data.get('items')
    if raw_items:
        refresh_list = [(it['imdb_id'], it.get('type') or None) for it in raw_items if it.get('imdb_id')]
    else:
        refresh_list = [(iid, None) for iid in data.get('imdb_ids', [])]
    if not refresh_list:
        return jsonify({'success': False, 'error': 'imdb_ids required'}), 400

    def _do_refresh(items):
        from cli_battery.app.direct_api import DirectAPI
        ok, failed = [], []
        for iid, itype in items:
            try:
                result, _ = DirectAPI.force_refresh_metadata(iid, item_type=itype)
                if result:
                    ok.append(iid)
                else:
                    failed.append({'imdb_id': iid, 'error': 'No data returned'})
            except Exception as e:
                failed.append({'imdb_id': iid, 'error': str(e)})
        logging.info(f"[BatteryAudit] Refresh complete: {len(ok)} ok, {len(failed)} failed")
        return ok, failed

    _battery_audit_cache['data'] = None  # invalidate cache

    # For small batches run synchronously so we can return real success/failure.
    # Large batches run in background to avoid gateway timeout.
    if len(refresh_list) <= 5:
        ok, failed = _do_refresh(refresh_list)
        if failed and not ok:
            return jsonify({'success': False,
                            'error': '; '.join(f['error'] for f in failed[:3])}), 500
        return jsonify({'success': True, 'refreshed': len(ok), 'failed': failed})

    threading.Thread(target=_do_refresh, args=(refresh_list,), daemon=True).start()
    return jsonify({'success': True, 'queued': len(refresh_list),
                    'message': f'Refreshing {len(refresh_list)} item(s) in background'})


@debrid_manager_bp.route('/api/battery_audit/delete_battery', methods=['POST'])
@admin_required
def api_battery_audit_delete_battery():
    """Delete battery items by IMDB ID (cascades to seasons, episodes, metadata)."""
    data = request.get_json(silent=True) or {}
    imdb_ids = data.get('imdb_ids', [])
    if not imdb_ids:
        return jsonify({'success': False, 'error': 'imdb_ids required'}), 400
    try:
        from cli_battery.app.database import managed_session, Item
        deleted, failed = 0, []
        for iid in imdb_ids:
            try:
                with managed_session() as session:
                    item = session.query(Item).filter_by(imdb_id=iid).first()
                    if item:
                        session.delete(item)
                        deleted += 1
            except Exception as e:
                failed.append({'imdb_id': iid, 'error': str(e)})
        _battery_audit_cache['data'] = None
        return jsonify({'success': True, 'deleted': deleted, 'failed': failed})
    except Exception as e:
        logging.error(f"[BatteryAudit] delete_battery error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/battery_audit/delete_mapping', methods=['POST'])
@admin_required
def api_battery_audit_delete_mapping():
    """Delete orphaned TVDB or TMDB mappings."""
    data = request.get_json(silent=True) or {}
    mapping_type = data.get('type', '')  # 'tvdb' or 'tmdb'
    ids = data.get('ids', [])            # tvdb_id list or tmdb_id list
    if mapping_type not in ('tvdb', 'tmdb') or not ids:
        return jsonify({'success': False, 'error': 'type (tvdb/tmdb) and ids required'}), 400
    try:
        from cli_battery.app.database import managed_session, TVDBToIMDBMapping, TMDBToIMDBMapping
        deleted = 0
        with managed_session() as session:
            if mapping_type == 'tvdb':
                for tid in ids:
                    n = session.query(TVDBToIMDBMapping).filter_by(tvdb_id=str(tid)).delete()
                    deleted += n
            else:
                for tid in ids:
                    n = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=str(tid)).delete()
                    deleted += n
        _battery_audit_cache['data'] = None
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        logging.error(f"[BatteryAudit] delete_mapping error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/battery_audit/reidentify/verify', methods=['POST'])
@admin_required
def api_battery_audit_reidentify_verify():
    """Verify a proposed new IMDB ID by fetching metadata. Returns title/year preview."""
    data = request.get_json(silent=True) or {}
    new_imdb_id = (data.get('new_imdb_id') or '').strip()
    item_type = (data.get('item_type') or '').strip()  # 'movie', 'show', or ''

    if not new_imdb_id or not new_imdb_id.startswith('tt'):
        return jsonify({'success': False, 'error': 'new_imdb_id must be a valid tt… IMDB ID'}), 400

    try:
        from cli_battery.app.direct_api import DirectAPI
        meta = None
        if item_type == 'movie':
            meta, _ = DirectAPI.get_movie_metadata(new_imdb_id)
        elif item_type == 'show':
            meta, _ = DirectAPI.get_show_metadata(new_imdb_id)
        else:
            # Try show first, then movie
            meta, _ = DirectAPI.get_show_metadata(new_imdb_id)
            if not meta:
                meta, _ = DirectAPI.get_movie_metadata(new_imdb_id)

        if not meta:
            return jsonify({'success': False,
                            'error': f'Could not find metadata for {new_imdb_id}. '
                                     'Check the IMDB ID is correct.'})

        return jsonify({
            'success': True,
            'imdb_id': new_imdb_id,
            'title':   meta.get('title', ''),
            'year':    meta.get('year', ''),
            'type':    meta.get('type', item_type or ''),
        })
    except Exception as e:
        logging.error(f"[BatteryAudit] reidentify/verify error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/battery_audit/reidentify/commit', methods=['POST'])
@admin_required
def api_battery_audit_reidentify_commit():
    """
    Commit a re-identify: delete old battery item, fetch metadata under new IMDB ID.
    Optionally update media_items.imdb_id in main DB.
    """
    data = request.get_json(silent=True) or {}
    old_imdb_id = (data.get('old_imdb_id') or '').strip()
    new_imdb_id = (data.get('new_imdb_id') or '').strip()
    sync_main_db = bool(data.get('sync_main_db', False))

    if not old_imdb_id or not new_imdb_id:
        return jsonify({'success': False, 'error': 'old_imdb_id and new_imdb_id required'}), 400
    if old_imdb_id == new_imdb_id:
        return jsonify({'success': False, 'error': 'old and new IMDB IDs are the same'}), 400

    try:
        from cli_battery.app.database import managed_session, Item
        from cli_battery.app.direct_api import DirectAPI

        # Step 1: Delete old battery item (cascades seasons/episodes/metadata)
        with managed_session() as session:
            old_item = session.query(Item).filter_by(imdb_id=old_imdb_id).first()
            if old_item:
                session.delete(old_item)

        # Step 2: Fetch metadata under new IMDB ID
        meta, _ = DirectAPI.force_refresh_metadata(new_imdb_id)
        if not meta:
            return jsonify({
                'success': False,
                'error': f'Old battery entry deleted, but could not fetch metadata for {new_imdb_id}. '
                         'You may need to trigger a manual refresh later.'
            })

        # Step 3 (optional): Update main DB
        main_updated = 0
        if sync_main_db:
            from database.core import get_db_connection
            conn = get_db_connection()
            try:
                conn.execute(
                    "UPDATE media_items SET imdb_id = ? WHERE imdb_id = ?",
                    (new_imdb_id, old_imdb_id)
                )
                main_updated = conn.execute("SELECT changes()").fetchone()[0]
                conn.commit()
            finally:
                conn.close()

        _battery_audit_cache['data'] = None
        return jsonify({
            'success': True,
            'old_imdb_id': old_imdb_id,
            'new_imdb_id': new_imdb_id,
            'new_title':   meta.get('title', ''),
            'main_updated': main_updated,
        })
    except Exception as e:
        logging.error(f"[BatteryAudit] reidentify/commit error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/battery_audit/set_imdb_id', methods=['POST'])
@admin_required
def api_battery_audit_set_imdb_id():
    """Set IMDB ID on media_items records that currently have none."""
    import re, sqlite3
    data = request.get_json(silent=True) or {}
    title     = data.get('title', '').strip()
    year      = data.get('year')
    item_type = data.get('type', '').strip()
    imdb_id   = data.get('imdb_id', '').strip()

    if not title or not item_type:
        return jsonify({'success': False, 'error': 'title and type required'}), 400
    if not re.fullmatch(r'tt\d{7,8}', imdb_id):
        return jsonify({'success': False, 'error': 'imdb_id must be in format tt1234567'}), 400

    db_path = os.path.join(
        os.environ.get('USER_DB_CONTENT', '/user/db_content'),
        'media_items.db'
    )
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            if year:
                n = conn.execute(
                    "UPDATE media_items SET imdb_id = ? "
                    "WHERE (imdb_id IS NULL OR imdb_id = '') "
                    "AND title = ? AND year = ? AND type = ?",
                    (imdb_id, title, year, item_type)
                ).rowcount
            else:
                n = conn.execute(
                    "UPDATE media_items SET imdb_id = ? "
                    "WHERE (imdb_id IS NULL OR imdb_id = '') "
                    "AND title = ? AND type = ?",
                    (imdb_id, title, item_type)
                ).rowcount
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logging.error(f"[BatteryAudit] set_imdb_id error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

    _battery_audit_cache['data'] = None  # invalidate cache
    logging.info(f"[BatteryAudit] set_imdb_id: set {imdb_id} on {n} rows for '{title}' ({item_type})")
    return jsonify({'success': True, 'updated': n, 'imdb_id': imdb_id})


def _tmdb_search_imdb(title, year, media_type):
    """Search TMDB for a title and return imdb_id + metadata. Returns dict or None."""
    import requests
    from utilities.settings import get_setting
    api_key = get_setting('TMDB', 'api_key')
    if not api_key:
        return None
    try:
        endpoint  = 'tv' if media_type in ('episode', 'show') else 'movie'
        year_param = 'first_air_date_year' if endpoint == 'tv' else 'primary_release_year'
        params = {'api_key': api_key, 'query': title, 'language': 'en-US'}
        if year:
            params[year_param] = year
        r = requests.get(f'https://api.themoviedb.org/3/search/{endpoint}',
                         params=params, timeout=8)
        r.raise_for_status()
        results = r.json().get('results', [])
        if not results and year:
            params.pop(year_param, None)
            r = requests.get(f'https://api.themoviedb.org/3/search/{endpoint}',
                             params=params, timeout=8)
            r.raise_for_status()
            results = r.json().get('results', [])
        if not results:
            return None
        hit    = results[0]
        tmdb_id = hit.get('id')
        if not tmdb_id:
            return None
        ext = requests.get(f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/external_ids',
                           params={'api_key': api_key}, timeout=8)
        ext.raise_for_status()
        imdb_id = ext.json().get('imdb_id')
        if not imdb_id:
            return None
        matched_title = hit.get('title') or hit.get('name') or title
        matched_year  = (hit.get('release_date') or hit.get('first_air_date') or '')[:4]
        return {
            'imdb_id':       imdb_id,
            'matched_title': matched_title,
            'matched_year':  int(matched_year) if matched_year.isdigit() else None,
            'source':        'tmdb',
        }
    except Exception as e:
        logging.debug(f"[BatteryAudit] TMDB fallback error: {e}")
        return None


def _lookup_single_imdb(title, year_int, search_type):
    """Search for an IMDB ID via DirectAPI (Trakt/TVDB) with TMDB fallback.

    Returns a dict with keys: imdb_id, matched_title, matched_year, source, confidence.
    Returns None if no match found.
    """
    from cli_battery.app.direct_api import DirectAPI

    results, source = DirectAPI.search_media(title, year=year_int, media_type=search_type)

    # Retry with shortened title (strip subtitle after ': ' or ' - ') if no results
    if not results:
        short = re.split(r':\s+|\s+-\s+', title, maxsplit=1)[0].strip()
        if short.lower() != title.lower():
            results, source = DirectAPI.search_media(short, year=year_int, media_type=search_type)

    best = None
    confidence = 'high'
    if results:
        best = DirectAPI.find_best_match_from_results(title, year_int, results)
        if not best or not best.get('imdb_id'):
            best = DirectAPI.find_best_match_from_results(title, year_int, results, min_score_threshold=0)
            confidence = 'low'

    # TMDB fallback when Trakt/TVDB returned nothing or a low-confidence match
    if not best or not best.get('imdb_id') or confidence == 'low':
        tmdb = _tmdb_search_imdb(title, year_int, search_type)
        if tmdb:
            return {**tmdb, 'confidence': 'high'}

    if not best or not best.get('imdb_id'):
        return None

    return {
        'imdb_id':       best.get('imdb_id'),
        'matched_title': best.get('title') or best.get('matched_title'),
        'matched_year':  best.get('year') or best.get('matched_year'),
        'source':        source or 'unknown',
        'confidence':    confidence,
    }


@debrid_manager_bp.route('/api/battery_audit/lookup_imdb', methods=['POST'])
@admin_required
def api_battery_audit_lookup_imdb():
    """Auto-search for an IMDB ID given title, year, and media type."""
    data = request.get_json(silent=True) or {}
    title      = (data.get('title') or '').strip()
    year       = data.get('year')
    media_type = (data.get('type') or '').strip()

    if not title:
        return jsonify({'success': False, 'error': 'title required'}), 400

    try:
        search_type = 'show' if media_type in ('episode', 'show') else 'movie'
        year_int    = int(year) if year else None
        best = _lookup_single_imdb(title, year_int, search_type)
        if not best:
            return jsonify({'success': False, 'error': 'No match found'})
        return jsonify({'success': True, **best})
    except Exception as e:
        logging.error(f"[BatteryAudit] lookup_imdb error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/battery_audit/lookup_imdb_bulk', methods=['POST'])
@admin_required
def api_battery_audit_lookup_imdb_bulk():
    """Bulk auto-search for IMDB IDs. Takes a list of {title, year, type} items."""
    data  = request.get_json(silent=True) or {}
    items = data.get('items', [])
    if not items or not isinstance(items, list):
        return jsonify({'success': False, 'error': 'items list required'}), 400

    items = items[:100]

    def _process(item):
        title      = (item.get('title') or '').strip()
        year       = item.get('year')
        media_type = (item.get('type') or '').strip()
        if not title:
            return {'success': False, 'error': 'no title'}
        try:
            search_type = 'show' if media_type in ('episode', 'show') else 'movie'
            year_int    = int(year) if year else None
            best = _lookup_single_imdb(title, year_int, search_type)
            if not best:
                return {'success': False, 'error': 'no match'}
            return {'success': True, **best}
        except Exception as e:
            return {'success': False, 'error': str(e)[:100]}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_process, items))

    return jsonify({'success': True, 'results': results})


@debrid_manager_bp.route('/api/battery_audit/sync', methods=['POST'])
@admin_required
def api_battery_audit_sync():
    """Trigger battery→media_items sync tasks (movie titles + episode metadata)."""
    def _do_sync():
        try:
            from database.maintenance import update_movie_titles, sync_episode_metadata, cleanup_title_year_suffixes
            logging.info("[BatteryAudit] Starting sync: update_movie_titles")
            update_movie_titles()
            logging.info("[BatteryAudit] Starting sync: sync_episode_metadata")
            sync_episode_metadata()
            logging.info("[BatteryAudit] Starting sync: cleanup_title_year_suffixes")
            cleanup_title_year_suffixes()
            logging.info("[BatteryAudit] Sync complete")
        except Exception as e:
            logging.error(f"[BatteryAudit] Sync error: {e}", exc_info=True)

    threading.Thread(target=_do_sync, daemon=True).start()
    return jsonify({'success': True, 'message': 'Sync tasks started in background'})
