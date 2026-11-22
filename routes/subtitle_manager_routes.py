"""Subtitles tool page: per-item status, manual search/pick, history.

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

subtitle_manager_bp = Blueprint('subtitle_manager', __name__)

SUB_EXTS = ('.srt', '.ass', '.ssa', '.sub', '.vtt', '.smi')
_CANDIDATES = {}          # token -> (monotonic, item_id, ctx, [subs])
_CAND_TTL = 15 * 60
_CAND_LOCK = threading.Lock()


def _is_plex_mode():
    from utilities.settings import get_setting
    return get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'


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


def _label(item):
    t = item.get('title') or ''
    if item.get('type') == 'episode' and item.get('season_number') is not None:
        t += f" S{int(item['season_number']):02d}E{int(item.get('episode_number') or 0):02d}"
    elif item.get('year'):
        t += f" ({item['year']})"
    return t


def _wanted_langs():
    """Configured languages as (ietf, alpha3) pairs."""
    from utilities.config.downsub_config import config
    from utilities.downsub import expand_languages
    config.reload()
    out = []
    for lang in expand_languages(config.SUBTITLE_LANGUAGES):
        ietf = getattr(lang, 'ietf', None) or str(lang)
        out.append((ietf, lang.alpha3))
    return out


def _sidecar_langs(path, dir_cache):
    """Languages of sidecar subtitles next to a library file, e.g.
    'Show - S01E01.en.srt' -> 'en'. A sidecar with no language tag counts
    as 'und'."""
    if not path:
        return {}
    d, base = os.path.split(path)
    stem = os.path.splitext(base)[0]
    if d not in dir_cache:
        try:
            dir_cache[d] = os.listdir(d)
        except OSError:
            dir_cache[d] = []
    out = {}
    for f in dir_cache[d]:
        low = f.lower()
        if not low.endswith(SUB_EXTS) or not f.startswith(stem + '.'):
            continue
        middle = f[len(stem) + 1:].rsplit('.', 1)[0]
        lang = middle.split('.')[0] if middle else 'und'
        out.setdefault(lang.lower(), []).append(os.path.join(d, f))
    return out


def _has_lang(found, ietf, alpha3):
    keys = {k.lower() for k in found}
    base = ietf.split('-')[0].lower()
    return bool(keys & {ietf.lower(), alpha3.lower(), base})


def _plex_conn():
    from utilities.settings import get_setting
    url = (get_setting('Plex', 'url', '') or '').rstrip('/')
    token = get_setting('Plex', 'token', '') or ''
    return url, token


_PLEX_HEADERS = {
    'X-Plex-Client-Identifier': 'cli-debrid-subtitles',
    'X-Plex-Product': 'cli_debrid',
    'X-Plex-Version': '1.0',
}


def _plex_stream_of_item(item, stream_id):
    """The external subtitle stream `stream_id` of this item, or None."""
    for st in _plex_streams(item.get('ms_item_id')) or []:
        if str(st.get('id')) == str(stream_id) and st.get('external') and st.get('key'):
            return st
    return None


def _plex_streams(rating_key):
    """Subtitle streams Plex has for an item (embedded and external)."""
    import requests
    from utilities.settings import get_setting
    url = (get_setting('Plex', 'url', '') or '').rstrip('/')
    token = get_setting('Plex', 'token', '') or ''
    if not (url and token and rating_key):
        return None
    try:
        r = requests.get(f"{url}/library/metadata/{rating_key}",
                         params={'X-Plex-Token': token},
                         headers={'Accept': 'application/json'}, timeout=10)
        if r.status_code != 200:
            return None
        meta = (r.json().get('MediaContainer') or {}).get('Metadata') or []
        out = []
        for m in meta[:1]:
            for media in m.get('Media') or []:
                for part in media.get('Part') or []:
                    for st in part.get('Stream') or []:
                        if st.get('streamType') != 3:
                            continue
                        out.append({
                            'id': st.get('id'),
                            'key': st.get('key') or '',
                            'language': st.get('languageTag') or st.get('languageCode') or '',
                            'codec': st.get('codec') or '',
                            'title': st.get('displayTitle') or st.get('extendedDisplayTitle') or '',
                            'forced': bool(st.get('forced')),
                            'external': bool(st.get('key')),
                        })
        return out
    except Exception as e:
        logging.debug(f"[subs] Plex stream lookup failed for {rating_key}: {e}")
        return None


@subtitle_manager_bp.route('/tools/subtitles')
@admin_required
def page():
    return render_template('subtitle_manager.html', plex_mode=_is_plex_mode())


@subtitle_manager_bp.route('/api/subtitles/overview')
@admin_required
def overview():
    """Collected items with per-language status. status=missing|has|all."""
    from database.core import get_db_connection
    from database import subtitle_history
    q = (request.args.get('q') or '').strip()
    status = request.args.get('status', 'missing')
    kind = request.args.get('type', 'all')
    page_no = max(1, int(request.args.get('page', 1) or 1))
    per_page = 50
    plex = _is_plex_mode()

    where = ["state IN ('Collected','Upgrading')", "type IN ('movie','episode')"]
    args = []
    if kind in ('movie', 'episode'):
        where.append('type = ?')
        args.append(kind)
    if q:
        where.append('(title LIKE ? OR episode_title LIKE ?)')
        args += [f'%{q}%', f'%{q}%']
    conn = get_db_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, year, type, season_number, episode_number, location_on_disk, "
            "collected_at, version, ms_item_id FROM media_items WHERE " + ' AND '.join(where)
            + " ORDER BY collected_at DESC LIMIT 5000", args).fetchall()]
    except Exception:
        # ms_item_id is added by a migration; very old DBs may lack it
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, year, type, season_number, episode_number, location_on_disk, "
            "collected_at, version FROM media_items WHERE " + ' AND '.join(where)
            + " ORDER BY collected_at DESC LIMIT 5000", args).fetchall()]
    finally:
        conn.close()

    wanted = _wanted_langs()
    history = subtitle_history.languages_by_item([r['id'] for r in rows])
    dir_cache = {}
    out = []
    for r in rows:
        found = {}
        if not plex:
            found = _sidecar_langs(r.get('location_on_disk'), dir_cache)
        for lang in history.get(r['id'], ()):
            found.setdefault((lang or '').lower(), [])
        langs = [{'lang': ietf, 'has': _has_lang(found, ietf, a3)} for ietf, a3 in wanted]
        missing = any(not lang['has'] for lang in langs)
        if status == 'missing' and not missing:
            continue
        if status == 'has' and missing:
            continue
        out.append({'id': r['id'], 'label': _label(r), 'type': r['type'],
                    'version': r.get('version'), 'collected_at': r.get('collected_at'),
                    'langs': langs})
    total = len(out)
    start = (page_no - 1) * per_page
    return jsonify({'items': out[start:start + per_page], 'total': total, 'page': page_no,
                    'per_page': per_page, 'plex_mode': plex,
                    'languages': [w[0] for w in wanted], 'capped': len(rows) >= 5000})


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>')
@admin_required
def item_detail(item_id):
    from database import subtitle_history
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    plex = _is_plex_mode()
    files, plex_streams = [], None
    if plex:
        plex_streams = _plex_streams(item.get('ms_item_id'))
    else:
        for lang, paths in _sidecar_langs(item.get('location_on_disk'), {}).items():
            for p in paths:
                files.append({'language': lang, 'path': p, 'name': os.path.basename(p)})
    return jsonify({
        'id': item_id, 'label': _label(item), 'type': item.get('type'),
        'release': item.get('filled_by_title') or item.get('original_scraped_torrent_title') or '',
        'file': item.get('filled_by_file') or '', 'location': item.get('location_on_disk') or '',
        'plex_mode': plex, 'sidecars': files, 'plex_streams': plex_streams,
        'history': subtitle_history.for_item(item_id),
        'languages': [w[0] for w in _wanted_langs()],
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
    return jsonify({'success': bool(ok),
                    'message': 'Subtitles saved' if ok else 'No subtitle passed the match threshold'})


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/delete', methods=['POST'])
@admin_required
def delete_sidecar(item_id):
    """Delete one sidecar subtitle of this item (Symlinked/Local only)."""
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    path = (request.get_json(silent=True) or {}).get('path') or ''
    allowed = {p for paths in _sidecar_langs(item.get('location_on_disk'), {}).values() for p in paths}
    if path not in allowed:
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


def _sync_mode_note(note):
    return note or 'not synced'


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/sync', methods=['POST'])
@admin_required
def sync_existing(item_id):
    """Resync one existing subtitle: a sidecar file (Symlinked/Local) or an
    uploaded Plex subtitle (download it, sync, upload, remove the old one)."""
    import requests
    from utilities import downsub
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    downsub.config.reload()
    video = downsub.resolve_item_path(item)
    if not video:
        return jsonify({'error': 'No file path for this item'}), 400
    data = request.get_json(silent=True) or {}

    if data.get('path'):
        allowed = {p for paths in _sidecar_langs(item.get('location_on_disk'), {}).values() for p in paths}
        path = data['path']
        if path not in allowed:
            return jsonify({'error': 'Not a subtitle of this item'}), 400
        real = os.path.realpath(video) if os.path.islink(video) else video
        with open(path, 'rb') as f:
            content = f.read()
        new, note = downsub.sync_content(real, content, os.path.splitext(path)[1][1:].lower())
        if new is not content and new != content:
            tmp = path + '.syncing'
            with open(tmp, 'wb') as f:
                f.write(new)
            os.replace(tmp, path)
            return jsonify({'success': True, 'changed': True, 'message': note})
        return jsonify({'success': True, 'changed': False, 'message': _sync_mode_note(note)})

    if data.get('stream_id') is not None:
        st = _plex_stream_of_item(item, data['stream_id'])
        if not st:
            return jsonify({'error': 'Not an uploaded subtitle of this item (embedded ones cannot be changed)'}), 400
        url, token = _plex_conn()
        r = requests.get(url + st['key'], params={'X-Plex-Token': token}, headers=_PLEX_HEADERS, timeout=30)
        if r.status_code != 200 or not r.content:
            return jsonify({'error': f'Could not download the subtitle from Plex (HTTP {r.status_code})'}), 502
        fmt = (st.get('codec') or 'srt').lower()
        new, note = downsub.sync_content(video, r.content, fmt)
        if new is r.content or new == r.content:
            return jsonify({'success': True, 'changed': False, 'message': _sync_mode_note(note)})
        lang = st.get('language') or 'und'
        if not downsub.upload_subtitle_to_plex(item.get('ms_item_id'), new, lang, fmt=fmt):
            return jsonify({'error': 'Synced, but the upload to Plex failed; the old subtitle was kept'}), 502
        d = requests.delete(url + st['key'], params={'X-Plex-Token': token}, headers=_PLEX_HEADERS, timeout=30)
        if d.status_code not in (200, 204):
            return jsonify({'success': True, 'changed': True,
                            'message': f'{note}; uploaded, but removing the old one failed (HTTP {d.status_code})'})
        return jsonify({'success': True, 'changed': True, 'message': note})

    return jsonify({'error': 'path or stream_id required'}), 400


@subtitle_manager_bp.route('/api/subtitles/item/<int:item_id>/plex_delete', methods=['POST'])
@admin_required
def plex_delete(item_id):
    """Remove an uploaded/external subtitle from the Plex item. Embedded
    tracks can't be removed."""
    import requests
    item = _item(item_id)
    if not item:
        return jsonify({'error': 'Item not found or not collected'}), 404
    st = _plex_stream_of_item(item, (request.get_json(silent=True) or {}).get('stream_id'))
    if not st:
        return jsonify({'error': 'Not an uploaded subtitle of this item (embedded ones cannot be removed)'}), 400
    url, token = _plex_conn()
    r = requests.delete(url + st['key'], params={'X-Plex-Token': token}, headers=_PLEX_HEADERS, timeout=30)
    if r.status_code not in (200, 204):
        return jsonify({'error': f'Plex refused the delete (HTTP {r.status_code})'}), 502
    logging.info(f"[subs] removed Plex subtitle stream {st['id']} ({st.get('language')}) from item {item_id}")
    return jsonify({'success': True})


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
