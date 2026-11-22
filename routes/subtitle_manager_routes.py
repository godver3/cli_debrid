"""Subtitles tool page: per-item status, manual search/pick, history, bulk
search of missing subtitles, bulk resync of existing ones, Bazarr import.

Status sources: sidecar files next to the library file (Symlinked/Local), the
Plex item's subtitle streams (Plex mode, per item only), and the downloader's
own history (both modes; the only list-wide source in Plex mode).
"""
import logging
import os
import threading
import time
import uuid

from flask import Blueprint, jsonify, render_template, request

from routes.models import admin_required
from utilities import subtitle_status as status_mod

subtitle_manager_bp = Blueprint('subtitle_manager', __name__)

_CANDIDATES = {}          # token -> (monotonic, item_id, ctx, [subs])
_CAND_TTL = 15 * 60
_CAND_LOCK = threading.Lock()


def _item(item_id):
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM media_items WHERE id = ? AND state IN ('Collected','Upgrading')",
            (item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _sidecar_paths(item):
    return {p for paths in status_mod.sidecar_langs(item.get('location_on_disk'), {}).values() for p in paths}


@subtitle_manager_bp.route('/tools/subtitles')
@admin_required
def page():
    return render_template('subtitle_manager.html', plex_mode=status_mod.is_plex_mode())


@subtitle_manager_bp.route('/api/subtitles/overview')
@admin_required
def overview():
    """Collected items with per-language status, for the grouped list.
    status=missing|has|all. Paging is done by the page, per show/movie."""
    q = (request.args.get('q') or '').strip()
    status = request.args.get('status', 'missing')
    kind = request.args.get('type', 'all')
    plex = status_mod.is_plex_mode()
    wanted = status_mod.wanted_langs()
    rows = status_mod.collected(q, kind, full=True)
    out = []
    for r in status_mod.with_status(rows, plex=plex, wanted=wanted):
        if status == 'missing' and not r['missing']:
            continue
        if status == 'has' and r['missing']:
            continue
        out.append({'id': r['id'], 'label': status_mod.label(r), 'type': r['type'],
                    'title': r.get('title'), 'year': r.get('year'), 'imdb_id': r.get('imdb_id'),
                    'season': r.get('season_number'), 'episode': r.get('episode_number'),
                    'episode_title': r.get('episode_title'),
                    'version': r.get('version'), 'collected_at': r.get('collected_at'),
                    'langs': r['langs']})
    scan = None
    try:
        from utilities import plex_subtitle_info as psi
        if psi.available():
            have = psi.cached_tracks([psi.cache_key(r) for r in rows])
            todo = [r for r in rows if psi.cache_key(r) and psi.cache_key(r) not in have]
            if todo:
                psi.start_scan(todo)
            scan = psi.scan_status()
    except Exception as e:
        logging.debug(f"[subs] embedded scan start failed: {e}")
    return jsonify({'items': out, 'total': len(out), 'plex_mode': plex,
                    'languages': [w[0] for w in wanted], 'capped': len(rows) >= status_mod.ROW_CAP,
                    'embedded_scan': scan})


@subtitle_manager_bp.route('/api/subtitles/embedded_scan')
@admin_required
def embedded_scan():
    from utilities import plex_subtitle_info as psi
    return jsonify(psi.scan_status())


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>')
@admin_required
def item_detail(item_id):
    from database import subtitle_history
    from utilities import subtitle_resync
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    plex = status_mod.is_plex_mode()
    files, plex_streams = [], None
    if plex:
        plex_streams = subtitle_resync.plex_streams(subtitle_resync.item_rk(item))
    for lang, paths in status_mod.sidecar_langs(item.get('location_on_disk'), {}).items():
        for p in paths:
            files.append({'language': lang, 'path': p, 'name': os.path.basename(p)})
    return jsonify({
        'id': item_id, 'label': status_mod.label(item), 'type': item.get('type'),
        'release': item.get('filled_by_title') or item.get('original_scraped_torrent_title') or '',
        'file': item.get('filled_by_file') or '', 'location': item.get('location_on_disk') or '',
        'plex_mode': plex, 'sidecars': files, 'plex_streams': plex_streams,
        'history': subtitle_history.for_item(item_id),
        'languages': [w[0] for w in status_mod.wanted_langs()],
    })


def _ctx_for(item, check_embedded=False):
    from utilities import downsub
    downsub.config.reload()
    path = downsub.resolve_item_path(item)
    if not path:
        return None, 'No file path for this item'
    ctx = downsub.prepare_video(path, item=item, check_embedded=check_embedded)
    if ctx is None:
        return None, 'Could not identify the file (see logs)'
    return ctx, None


def _prune_candidates():
    now = time.monotonic()
    for k in [k for k, v in _CANDIDATES.items() if now - v[0] > _CAND_TTL]:
        _CANDIDATES.pop(k, None)


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/search', methods=['POST'])
@admin_required
def search(item_id):
    from babelfish import Language
    from utilities import downsub
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    ctx, err = _ctx_for(item)
    if err:
        return jsonify({'error': err}), 400
    data = request.get_json(silent=True) or {}
    langs = None
    if data.get('language'):
        try:
            langs = downsub.expand_languages([data['language']]) or [Language.fromietf(data['language'])]
        except Exception:
            return jsonify({'error': f"Unknown language {data['language']}"}), 400
    t0 = time.time()
    try:
        cands = downsub.search_candidates(ctx, langs)
    except Exception as e:
        logging.error(f"[subs] manual search failed for item {item_id}: {e}", exc_info=True)
        return jsonify({'error': f'Search failed: {e}'}), 500
    cands = cands[:60]
    token = uuid.uuid4().hex
    with _CAND_LOCK:
        _prune_candidates()
        _CANDIDATES[token] = (time.monotonic(), item_id, ctx, [c['sub'] for c in cands])
    video = ctx['video']
    return jsonify({
        'token': token, 'took': round(time.time() - t0, 1),
        'min_score': ctx['min_score'], 'hashed': ctx['hash'],
        'parsed': {'name': ctx['parse_name'], 'group': video.release_group,
                   'source': video.source, 'service': video.streaming_service,
                   'resolution': video.resolution, 'ids': dict(video.external_ids)},
        'providers': downsub.selected_providers(),
        'paused': downsub.blocked_providers(),
        'candidates': [{k: v for k, v in c.items() if k != 'sub'} for c in cands],
    })


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/download', methods=['POST'])
@admin_required
def download(item_id):
    from utilities import downsub
    data = request.get_json(silent=True) or {}
    with _CAND_LOCK:
        entry = _CANDIDATES.get(data.get('token') or '')
    if not entry or entry[1] != item_id:
        return jsonify({'error': 'Search expired, search again'}), 410
    try:
        sub = entry[3][int(data.get('index'))]
    except (TypeError, ValueError, IndexError):
        return jsonify({'error': 'Unknown candidate'}), 400
    try:
        saved = downsub.download_candidate(entry[2], sub)
    except Exception as e:
        logging.error(f"[subs] manual download failed for item {item_id}: {e}", exc_info=True)
        return jsonify({'error': f'Download failed: {e}'}), 500
    if not saved:
        return jsonify({'error': 'Provider returned nothing or the subtitle could not be saved (see logs)'}), 502
    return jsonify({'success': True})


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/auto', methods=['POST'])
@admin_required
def auto(item_id):
    """Best match per configured language, as the automatic download does."""
    from utilities import downsub
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    downsub.config.reload()
    path = downsub.resolve_item_path(item)
    if not path:
        return jsonify({'error': 'No file path for this item'}), 400
    ok = downsub.download_subtitles_for_video(path, item=item)
    # The downloader's own note says what happened: saved (which, from where),
    # nothing needed (every language embedded), or why nothing was saved.
    note = downsub.last_result_note(item) or ''
    if ok and note.startswith('every language'):
        message = 'Nothing to download: ' + note
    elif ok:
        message = 'Saved: ' + note if note else 'Subtitles saved'
    else:
        message = note[:1].upper() + note[1:] if note else 'No subtitle passed the match threshold'
    return jsonify({'success': bool(ok), 'message': message})


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/delete', methods=['POST'])
@admin_required
def delete_sidecar(item_id):
    """Delete one sidecar subtitle of this item (Symlinked/Local only)."""
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    path = (request.get_json(silent=True) or {}).get('path') or ''
    if path not in _sidecar_paths(item):
        return jsonify({'error': 'Not a subtitle of this item'}), 400
    try:
        os.remove(path)
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    logging.info(f"[subs] deleted sidecar {path} (item {item_id})")
    return jsonify({'success': True})


@subtitle_manager_bp.route('/api/subtitles/history')
@admin_required
def history():
    from database import subtitle_history
    page_no = max(1, int(request.args.get('page', 1) or 1))
    rows = subtitle_history.recent(limit=50, offset=(page_no - 1) * 50)
    return jsonify({'rows': rows, 'page': page_no})


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/plex_delete', methods=['POST'])
@admin_required
def plex_delete(item_id):
    """Remove an uploaded/external subtitle from the Plex item. Embedded
    tracks can't be removed."""
    from utilities import subtitle_resync
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    st = subtitle_resync.plex_stream_of_item(item, (request.get_json(silent=True) or {}).get('stream_id'))
    if not st:
        return jsonify({'error': 'Not an uploaded subtitle of this item (embedded ones cannot be removed)'}), 400
    ok, code = subtitle_resync.plex_delete(st)
    if not ok:
        return jsonify({'error': f'Plex refused the delete (HTTP {code})'}), 502
    logging.info(f"[subs] removed Plex subtitle stream {st['id']} ({st.get('language')}) from item {item_id}")
    return jsonify({'success': True})


# ---- bulk runs ------------------------------------------------------------

@subtitle_manager_bp.route('/api/subtitles/bulk/start', methods=['POST'])
@admin_required
def bulk_start():
    """Queue a search (missing languages) or sync (existing subtitles) for:
    ids=[...] (rows), imdb_id (+ season) (a show/season), or the filter
    q/type (everything shown). Search only takes items missing a language."""
    from utilities import subtitle_bulk
    data = request.get_json(silent=True) or {}
    kind = 'sync' if data.get('kind') == 'sync' else 'search'
    rows = status_mod.collected((data.get('q') or '').strip() if not (data.get('ids') or data.get('imdb_id')) else '',
                                data.get('type') or 'all', full=True)
    scope = 'the current filter'
    if data.get('ids'):
        ids = {int(i) for i in data['ids']}
        rows = [r for r in rows if r['id'] in ids]
        scope = 'this item' if len(ids) == 1 else f'{len(ids)} items'
    elif data.get('imdb_id'):
        rows = [r for r in rows if r.get('imdb_id') == data['imdb_id']
                and (data.get('season') is None or r.get('season_number') == int(data['season']))]
        scope = 'this season' if data.get('season') is not None else 'this show'
    if kind == 'search':
        items = [r for r in status_mod.with_status(rows) if r['missing']]
    else:
        from utilities.subtitle_resync import needs_check
        cache = {}
        items = [r for r in rows if needs_check(r, cache)]
    if not items:
        what = 'missing a language' if kind == 'search' else 'with subtitles that still need checking'
        return jsonify({'success': False, 'message': f'Nothing to do: no items {what} in {scope}'})
    how, n = subtitle_bulk.start(items, label=f"{kind} {scope}", kind=kind)
    verb = 'searching' if kind == 'search' else 'syncing'
    if not n:
        return jsonify({'success': True, 'total': 0, 'message': f'Already queued for {verb}'})
    s_ = 's' if n != 1 else ''
    msg = (f"Started {verb} {n} item{s_}" if how == 'started'
           else f"Added {n} item{s_} to the {kind} queue (a {kind} run is already going)")
    return jsonify({'success': True, 'total': n, 'message': msg})


@subtitle_manager_bp.route('/api/subtitles/bulk/status')
@admin_required
def bulk_status():
    from utilities import subtitle_bulk
    return jsonify(subtitle_bulk.status())


@subtitle_manager_bp.route('/api/subtitles/bulk/stop', methods=['POST'])
@admin_required
def bulk_stop():
    from utilities import subtitle_bulk
    subtitle_bulk.stop((request.get_json(silent=True) or {}).get('kind'))
    return jsonify({'success': True})


# ---- Bazarr import --------------------------------------------------------

@subtitle_manager_bp.route('/api/subtitles/bazarr/preview', methods=['POST'])
@admin_required
def bazarr_preview():
    from utilities import bazarr_import
    data = request.get_json(silent=True) or {}
    try:
        plan = bazarr_import.fetch_plan(data.get('url') or '', data.get('api_key') or '')
    except bazarr_import.BazarrError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(bazarr_import.public_plan(plan))


@subtitle_manager_bp.route('/api/subtitles/bazarr/import', methods=['POST'])
@admin_required
def bazarr_apply():
    from utilities import bazarr_import
    data = request.get_json(silent=True) or {}
    try:
        plan = bazarr_import.fetch_plan(data.get('url') or '', data.get('api_key') or '')
    except bazarr_import.BazarrError as e:
        return jsonify({'error': str(e)}), 400
    applied = bazarr_import.apply_plan(plan, enable=bool(data.get('enable_downloads', True)),
                                       stop_bazarr_feed=bool(data.get('stop_bazarr_feed')))
    return jsonify({'success': True, 'applied': applied})
