import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, jsonify, request
from debrid import get_debrid_provider
from database.torrent_tracking import get_recent_additions
from .models import admin_required

debrid_manager_bp = Blueprint('debrid_manager', __name__)

# ---------------------------------------------------------------------------
# Library cache — progressive loading pattern
#
# First load: fetch INITIAL_PAGES synchronously (fast, ~2-3s), return
# immediately with loading=True. Background thread fetches remaining pages
# and accumulates into the cache.  Frontend polls while loading=True.
#
# Subsequent loads within TTL: instant from stable cache.
# After TTL: serve stale snapshot instantly, re-fetch in background.
# Force refresh: re-fetch everything from scratch.
# ---------------------------------------------------------------------------
_PAGE_SIZE    = 500       # items per RD API call
_INITIAL_PAGES = 3        # pages fetched synchronously before first response
_LIB_TTL      = 86400 * 7  # 7 days — background refresh only triggers after this

# _stable: last complete snapshot dict  {torrents, total, total_bytes, fetched_at}
# _partial: list accumulating during an in-progress fetch
# _gen:     monotonically-increasing counter; background threads abort if theirs
#           doesn't match, preventing stale writes after a force-refresh.
_lib = {
    'stable':  None,
    'partial': [],
    'loading': False,
    'gen':     0,
    'lock':    threading.Lock(),
}


def _fetch_pages_bg(api_key, start_page, gen):
    """Background thread: fetch pages start_page..end and accumulate into _lib['partial'].
    Aborts silently if a newer fetch generation has started."""
    from debrid.real_debrid.api import make_request
    page = start_page
    try:
        while True:
            with _lib['lock']:
                if _lib['gen'] != gen:
                    return  # superseded by a newer fetch
            try:
                result = make_request('GET', '/torrents', api_key,
                                      params={'limit': _PAGE_SIZE, 'page': page})
            except Exception as e:
                logging.error(f"Library bg fetch error at page {page}: {e}")
                break

            if not isinstance(result, list) or not result:
                break

            with _lib['lock']:
                if _lib['gen'] != gen:
                    return
                _lib['partial'].extend(result)

            if len(result) < _PAGE_SIZE:
                break
            page += 1

        # Enrich with DB metadata before promoting to stable (done outside lock — it's a read-only query)
        with _lib['lock']:
            if _lib['gen'] != gen:
                return
            all_torrents = list(_lib['partial'])

        _enrich_with_db(all_torrents)

        # Promote partial → stable
        with _lib['lock']:
            if _lib['gen'] != gen:
                return
            _lib['stable'] = {
                'torrents':    all_torrents,
                'total':       len(all_torrents),
                'total_bytes': sum(t.get('bytes', 0) or 0 for t in all_torrents),
                'fetched_at':  time.time(),
            }
            _lib['partial'] = []
            _lib['loading'] = False
        logging.info(f"Library cache complete: {len(all_torrents)} torrents")

    except Exception as e:
        logging.error(f"Library bg fetch unexpected error: {e}")
        with _lib['lock']:
            if _lib['gen'] == gen:
                _lib['loading'] = False


def _enrich_with_db(torrents):
    """Annotate each torrent with _db metadata from media_items.filled_by_torrent_id.
    Single bulk query per cache build — zero per-request cost."""
    if not torrents:
        return torrents
    id_map = {t['id']: t for t in torrents if t.get('id')}
    if not id_map:
        return torrents
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        ids = list(id_map.keys())
        placeholders = ','.join(['?'] * len(ids))
        rows = conn.execute(
            f"SELECT filled_by_torrent_id, title, year, type, season_number, episode_number "
            f"FROM media_items WHERE filled_by_torrent_id IN ({placeholders})",
            ids
        ).fetchall()
        conn.close()
        for row in rows:
            tid = row[0]
            if tid and tid in id_map:
                id_map[tid]['_db'] = {
                    'title':          row[1],
                    'year':           row[2],
                    'type':           row[3],
                    'season_number':  row[4],
                    'episode_number': row[5],
                }
        logging.debug(f"Library DB enrichment: {len(rows)}/{len(ids)} matched")
    except Exception as e:
        logging.warning(f"Library DB enrichment skipped: {e}")
    return torrents


def _invalidate_library_cache():
    """Mark cache stale after a mutation (delete / reinsert)."""
    with _lib['lock']:
        if _lib['stable']:
            _lib['stable']['fetched_at'] = 0.0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@debrid_manager_bp.route('/')
@admin_required
def index():
    from utilities.settings import get_setting
    file_mgmt = get_setting('File Management', 'file_collection_management', default='Plex')
    media_server = get_setting('File Management', 'media_server_type', default='plex')
    show_plex_trash = (
        file_mgmt == 'Plex' or
        (file_mgmt == 'Symlinked/Local' and media_server == 'plex')
    )
    return render_template('debrid_manager.html', show_plex_trash=show_plex_trash)


_plex_trash_cache = {'filenames': None, 'ts': 0}
_PLEX_TRASH_TTL = 300  # 5 minutes

@debrid_manager_bp.route('/api/plex_trash')
@admin_required
def api_plex_trash():
    try:
        import requests
        import xml.etree.ElementTree as ET
        from utilities.settings import get_setting

        force = request.args.get('force') == '1'

        # Serve from cache if fresh (unless force refresh requested)
        if not force and _plex_trash_cache['filenames'] is not None and (time.time() - _plex_trash_cache['ts']) < _PLEX_TRASH_TTL:
            return jsonify({'success': True, 'filenames': _plex_trash_cache['filenames'], 'cached': True})

        plex_url = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
        if not plex_url or not plex_token:
            return jsonify({'success': False, 'error': 'Plex URL or token not configured'})

        headers = {'X-Plex-Token': plex_token, 'Accept': 'application/xml'}

        # Get all library sections
        r = requests.get(f'{plex_url}/library/sections', headers=headers, timeout=10)
        root = ET.fromstring(r.text)
        sections = [
            (d.get('key'), '1' if d.get('type') == 'movie' else '4')
            for d in root.findall('Directory')
        ]

        def fetch_section(section_id, media_type):
            r2 = requests.get(
                f'{plex_url}/library/sections/{section_id}/all',
                headers=headers,
                params={'type': media_type},
                timeout=30
            )
            names = []
            for video in ET.fromstring(r2.text).findall('Video'):
                for media in video.findall('Media'):
                    if media.get('deletedAt'):
                        for part in media.findall('Part'):
                            file_path = part.get('file', '')
                            if file_path:
                                parts = [p for p in file_path.split('/') if p]
                                names.append(parts[-2] if len(parts) >= 2 else parts[-1])
            return names

        filenames = []
        with ThreadPoolExecutor(max_workers=max(1, len(sections))) as ex:
            futures = [ex.submit(fetch_section, sid, mtype) for sid, mtype in sections]
            for f in as_completed(futures):
                filenames.extend(f.result())

        result = list(set(filenames))
        _plex_trash_cache['filenames'] = result
        _plex_trash_cache['ts'] = time.time()
        return jsonify({'success': True, 'filenames': result, 'cached': False})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@debrid_manager_bp.route('/api/active')
@admin_required
def api_active():
    try:
        from debrid.real_debrid.api import make_request
        provider = get_debrid_provider()
        api_key = provider.api_key
        # filter=active → only in-progress/downloading torrents
        torrents = make_request('GET', '/torrents', api_key,
                                params={'filter': 'active', 'limit': 100})
        if not isinstance(torrents, list):
            torrents = []

        # Fetch recent torrents to surface errors and no-files states
        all_recent = make_request('GET', '/torrents', api_key,
                                  params={'limit': 200})
        if not isinstance(all_recent, list):
            all_recent = []
        _error_statuses = {'error', 'magnet_error', 'virus', 'dead'}
        _no_files_statuses = {'waiting_files_selection', 'magnet_conversion', 'compressing', 'uploading'}
        errors = [t for t in all_recent if (t.get('status') or '').lower() in _error_statuses]
        no_files = [t for t in all_recent if (t.get('status') or '').lower() in _no_files_statuses]

        active_count, max_downloads = provider.get_active_downloads()
        return jsonify({
            'success': True,
            'active_count': active_count,
            'max_downloads': max_downloads,
            'torrents': torrents,
            'errors': errors,
            'no_files': no_files,
        })
    except Exception as e:
        logging.error(f"Debrid Manager active API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/torrent/<torrent_id>', methods=['GET'])
@admin_required
def api_torrent_detail(torrent_id):
    try:
        from debrid.real_debrid.api import make_request
        provider = get_debrid_provider()
        detail = make_request('GET', f'/torrents/info/{torrent_id}', provider.api_key)
        return jsonify({'success': True, 'torrent': detail})
    except Exception as e:
        logging.error(f"Debrid Manager torrent detail error for {torrent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/torrent/<torrent_id>', methods=['DELETE'])
@admin_required
def api_delete_torrent(torrent_id):
    try:
        provider = get_debrid_provider()
        provider.remove_torrent(torrent_id, removal_reason='Manual removal via Debrid Manager')
        _invalidate_library_cache()
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Debrid Manager delete error for {torrent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/torrent/batch-delete', methods=['POST'])
@admin_required
def api_batch_delete():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'success': False, 'error': 'No IDs provided'}), 400

    provider = get_debrid_provider()
    results = {'deleted': [], 'failed': []}

    def delete_one(tid):
        try:
            provider.remove_torrent(tid, removal_reason='Batch removal via Debrid Manager')
            return tid, None
        except Exception as e:
            return tid, str(e)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(delete_one, tid): tid for tid in ids}
        for future in as_completed(futures):
            tid, err = future.result()
            if err:
                results['failed'].append({'id': tid, 'error': err})
            else:
                results['deleted'].append(tid)

    if results['deleted']:
        _invalidate_library_cache()

    return jsonify({'success': True, **results})


@debrid_manager_bp.route('/api/torrent/batch-reinsert', methods=['POST'])
@admin_required
def api_batch_reinsert():
    data = request.get_json(silent=True) or {}
    torrents = data.get('torrents', [])  # list of {id, hash, filename}
    if not torrents:
        return jsonify({'success': False, 'error': 'No torrents provided'}), 400

    provider = get_debrid_provider()
    results = {'reinserted': [], 'failed': []}

    def reinsert_one(t):
        tid = t.get('id', '')
        h   = t.get('hash', '')
        fn  = t.get('filename', '')
        if not h:
            return tid, 'missing hash'
        try:
            from urllib.parse import quote
            magnet = f'magnet:?xt=urn:btih:{h}'
            if fn:
                magnet += f'&dn={quote(fn)}'
            # Add first — only delete old entry if add succeeds
            provider.add_torrent(magnet)
            if tid:
                try:
                    provider.remove_torrent(tid)
                except Exception:
                    pass  # new copy added; best-effort cleanup of old
            return tid, None
        except Exception as e:
            return tid, str(e)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(reinsert_one, t): t for t in torrents}
        for future in as_completed(futures):
            tid, err = future.result()
            if err:
                results['failed'].append({'id': tid, 'error': err})
            else:
                results['reinserted'].append(tid)

    if results['reinserted']:
        _invalidate_library_cache()

    return jsonify({'success': True, **results})


@debrid_manager_bp.route('/api/torrent/<torrent_id>/reinsert', methods=['POST'])
@admin_required
def api_reinsert_torrent(torrent_id):
    data = request.get_json(silent=True) or {}
    torrent_hash = data.get('hash', '')
    filename = data.get('filename', '')
    if not torrent_hash:
        return jsonify({'success': False, 'error': 'hash required'}), 400
    try:
        provider = get_debrid_provider()
        magnet = f'magnet:?xt=urn:btih:{torrent_hash}'
        if filename:
            from urllib.parse import quote
            magnet += f'&dn={quote(filename)}'
        # Add first — only delete old entry if add succeeds
        new_id = provider.add_torrent(magnet)
        try:
            provider.remove_torrent(torrent_id)
        except Exception:
            pass  # new copy added; best-effort cleanup of old
        _invalidate_library_cache()
        return jsonify({'success': True, 'new_id': new_id})
    except Exception as e:
        logging.error(f"Debrid Manager reinsert error for {torrent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/library')
@admin_required
def api_library():
    force = request.args.get('force') == '1'
    now   = time.time()

    with _lib['lock']:
        stable   = _lib['stable']
        is_loading = _lib['loading']
        partial  = list(_lib['partial'])       # snapshot to avoid holding lock

    stable_age = int(now - stable['fetched_at']) if stable else None
    is_fresh   = stable is not None and stable_age < _LIB_TTL

    # ── 1. Complete fresh cache ─────────────────────────────────────────────
    if is_fresh and not is_loading and not force:
        return jsonify({
            'success': True, 'loading': False,
            'cache_status': 'fresh', 'cache_age': stable_age,
            'torrents': stable['torrents'], 'total': stable['total'],
            'total_bytes': stable['total_bytes'],
        })

    # ── 2. Already loading ──────────────────────────────────────────────────
    if is_loading and not force:
        if stable:
            # Stale refresh — serve old complete snapshot while new one builds
            return jsonify({
                'success': True, 'loading': True,
                'cache_status': 'refreshing', 'cache_age': stable_age,
                'torrents': stable['torrents'], 'total': stable['total'],
                'total_bytes': stable['total_bytes'],
            })
        else:
            # First load — serve whatever partial data we have so far
            total_bytes = sum(t.get('bytes', 0) or 0 for t in partial)
            return jsonify({
                'success': True, 'loading': True,
                'cache_status': 'partial', 'cache_age': 0,
                'torrents': partial, 'total': len(partial),
                'total_bytes': total_bytes,
            })

    # ── 3. Complete but stale — serve stale, trigger background re-fetch ────
    if stable and not is_fresh and not is_loading and not force:
        with _lib['lock']:
            _lib['gen'] += 1
            gen = _lib['gen']
            _lib['loading'] = True
            _lib['partial'] = []
        provider = get_debrid_provider()
        threading.Thread(target=_fetch_pages_bg,
                         args=(provider.api_key, 1, gen), daemon=True).start()
        return jsonify({
            'success': True, 'loading': True,
            'cache_status': 'stale', 'cache_age': stable_age,
            'torrents': stable['torrents'], 'total': stable['total'],
            'total_bytes': stable['total_bytes'],
        })

    # ── 4. No cache or force refresh — fetch initial pages synchronously ────
    from debrid.real_debrid.api import make_request
    provider = get_debrid_provider()
    api_key  = provider.api_key

    with _lib['lock']:
        _lib['gen'] += 1
        gen = _lib['gen']
        _lib['stable']  = None
        _lib['partial'] = []
        _lib['loading'] = True

    initial = []
    try:
        for page in range(1, _INITIAL_PAGES + 1):
            result = make_request('GET', '/torrents', api_key,
                                  params={'limit': _PAGE_SIZE, 'page': page})
            if not isinstance(result, list) or not result:
                break
            initial.extend(result)
            if len(result) < _PAGE_SIZE:
                # All torrents fit in the first few pages — enrich and cache
                _enrich_with_db(initial)
                with _lib['lock']:
                    if _lib['gen'] == gen:
                        _lib['stable'] = {
                            'torrents':    initial,
                            'total':       len(initial),
                            'total_bytes': sum(t.get('bytes', 0) or 0 for t in initial),
                            'fetched_at':  time.time(),
                        }
                        _lib['partial'] = []
                        _lib['loading'] = False
                return jsonify({
                    'success': True, 'loading': False,
                    'cache_status': 'live', 'cache_age': 0,
                    'torrents': initial, 'total': len(initial),
                    'total_bytes': sum(t.get('bytes', 0) or 0 for t in initial),
                })
    except Exception as e:
        logging.error(f"Library initial fetch error: {e}")
        with _lib['lock']:
            if _lib['gen'] == gen:
                _lib['loading'] = False
        return jsonify({'success': False, 'error': str(e)}), 500

    # Seed partial with initial pages, launch background for the rest
    with _lib['lock']:
        if _lib['gen'] == gen:
            _lib['partial'] = initial

    threading.Thread(target=_fetch_pages_bg,
                     args=(api_key, _INITIAL_PAGES + 1, gen), daemon=True).start()

    total_bytes = sum(t.get('bytes', 0) or 0 for t in initial)
    return jsonify({
        'success': True, 'loading': True,
        'cache_status': 'partial', 'cache_age': 0,
        'torrents': initial, 'total': len(initial),
        'total_bytes': total_bytes,
    })


# ---------------------------------------------------------------------------
# Backup / Restore API
# ---------------------------------------------------------------------------

@debrid_manager_bp.route('/api/backup/status')
@admin_required
def api_backup_status():
    try:
        from utilities.debrid_backup import get_backup_status
        return jsonify({'success': True, **get_backup_status()})
    except Exception as e:
        logging.error(f'Debrid backup status error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/files')
@admin_required
def api_backup_files():
    try:
        from utilities.debrid_backup import list_backup_files
        return jsonify({'success': True, 'files': list_backup_files()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/settings', methods=['POST'])
@admin_required
def api_backup_settings_save():
    data = request.get_json(silent=True) or {}
    try:
        from utilities.settings import set_setting
        set_setting('Debrid Backup', 'enabled', bool(data.get('enabled', False)))
        set_setting('Debrid Backup', 'slot_1d_hours', int(data.get('slot_1d_hours', 24)))
        set_setting('Debrid Backup', 'slot_3d_hours', int(data.get('slot_3d_hours', 72)))
        set_setting('Debrid Backup', 'slot_7d_hours', int(data.get('slot_7d_hours', 168)))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/run', methods=['POST'])
@admin_required
def api_backup_run():
    """Trigger a manual backup (force=True bypasses the enabled check)."""
    try:
        from utilities.debrid_backup import run_backup
        result = run_backup(force=True)
        return jsonify(result)
    except Exception as e:
        logging.error(f'Manual backup error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/restore', methods=['POST'])
@admin_required
def api_backup_restore():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename', '')
    dry_run = bool(data.get('dry_run', False))
    if not filename:
        return jsonify({'success': False, 'error': 'filename required'}), 400
    try:
        from utilities.debrid_backup import restore_from_file
        result = restore_from_file(filename, dry_run=dry_run)
        return jsonify(result)
    except Exception as e:
        logging.error(f'Restore error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Cleanup API
# ---------------------------------------------------------------------------

@debrid_manager_bp.route('/api/cleanup/settings', methods=['POST'])
@admin_required
def api_cleanup_settings_save():
    data = request.get_json(silent=True) or {}
    try:
        from utilities.settings import set_setting
        set_setting('Debrid Cleanup', 'enabled',        bool(data.get('enabled', False)))
        set_setting('Debrid Cleanup', 'delete_errors',  bool(data.get('delete_errors', True)))
        set_setting('Debrid Cleanup', 'delete_dupes',   bool(data.get('delete_dupes', True)))
        set_setting('Debrid Cleanup', 'delete_stalled', bool(data.get('delete_stalled', False)))
        set_setting('Debrid Cleanup', 'stalled_days',   int(data.get('stalled_days', 3)))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/cleanup/run', methods=['POST'])
@admin_required
def api_cleanup_run():
    """Trigger a manual cleanup (force=True bypasses the enabled check)."""
    try:
        from utilities.debrid_backup import run_cleanup
        result = run_cleanup(force=True)
        return jsonify(result)
    except Exception as e:
        logging.error(f'Manual cleanup error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/usage')
@admin_required
def api_usage():
    """Return account info + traffic details for the Usage tab."""
    try:
        from datetime import datetime
        provider = get_debrid_provider()
        sub = provider.get_subscription_status()

        user_data = {}
        traffic_details = {}
        traffic_summary = {}
        try:
            from utilities.settings import get_setting
            from debrid.real_debrid.api import make_request
            api_key = get_setting("Debrid Provider", "api_key")
            user_data        = make_request('GET', '/user', api_key) or {}
            traffic_details  = make_request('GET', '/traffic/details', api_key) or {}
            traffic_summary  = make_request('GET', '/traffic', api_key) or {}
        except Exception as e:
            logging.warning(f"Usage API direct call failed: {e}")

        today_utc  = datetime.utcnow().strftime("%Y-%m-%d")
        today_data = traffic_details.get(today_utc, {})
        today_bytes = today_data.get('bytes', 0)

        history = []
        for date_str in sorted(traffic_details.keys(), reverse=True):
            day  = traffic_details[date_str]
            b    = day.get('bytes', 0)
            # RD uses 'host' key (not 'hosters'); fall back to 'hosters' for other providers
            hosters_raw = day.get('host', {}) or day.get('hosters', {}) or {}
            hosters = []
            for name, info in hosters_raw.items():
                if isinstance(info, dict):
                    hb = info.get('bytes', 0) or info.get('downloaded', 0)
                else:
                    hb = int(info or 0)
                hosters.append({'name': name, 'bytes': hb,
                                'gb': round(hb / (1024 ** 3), 2)})
            history.append({
                'date':    date_str,
                'bytes':   b,
                'gb':      round(b / (1024 ** 3), 2),
                'hosters': hosters,
            })

        # Aggregate all-time total from history
        total_all_bytes = sum(
            traffic_details[d].get('bytes', 0) for d in traffic_details
        )
        # Active downloads slot info
        try:
            active_count, max_dl = provider.get_active_downloads()
        except Exception:
            active_count, max_dl = 0, 0

        return jsonify({
            'success': True,
            'user': {
                'username':       user_data.get('username', ''),
                'email':          user_data.get('email', ''),
                'type':           user_data.get('type', ''),
                'premium':        bool(user_data.get('premium', False)),
                'expiration':     sub.get('expiration', ''),
                'days_remaining': sub.get('days_remaining'),
                'points':         user_data.get('points', 0),
                'locale':         user_data.get('locale', ''),
            },
            'today': {
                'date':          today_utc,
                'downloaded_gb': round(today_bytes / (1024 ** 3), 2),
                'limit_gb':      2000,
            },
            'totals': {
                'all_time_gb':   round(total_all_bytes / (1024 ** 3), 2),
                'active_slots':  active_count,
                'max_slots':     max_dl,
            },
            'history': history,
        })
    except Exception as e:
        logging.error(f"Usage API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/history')
@admin_required
def api_history():
    try:
        raw = get_recent_additions(1000)
        entries = []
        for row in raw:
            e = dict(row)
            item_data = {}
            if e.get('item_data'):
                try:
                    item_data = json.loads(e['item_data'])
                except Exception:
                    pass

            title = (
                item_data.get('filled_by_title')
                or item_data.get('title', '')
            )
            year = item_data.get('year', '')
            season = item_data.get('season_number')
            episode = item_data.get('episode_number')
            media_type = item_data.get('type') or item_data.get('media_type', '')

            label = title
            if year:
                label += f' ({year})'
            if season is not None:
                label += f' S{str(season).zfill(2)}'
                if episode is not None:
                    label += f'E{str(episode).zfill(2)}'

            entries.append({
                'id': e.get('id'),
                'torrent_hash': e.get('torrent_hash', ''),
                'timestamp': str(e.get('timestamp', '')),
                'trigger_source': e.get('trigger_source', ''),
                'rationale': e.get('rationale', ''),
                'is_still_present': e.get('is_still_present', True),
                'removal_reason': e.get('removal_reason', ''),
                'removal_timestamp': str(e.get('removal_timestamp', '') or ''),
                'label': label,
                'media_type': media_type,
                'item_data': e.get('item_data', ''),
                'trigger_details': e.get('trigger_details', ''),
                'additional_metadata': e.get('additional_metadata', ''),
            })
        return jsonify({'success': True, 'entries': entries})
    except Exception as e:
        logging.error(f"Debrid Manager history API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
