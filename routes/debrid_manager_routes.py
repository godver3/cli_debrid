import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
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
_lib_db_loaded = False  # track whether we've attempted a DB load this process
_lib_last_accessed = 0.0  # timestamp of last debrid manager visit — used for idle eviction


def _save_lib_cache_to_db(stable_data: dict) -> None:
    """Persist the full RD library to SQLite so it survives restarts."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rd_library_cache (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data_json TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO rd_library_cache (id, data_json, fetched_at) VALUES (1, ?, ?)",
            (json.dumps({
                'torrents':    stable_data['torrents'],
                'total':       stable_data['total'],
                'total_bytes': stable_data['total_bytes'],
            }), stable_data['fetched_at'])
        )
        conn.commit()
        conn.close()
        logging.debug(f"[LibCache] Saved {stable_data['total']} torrents to DB")
    except Exception as e:
        logging.warning(f"[LibCache] Could not save library to DB: {e}")


def _load_lib_cache_from_db() -> None:
    """Load a previously persisted RD library into the in-memory cache (once per process)."""
    global _lib_db_loaded
    if _lib_db_loaded:
        return
    _lib_db_loaded = True
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        row = conn.execute(
            "SELECT data_json, fetched_at FROM rd_library_cache WHERE id=1"
        ).fetchone()
        conn.close()
        if row:
            data = json.loads(row[0])
            with _lib['lock']:
                if _lib['stable'] is None:  # don't overwrite a fresher in-memory copy
                    _lib['stable'] = {
                        'torrents':    data.get('torrents', []),
                        'total':       data.get('total', 0),
                        'total_bytes': data.get('total_bytes', 0),
                        'fetched_at':  row[1],
                    }
            logging.info(f"[LibCache] Loaded {data.get('total', 0)} torrents from DB "
                         f"(age: {int(time.time() - row[1])}s)")
    except Exception as e:
        logging.debug(f"[LibCache] Could not load library from DB: {e}")


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
        new_stable = {
            'torrents':    all_torrents,
            'total':       len(all_torrents),
            'total_bytes': sum(t.get('bytes', 0) or 0 for t in all_torrents),
            'fetched_at':  time.time(),
        }
        with _lib['lock']:
            if _lib['gen'] != gen:
                return
            _lib['stable'] = new_stable
            _lib['partial'] = []
            _lib['loading'] = False
        _save_lib_cache_to_db(new_stable)
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
        try:
            ids = list(id_map.keys())
            placeholders = ','.join(['?'] * len(ids))
            rows = conn.execute(
                f"SELECT filled_by_torrent_id, title, year, type, season_number, episode_number "
                f"FROM media_items WHERE filled_by_torrent_id IN ({placeholders})",
                ids
            ).fetchall()
        finally:
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
    symlink_mode = (file_mgmt == 'Symlinked/Local')
    return render_template(
        'debrid_manager.html',
        show_plex_trash=show_plex_trash,
        symlink_mode=symlink_mode,
        media_server_type=media_server,
    )


_plex_trash_cache = {'filenames': None, 'ts': 0}
_plex_trash_items_cache = {'items': None, 'ts': 0}
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
            return jsonify({'success': True, 'filenames': _plex_trash_cache['filenames'],
                            'orphaned_filenames': _plex_trash_cache.get('orphaned_filenames', []), 'cached': True})

        plex_url = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
        if not plex_url or not plex_token:
            return jsonify({'success': False, 'error': 'Plex URL or token not configured'})

        headers = {'X-Plex-Token': plex_token, 'Accept': 'application/xml'}

        # Get all library sections (movies and TV shows only)
        r = requests.get(f'{plex_url}/library/sections', headers=headers, timeout=10)
        root = ET.fromstring(r.text)
        sections = [
            (d.get('key'), '1' if d.get('type') == 'movie' else '4')
            for d in root.findall('Directory')
            if d.get('type') in ('movie', 'show')
        ]

        import re as _re
        _season_re = _re.compile(r'^season\s+\d+$', _re.IGNORECASE)

        def fetch_section(section_id, media_type):
            """Fetch trashed items from a section using trash=1."""
            names = []
            orphaned = []  # folder names from Media elements with deletedAt
            r2 = requests.get(
                f'{plex_url}/library/sections/{section_id}/all',
                headers=headers,
                params={'type': media_type, 'trash': '1',
                        'X-Plex-Container-Start': 0,
                        'X-Plex-Container-Size': 1000},
                timeout=8
            )
            container = ET.fromstring(r2.text)
            for video in container.findall('Video'):
                for media_el in video.findall('Media'):
                    has_deleted_at = bool(media_el.get('deletedAt'))
                    for part in media_el.findall('Part'):
                        file_path = part.get('file', '')
                        if file_path:
                            parts = [p for p in file_path.split('/') if p]
                            if len(parts) >= 2:
                                parent = parts[-2]
                                # For TV: parent is "Season X" — use show folder instead
                                if _season_re.match(parent) and len(parts) >= 3:
                                    folder = parts[-3]
                                else:
                                    folder = parent
                            else:
                                folder = parts[-1] if parts else ''
                            if folder:
                                names.append(folder)
                                if has_deleted_at:
                                    orphaned.append(folder)
            return names, orphaned

        filenames = []
        orphaned_all = []
        for sid, mtype in sections:
            try:
                _names, _orphaned = fetch_section(sid, mtype)
                filenames.extend(_names)
                orphaned_all.extend(_orphaned)
            except Exception as _fe:
                logging.warning(f"Plex trash fetch_section error (section {sid}): {_fe}")

        result = list(set(filenames))
        orphaned_result = list(set(orphaned_all))
        _plex_trash_cache['filenames'] = result
        _plex_trash_cache['orphaned_filenames'] = orphaned_result
        _plex_trash_cache['ts'] = time.time()
        return jsonify({'success': True, 'filenames': result, 'orphaned_filenames': orphaned_result, 'cached': False})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@debrid_manager_bp.route('/api/plex_trash_items')
@admin_required
def api_plex_trash_items():
    try:
        import requests
        import xml.etree.ElementTree as ET
        import re as _re
        from utilities.settings import get_setting
        _t0 = time.time()
        logging.info(f"[PlexTrash] api_plex_trash_items called")

        force = request.args.get('force') == '1'

        if (not force
                and _plex_trash_items_cache['items'] is not None
                and (time.time() - _plex_trash_items_cache['ts']) < _PLEX_TRASH_TTL):
            logging.info(f"[PlexTrash] Serving from cache")
            return jsonify({'success': True, 'cached': True, **_plex_trash_items_cache['items']})

        plex_url = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
        logging.info(f"[PlexTrash] plex_url={plex_url!r}")
        if not plex_url or not plex_token:
            return jsonify({'success': False, 'error': 'Plex URL or token not configured'})

        headers = {'X-Plex-Token': plex_token, 'Accept': 'application/xml'}

        logging.info(f"[PlexTrash] Fetching /library/sections ({time.time()-_t0:.1f}s)")
        r = requests.get(f'{plex_url}/library/sections', headers=headers, timeout=10)
        root = ET.fromstring(r.text)
        sections = [
            (d.get('key'), '1' if d.get('type') == 'movie' else '4', d.get('type'))
            for d in root.findall('Directory')
            if d.get('type') in ('movie', 'show')
        ]
        logging.info(f"[PlexTrash] Got {len(sections)} sections ({time.time()-_t0:.1f}s)")

        _season_re = _re.compile(r'^season\s+\d+$', _re.IGNORECASE)

        import os as _os

        def fetch_section_full(section_id, media_type_num, media_type):
            """Fetch up to 1000 trashed items from a section in a single request."""
            _ts = time.time()
            logging.info(f"[PlexTrash] fetch_section_full section={section_id} type={media_type}")
            items = []
            r2 = requests.get(
                f'{plex_url}/library/sections/{section_id}/all',
                headers=headers,
                params={'type': media_type_num, 'trash': '1',
                        'X-Plex-Container-Start': 0,
                        'X-Plex-Container-Size': 1000},
                timeout=8
            )
            logging.info(f"[PlexTrash] section {section_id} response: {len(r2.text)} bytes in {time.time()-_ts:.1f}s")
            container = ET.fromstring(r2.text)
            for video in container.findall('Video'):
                rating_key = video.get('ratingKey', '')
                title = video.get('title', '')
                year = video.get('year', '')
                show_title = video.get('grandparentTitle', '')  # non-empty for episodes
                # Collect all file paths for this item; derive folder_name from first
                all_files = []        # available (non-unavailable) media file paths
                unavail_files = []   # media versions marked unavailable="1" by Plex
                deleted_at_files = []  # media versions with deletedAt (orphaned by Plex)
                folder_name = None
                file_dir = None
                # Map filename -> (media_id, part_id) for per-part deletion
                part_ids = {}
                for media_el in video.findall('Media'):
                    media_id = media_el.get('id', '')
                    is_unavail = media_el.get('unavailable') in ('1', 'true')
                    has_deleted_at = bool(media_el.get('deletedAt'))
                    for part_el in media_el.findall('Part'):
                        file_path = part_el.get('file', '')
                        part_id = part_el.get('id', '')
                        if not file_path:
                            continue
                        if is_unavail:
                            unavail_files.append(file_path)
                        else:
                            all_files.append(file_path)
                        if has_deleted_at:
                            deleted_at_files.append(file_path)
                        basename = _os.path.basename(file_path)
                        if media_id and part_id:
                            part_ids[basename] = {'media_id': media_id, 'part_id': part_id}
                            logging.debug(f"[PlexTrash] trash API part_ids[{basename!r}] = mid={media_id} pid={part_id}")
                        if folder_name is None and not is_unavail:
                            parts_list = [p for p in file_path.split('/') if p]
                            if len(parts_list) >= 2:
                                parent = parts_list[-2]
                                if _season_re.match(parent) and len(parts_list) >= 3:
                                    folder_name = parts_list[-3]
                                else:
                                    folder_name = parent
                            elif parts_list:
                                folder_name = parts_list[-1]
                            file_dir = _os.path.dirname(file_path) or None
                # Fall back to unavailable file paths for folder_name if no good ones found
                if folder_name is None:
                    for file_path in unavail_files:
                        parts_list = [p for p in file_path.split('/') if p]
                        if len(parts_list) >= 2:
                            parent = parts_list[-2]
                            if _season_re.match(parent) and len(parts_list) >= 3:
                                folder_name = parts_list[-3]
                            else:
                                folder_name = parent
                        elif parts_list:
                            folder_name = parts_list[-1]
                        file_dir = _os.path.dirname(file_path) or None
                        break
                # Merge for the good/bad detection pass that happens later;
                # unavailable paths go at the end so _folder_from_path prefers good files.
                all_files = all_files + unavail_files

                if rating_key:
                    items.append({
                        'rating_key': rating_key,
                        'title': title,
                        'show_title': show_title,  # grandparentTitle — show name for episodes
                        'year': year,
                        'media_type': media_type,
                        'folder_name': folder_name or title,
                        'file_dir': file_dir,
                        'all_files': all_files,
                        'deleted_at_files': deleted_at_files,
                        'part_ids': part_ids,
                        'bad_file': None,
                        'has_good_file': False,
                        'bad_file_orphaned': False,
                    })
            logging.info(f"[PlexTrash] section {section_id} returned {len(items)} trash items")
            return items

        all_items = []
        for sid, mnum, mtype in sections:
            try:
                all_items.extend(fetch_section_full(sid, mnum, mtype))
            except Exception as _fe:
                logging.warning(f"Plex trash section {sid} ({mtype}) fetch error: {_fe}")
        logging.info(f"[PlexTrash] All sections done, {len(all_items)} items total ({time.time()-_t0:.1f}s)")

        # Deduplicate by rating_key
        seen_rk = set()
        unique_items = []
        for item in all_items:
            if item['rating_key'] not in seen_rk:
                seen_rk.add(item['rating_key'])
                unique_items.append(item)

        logging.info(f"[PlexTrash] Skipping DB lookup ({time.time()-_t0:.1f}s)")

        # Cross-reference against RD library cache
        def _norm(s):
            return _re.sub(r'[._\-\s]+', ' ', (s or '').lower()).strip()

        # Ensure the RD library is loaded — try the persistent DB cache if not in memory
        _load_lib_cache_from_db()
        logging.info(f"[PlexTrash] Acquiring _lib lock ({time.time()-_t0:.1f}s)")
        with _lib['lock']:
            stable = _lib['stable']
            rd_is_loading = _lib['loading']
        logging.info(f"[PlexTrash] Got _lib lock, stable={'yes' if stable else 'no'} ({time.time()-_t0:.1f}s)")

        # If still no stable data, kick off a background RD fetch so next request will have it
        rd_library_status = 'ok'
        if stable is None and not rd_is_loading:
            try:
                provider = get_debrid_provider()
                with _lib['lock']:
                    _lib['gen'] += 1
                    _bg_gen = _lib['gen']
                    _lib['loading'] = True
                    _lib['partial'] = []
                threading.Thread(target=_fetch_pages_bg,
                                 args=(provider.api_key, 1, _bg_gen), daemon=True).start()
                logging.info("[PlexTrash] No RD library — triggered background fetch")
                rd_library_status = 'loading'
            except Exception as _bg_e:
                logging.warning(f"[PlexTrash] Could not start bg fetch: {_bg_e}")
                rd_library_status = 'unavailable'
        elif stable is None and rd_is_loading:
            rd_library_status = 'loading'

        in_rd = []
        not_in_rd = []
        if stable:
            rd_entries = [(t.get('id', ''), t.get('filename', ''), _norm(t.get('filename', '')), t.get('hash', ''))
                          for t in stable['torrents']]
            for item in unique_items:
                fn = _norm(item['folder_name'])
                matched_id = None
                matched_name = None
                matched_hash = None
                if fn:
                    for rd_id, rd_filename, rd_norm_name, rd_hash in rd_entries:
                        if fn in rd_norm_name or rd_norm_name.startswith(fn):
                            matched_id = rd_id
                            matched_name = rd_filename
                            matched_hash = rd_hash
                            break
                if matched_id:
                    item['rd_torrent_id'] = matched_id
                    item['rd_filename'] = matched_name
                    item['rd_hash'] = matched_hash
                    in_rd.append(item)
                else:
                    not_in_rd.append(item)
            # For items in RD that have multiple files, identify which specific file
            # is NOT in RD — that is the orphaned/unavailable file causing the trash entry.
            def _folder_from_path(fp):
                pts = [p for p in fp.split('/') if p]
                if len(pts) >= 2:
                    par = pts[-2]
                    if _season_re.match(par) and len(pts) >= 3:
                        return pts[-3]
                    return par
                return pts[-1] if pts else ''

            # For in_rd items with only 1 file in trash, fetch full Plex metadata to
            # discover additional media versions (e.g. unavailable duplicates in active
            # library that don't appear in the trash API response).
            for _sfi in in_rd:
                if len(_sfi.get('all_files') or []) <= 1 and _sfi.get('rating_key'):
                    try:
                        _mr = requests.get(
                            f'{plex_url}/library/metadata/{_sfi["rating_key"]}',
                            headers=headers, timeout=5
                        )
                        logging.info(f"[PlexTrash] Metadata fetch rk={_sfi['rating_key']} title={_sfi.get('title')!r} status={_mr.status_code} bytes={len(_mr.text)}")
                        _meta = ET.fromstring(_mr.text)
                        _existing = set(_sfi.get('all_files') or [])
                        _media_count = 0
                        for _mv in _meta.iter('Video'):
                            for _me in _mv.findall('Media'):
                                _media_count += 1
                                _mid = _me.get('id', '')
                                _unavail = _me.get('unavailable', '')
                                for _pe in _me.findall('Part'):
                                    _fp = _pe.get('file', '')
                                    logging.info(f"[PlexTrash]   Media id={_mid} unavail={_unavail!r} Part file={_fp!r}")
                                    if _fp and _fp not in _existing:
                                        _sfi.setdefault('all_files', []).append(_fp)
                                        _existing.add(_fp)
                                        _bn = _os.path.basename(_fp)
                                        _pid = _pe.get('id', '')
                                        if _mid and _pid:
                                            # Don't overwrite part_ids set by the trash API —
                                            # those IDs are correct for the trash item; active-library
                                            # IDs for the same filename would cause 404 on delete.
                                            existing = _sfi.setdefault('part_ids', {}).get(_bn)
                                            if existing:
                                                logging.debug(f"[PlexTrash] metadata fetch skipping part_ids[{_bn!r}] (already set by trash API: mid={existing['media_id']} pid={existing['part_id']}); metadata mid={_mid} pid={_pid}")
                                            else:
                                                _sfi['part_ids'][_bn] = {'media_id': _mid, 'part_id': _pid}
                                                logging.debug(f"[PlexTrash] metadata fetch set part_ids[{_bn!r}] = mid={_mid} pid={_pid}")
                        logging.info(f"[PlexTrash] Metadata result: {_media_count} Media elements, all_files now={_sfi.get('all_files')}")
                    except Exception as _sfe:
                        logging.info(f"[PlexTrash] Metadata fetch EXCEPTION for rk={_sfi.get('rating_key')}: {_sfe}")

            for item in in_rd:
                all_files = item.pop('all_files', [])
                deleted_at_set = set(item.pop('deleted_at_files', []))
                if len(all_files) <= 1:
                    continue
                unmatched = []
                matched = []
                if deleted_at_set:
                    # Primary signal: Plex's deletedAt flag identifies orphaned files
                    for fp in all_files:
                        if fp in deleted_at_set:
                            unmatched.append(fp)
                        else:
                            matched.append(fp)
                else:
                    # Fallback: compare parent folder against the specific matched RD torrent
                    matched_rnn = _norm(item.get('rd_filename', ''))
                    for fp in all_files:
                        fn = _norm(_folder_from_path(fp))
                        is_matched = bool(fn and matched_rnn and
                                          (fn in matched_rnn or matched_rnn.startswith(fn)))
                        if is_matched:
                            matched.append(fp)
                        else:
                            unmatched.append(fp)
                if unmatched and matched:
                    item['bad_file'] = _os.path.basename(unmatched[0])
                    item['has_good_file'] = True
                    item['good_file'] = _os.path.basename(matched[0])
                    item['bad_file_orphaned'] = bool(deleted_at_set and unmatched[0] in deleted_at_set)

            for item in not_in_rd:
                item.pop('all_files', None)

            # Title match count: how many RD entries share the same title+year
            for item in in_rd + not_in_rd:
                title_norm = _norm(item.get('title', ''))
                year = str(item.get('year', '') or '').strip()
                tk = f"{title_norm} {year}".strip() if year else title_norm
                item['title_match_count'] = sum(
                    1 for _, _, rnn, _ in rd_entries if tk and rnn.startswith(tk)
                ) if tk else 0
        else:
            for item in unique_items:
                item['title_match_count'] = 0
                item.pop('all_files', None)
            not_in_rd = unique_items

        # Enrich not_in_rd items with hash from filled_by_magnet in media_items DB
        if not_in_rd:
            try:
                import re as _mre
                from database.core import get_db_connection as _get_conn
                _mconn = _get_conn()
                # Build lookup: (lookup_title, year) -> hash
                # For episodes use show_title, for movies use title
                _magnet_map = {}
                _lookup_pairs = set()
                for _item in not_in_rd:
                    _lt = (_item.get('show_title') or _item.get('title', '')).strip()
                    _ly = str(_item.get('year', '') or '').strip()
                    if _lt:
                        _lookup_pairs.add((_lt, _ly))
                for _lt, _ly in _lookup_pairs:
                    _row = _mconn.execute(
                        "SELECT filled_by_magnet FROM media_items "
                        "WHERE title = ? AND filled_by_magnet IS NOT NULL LIMIT 1",
                        (_lt,)
                    ).fetchone()
                    if _row and _row[0]:
                        _hm = _mre.search(r'btih:([a-fA-F0-9]{32,40})', _row[0], _mre.IGNORECASE)
                        if _hm:
                            _magnet_map[(_lt, _ly)] = _hm.group(1).lower()
                _mconn.close()
                for _item in not_in_rd:
                    if _item.get('rd_hash'):
                        continue
                    _lt = (_item.get('show_title') or _item.get('title', '')).strip()
                    _ly = str(_item.get('year', '') or '').strip()
                    _h = _magnet_map.get((_lt, _ly)) or _magnet_map.get((_lt, ''))
                    if _h:
                        _item['rd_hash'] = _h
                logging.debug(f"[PlexTrash] Magnet lookup enriched {sum(1 for i in not_in_rd if i.get('rd_hash'))} not-in-RD items")
            except Exception as _me:
                logging.debug(f"[PlexTrash] Magnet lookup error: {_me}")

        result = {'in_rd': in_rd, 'not_in_rd': not_in_rd}
        _plex_trash_items_cache['items'] = result
        _plex_trash_items_cache['ts'] = time.time()
        return jsonify({'success': True, 'cached': False, 'rd_library_status': rd_library_status, **result})
    except Exception as e:
        logging.error(f"Plex trash items API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/plex_trash_purge', methods=['POST'])
@admin_required
def api_plex_trash_purge():
    try:
        import requests
        from utilities.settings import get_setting

        data = request.get_json(silent=True) or {}
        rating_keys = data.get('rating_keys', [])
        if not rating_keys:
            return jsonify({'success': False, 'error': 'No rating keys provided'}), 400

        plex_url = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
        if not plex_url or not plex_token:
            return jsonify({'success': False, 'error': 'Plex URL or token not configured'})

        headers = {'X-Plex-Token': plex_token}
        deleted = []
        failed = []

        def purge_one(rk):
            try:
                r = requests.delete(
                    f'{plex_url}/library/metadata/{rk}',
                    headers=headers,
                    timeout=30
                )
                if r.status_code in (200, 204):
                    return rk, None
                return rk, f'HTTP {r.status_code}'
            except Exception as e:
                return rk, str(e)

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(purge_one, rk): rk for rk in rating_keys}
            for f in as_completed(futures):
                rk, err = f.result()
                if err:
                    failed.append({'key': rk, 'error': err})
                else:
                    deleted.append(rk)

        # Invalidate trash caches
        _plex_trash_items_cache['items'] = None
        _plex_trash_items_cache['ts'] = 0
        _plex_trash_cache['filenames'] = None
        _plex_trash_cache['ts'] = 0

        return jsonify({'success': True, 'deleted': deleted, 'failed': failed})
    except Exception as e:
        logging.error(f"Plex trash purge error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/plex_delete_part', methods=['POST'])
@admin_required
def api_plex_delete_part():
    """Delete a single media part (file) from a Plex item without deleting the whole item."""
    try:
        import requests
        from utilities.settings import get_setting

        data = request.get_json(silent=True) or {}
        rating_key = data.get('rating_key', '')
        media_id = data.get('media_id', '')
        part_id = data.get('part_id', '')

        if not all([rating_key, media_id]):
            return jsonify({'success': False, 'error': 'rating_key and media_id are required'}), 400

        plex_url = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
        if not plex_url or not plex_token:
            return jsonify({'success': False, 'error': 'Plex URL or token not configured'})

        headers = {'X-Plex-Token': plex_token}
        # Use /media/{mid} endpoint — /media/{mid}/parts/{pid} returns 404 for soft-deleted
        # (deletedAt) media versions which is the common case in Plex trash
        url = f'{plex_url}/library/metadata/{rating_key}/media/{media_id}'
        r = requests.delete(url, headers=headers, timeout=15)

        if r.status_code in (200, 204):
            # Invalidate trash caches so next load is fresh
            _plex_trash_items_cache['items'] = None
            _plex_trash_items_cache['ts'] = 0
            _plex_trash_cache['filenames'] = None
            _plex_trash_cache['ts'] = 0
            return jsonify({'success': True})
        body_preview = r.text[:200] if r.text else ''
        logging.warning(f"Plex delete part HTTP {r.status_code}: url={url} body={body_preview!r}")
        return jsonify({'success': False, 'error': f'Plex returned HTTP {r.status_code}: {body_preview}'}), 502
    except Exception as e:
        logging.error(f"Plex delete part error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/plex_force_scan', methods=['POST'])
@admin_required
def api_plex_force_scan():
    """Trigger a media-server library scan/refresh. Supports Plex and Jellyfin/Emby."""
    from utilities.settings import get_setting
    try:
        data = request.get_json(silent=True) or {}
        paths = data.get('paths', [])
        if not paths:
            return jsonify({'success': False, 'error': 'No paths provided'}), 400

        media_server = get_setting('File Management', 'media_server_type', 'plex')

        if media_server == 'jellyfin':
            jf_url   = get_setting('Debug', 'emby_jellyfin_url', default='').rstrip('/')
            jf_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()
            if not jf_url or not jf_token:
                return jsonify({'success': False, 'error': 'Jellyfin URL or token not configured in Settings → Debug (emby_jellyfin_url / emby_jellyfin_token)'})
            import requests as _rq
            headers = {'X-Emby-Token': jf_token, 'Content-Type': 'application/json'}
            # Notify Jellyfin of updated paths, then trigger a full library refresh
            errors = []
            for path in paths:
                try:
                    _rq.post(
                        f'{jf_url}/Library/Media/Updated',
                        headers=headers,
                        json=[{'Path': path, 'UpdateType': 'Modified'}],
                        timeout=10
                    )
                except Exception as _e:
                    errors.append(str(_e))
            # Always trigger a background library refresh so Jellyfin picks up changes
            try:
                _rq.post(f'{jf_url}/Library/Refresh', headers=headers, timeout=10)
            except Exception as _e:
                errors.append(f'Refresh trigger failed: {_e}')
            return jsonify({'success': len(errors) == 0, 'paths_scanned': paths, 'errors': errors})
        else:
            from utilities.plex_functions import scan_and_empty_plex_trash
            result = scan_and_empty_plex_trash(paths=paths, empty_trash=False)
            return jsonify({'success': result.get('success', False),
                            'paths_scanned': result.get('paths_scanned', []),
                            'errors': result.get('errors', [])})
    except Exception as e:
        logging.error(f"Media server scan error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
        _reconcile_cache['data'] = None
        with _lib['lock']:
            if _lib['stable']:
                _lib['stable']['torrents'] = [
                    t for t in _lib['stable']['torrents'] if t.get('id') != torrent_id
                ]
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
        _reconcile_cache['data'] = None
        deleted_set = set(results['deleted'])
        with _lib['lock']:
            if _lib['stable']:
                _lib['stable']['torrents'] = [
                    t for t in _lib['stable']['torrents'] if t.get('id') not in deleted_set
                ]

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
    global _lib_last_accessed
    force = request.args.get('force') == '1'
    now   = time.time()
    _lib_last_accessed = now  # track for idle eviction

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
                new_stable = {
                    'torrents':    initial,
                    'total':       len(initial),
                    'total_bytes': sum(t.get('bytes', 0) or 0 for t in initial),
                    'fetched_at':  time.time(),
                }
                with _lib['lock']:
                    if _lib['gen'] == gen:
                        _lib['stable'] = new_stable
                        _lib['partial'] = []
                        _lib['loading'] = False
                _save_lib_cache_to_db(new_stable)
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


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
_reconcile_cache = {'data': None, 'ts': 0}
_RECONCILE_TTL = 300  # 5 minutes


@debrid_manager_bp.route('/api/reconcile')
@admin_required
def api_reconcile():
    """
    Cross-reference media_items (state=Collected) against the RD library cache
    and a live Plex library fetch to surface discrepancies.

    Categories:
      healthy       — Collected + torrent in RD + found in Plex  (count only)
      not_in_plex   — Collected + torrent in RD  + NOT in Plex
      torrent_gone  — Collected + NOT in RD      + in Plex
      lost          — Collected + NOT in RD      + NOT in Plex
      untracked_rd  — RD torrent IDs not referenced by any Collected DB row
    """
    try:
        import os as _os
        import requests as _req
        import xml.etree.ElementTree as ET
        import re as _re
        from utilities.settings import get_setting
        from database.core import get_db_connection

        force = request.args.get('force') == '1'

        if (not force
                and _reconcile_cache['data'] is not None
                and (time.time() - _reconcile_cache['ts']) < _RECONCILE_TTL):
            return jsonify({'success': True, 'cached': True, **_reconcile_cache['data']})

        # ── 1. RD library cache ────────────────────────────────────────────
        # When force-refreshing the audit, also kick off a background library
        # re-fetch so the next audit run uses up-to-date RD torrent data.
        # This is non-blocking — reconcile still runs against the current stable
        # snapshot; the background fetch updates the cache for the next call.
        if force:
            with _lib['lock']:
                is_already_loading = _lib['loading']
            if not is_already_loading:
                try:
                    provider = get_debrid_provider()
                    with _lib['lock']:
                        _lib['gen'] += 1
                        _gen = _lib['gen']
                        _lib['loading'] = True
                        _lib['partial'] = []
                    threading.Thread(
                        target=_fetch_pages_bg,
                        args=(provider.api_key, 1, _gen),
                        daemon=True,
                    ).start()
                    logging.info('[Reconcile] Force refresh — triggered background RD library re-fetch')
                except Exception as _e:
                    logging.warning(f'[Reconcile] Could not trigger library re-fetch: {_e}')

        _load_lib_cache_from_db()
        with _lib['lock']:
            stable = _lib['stable']

        if stable is None:
            return jsonify({
                'success': False,
                'error': 'RD library not yet loaded — visit the Torrents tab first, then retry.'
            })

        rd_torrents  = stable['torrents']
        rd_id_set    = {t['id'] for t in rd_torrents if t.get('id')}
        rd_id_to_fn  = {t['id']: t.get('filename', '') for t in rd_torrents}
        rd_total     = len(rd_id_set)
        # Hash match — torrent deleted and re-added to RD (same hash, new ID).
        rd_hash_set  = {t['hash'].lower() for t in rd_torrents if t.get('hash')}
        # Filename match — RD torrent name equals a folder/file name in location_on_disk.
        # This catches items whose filled_by_torrent_id is NULL but whose mount path
        # still contains the RD torrent folder name (season packs, movies, single eps).
        rd_filename_set = {t['filename'].lower() for t in rd_torrents if t.get('filename')}

        # ── 2. Live media-server library fetch (active items only) ────────────
        # plex_file_set: basenames of every file the media server knows about.
        # Used for "in_library" checks — filled_by_file/location_on_disk basename matching.
        plex_file_set    = set()   # basenames  — matches single-file torrent names
        plex_folder_set  = set()   # parent dirs — matches pack/folder torrent names
        plex_rating_keys = set()   # ratingKeys of every Video Plex knows about
        plex_total = 0

        _media_server = get_setting('File Management', 'media_server_type', 'plex')

        if _media_server == 'jellyfin':
            # ── Jellyfin / Emby ──────────────────────────────────────────────
            _jf_url   = get_setting('Debug', 'emby_jellyfin_url', default='').rstrip('/')
            _jf_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()
            if _jf_url and _jf_token:
                _jf_headers = {'X-Emby-Token': _jf_token, 'Accept': 'application/json'}
                try:
                    # Fetch all Movie + Episode items with Path field, paginated
                    _jf_start = 0
                    _jf_limit = 5000
                    while True:
                        _jr = _req.get(
                            f'{_jf_url}/Items',
                            headers=_jf_headers,
                            params={
                                'Recursive': 'true',
                                'IncludeItemTypes': 'Movie,Episode',
                                'fields': 'Path',
                                'StartIndex': _jf_start,
                                'Limit': _jf_limit,
                            },
                            timeout=30
                        )
                        _jd = _jr.json()
                        _jf_items = _jd.get('Items', [])
                        for _ji in _jf_items:
                            _fp = (_ji.get('Path') or '').rstrip('/')
                            if not _fp:
                                continue
                            _parts = _fp.rsplit('/', 2)
                            plex_file_set.add(_parts[-1].lower())
                            if len(_parts) >= 2:
                                plex_folder_set.add(_parts[-2].lower())
                        if len(_jf_items) < _jf_limit:
                            break
                        _jf_start += _jf_limit
                    plex_total = len(plex_file_set)
                    logging.info(f"[Reconcile] Jellyfin library: {plex_total} files fetched")
                except Exception as _jf_err:
                    logging.warning(f"[Reconcile] Jellyfin fetch failed (continuing without): {_jf_err}")
        else:
            # ── Plex ─────────────────────────────────────────────────────────
            plex_url   = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
            if plex_url and plex_token:
                _ph = {'X-Plex-Token': plex_token, 'Accept': 'application/xml'}
                try:
                    _sr = _req.get(f'{plex_url}/library/sections', headers=_ph, timeout=10)
                    # Plex media type codes: 1=movie, 4=episode (returns Video+Part/file)
                    _sections = []
                    for d in ET.fromstring(_sr.text).findall('Directory'):
                        sid = d.get('key')
                        if d.get('type') == 'movie':
                            _sections.append((sid, '1'))
                        elif d.get('type') == 'show':
                            _sections.append((sid, '4'))

                    def _plex_section_filenames(sid, mtype):
                        names = []
                        start = 0
                        page_size = 5000
                        while True:
                            r2 = _req.get(
                                f'{plex_url}/library/sections/{sid}/all',
                                headers=_ph,
                                params={'type': mtype, 'trash': '0',
                                        'X-Plex-Container-Start': start,
                                        'X-Plex-Container-Size': page_size},
                                timeout=30
                            )
                            root2 = ET.fromstring(r2.text)
                            videos = root2.findall('Video')
                            for video in videos:
                                rk = video.get('ratingKey', '')
                                if rk:
                                    names.append(('rk', rk))
                                for part in video.iter('Part'):
                                    fp = part.get('file', '')
                                    if fp:
                                        fp = fp.rstrip('/')
                                        parts = fp.rsplit('/', 2)
                                        names.append(('f', parts[-1].lower()))
                                        if len(parts) >= 2:
                                            names.append(('d', parts[-2].lower()))
                            if len(videos) < page_size:
                                break
                            start += page_size
                        return names

                    with ThreadPoolExecutor(max_workers=4) as _ex:
                        _futs = {_ex.submit(_plex_section_filenames, sid, mt): sid
                                 for sid, mt in _sections}
                        for _f in as_completed(_futs):
                            try:
                                for _kind, _name in _f.result():
                                    if _kind == 'f':
                                        plex_file_set.add(_name)
                                    elif _kind == 'd':
                                        plex_folder_set.add(_name)
                                    elif _kind == 'rk':
                                        plex_rating_keys.add(_name)
                            except Exception as _pe:
                                logging.warning(f"[Reconcile] Plex section fetch error: {_pe}")
                    plex_total = len(plex_file_set)
                except Exception as _plex_err:
                    logging.warning(f"[Reconcile] Plex fetch failed (continuing without): {_plex_err}")

        # Detect symlink mode early — needed for all_known_rd_names path strategy.
        _file_mgmt    = get_setting('File Management', 'file_collection_management', 'Plex')
        _symlink_mode = (_file_mgmt == 'Symlinked/Local')

        # ── 3. Query DB ─────────────────────────────────────────────────────
        conn = get_db_connection()
        try:
            db_rows = conn.execute("""
                SELECT id, title, year, type, imdb_id,
                       filled_by_file, filled_by_torrent_id,
                       filled_by_magnet, ms_item_id, location_on_disk
                FROM media_items
                WHERE state = 'Collected'
            """).fetchall()
            # All torrent IDs across every state — items in Checking/Adding/etc.
            # are still tracked by the app; don't report them as untracked.
            all_tracked_tids = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT filled_by_torrent_id FROM media_items"
                    " WHERE filled_by_torrent_id IS NOT NULL AND filled_by_torrent_id != ''"
                ).fetchall()
            }
            # Magnet hashes for torrent re-add detection: same torrent re-added to
            # RD gets a new ID but same hash — skip it in untracked if hash matches.
            _btih_re_local = _re.compile(r'btih:([a-fA-F0-9]{32,40})', _re.IGNORECASE)
            all_tracked_hashes = set()
            for (_mag,) in conn.execute(
                "SELECT DISTINCT filled_by_magnet FROM media_items"
                " WHERE filled_by_magnet IS NOT NULL AND filled_by_magnet != ''"
            ).fetchall():
                _hm = _btih_re_local.search(_mag)
                if _hm:
                    all_tracked_hashes.add(_hm.group(1).lower())
            # Build a set of known names that can match an RD t['filename'].
            # t['filename'] is a torrent/pack folder name or bare filename.
            # Non-symlink: location_on_disk = /zurg/__all__/PackFolder/file.mkv
            #   → parts[-2] = pack folder name → direct match.
            # Symlink mode: location_on_disk = /mnt/symlinked/Show/Season 02/renamed.mkv
            #   → parts[-2] = "Season 02" → useless for RD matching.
            #   Read the symlink target to get the actual RD torrent folder name.
            # Only items without filled_by_torrent_id need path-based matching —
            # items with a torrent ID are already covered by all_tracked_tids.
            all_known_rd_names = set()
            for _r in conn.execute(
                "SELECT location_on_disk, filled_by_file FROM media_items"
                " WHERE filled_by_torrent_id IS NULL"
                " AND (location_on_disk IS NOT NULL OR filled_by_file IS NOT NULL)"
            ).fetchall():
                loc, fbf = _r[0], _r[1]
                if fbf:
                    all_known_rd_names.add(fbf.lower())
                if loc:
                    if _symlink_mode:
                        # Read the symlink target to extract the RD torrent/pack
                        # folder name (parts[-2] of the target path).
                        try:
                            _target = _os.readlink(loc)
                            _tparts = [p for p in _target.replace('\\', '/').split('/') if p]
                            for _tp in _tparts[-2:]:
                                all_known_rd_names.add(_tp.lower())
                        except OSError:
                            pass  # broken/missing symlink — filled_by_file already added
                    else:
                        # Non-symlink: parts[-2] = pack folder, parts[-1] = filename.
                        _lparts = [p for p in loc.replace('\\', '/').split('/') if p]
                        for _p in _lparts[-2:]:
                            all_known_rd_names.add(_p.lower())
        finally:
            conn.close()

        db_total = len(db_rows)

        _BTIH_RE = _re.compile(r'btih:([a-fA-F0-9]{32,40})', _re.IGNORECASE)

        def _basename(fpath):
            """Return the bare filename from a path or filename string."""
            if not fpath:
                return ''
            return fpath.rstrip('/').rsplit('/', 1)[-1]

        def _row_dict(row):
            _m = _BTIH_RE.search(row['filled_by_magnet'] or '')
            return {
                'id':                   row['id'],
                'title':                row['title'] or '',
                'year':                 row['year'] or '',
                'type':                 row['type'] or '',
                'imdb_id':              row['imdb_id'] or '',
                'filled_by_file':       row['filled_by_file'] or '',
                'filled_by_torrent_id': row['filled_by_torrent_id'] or '',
                'magnet_hash':          _m.group(1).lower() if _m else '',
                'ms_item_id':           row['ms_item_id'],
                'rd_filename':          rd_id_to_fn.get(row['filled_by_torrent_id'] or '', ''),
                'location_on_disk':     row['location_on_disk'] or '',
            }

        # Zurg __all__ mount path — used for precise Match 3 in both modes.
        # Stripping this prefix from location_on_disk and taking the next component
        # gives the exact torrent/pack folder name that RD assigns.
        _zurg_all = (
            get_setting('Plex', 'mounted_file_location', '')
            or get_setting('File Management', 'original_files_path', '/mnt/zurg/__all__')
        ).rstrip('/')

        if _symlink_mode:
            # Auto-detect the actual rclone/debrid mount prefix from live symlink targets.
            # The configured path may differ from the real mount (e.g. user has
            # /mnt/debrid/clid/__all__ but setting defaults to /mnt/zurg/__all__).
            # Sample location_on_disk values from already-fetched db_rows to find the
            # common target prefix. If detected, use it; otherwise fall back to the
            # configured path. Empty string = existence-only check (no target validation).
            _detected_rclone_dir = ''
            _prefix_counts: dict = {}
            for _row in db_rows[:50]:
                _sloc = _row['location_on_disk'] or ''
                if not _sloc:
                    continue
                try:
                    if os.path.islink(_sloc):
                        _target = os.readlink(_sloc)
                        # Extract everything up to and including __all__ segment
                        _tidx = _target.find('/__all__')
                        if _tidx != -1:
                            _candidate = _target[:_tidx + len('/__all__')]
                            _prefix_counts[_candidate] = _prefix_counts.get(_candidate, 0) + 1
                        else:
                            # No __all__ — use first 2 path components as prefix
                            _tparts = [p for p in _target.replace('\\', '/').split('/') if p]
                            if len(_tparts) >= 2:
                                _candidate = '/' + '/'.join(_tparts[:2])
                                _prefix_counts[_candidate] = _prefix_counts.get(_candidate, 0) + 1
                except OSError:
                    pass
            if _prefix_counts:
                _detected_rclone_dir = max(_prefix_counts, key=_prefix_counts.get)

            # Use detected prefix if found, otherwise fall back to configured path.
            # Empty string means "skip target validation — existence check only".
            _rclone_dir = _detected_rclone_dir or _zurg_all
        else:
            _rclone_dir = ''

        # Derive the RD mount root path component(s) from items currently confirmed
        # in RD. e.g. location_on_disk='/debrid/movies/...' → root='debrid'.
        # Count occurrences — the real RD mount will have thousands of items.
        # Outliers (NAS items whose torrent ID coincidentally matches an RD entry)
        # are excluded by only accepting roots with at least 5 items.
        _rd_root_counts: dict = {}
        for _row in db_rows:
            if _row['filled_by_torrent_id'] and _row['filled_by_torrent_id'] in rd_id_set:
                _loc_root = (_row['location_on_disk'] or '').lstrip('/').split('/')[0]
                if _loc_root:
                    _rd_root_counts[_loc_root] = _rd_root_counts.get(_loc_root, 0) + 1
        rd_mount_prefixes = {r for r, c in _rd_root_counts.items() if c >= 5}

        not_in_plex  = []
        torrent_gone = []
        lost         = []
        nas_items    = []
        healthy_count = 0
        nas_count     = 0

        for row in db_rows:
            tid     = row['filled_by_torrent_id']

            if plex_file_set:
                # In symlink mode Plex sees the symlink file (location_on_disk), not
                # the original RD filename (filled_by_file) — symlinkers rename files.
                # In RD mode, filled_by_file is the actual filename Plex sees.
                if _symlink_mode:
                    fname = _basename(row['location_on_disk'])
                else:
                    fname = _basename(row['filled_by_file'])
                in_plex = bool(fname and fname.lower() in plex_file_set)
                # Secondary check: if filename doesn't match but ms_item_id is a known
                # Plex ratingKey, the item IS in Plex (multi-version movies share a
                # ratingKey and one version's file may not appear in plex_file_set if
                # Plex hasn't scanned it yet or the filename differs slightly).
                if not in_plex and row['ms_item_id'] and plex_rating_keys:
                    in_plex = str(row['ms_item_id']) in plex_rating_keys
            else:
                # Plex unreachable — fall back to last known ms_item_id
                in_plex = bool(row['ms_item_id'])

            if _symlink_mode:
                # In symlink mode: healthy = in Plex + symlink exists + target is in rclone mount.
                # os.readlink() reads the link value without dereferencing (no rclone network hit).
                _loc = row['location_on_disk'] or ''
                try:
                    _symlink_ok = bool(
                        _loc
                        and os.path.islink(_loc)
                        and (_rclone_dir == '' or os.readlink(_loc).startswith(_rclone_dir))
                    )
                except OSError:
                    _symlink_ok = False
                if in_plex and _symlink_ok:
                    healthy_count += 1
                elif in_plex and not _symlink_ok:
                    torrent_gone.append(_row_dict(row))   # symlink broken / missing
                elif _symlink_ok and not in_plex:
                    not_in_plex.append(_row_dict(row))
                # else: no symlink and not in Plex — skip (item may not be collected yet)
            else:
                # RD mode: healthy = torrent still in RD + in Plex

                # Match 1: torrent ID stored at collection time
                in_rd = bool(tid and tid in rd_id_set)

                # Match 2: same hash, torrent re-added to RD with a new ID
                if not in_rd and row['filled_by_magnet']:
                    _hm = _BTIH_RE.search(row['filled_by_magnet'])
                    if _hm and _hm.group(1).lower() in rd_hash_set:
                        in_rd = True

                # Match 3: check location_on_disk components and filled_by_file against
                # rd_filename_set. The last 2 path components are TorrentFolder/filename.
                # _parts[-2] = torrent/pack folder, _parts[-1] = filename (single-file torrents
                # often have t['filename'] == the mkv name). Also check filled_by_file directly.
                if not in_rd:
                    _loc = (row['location_on_disk'] or '').replace('\\', '/')
                    _parts = [p for p in _loc.split('/') if p]
                    _fbf_lower = (row['filled_by_file'] or '').lower()
                    if (
                        (len(_parts) >= 2 and _parts[-2].lower() in rd_filename_set)
                        or (len(_parts) >= 1 and _parts[-1].lower() in rd_filename_set)
                        or (_fbf_lower and _fbf_lower in rd_filename_set)
                    ):
                        in_rd = True

                # Detect NAS / non-RD storage
                _item_root = (row['location_on_disk'] or '').lstrip('/').split('/')[0]
                is_nas = bool(rd_mount_prefixes and _item_root and _item_root not in rd_mount_prefixes)
                if is_nas:
                    nas_count += 1
                    nas_items.append(_row_dict(row))
                # has_rd_evidence: torrent ID, magnet hash, or filename — any one is
                # sufficient. is_nas guards against NAS/local items that coincidentally
                # have the same filename as an RD torrent.
                has_rd_evidence = (
                    bool(tid) or bool(row['filled_by_magnet']) or bool(row['filled_by_file'])
                ) and not is_nas
                was_ever_rd = has_rd_evidence

                if in_rd and in_plex:
                    healthy_count += 1
                elif in_rd and not in_plex:
                    not_in_plex.append(_row_dict(row))
                elif not in_rd and in_plex and was_ever_rd:
                    torrent_gone.append(_row_dict(row))
                elif not in_rd and not in_plex and has_rd_evidence:
                    lost.append(_row_dict(row))

        # Deduplicate torrent_gone by filled_by_torrent_id — season packs produce many
        # DB rows per torrent; collapse them to one representative row with affected_count.
        # Store all affected item IDs so re-queue/delete acts on every episode, not just one.
        _tg_seen: dict = {}
        _tg_deduped = []
        for _item in torrent_gone:
            _tid = _item['filled_by_torrent_id']
            if _tid:
                if _tid not in _tg_seen:
                    _item['affected_count'] = 1
                    _item['affected_ids'] = [_item['id']] if _item['id'] else []
                    _tg_seen[_tid] = _item
                    _tg_deduped.append(_item)
                else:
                    _tg_seen[_tid]['affected_count'] += 1
                    if _item['id']:
                        _tg_seen[_tid]['affected_ids'].append(_item['id'])
            else:
                _item['affected_count'] = 1
                _item['affected_ids'] = [_item['id']] if _item['id'] else []
                _tg_deduped.append(_item)
        torrent_gone = _tg_deduped

        # ── 4. Untracked RD torrents ───────────────────────────────────────
        # A torrent is "tracked" if any of these match:
        #   1. filled_by_torrent_id == t['id']   (primary — stored at collection time)
        #   2. filled_by_magnet btih hash == t['hash']  (torrent re-added, same hash)
        #   3. t['filename'] is a path component of any location_on_disk  (items stored
        #      without a torrent ID but whose location path includes the pack folder name)
        untracked_rd = []
        for t in rd_torrents:
            if not t.get('id'):
                continue
            if t['id'] in all_tracked_tids:
                continue
            # Re-added torrent: same hash, new ID
            if t.get('hash') and t['hash'].lower() in all_tracked_hashes:
                continue
            fname = t.get('filename', '')
            if fname and fname.lower() in all_known_rd_names:
                continue  # folder/filename found in a DB item's path
            # Check if this torrent's content exists in Plex even without a DB entry.
            # Single-file torrent: fname matches a Plex file basename.
            # Pack/folder torrent: fname matches a Plex parent folder name.
            _in_plex = bool(fname and (fname.lower() in plex_file_set or fname.lower() in plex_folder_set))
            untracked_rd.append({
                'id':                   '',
                'title':                fname,
                'year':                 '',
                'type':                 '',
                'imdb_id':              '',
                'filled_by_file':       '',
                'filled_by_torrent_id': t['id'],
                'magnet_hash':          '',
                'ms_item_id':           None,
                'rd_filename':          fname,
                'in_plex':              _in_plex,
                'db_episode_match':     None,  # populated in pass below
            })

        # ── 4b. Untracked pack → DB episode fuzzy match ────────────────────
        # Detects season packs in RD that serve already-tracked episodes under
        # a different torrent ID.  The pack and episode names normalise to the
        # same string once season/episode markers are stripped.
        # This is PURELY informational — nothing is skipped or changed above.
        if untracked_rd:
            import re as _re2
            def _norm_release(name):
                n = (name or '').rstrip('/')
                n = _re2.sub(r'\.\w{2,5}$', '', n)          # strip extension
                n = n.replace('.', ' ')                       # dots → spaces
                n = _re2.sub(r'\bS\d{1,2}E\d{2,3}(?:[-–E]\d{2,3})*\b', '', n, flags=_re2.IGNORECASE)  # S01E08
                n = _re2.sub(r'\bS\d{1,2}\b', '', n, flags=_re2.IGNORECASE)   # S01 (season-only)
                n = _re2.sub(r'\s+', ' ', n).strip().lower()
                return n

            # Build lookup: normalized_release_name → {file, torrent_id}
            # Only consider tracked items (those with a filled_by_torrent_id).
            _ep_norm_map = {}   # norm_name → first matching {filled_by_file, filled_by_torrent_id}
            try:
                _ep_conn = get_db_connection()
                _ep_rows = _ep_conn.execute(
                    "SELECT filled_by_file, filled_by_torrent_id FROM media_items"
                    " WHERE filled_by_torrent_id IS NOT NULL AND filled_by_torrent_id != ''"
                    " AND filled_by_file IS NOT NULL AND filled_by_file != ''"
                ).fetchall()
                _ep_conn.close()
                for _ef, _et in _ep_rows:
                    _k = _norm_release(_ef)
                    if _k and _k not in _ep_norm_map:
                        _ep_norm_map[_k] = {'matched_file': _ef, 'db_torrent_id': _et}
            except Exception:
                _ep_norm_map = {}

            for _u in untracked_rd:
                _nk = _norm_release(_u['rd_filename'])
                if _nk and _nk in _ep_norm_map:
                    _u['db_episode_match'] = _ep_norm_map[_nk]

            # Items with a DB match are tracked in the DB (different torrent ID) — exclude from untracked
            untracked_rd = [_u for _u in untracked_rd if not _u.get('db_episode_match')]

        # Rclone file count:
        # - Symlink mode: read from symlink audit cache (populated by background audit scan)
        # - Plex mode:    read from _rclone_count_state cache; trigger background walk if
        #                 idle/stale so subsequent loads get a real count instantly
        rclone_total = 0
        if _symlink_mode:
            with _symlink_audit_state['lock']:
                _sa = _symlink_audit_state.get('data')
                if _sa:
                    rclone_total = _sa.get('stats', {}).get('rclone_scanned', 0)
        else:
            _mount_path = (get_setting('Plex', 'mounted_file_location', '') or
                           get_setting('File Management', 'original_files_path', ''))
            if _mount_path and os.path.isdir(_mount_path):
                _spawn_rclone = False
                _run_inline = False
                with _rclone_count_state['lock']:
                    _rc_count  = _rclone_count_state['count']
                    _rc_ts     = _rclone_count_state['ts']
                    _rc_status = _rclone_count_state['status']
                    _stale = (time.time() - _rc_ts) > _RCLONE_COUNT_STALE
                    if _rc_status == 'idle':
                        # Never scanned — run inline so first call returns a real count
                        _rclone_count_state['status'] = 'scanning'
                        _run_inline = True
                    elif _rc_status == 'done' and _stale:
                        # Stale — refresh in background, return old count immediately
                        _rclone_count_state['status'] = 'scanning'
                        _spawn_rclone = True
                if _run_inline:
                    _run_rclone_count_bg(_mount_path)
                    with _rclone_count_state['lock']:
                        _rc_count = _rclone_count_state['count']
                elif _spawn_rclone:
                    threading.Thread(
                        target=_run_rclone_count_bg,
                        args=(_mount_path,),
                        daemon=True,
                        name='rclone-count-bg'
                    ).start()
                rclone_total = _rc_count

        result = {
            'healthy_count': healthy_count,
            'not_in_plex':   not_in_plex,
            'torrent_gone':  torrent_gone,
            'lost':          lost,
            'untracked_rd':  untracked_rd,
            'plex_total':    plex_total,
            'rd_total':      rd_total,
            'db_total':      db_total,
            'rclone_total':  rclone_total,
            'nas_total':     nas_count,
            'nas_items':     nas_items,
        }
        _reconcile_cache['data'] = result
        _reconcile_cache['ts']   = time.time()

        return jsonify({'success': True, 'cached': False, **result})

    except Exception as e:
        logging.error(f"[Reconcile] API error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/clear_cache', methods=['POST'])
@admin_required
def api_clear_cache():
    """Clear the RD library cache and reconcile cache, forcing a full re-fetch."""
    global _lib_db_loaded
    with _lib['lock']:
        _lib['stable']  = None
        _lib['partial'] = []
        _lib['loading'] = False
        _lib['gen']    += 1
    _lib_db_loaded = False
    _reconcile_cache['data'] = None
    _reconcile_cache['ts']   = 0
    # Also clear the persisted RD library cache in DB so stale data isn't reloaded
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        conn.execute("DELETE FROM rd_library_cache")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f'[LibCache] Could not clear rd_library_cache table: {e}')
    logging.info('[LibCache] Cache cleared manually via Debrid Manager')
    return jsonify({'success': True})


@debrid_manager_bp.route('/api/reconcile/bulk_requeue', methods=['POST'])
@admin_required
def api_reconcile_bulk_requeue():
    """Move a list of media item IDs back to Wanted state for re-scraping."""
    from database.core import get_db_connection
    data = request.get_json(silent=True) or {}
    item_ids = data.get('item_ids', [])
    if not item_ids or not isinstance(item_ids, list):
        return jsonify({'success': False, 'error': 'item_ids required'}), 400
    try:
        conn = get_db_connection()
        updated = 0
        for iid in item_ids:
            try:
                conn.execute(
                    "UPDATE media_items SET state='Wanted', filled_by_torrent_id=NULL,"
                    " filled_by_magnet=NULL, filled_by_file=NULL, filled_by_title=NULL,"
                    " scrape_results=NULL WHERE id=? AND state='Collected'",
                    (int(iid),)
                )
                updated += conn.execute("SELECT changes()").fetchone()[0]
            except Exception:
                pass
        conn.commit()
        conn.close()
        # Invalidate reconcile cache
        _reconcile_cache['data'] = None
        return jsonify({'success': True, 'updated': updated})
    except Exception as e:
        logging.error(f"[Reconcile] bulk_requeue error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/reconcile/bulk_delete_db', methods=['POST'])
@admin_required
def api_reconcile_bulk_delete_db():
    """Permanently delete a list of media item IDs from the database."""
    from database.core import get_db_connection
    data = request.get_json(silent=True) or {}
    item_ids = data.get('item_ids', [])
    if not item_ids or not isinstance(item_ids, list):
        return jsonify({'success': False, 'error': 'item_ids required'}), 400
    try:
        conn = get_db_connection()
        deleted = 0
        for iid in item_ids:
            try:
                conn.execute("DELETE FROM media_items WHERE id=?", (int(iid),))
                deleted += conn.execute("SELECT changes()").fetchone()[0]
            except Exception:
                pass
        conn.commit()
        conn.close()
        _reconcile_cache['data'] = None
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        logging.error(f"[Reconcile] bulk_delete_db error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Symlink Audit
# ---------------------------------------------------------------------------

_SYMLINK_AUDIT_STALE_AFTER = 24 * 3600   # auto-rescan if cache older than 24h
_SYMLINK_AUDIT_CACHE_FILE  = os.path.join(
    os.environ.get('USER_DB_CONTENT', '/user/db_content'),
    'symlink_audit_cache.json'
)

_symlink_audit_state = {
    'status': 'idle',   # idle | scanning | done | error
    'data':   None,
    'error':  None,
    'ts':     0,
    'lock':   threading.Lock(),
}

# Rclone file count cache for Plex mode — populated by background thread,
# served instantly from cache; staleness threshold 1 hour.
_RCLONE_COUNT_STALE = 3600
_rclone_count_state = {
    'count':  0,
    'ts':     0.0,
    'status': 'idle',   # idle | scanning | done
    'lock':   threading.Lock(),
}


def _run_rclone_count_bg(mount_path):
    """Background thread: count files under mount_path using parallel walk."""
    try:
        top_entries = list(os.scandir(mount_path))
    except OSError as e:
        logging.warning(f"[RcloneCount] Cannot scan {mount_path}: {e}")
        with _rclone_count_state['lock']:
            _rclone_count_state['status'] = 'idle'
        return

    top_files = [e for e in top_entries if e.is_file(follow_symlinks=False)]
    top_dirs  = [e.path for e in top_entries if e.is_dir(follow_symlinks=False)]

    total = len(top_files)
    with ThreadPoolExecutor(max_workers=16) as ex:
        for pairs in ex.map(_walk_rclone_dir, top_dirs):
            total += len(pairs)

    with _rclone_count_state['lock']:
        _rclone_count_state['count']  = total
        _rclone_count_state['ts']     = time.time()
        _rclone_count_state['status'] = 'done'
    logging.info(f"[RcloneCount] Counted {total} files under {mount_path}")


def _symlink_audit_load_cache():
    """Load persisted symlink audit cache from disk into memory at startup."""
    try:
        if os.path.exists(_SYMLINK_AUDIT_CACHE_FILE):
            with open(_SYMLINK_AUDIT_CACHE_FILE, 'r') as f:
                saved = json.load(f)
            data = saved.get('data')
            ts   = saved.get('ts', 0)
            if data and ts:
                with _symlink_audit_state['lock']:
                    _symlink_audit_state['status'] = 'done'
                    _symlink_audit_state['data']   = data
                    _symlink_audit_state['ts']     = ts
                age_h = (time.time() - ts) / 3600
                logging.info(f"[SymlinkAudit] Loaded cached results from disk (age: {age_h:.1f}h)")
    except Exception as exc:
        logging.warning(f"[SymlinkAudit] Could not load cache from disk: {exc}")


def _symlink_audit_save_cache(data, ts):
    """Persist symlink audit results to disk."""
    try:
        with open(_SYMLINK_AUDIT_CACHE_FILE, 'w') as f:
            json.dump({'data': data, 'ts': ts}, f)
        logging.info(f"[SymlinkAudit] Cache saved to disk")
    except Exception as exc:
        logging.warning(f"[SymlinkAudit] Could not save cache to disk: {exc}")


# Load any existing cache immediately at import time
_symlink_audit_load_cache()


def _extract_ep(filename):
    result = set()
    for m in re.finditer(r"[Ss](\d+)[Ee](\d+)((?:[-]?[Ee]?\d+)*)", filename):
        season = int(m.group(1))
        result.add((season, int(m.group(2))))
        for ep in re.findall(r"[-]?[Ee]?(\d+)", m.group(3)):
            if ep:
                result.add((season, int(ep)))
    return result


def _extract_ep_from_symlink(symlink_path):
    name = os.path.basename(symlink_path)
    clean = re.split(r" - Default - ", name)[0]
    return _extract_ep(clean)


def _fmt_size(b):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024:
            return f'{b:.1f} {unit}'
        b /= 1024
    return f'{b:.1f} PB'


def _walk_rclone_dir(top_dir):
    """Walk a single rclone torrent directory, returning (path, size) tuples.
    Size is read from the DirEntry stat cache to avoid extra FUSE stat calls."""
    results = []
    try:
        for entry in os.scandir(top_dir):
            if entry.is_file(follow_symlinks=False):
                try:
                    sz = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = 0
                results.append((entry.path, sz))
            elif entry.is_dir(follow_symlinks=False):
                # Deeper nesting (e.g. season subdirectories)
                for sub_root, _dirs, sub_files in os.walk(entry.path):
                    for name in sub_files:
                        fp = os.path.join(sub_root, name)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            sz = 0
                        results.append((fp, sz))
    except OSError:
        pass
    return results


def _run_symlink_audit_bg(rclone_dir, symlink_dir):
    """Background thread: walk dirs, build results, store in _symlink_audit_state."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        # Step 1: build symlink target index
        target_to_links = defaultdict(list)
        total_symlinks = 0
        for root, dirs, files in os.walk(symlink_dir, followlinks=False):
            for name in files:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    total_symlinks += 1
                    raw = os.readlink(path)
                    target = raw if os.path.isabs(raw) else os.path.normpath(
                        os.path.join(os.path.dirname(path), raw))
                    target_to_links[target].append(path)

        symlink_targets = set(target_to_links.keys())
        dupes = {t: l for t, l in target_to_links.items() if len(l) > 1}

        # Step 2: scan rclone for unlinked files — parallel walk of top-level dirs
        # Each top-level entry in __all__ is one torrent folder; walking them
        # concurrently amortises FUSE latency significantly on large libraries.
        unlinked = []
        unlinked_size = 0
        scanned = 0
        try:
            top_entries = list(os.scandir(rclone_dir))
        except OSError:
            top_entries = []

        # Top-level files (rare) — grab size from DirEntry stat cache
        all_rclone_files = []
        for e in top_entries:
            if e.is_file(follow_symlinks=False):
                try:
                    sz = e.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = 0
                all_rclone_files.append((e.path, sz))

        top_dirs = [e.path for e in top_entries if e.is_dir(follow_symlinks=False)]
        with ThreadPoolExecutor(max_workers=16) as _ex:
            for result in _ex.map(_walk_rclone_dir, top_dirs):
                all_rclone_files.extend(result)

        for path, sz in all_rclone_files:
            scanned += 1
            if path not in symlink_targets:
                unlinked_size += sz
                unlinked.append({'path': path, 'size': sz, 'size_str': _fmt_size(sz)})

        # Step 3: broken symlinks
        broken = []
        for root, dirs, files in os.walk(symlink_dir, followlinks=False):
            for name in files:
                path = os.path.join(root, name)
                if os.path.islink(path) and not os.path.exists(path):
                    broken.append(path)

        # Step 4: categorise multi-linked
        correct_dupes = {}
        incorrect_dupes = {}
        for target, links in dupes.items():
            target_eps = _extract_ep(os.path.basename(target))
            is_correct = True
            for link in links:
                link_eps = _extract_ep_from_symlink(link)
                if target_eps and link_eps and target_eps.isdisjoint(link_eps):
                    is_correct = False
                    break
            if is_correct:
                correct_dupes[target] = links
            else:
                incorrect_dupes[target] = links

        incorrect_list = [
            {
                'target':      t,
                'target_name': os.path.basename(t),
                'links':       lnks,
            }
            for t, lnks in list(incorrect_dupes.items())[:500]
        ]

        ts_now = time.time()
        data = {
            'stats': {
                'rclone_scanned':   scanned,
                'symlinks_indexed': total_symlinks,
                'unique_targets':   len(symlink_targets),
                'unlinked_count':   len(unlinked),
                'unlinked_size':    unlinked_size,
                'unlinked_size_str': _fmt_size(unlinked_size),
                'broken_count':     len(broken),
                'incorrect_count':  len(incorrect_dupes),
                'correct_count':    len(correct_dupes),
            },
            'unlinked':          unlinked[:1000],
            'unlinked_all':      [f['path'] for f in unlinked],  # full list for bulk delete
            'broken':            broken[:1000],
            'broken_all':        broken,  # full list for bulk delete
            'incorrect_dupes':   incorrect_list,
            'rclone_dir':        rclone_dir,
            'symlink_dir':       symlink_dir,
            'scanned_at':        ts_now,
        }

        with _symlink_audit_state['lock']:
            _symlink_audit_state['status'] = 'done'
            _symlink_audit_state['data']   = data
            _symlink_audit_state['error']  = None
            _symlink_audit_state['ts']     = ts_now

        _symlink_audit_save_cache(data, ts_now)

    except Exception as exc:
        logging.error(f"[SymlinkAudit] scan error: {exc}")
        with _symlink_audit_state['lock']:
            _symlink_audit_state['status'] = 'error'
            _symlink_audit_state['error']  = str(exc)


@debrid_manager_bp.route('/api/symlink_audit')
@admin_required
def api_symlink_audit():
    """Return symlink audit state (idle/scanning/done/error).

    If cached results exist but are older than _SYMLINK_AUDIT_STALE_AFTER,
    return the stale data immediately (so the UI renders instantly) AND
    kick off a background rescan — the frontend will poll and update.
    """
    from utilities.settings import get_setting
    file_mgmt = get_setting('File Management', 'file_collection_management', 'Plex')
    if file_mgmt != 'Symlinked/Local':
        return jsonify({'success': False, 'error': 'Not in symlink mode'}), 400

    with _symlink_audit_state['lock']:
        status = _symlink_audit_state['status']
        data   = _symlink_audit_state['data']
        ts     = _symlink_audit_state['ts']
        err    = _symlink_audit_state['error']

    now   = time.time()
    stale = status == 'done' and (now - ts) > _SYMLINK_AUDIT_STALE_AFTER

    if stale and status != 'scanning':
        # Return stale data immediately, start background refresh
        rclone_dir  = get_setting('File Management', 'original_files_path',  '/mnt/zurg/__all__')
        symlink_dir = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
        if os.path.isdir(symlink_dir) and os.path.isdir(rclone_dir):
            with _symlink_audit_state['lock']:
                _symlink_audit_state['status'] = 'scanning'
            threading.Thread(
                target=_run_symlink_audit_bg,
                args=(rclone_dir, symlink_dir),
                daemon=True,
            ).start()
            logging.info("[SymlinkAudit] Stale cache — auto-refresh started")
            # Return stale data with scanning=True so UI shows stale results + spinner
            return jsonify({'success': True, 'status': 'scanning', 'data': data,
                            'stale': True, 'error': None})

    return jsonify({'success': True, 'status': status, 'data': data,
                    'stale': stale, 'error': err})


@debrid_manager_bp.route('/api/symlink_audit/scan', methods=['POST'])
@admin_required
def api_symlink_audit_scan():
    """Trigger a background symlink audit scan."""
    from utilities.settings import get_setting
    file_mgmt = get_setting('File Management', 'file_collection_management', 'Plex')
    if file_mgmt != 'Symlinked/Local':
        return jsonify({'success': False, 'error': 'Not in symlink mode'}), 400

    rclone_dir  = get_setting('File Management', 'original_files_path',   '/mnt/zurg/__all__')
    symlink_dir = get_setting('File Management', 'symlinked_files_path',  '/mnt/symlinked')

    if not os.path.isdir(symlink_dir):
        return jsonify({'success': False, 'error': f'Symlink directory not found: {symlink_dir}'}), 400
    if not os.path.isdir(rclone_dir):
        return jsonify({'success': False, 'error': f'Rclone directory not found: {rclone_dir}'}), 400

    with _symlink_audit_state['lock']:
        if _symlink_audit_state['status'] == 'scanning':
            return jsonify({'success': True, 'status': 'scanning', 'message': 'Scan already in progress'})
        _symlink_audit_state['status'] = 'scanning'
        _symlink_audit_state['data']   = None
        _symlink_audit_state['error']  = None

    t = threading.Thread(
        target=_run_symlink_audit_bg,
        args=(rclone_dir, symlink_dir),
        daemon=True,
    )
    t.start()
    logging.info(f"[SymlinkAudit] Scan started: rclone={rclone_dir} symlinks={symlink_dir}")
    return jsonify({'success': True, 'status': 'scanning'})


@debrid_manager_bp.route('/api/symlink_audit/delete', methods=['POST'])
@admin_required
def api_symlink_audit_delete():
    """Delete a list of symlink/file paths from the filesystem."""
    from utilities.settings import get_setting
    file_mgmt = get_setting('File Management', 'file_collection_management', 'Plex')
    if file_mgmt != 'Symlinked/Local':
        return jsonify({'success': False, 'error': 'Not in symlink mode'}), 400

    data  = request.get_json(silent=True) or {}
    paths = data.get('paths', [])
    if not paths or not isinstance(paths, list):
        return jsonify({'success': False, 'error': 'paths list required'}), 400

    symlink_dir = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
    rclone_dir  = get_setting('File Management', 'original_files_path',  '/mnt/zurg/__all__')
    deleted, failed, errors = 0, 0, []

    for p in paths:
        if not isinstance(p, str):
            continue
        # Safety: path must be under symlink_dir or rclone_dir
        if not (p.startswith(symlink_dir) or p.startswith(rclone_dir)):
            errors.append(f'Refused: {p}')
            failed += 1
            continue
        try:
            os.remove(p)
            deleted += 1
        except Exception as exc:
            errors.append(f'{p}: {exc}')
            failed += 1

    # Invalidate both in-memory and disk cache so next open triggers a fresh scan
    if deleted:
        with _symlink_audit_state['lock']:
            _symlink_audit_state['status'] = 'idle'
            _symlink_audit_state['data']   = None
            _symlink_audit_state['ts']     = 0
        try:
            if os.path.exists(_SYMLINK_AUDIT_CACHE_FILE):
                os.remove(_SYMLINK_AUDIT_CACHE_FILE)
        except Exception:
            pass

    return jsonify({'success': True, 'deleted': deleted, 'failed': failed, 'errors': errors[:20]})


# ---------------------------------------------------------------------------
# Battery Audit
# ---------------------------------------------------------------------------

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
