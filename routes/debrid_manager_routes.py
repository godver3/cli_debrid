import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, jsonify, request
from debrid import get_debrid_provider, get_provider_display_name, get_debrid_providers
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


def _fetch_all_bg(gen):
    """Background thread for providers that return all torrents in one call (e.g. AllDebrid, Premiumize).
    For Premiumize, uses list_completed_torrents() so the library/reconcile only
    shows finished items — pending downloads are excluded.
    Aborts silently if a newer fetch generation has started."""
    try:
        provider = get_debrid_provider()
        # Premiumize: use completed-only list for the library snapshot
        if hasattr(provider, 'list_completed_torrents'):
            result = provider.list_completed_torrents()
        else:
            result = provider.list_active_torrents()
        if not isinstance(result, list):
            result = []

        with _lib['lock']:
            if _lib['gen'] != gen:
                return
            _lib['partial'].extend(result)

    except Exception as e:
        logging.error(f"Library bg fetch error (all-at-once): {e}")


def _fetch_pages_bg(api_key, start_page, gen):
    """Background thread for RD-style paginated /torrents endpoint.
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
        # Count how many media_items rows share each torrent ID
        torrent_row_counts = {}
        for row in rows:
            tid = row[0]
            if tid:
                torrent_row_counts[tid] = torrent_row_counts.get(tid, 0) + 1

        seen = set()
        for row in rows:
            tid = row[0]
            if tid and tid in id_map and tid not in seen:
                seen.add(tid)
                is_pack = torrent_row_counts.get(tid, 1) > 1
                id_map[tid]['_db'] = {
                    'title':          row[1],
                    'year':           row[2],
                    'type':           row[3],
                    'season_number':  row[4],
                    # Omit episode number for season packs (multiple items share this torrent)
                    'episode_number': None if is_pack else row[5],
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
    provider_name = get_provider_display_name()
    # Derive abbreviation from the backup prefix map (single source of truth)
    # prefix → display name is defined in _bkPfxLabel in the template;
    # here we reverse it: display name → prefix.upper()
    _prefix_map = {
        'Real-Debrid':  'RD',
        'AllDebrid':    'AD',
        'Premiumize':   'PM',
        'Torbox':       'TB',
        'Debrid-Link':  'DL',
    }
    provider_abbrev = _prefix_map.get(provider_name, provider_name[:2].upper())
    climount_mode = _get_bad_folder_path() is not None
    # Show cli_mount tab only when a cli_mount/usenet provider is configured and enabled
    try:
        from usenet.climount_client import get_climount_client
        _dc = get_climount_client()
        show_climount_tab = _dc.is_enabled()
    except Exception:
        show_climount_tab = False
    # Build provider list for reinsert target selector
    try:
        all_providers = [{'index': i, 'name': p.PROVIDER_NAME} for i, p in enumerate(get_debrid_providers())]
    except Exception:
        all_providers = [{'index': 0, 'name': provider_name}]
    return render_template(
        'debrid_manager.html',
        show_plex_trash=show_plex_trash,
        symlink_mode=symlink_mode,
        climount_mode=climount_mode,
        show_climount_tab=show_climount_tab,
        media_server_type=media_server,
        debrid_provider_name=provider_name,
        debrid_provider_abbrev=provider_abbrev,
        debrid_all_providers=all_providers,
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
                if provider.PROVIDER_NAME == 'Real-Debrid':
                    threading.Thread(target=_fetch_pages_bg,
                                     args=(provider.api_key, 1, _bg_gen), daemon=True).start()
                else:
                    threading.Thread(target=_fetch_all_bg,
                                     args=(_bg_gen,), daemon=True).start()
                logging.info("[PlexTrash] No library — triggered background fetch")
                rd_library_status = 'loading'
            except Exception as _bg_e:
                logging.warning(f"[PlexTrash] Could not start bg fetch: {_bg_e}")
                rd_library_status = 'unavailable'
        elif stable is None and rd_is_loading:
            rd_library_status = 'loading'

        # ── NZB / Usenet — DB lookup (must run before all_files pops) ──────────
        _usenet_candidates = {}
        try:
            import json as _srj2
            from database.core import get_db_connection as _get_dbc2
            _uconn2 = _get_dbc2()
            for _item in unique_items:
                _lt = (_item.get('show_title') or _item.get('title', '')).strip()
                _urow = _uconn2.execute(
                    "SELECT id, filled_by_torrent_id, filled_by_title, scrape_results "
                    "FROM media_items WHERE title = ? AND filled_by_torrent_id LIKE 'nzb:%' "
                    "ORDER BY rowid DESC LIMIT 1", (_lt,)
                ).fetchone()
                if _urow:
                    _uid, _ufbt, _ufbt_title, _usr = _urow
                    _ujob_id = _ufbt[4:] if _ufbt and _ufbt.startswith('nzb:') else ''
                    _unzb_url = ''
                    if _usr:
                        try:
                            _usl = _srj2.loads(_usr) if isinstance(_usr, str) else _usr
                            if _usl:
                                _unzb_url = _usl[0].get('nzb_url') or _usl[0].get('magnet', '')
                        except Exception:
                            pass
                    _item['nzb_job_id'] = _ujob_id
                    _item['nzb_title'] = _ufbt_title or ''
                    _item['nzb_url'] = _unzb_url
                    _item['nzb_item_id'] = _uid
                    _usenet_candidates[_item['rating_key']] = _item
            _uconn2.close()
        except Exception as _upe:
            logging.debug(f"[PlexTrash] Usenet pre-match error: {_upe}")

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

            # Good/bad file detection for usenet items — same logic as debrid in_rd
            # Metadata fetch for single-file usenet items to discover additional versions
            for _ui in list(_usenet_candidates.values()):
                if len(_ui.get('all_files') or []) <= 1 and _ui.get('rating_key'):
                    try:
                        _mr2 = requests.get(
                            f'{plex_url}/library/metadata/{_ui["rating_key"]}',
                            headers=headers, timeout=5
                        )
                        _meta2 = ET.fromstring(_mr2.text)
                        _existing2 = set(_ui.get('all_files') or [])
                        for _mv2 in _meta2.iter('Video'):
                            for _me2 in _mv2.findall('Media'):
                                _mid2 = _me2.get('id', '')
                                for _pe2 in _me2.findall('Part'):
                                    _fp2 = _pe2.get('file', '')
                                    if _fp2 and _fp2 not in _existing2:
                                        _ui.setdefault('all_files', []).append(_fp2)
                                        _existing2.add(_fp2)
                                        _bn2 = _os.path.basename(_fp2)
                                        _pid2 = _pe2.get('id', '')
                                        if _mid2 and _pid2 and _bn2 not in _ui.get('part_ids', {}):
                                            _ui.setdefault('part_ids', {})[_bn2] = {'media_id': _mid2, 'part_id': _pid2}
                    except Exception:
                        pass

            # Classify good/bad files for usenet items using same logic as debrid
            for _ui in list(_usenet_candidates.values()):
                _uall = _ui.pop('all_files', [])
                _udel_set = set(_ui.pop('deleted_at_files', []))
                if len(_uall) <= 1:
                    continue
                _uunmatched, _umatched = [], []
                if _udel_set:
                    for _ufp in _uall:
                        (_uunmatched if _ufp in _udel_set else _umatched).append(_ufp)
                else:
                    # Fallback: the old file's folder won't match the new nzb_title
                    _new_title_norm = _norm(_ui.get('nzb_title', ''))
                    for _ufp in _uall:
                        _ufn = _norm(_folder_from_path(_ufp))
                        if _new_title_norm and _ufn and (_ufn in _new_title_norm or _new_title_norm.startswith(_ufn)):
                            _umatched.append(_ufp)
                        else:
                            _uunmatched.append(_ufp)
                if _uunmatched and _umatched:
                    _ui['bad_file'] = _os.path.basename(_uunmatched[0])
                    _ui['has_good_file'] = True
                    _ui['good_file'] = _os.path.basename(_umatched[0])
                    _ui['bad_file_orphaned'] = bool(_udel_set and _uunmatched[0] in _udel_set)

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

        # ── NZB / Usenet — split into in/not lists, remove from debrid lists ────
        # Usenet items must not appear in in_rd/not_in_rd — remove them and put
        # them in the usenet-specific lists instead.
        in_usenet = []
        not_in_usenet = []
        _usenet_rks = set(_usenet_candidates.keys())
        # Only keep debrid-matched items in in_rd (usenet items should not appear there,
        # but guard just in case a title matches both)
        in_rd = [i for i in in_rd if i['rating_key'] not in _usenet_rks]
        # Remove usenet items from not_in_rd so they don't show in the debrid tab
        not_in_rd = [i for i in not_in_rd if i['rating_key'] not in _usenet_rks]
        for _uitem in _usenet_candidates.values():
            if _uitem.get('nzb_job_id'):
                in_usenet.append(_uitem)
            else:
                not_in_usenet.append(_uitem)

        result = {'in_rd': in_rd, 'not_in_rd': not_in_rd, 'in_usenet': in_usenet, 'not_in_usenet': not_in_usenet}
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
        provider = get_debrid_provider()
        all_torrents = provider.list_active_torrents()
        if not isinstance(all_torrents, list):
            all_torrents = []

        _error_statuses = {'error', 'magnet_error', 'virus', 'dead'}
        _no_files_statuses = {'waiting_files_selection', 'magnet_conversion', 'compressing', 'uploading'}
        _active_statuses = {'downloading', 'active', 'queued'}

        torrents = [t for t in all_torrents if (t.get('status') or '').lower() in _active_statuses]
        errors   = [t for t in all_torrents if (t.get('status') or '').lower() in _error_statuses]
        no_files = [t for t in all_torrents if (t.get('status') or '').lower() in _no_files_statuses]

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


@debrid_manager_bp.route('/api/active_files')
@admin_required
def api_active_files():
    """
    Return currently open/active files from cli_mount.
    DFS mode:    active_streams from /debug/stats  (entry_name, file_name, started_at, cli_debrid_ids)
    Rclone mode: mount.detail.transferring from /debug/stats (name, bytes, size, speed, progress)
    """
    try:
        import requests as _req
        from usenet.climount_client import get_climount_client
        dc = get_climount_client()
        if not dc or not dc.is_enabled():
            return jsonify({'success': False, 'error': 'cli_mount not enabled'}), 400

        base = dc.base_url.rstrip('/')
        resp = _req.get(f'{base}/debug/stats', timeout=8)
        if resp.status_code != 200:
            return jsonify({'success': False, 'error': f'cli_mount stats returned {resp.status_code}'}), 502

        data = resp.json()
        now = int(time.time())

        # DFS / WebDAV mode — active_streams
        active_streams_raw = (data.get('active_streams') or {}).get('streams') or []
        streams_out = []
        for s in active_streams_raw:
            started_at = s.get('started_at') or 0
            duration_s = max(0, now - started_at) if started_at else 0
            cli_debrid_ids = s.get('cli_debrid_ids') or {}
            # Resolve cli_debrid_id for this specific file_name
            file_name = s.get('file_name', '')
            cli_debrid_id = cli_debrid_ids.get(file_name) if cli_debrid_ids else None

            streams_out.append({
                'mode': 'dfs',
                'entry_name': s.get('entry_name', ''),
                'file_name': file_name,
                'file_size': s.get('file_size', 0),
                'source': s.get('source', ''),
                'debrid': s.get('debrid', ''),
                'client': s.get('client', ''),
                'started_at': started_at,
                'last_active': s.get('last_active', 0),
                'duration_seconds': duration_s,
                'cli_debrid_id': cli_debrid_id,
                'cli_debrid_ids': cli_debrid_ids,
            })

        # Rclone mode — mount.detail.core.transferring
        # Path format: "<root_folder>/<EntryName>/<filename>"
        mount_detail = (data.get('mount') or {}).get('detail') or {}
        transferring = (mount_detail.get('core') or {}).get('transferring') or []
        rclone_out = []
        for t in transferring:
            full_path = t.get('name', '')
            # Strip leading root folder (movies/ or shows/) and split into entry+file
            parts = full_path.split('/', 2)
            entry_name = parts[1] if len(parts) >= 3 else ''
            file_name = parts[2] if len(parts) >= 3 else full_path
            rclone_out.append({
                'mode': 'rclone',
                'name': file_name,
                'entry_name': entry_name,
                'full_path': full_path,
                'bytes': t.get('bytes', 0),
                'size': t.get('size', 0),
                'speed': t.get('speed', 0),
                'progress': t.get('progress', 0),
                'eta': t.get('eta', 0),
            })

        mount_type = (data.get('mount') or {}).get('type', 'unknown')
        return jsonify({
            'success': True,
            'mount_type': mount_type,
            'dfs_streams': streams_out,
            'rclone_transfers': rclone_out,
        })
    except Exception as e:
        logging.warning(f'[ActiveFiles] cli_mount unreachable or error: {type(e).__name__}')
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/active_files/delete', methods=['POST'])
@admin_required
def api_active_files_delete():
    """
    Delete an active file from cli_mount — handles season packs by resetting ALL siblings.
    Body (DFS):    { cli_debrid_ids: {filename: id, ...}, entry_name: str }
    Body (rclone): { entry_name: str }
    Steps:
      1. Collect all item IDs — from cli_debrid_ids map (DFS) or DB lookup by entry_name (rclone)
      2. Move every item to Wanted + delete each from Plex
      3. Remove the entry from cli_mount (always — all siblings are being reset)
    """
    try:
        from database.database_writing import update_media_item_state
        from database.database_reading import get_media_item_by_id
        from usenet.repair_engine import _delete_from_plex
        from usenet.climount_client import get_climount_client
        import re as _re

        data = request.get_json(silent=True) or {}
        entry_name = data.get('entry_name', '')
        # cli_debrid_ids: {filename: item_id} — all siblings in the season pack
        cli_debrid_ids = data.get('cli_debrid_ids') or {}

        # Collect all item IDs to reset
        item_ids = list({int(v) for v in cli_debrid_ids.values() if v}) if cli_debrid_ids else []

        # Fallback when cli_debrid_ids not populated (entry not yet registered):
        # Find one item by entry_name match, then expand to ALL siblings via filled_by_torrent_id
        if not item_ids and entry_name:
            from database.core import get_db_connection as _gdb_af
            _conn_af = _gdb_af()
            try:
                # Step 1: find any one item matching this entry name
                seed = _conn_af.execute(
                    "SELECT id, filled_by_torrent_id FROM media_items "
                    "WHERE (debrid_folder_name = ? OR filled_by_title = ? OR original_scraped_torrent_title = ?) "
                    "AND state IN ('Collected','Upgrading','Checking') LIMIT 1",
                    (entry_name, entry_name, entry_name)
                ).fetchone()
                if seed:
                    torrent_id_seed = seed[1] or ''
                    if torrent_id_seed:
                        # Step 2: get ALL items with same filled_by_torrent_id (whole season pack)
                        rows = _conn_af.execute(
                            "SELECT id FROM media_items WHERE filled_by_torrent_id = ? "
                            "AND state IN ('Collected','Upgrading','Checking')",
                            (torrent_id_seed,)
                        ).fetchall()
                        item_ids = [r[0] for r in rows]
                    else:
                        item_ids = [seed[0]]
            finally:
                _conn_af.close()

        if not item_ids:
            return jsonify({'success': False, 'error': 'No items found to delete'}), 404

        results = {'wanted': 0, 'plex': 0, 'cli_mount': False}

        # Get infohash from the first item (all siblings share the same entry)
        first_item = dict(get_media_item_by_id(item_ids[0]) or {})
        info_hash = ''
        torrent_id = first_item.get('filled_by_torrent_id', '')
        if torrent_id.startswith('nzb:'):
            info_hash = torrent_id[4:]
        else:
            m = _re.search(r'urn:btih:([0-9a-fA-F]{40})', first_item.get('filled_by_magnet', ''), _re.IGNORECASE)
            info_hash = m.group(1).lower() if m else ''

        # 1+2. Reset all items to Wanted and delete from Plex
        for item_id in item_ids:
            item = get_media_item_by_id(item_id)
            if not item:
                continue
            item = dict(item)
            try:
                update_media_item_state(item_id, 'Wanted')
                results['wanted'] += 1
            except Exception as e:
                logging.warning(f'[ActiveFiles] DB reset failed for {item_id}: {e}')
            try:
                # For episodes, skip ms_item_id (show-level key — deletes entire show)
                # and use location_on_disk path instead to target only this episode.
                _item_for_plex = dict(item)
                if _item_for_plex.get('type') == 'episode':
                    _item_for_plex['ms_item_id'] = None
                if _delete_from_plex(_item_for_plex):
                    results['plex'] += 1
            except Exception as e:
                logging.warning(f'[ActiveFiles] Plex delete failed for {item_id}: {e}')

        # 3. cli_mount removal intentionally skipped — the entry_name from an active
        # stream maps to one cli_mount entry which may contain multiple seasons or
        # episodes that share it. Removing the entry would delete files for all of
        # them. Items reset to Wanted will re-scrape and re-add naturally.

        logging.info(f'[ActiveFiles] Deleted entry {entry_name!r}: wanted={results["wanted"]}, plex={results["plex"]}, cli_mount={results["cli_mount"]}')
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logging.error(f'[ActiveFiles] Delete error: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/torrent/<torrent_id>', methods=['GET'])
@admin_required
def api_torrent_detail(torrent_id):
    try:
        provider = get_debrid_provider()
        detail = provider.get_torrent_info(torrent_id)
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

    provider_index = int(data.get('provider_index', 0))
    try:
        all_providers = get_debrid_providers()
        provider = all_providers[provider_index] if provider_index < len(all_providers) else all_providers[0]
    except Exception:
        provider = get_debrid_provider()
    results = {'reinserted': [], 'failed': []}

    def _hash_exists_on_provider(h):
        try:
            if hasattr(provider, '_find_existing_torrent'):
                ex = provider._find_existing_torrent(h)
                return str(ex.get('id', '')) if ex else None
            elif hasattr(provider, '_find_by_hash'):
                ex = provider._find_by_hash(h)
                return str(ex.get('id', '')) if ex else None
            elif hasattr(provider, 'get_torrents'):
                for t in (provider.get_torrents() or []):
                    if (t.get('hash') or '').lower() == h.lower():
                        return str(t.get('id', ''))
        except Exception:
            pass
        return None

    def reinsert_one(t):
        tid = t.get('id', '')
        h   = t.get('hash', '').lower()
        fn  = t.get('filename', '')
        if not h:
            return tid, 'missing hash'
        try:
            from urllib.parse import quote
            # Pre-check: skip add if already on target provider
            existing_id = _hash_exists_on_provider(h)
            if existing_id:
                logging.info(f'[Reinsert] Hash already on {provider.PROVIDER_NAME} as {existing_id}, skipping add')
                return tid, None
            magnet = f'magnet:?xt=urn:btih:{h}'
            if fn:
                magnet += f'&dn={quote(fn)}'
            new_id = provider.add_torrent(magnet)
            if new_id is None:
                # Provider found it already exists (e.g. RD duplicate 404)
                logging.info(f'[Reinsert] add_torrent returned None (already exists) for hash {h}')
                return tid, None
            if tid and str(new_id) != str(tid):
                try:
                    provider.remove_torrent(tid)
                except Exception:
                    pass
            time.sleep(0.5)
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
        provider_index = int(data.get('provider_index', 0))
        try:
            all_providers = get_debrid_providers()
            provider = all_providers[provider_index] if provider_index < len(all_providers) else all_providers[0]
        except Exception:
            provider = get_debrid_provider()
        from urllib.parse import quote
        torrent_hash = torrent_hash.lower()
        # Pre-check: if already on target provider, skip add
        existing_id = None
        try:
            if hasattr(provider, '_find_existing_torrent'):
                ex = provider._find_existing_torrent(torrent_hash)
                existing_id = str(ex.get('id', '')) if ex else None
            elif hasattr(provider, '_find_by_hash'):
                ex = provider._find_by_hash(torrent_hash)
                existing_id = str(ex.get('id', '')) if ex else None
            elif hasattr(provider, 'get_torrents'):
                for t in (provider.get_torrents() or []):
                    if (t.get('hash') or '').lower() == torrent_hash:
                        existing_id = str(t.get('id', ''))
                        break
        except Exception:
            pass
        if existing_id:
            logging.info(f'[Reinsert] Hash already on {provider.PROVIDER_NAME} as {existing_id}, skipping add')
            _invalidate_library_cache()
            return jsonify({'success': True, 'new_id': existing_id, 'already_existed': True})
        magnet = f'magnet:?xt=urn:btih:{torrent_hash}'
        if filename:
            magnet += f'&dn={quote(filename)}'
        new_id = provider.add_torrent(magnet)
        if new_id is None:
            logging.info(f'[Reinsert] add_torrent returned None (already exists) for {torrent_id}')
            _invalidate_library_cache()
            return jsonify({'success': True, 'new_id': torrent_id, 'already_existed': True})
        if str(new_id) != str(torrent_id):
            try:
                provider.remove_torrent(torrent_id)
            except Exception:
                pass
        _invalidate_library_cache()
        return jsonify({'success': True, 'new_id': new_id})
    except Exception as e:
        logging.error(f"Debrid Manager reinsert error for {torrent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/nzb/reinsert', methods=['POST'])
@admin_required
def api_nzb_reinsert():
    """
    Re-submit a Plex-trashed NZB item to cli_mount.

    Step 1 — try the stored NZB URL from scrape_results.
    Step 2 — if that fails, run a targeted re-scrape and submit the best result.

    Body: { item_id: int, nzb_url: str, nzb_title: str, rating_key: str }
    """
    try:
        data = request.get_json(force=True) or {}
        item_id = data.get('item_id')
        nzb_url = (data.get('nzb_url') or '').strip()
        nzb_title = (data.get('nzb_title') or '').strip()
        rating_key = data.get('rating_key', '')

        if not item_id:
            return jsonify({'success': False, 'error': 'item_id required'}), 400

        from database.core import get_db_connection as _gdbc
        conn = _gdbc()
        row = conn.execute(
            "SELECT id, title, year, type, imdb_id, tmdb_id, season_number, "
            "episode_number, version, genres, filled_by_torrent_id, scrape_results "
            "FROM media_items WHERE id = ?", (item_id,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False, 'error': 'Item not found in database'}), 404

        item = dict(zip(
            ['id', 'title', 'year', 'type', 'imdb_id', 'tmdb_id', 'season_number',
             'episode_number', 'version', 'genres', 'filled_by_torrent_id', 'scrape_results'],
            row
        ))

        from usenet.climount_client import get_climount_client, reset_climount_client
        reset_climount_client()
        client = get_climount_client()

        new_job_id = None
        used_url = None
        method = None
        new_release_title = nzb_title  # updated if rescrape finds a different release

        # ── Step 1: try stored NZB URL ──────────────────────────────────────
        if nzb_url:
            try:
                import requests as _req
                resp = _req.get(nzb_url, timeout=15, allow_redirects=True,
                                headers={'User-Agent': 'Sabnzbd/3.0.0'})
                if resp.status_code == 200 and '<nzb' in resp.text.lower():
                    new_job_id = client.add_nzb_content(
                        nzb_content=resp.text,
                        title=nzb_title or item['title']
                    )
                    if new_job_id:
                        used_url = nzb_url
                        method = 'url'
                        logging.info(f"[ReNZB] item={item_id} re-submitted via stored URL, job={new_job_id}")
            except Exception as _e1:
                logging.warning(f"[ReNZB] item={item_id} Step 1 failed: {_e1}")

        # ── Step 2: fallback — re-scrape and submit best result ─────────────
        if not new_job_id:
            logging.info(f"[ReNZB] item={item_id} falling back to re-scrape")
            try:
                from usenet.repair_engine import _scrape_for_replacement, _submit_replacement, _update_db_for_repair
                results = _scrape_for_replacement(item, broken_nzb_title=nzb_title)
                if results:
                    best = results[0]
                    new_job_id = _submit_replacement(best, item['title'])
                    if new_job_id:
                        method = 'rescrape'
                        new_release_title = best.get('title') or nzb_title
                        used_url = best.get('nzb_url') or ''
                        logging.info(f"[ReNZB] item={item_id} re-scraped and submitted, job={new_job_id}")
                        _update_db_for_repair(item, new_job_id, best, results)
                        _plex_trash_items_cache['items'] = None
                        return jsonify({'success': True, 'job_id': new_job_id, 'method': method, 'new_release_title': new_release_title})
            except Exception as _e2:
                logging.error(f"[ReNZB] item={item_id} Step 2 failed: {_e2}", exc_info=True)

        if not new_job_id:
            return jsonify({'success': False, 'error': 'Failed to re-submit NZB — URL expired and re-scrape found no results'}), 502

        # Update DB with new job ID (URL path — state stays Collected, just refresh torrent ID)
        try:
            from database.database_writing import update_media_item as _umi
            _umi(item_id, filled_by_torrent_id=f'nzb:{new_job_id}', fall_back_to_single_scraper=False)
        except Exception as _dbe:
            logging.warning(f"[ReNZB] DB update failed for item {item_id}: {_dbe}")

        _plex_trash_items_cache['items'] = None
        return jsonify({'success': True, 'job_id': new_job_id, 'method': method, 'new_release_title': new_release_title})

    except Exception as e:
        logging.error(f"[ReNZB] Unexpected error: {e}", exc_info=True)
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

    provider = get_debrid_provider()
    _uses_pagination = (provider.PROVIDER_NAME == 'Real-Debrid')

    # ── 3. Complete but stale — serve stale, trigger background re-fetch ────
    if stable and not is_fresh and not is_loading and not force:
        with _lib['lock']:
            _lib['gen'] += 1
            gen = _lib['gen']
            _lib['loading'] = True
            _lib['partial'] = []
        if _uses_pagination:
            threading.Thread(target=_fetch_pages_bg,
                             args=(provider.api_key, 1, gen), daemon=True).start()
        else:
            threading.Thread(target=_fetch_all_bg,
                             args=(gen,), daemon=True).start()
        return jsonify({
            'success': True, 'loading': True,
            'cache_status': 'stale', 'cache_age': stable_age,
            'torrents': stable['torrents'], 'total': stable['total'],
            'total_bytes': stable['total_bytes'],
        })

    # ── 4. No cache or force refresh — fetch synchronously ──────────────────
    with _lib['lock']:
        _lib['gen'] += 1
        gen = _lib['gen']
        _lib['stable']  = None
        _lib['partial'] = []
        _lib['loading'] = True

    initial = []
    try:
        if not _uses_pagination:
            # Non-paginated providers: Premiumize uses completed-only list for
            # the library snapshot; others use list_active_torrents
            if hasattr(provider, 'list_completed_torrents'):
                initial = provider.list_completed_torrents() or []
            else:
                initial = provider.list_active_torrents() or []
            # Ensure status is always a plain string (enum .value may be int for UNKNOWN)
            for t in initial:
                if 'status' in t and not isinstance(t['status'], str):
                    t['status'] = str(t['status'])
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
        else:
            # RD: fetch initial pages synchronously; if all fit, cache and return.
            # If more pages exist, fall through to background thread below.
            from debrid.real_debrid.api import make_request
            api_key = provider.api_key
            all_fit = False
            for page in range(1, _INITIAL_PAGES + 1):
                result = make_request('GET', '/torrents', api_key,
                                      params={'limit': _PAGE_SIZE, 'page': page})
                if not isinstance(result, list) or not result:
                    all_fit = True
                    break
                initial.extend(result)
                if len(result) < _PAGE_SIZE:
                    # Partial page — all torrents fetched
                    all_fit = True
                    break
            if all_fit:
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
            # More pages exist — fall through to background thread
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

    if _uses_pagination:
        threading.Thread(target=_fetch_pages_bg,
                         args=(provider.api_key, _INITIAL_PAGES + 1, gen), daemon=True).start()

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
        from usenet.repair_engine import get_available_versions
        status = get_backup_status()
        status['scraping_versions'] = get_available_versions()
        return jsonify({'success': True, **status})
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


_xp_restore_state = {
    'status': 'idle',   # idle | running | done | error
    'current': 0,
    'total': 0,
    'current_name': '',
    'result': None,
    'error': None,
    'started_at': 0,
}
_xp_restore_lock = threading.Lock()
_XP_RESTORE_STALE_AFTER = 1800  # 30 minutes


@debrid_manager_bp.route('/api/backup/restore-to-provider', methods=['POST'])
@admin_required
def api_backup_restore_to_provider():
    data = request.get_json(silent=True) or {}
    filename     = data.get('filename', '')
    provider_id  = data.get('provider_id', '')
    api_key      = data.get('api_key', '')
    dry_run      = bool(data.get('dry_run', False))
    cached_only  = bool(data.get('cached_only', False))
    if not filename:
        return jsonify({'success': False, 'error': 'filename required'}), 400
    if not provider_id:
        return jsonify({'success': False, 'error': 'provider_id required'}), 400
    if not api_key:
        return jsonify({'success': False, 'error': 'api_key required'}), 400

    with _xp_restore_lock:
        if _xp_restore_state['status'] == 'running':
            age = time.time() - _xp_restore_state.get('started_at', 0)
            if age < _XP_RESTORE_STALE_AFTER:
                return jsonify({'success': False, 'error': 'A restore is already in progress'}), 409
            logging.warning(f'[XPRestore] Stale running state ({age:.0f}s), resetting')
        _xp_restore_state.update({'status': 'running', 'current': 0, 'total': 0,
                                   'current_name': '', 'result': None, 'error': None,
                                   'started_at': time.time()})

    def _run():
        try:
            from utilities.debrid_backup import restore_from_file_to_provider
            logging.info(f'[XPRestore] Starting: {filename} -> {provider_id}, dry_run={dry_run}, cached_only={cached_only}')
            result = restore_from_file_to_provider(
                filename, provider_id, api_key,
                dry_run=dry_run, cached_only=cached_only,
                progress_cb=_xp_progress_cb,
            )
            logging.info(f'[XPRestore] Done: {result}')
            with _xp_restore_lock:
                _xp_restore_state.update({'status': 'done', 'result': result,
                                           'current_name': '', 'error': None})
        except Exception as e:
            logging.error(f'[XPRestore] Error: {e}', exc_info=True)
            with _xp_restore_lock:
                _xp_restore_state.update({'status': 'error', 'error': str(e), 'result': None})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'status': 'started'})


def _xp_progress_cb(current, total, name):
    with _xp_restore_lock:
        _xp_restore_state.update({'current': current, 'total': total, 'current_name': name})


@debrid_manager_bp.route('/api/backup/restore-to-provider/progress')
@admin_required
def api_backup_restore_to_provider_progress():
    with _xp_restore_lock:
        s = dict(_xp_restore_state)
    return jsonify(s)


@debrid_manager_bp.route('/api/backup/restore-to-provider/reset', methods=['POST'])
@admin_required
def api_backup_restore_to_provider_reset():
    with _xp_restore_lock:
        _xp_restore_state.update({'status': 'idle', 'current': 0, 'total': 0,
                                   'current_name': '', 'result': None, 'error': None, 'started_at': 0})
    return jsonify({'success': True})


# ── Migrate to Usenet ────────────────────────────────────────────────────────

_usenet_migrate_state = {
    'status': 'idle',   # idle | running | done | error | stopped
    'current': 0, 'total': 0, 'current_name': '',
    'submitted': 0, 'not_found': 0, 'failed': 0, 'skipped': 0,
    'error': None, 'started_at': 0,
    'stop_requested': False,
}
_usenet_migrate_lock = threading.Lock()
_USENET_MIGRATE_STALE_AFTER = 3600


@debrid_manager_bp.route('/api/usenet/migrate', methods=['POST'])
@admin_required
def api_usenet_migrate():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename', '')
    version_override = data.get('version') or ''
    if not filename:
        return jsonify({'success': False, 'error': 'filename required'}), 400

    with _usenet_migrate_lock:
        if _usenet_migrate_state['status'] == 'running' or _usenet_migrate_state.get('stop_requested'):
            age = time.time() - _usenet_migrate_state.get('started_at', 0)
            if age < _USENET_MIGRATE_STALE_AFTER:
                return jsonify({'success': False, 'error': 'Migration already in progress'}), 409
        _usenet_migrate_state.update({
            'status': 'running', 'current': 0, 'total': 0, 'current_name': '',
            'submitted': 0, 'not_found': 0, 'failed': 0, 'skipped': 0,
            'error': None, 'started_at': time.time(),
        })

    def _run():
        try:
            from utilities.debrid_backup import get_backup_dir
            from usenet.climount_client import get_climount_client, reset_climount_client
            from scraper.newznab import scrape_newznab_instance
            from utilities.settings import get_setting
            import json as _json
            import os as _os

            path = _os.path.join(get_backup_dir(), filename)
            submitted_path = path + '.migrated'
            if not _os.path.exists(path):
                raise FileNotFoundError(f'Backup file not found: {filename}')

            with open(path, encoding='utf-8') as f:
                backup = _json.load(f)
            if not isinstance(backup, list):
                raise ValueError('Invalid backup format')

            # Load all enabled Newznab scrapers
            all_scrapers = get_setting('Scrapers') or {}
            newznab_scrapers = [
                (sid, cfg) for sid, cfg in all_scrapers.items()
                if isinstance(cfg, dict) and cfg.get('type') == 'Newznab'
                and cfg.get('enabled') and cfg.get('url') and cfg.get('api_key', '').strip()
            ]
            if not newznab_scrapers:
                raise ValueError('No enabled Newznab scrapers configured')

            # Load version filter settings for result filtering on non-group queries
            _version_settings = {}
            if version_override:
                _all_versions = get_setting('Scraping', 'versions', {})
                _version_settings = _all_versions.get(version_override, {})
                logging.info(f'[UsenetMigrate] Using version filter: {version_override!r}')

            import re as _re_vf

            def _passes_re(pat):
                try:
                    _re_vf.compile(pat)
                    return True
                except Exception:
                    return False

            def _passes_version_filter(result_title):
                """Return True if result_title passes the selected version's filter_in/filter_out/resolution."""
                if not _version_settings:
                    return True
                t = result_title or ''
                def _p(item): return item['pattern'] if isinstance(item, dict) else item
                filter_out = [_p(x) for x in (_version_settings.get('filter_out', []) or [])]
                filter_in  = [_p(x) for x in (_version_settings.get('filter_in', []) or [])]
                for pat in filter_out:
                    try:
                        if _re_vf.search(pat, t, _re_vf.IGNORECASE):
                            return False
                    except Exception:
                        pass
                if filter_in:
                    if not any(_re_vf.search(pat, t, _re_vf.IGNORECASE) for pat in filter_in
                               if _passes_re(pat)):
                        return False
                max_res = _version_settings.get('max_resolution', '')
                res_wanted = _version_settings.get('resolution_wanted', '<=')
                if max_res:
                    try:
                        from scraper.functions.filter_results import resolution_filter
                        from PTT import parse_title as _ptt2
                        parsed_res = (_ptt2(t) or {}).get('resolution', '') or ''
                        if parsed_res and not resolution_filter(parsed_res, max_res, res_wanted):
                            return False
                    except Exception:
                        pass
                return True

            reset_climount_client()
            client = get_climount_client()
            if not client.is_enabled():
                raise ValueError('Usenet provider (cli_mount) is not enabled')

            # Fetch existing NZBs from cli_mount to skip already-submitted items
            # Normalize names for fuzzy matching: lowercase, strip punctuation/dots/underscores
            import re as _re_dedup
            def _norm(s):
                return _re_dedup.sub(r'[^a-z0-9 ]', ' ', s.lower().replace('.', ' ').replace('_', ' ')).split()

            # Load persisted submitted set (survives restarts)
            already_submitted = set()
            try:
                with open(submitted_path, encoding='utf-8') as _sf:
                    for _line in _sf:
                        _line = _line.strip()
                        if _line:
                            already_submitted.add(_line)
                logging.info(f'[UsenetMigrate] Loaded {len(already_submitted)} previously submitted items from {submitted_path}')
            except FileNotFoundError:
                pass
            except Exception as _le:
                logging.warning(f'[UsenetMigrate] Could not load submitted file: {_le}')

            # Load all current cli_mount NZBs (paginated) to skip already-submitted items
            from routes.api_tracker import api as _api_dc
            try:
                _queue_count = 0
                _page = 1
                while True:
                    _r = _api_dc.get(f'{client.base_url}/api/browse/nzbs', headers=client._headers(), timeout=15, params={'page': _page})
                    if _r.status_code != 200:
                        break
                    _data = _r.json()
                    _entries = _data.get('entries', _data) if isinstance(_data, dict) else _data
                    if not _entries:
                        break
                    for _n in _entries:
                        _nname = _n.get('name') or _n.get('title') or _n.get('filename') or ''
                        if _nname:
                            already_submitted.add(' '.join(_norm(_nname)))
                            _queue_count += 1
                    _total_pages = _data.get('total_pages', 1) if isinstance(_data, dict) else 1
                    if _page >= _total_pages:
                        break
                    _page += 1
                logging.info(f'[UsenetMigrate] Loaded {_queue_count} NZBs from cli_mount queue across {_page} pages')
            except Exception as _de:
                logging.warning(f'[UsenetMigrate] Could not fetch cli_mount queue: {_de}')

            total = len(backup)
            submitted = not_found = failed = skipped = 0
            failed_items = []
            not_found_items = []
            if backup:
                _sname = backup[0].get('name', '') or backup[0].get('filename', '')
                logging.info(f'[UsenetMigrate] Sample backup name: {_sname!r} -> normalized: {" ".join(_norm(_sname))!r}')

            with _usenet_migrate_lock:
                _usenet_migrate_state['total'] = total

            for idx, torrent in enumerate(backup, 1):
                with _usenet_migrate_lock:
                    if _usenet_migrate_state.get('stop_requested'):
                        _usenet_migrate_state.update({'status': 'stopped', 'stop_requested': False})
                        logging.info(f'[UsenetMigrate] Stopped by user at {idx}/{total}')
                        return

                name = torrent.get('name', '') or torrent.get('filename', '')
                hash_val = (torrent.get('hash') or '').lower()

                with _usenet_migrate_lock:
                    _usenet_migrate_state.update({'current': idx, 'current_name': name or hash_val})

                if not name and not hash_val:
                    not_found += 1
                    continue

                dedup_key = ' '.join(_norm(name)) if name else hash_val
                if dedup_key in already_submitted:
                    logging.debug(f'[UsenetMigrate] Skipping already submitted: {name or hash_val}')
                    skipped += 1
                    continue

                # Collect ALL candidate NZB URLs via tiered name search
                # (torrent hash is not applicable for Usenet — Newznab indexers use title/episode search)
                candidate_urls = []

                if name:
                    import re as _re
                    try:
                        from PTT import parse_title as _ptt
                        _base = _re.sub(r'\.(mkv|mp4|avi|nzb)$', '', name, flags=_re.IGNORECASE)
                        _parsed = _ptt(_base)
                        _title   = _parsed.get('title', '') or ''
                        _seasons = _parsed.get('seasons', [])
                        _episodes = _parsed.get('episodes', [])
                        _res     = _parsed.get('resolution', '')
                        _group   = _parsed.get('group', '')
                        _hdr_list = _parsed.get('hdr', [])
                        _hdr     = ' '.join(_hdr_list) if _hdr_list else ''
                        _sep = f"S{_seasons[0]:02d}E{_episodes[0]:02d}" if _seasons and _episodes \
                              else (f"S{_seasons[0]:02d}" if _seasons else '')
                        _year    = _parsed.get('year')

                        queries = []
                        if _title and _sep:
                            _q_base = f"{_title} {_sep}"
                            if _res and _hdr and _group:
                                queries.append(f"{_q_base} {_res} {_hdr} {_group}")
                            if _res and _hdr:
                                queries.append(f"{_q_base} {_res} {_hdr}")
                            if _res:
                                queries.append(f"{_q_base} {_res}")
                            queries.append(_q_base)
                        if not queries:
                            if _title and _year and _res and _hdr:
                                queries.append(f"{_title} {_year} {_res} {_hdr}")
                            if _title and _year and _res:
                                queries.append(f"{_title} {_year} {_res}")
                            if _title and _year:
                                queries.append(f"{_title} {_year}")
                            if _title:
                                queries.append(_title)
                        if not queries:
                            queries = [_re.sub(r'[\s._-]+', ' ', _base).strip()]
                    except Exception:
                        queries = [_re.sub(r'[\s._-]+', ' ', _re.sub(r'\.(mkv|mp4|avi|nzb)$', '', name, flags=_re.IGNORECASE)).strip()]

                    from routes.api_tracker import api as _api
                    from scraper.newznab import _parse_newznab_xml
                    for _qi, query in enumerate(queries):
                        if candidate_urls:
                            break
                        _needs_filter = _qi > 0 and bool(_version_settings)
                        logging.debug(f'[UsenetMigrate] Name search (Q{_qi+1}, filter={_needs_filter}): {query!r}')
                        for sid, cfg in newznab_scrapers:
                            try:
                                params = {'apikey': cfg['api_key'].strip(), 't': 'search', 'q': query, 'limit': 10}
                                r = _api.get(f"{cfg['url'].rstrip('/')}/api", params=params, timeout=15)
                                if r.status_code == 200:
                                    results = _parse_newznab_xml(r.text, sid)
                                    if results:
                                        if _needs_filter:
                                            results = [res for res in results
                                                       if _passes_version_filter(res.get('title') or res.get('original_title') or '')]
                                        for res in results:
                                            u = res.get('nzb_url')
                                            if u and u not in candidate_urls:
                                                candidate_urls.append(u)
                                        if candidate_urls:
                                            logging.debug(f'[UsenetMigrate] Found {len(candidate_urls)} candidate(s) with query {query!r} on {sid} (Q{_qi+1})')
                                            break
                            except Exception:
                                continue

                if not candidate_urls:
                    logging.info(f'[UsenetMigrate] Not found on usenet: {name or hash_val}')
                    not_found_items.append(torrent)
                    not_found += 1
                else:
                    # Try each candidate URL in order — fallback to next if broken
                    from routes.api_tracker import api as _api2
                    from database.not_wanted_magnets import is_nzb_segment_not_wanted, add_to_not_wanted_nzb_segment, extract_nzb_segment_id
                    from utilities.settings import get_setting as _gs_dm
                    from routes.api_tracker import api as _del_api

                    _item_submitted = False
                    for _cand_idx, nzb_url in enumerate(candidate_urls):
                        logging.debug(f'[UsenetMigrate] Trying candidate {_cand_idx+1}/{len(candidate_urls)} for {name!r}')
                        try:
                            _nzb_r = _api2.get(nzb_url, timeout=15)
                            _nzb_text = _nzb_r.text if _nzb_r.status_code == 200 else ''
                            if not _nzb_text or '<nzb' not in _nzb_text.lower():
                                logging.debug(f'[UsenetMigrate] Candidate {_cand_idx+1} invalid content — trying next')
                                continue
                        except Exception as _ve:
                            logging.debug(f'[UsenetMigrate] Candidate {_cand_idx+1} fetch failed: {_ve} — trying next')
                            continue

                        # Skip if guid already known broken
                        try:
                            from database.not_wanted_magnets import is_nzb_guid_not_wanted as _is_guid_nw
                            if _is_guid_nw(nzb_url):
                                logging.info(f'[UsenetMigrate] Candidate {_cand_idx+1} guid in not-wanted — trying next')
                                continue
                        except Exception:
                            pass

                        # Submit to cli_mount
                        job_id = client.add_nzb_content(nzb_content=_nzb_text, title=name or hash_val)
                        if not job_id:
                            logging.debug(f'[UsenetMigrate] Candidate {_cand_idx+1} rejected by cli_mount — trying next')
                            continue

                        # Health check
                        try:
                            health = client.check_entry_health(name or hash_val)
                        except Exception as _he:
                            health = None
                            logging.debug(f'[UsenetMigrate] Health check error for candidate {_cand_idx+1}: {_he} — keeping')

                        if health == 'broken':
                            logging.warning(f'[UsenetMigrate] Candidate {_cand_idx+1} for {name!r} is BROKEN — blacklisting and trying next')
                            try:
                                from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid
                                _add_guid(nzb_url)
                            except Exception:
                                pass
                            # Delete broken entry from cli_mount
                            try:
                                _dcy_url = _gs_dm('Usenet Provider', 'url', default='').rstrip('/')
                                _dcy_token = _gs_dm('Usenet Provider', 'api_token', default='')
                                _dh = {'Authorization': f'Bearer {_dcy_token}'} if _dcy_token else {}
                                _search_name = (name or hash_val).strip()
                                _real_hash = None
                                _pg = 1
                                while not _real_hash:
                                    _tr = _del_api.get(f'{_dcy_url}/api/torrents',
                                                       params={'page': _pg, 'limit': 50, 'sort_by': 'added_on', 'sort_order': 'desc'},
                                                       headers=_dh, timeout=10)
                                    if _tr.status_code != 200:
                                        break
                                    _td = _tr.json()
                                    for _t in _td.get('torrents', []):
                                        if _t.get('name', '').strip() == _search_name:
                                            _real_hash = _t.get('info_hash', '')
                                            break
                                    if _real_hash or not _td.get('has_next'):
                                        break
                                    _pg += 1
                                if _real_hash:
                                    _del_api.delete(f'{_dcy_url}/api/torrents', headers=_dh, params={'hashes': _real_hash}, timeout=10)
                                    logging.info(f'[UsenetMigrate] Deleted broken entry {_real_hash} from cli_mount')
                            except Exception as _del_e:
                                logging.warning(f'[UsenetMigrate] Could not delete broken entry: {_del_e}')
                            continue  # try next candidate

                        # Success (healthy or inconclusive)
                        if health == 'healthy':
                            logging.info(f'[UsenetMigrate] NZB {name!r} health check passed (candidate {_cand_idx+1})')
                        else:
                            logging.info(f'[UsenetMigrate] NZB {name!r} health check inconclusive — keeping (candidate {_cand_idx+1})')

                        logging.info(f'[UsenetMigrate] Submitted: {name} -> job {job_id} url={nzb_url} (candidate {_cand_idx+1}/{len(candidate_urls)})')
                        already_submitted.add(dedup_key)
                        try:
                            with open(submitted_path, 'a', encoding='utf-8') as _sf:
                                _sf.write(dedup_key + '\n')
                        except Exception:
                            pass
                        # Write name|url to .urls file for backfilling filled_by_magnet
                        try:
                            _urls_path = submitted_path.replace('.migrated', '.urls')
                            with open(_urls_path, 'a', encoding='utf-8') as _uf:
                                _uf.write(f'{name}|{nzb_url}\n')
                        except Exception:
                            pass

                        # Update DB: find items whose old torrent_id matches this entry,
                        # add old NZB URL to not-wanted, set new torrent_id and move to Checking
                        # so the normal pipeline tracks the replacement download.
                        try:
                            from database import get_db_connection as _gdb_rep
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid_rep
                            from database.database_writing import update_media_item as _umi_rep
                            from database.database_writing import update_media_item_state as _umis_rep
                            _new_checking_id = f'nzb:{job_id}'
                            _conn_rep = _gdb_rep()
                            try:
                                # Find DB items referencing the old job (by hash or name match)
                                _old_items = _conn_rep.execute(
                                    "SELECT id, filled_by_magnet, filled_by_torrent_id, filled_by_file, location_on_disk FROM media_items "
                                    "WHERE state='Collected' AND ("
                                    "  filled_by_torrent_id=? OR filled_by_torrent_id=?"
                                    ")",
                                    (hash_val, f'nzb:{hash_val}')
                                ).fetchall()
                            finally:
                                _conn_rep.close()
                            # Extract segment ID from the new NZB XML (already fetched above)
                            _mig_seg_id = ''
                            try:
                                from database.not_wanted_magnets import extract_nzb_segment_id as _ext_seg_mig
                                _mig_seg_id = _ext_seg_mig(_nzb_text)
                            except Exception:
                                pass
                            for _rep_item in _old_items:
                                try:
                                    # Blacklist old NZB URL + segment ID
                                    _old_url = _rep_item['filled_by_magnet'] or ''
                                    _old_seg = _rep_item.get('nzb_segment_id', '') or ''
                                    if _old_url:
                                        _add_guid_rep(_old_url)
                                    if _old_seg:
                                        from database.not_wanted_magnets import add_to_not_wanted_nzb_segment as _add_seg_mig
                                        _add_seg_mig(_old_seg)
                                    # Update to new job with new segment ID, move to Checking
                                    _seg_mig_kwargs = {'nzb_segment_id': _mig_seg_id} if _mig_seg_id else {}
                                    _umi_rep(_rep_item['id'],
                                        filled_by_torrent_id=_new_checking_id,
                                        filled_by_magnet=nzb_url,
                                        filled_by_file=name,
                                        filled_by_title=name,
                                        **_seg_mig_kwargs,
                                    )
                                    _umis_rep(_rep_item['id'], 'Checking')
                                    logging.info(f'[UsenetMigrate] Updated DB item {_rep_item["id"]} → Checking with new job {_new_checking_id}')
                                except Exception as _rep_err:
                                    logging.debug(f'[UsenetMigrate] DB update error for item {_rep_item["id"]}: {_rep_err}')
                        except Exception as _db_rep_err:
                            logging.warning(f'[UsenetMigrate] DB replacement update failed: {_db_rep_err}')

                        submitted += 1
                        _item_submitted = True
                        break  # done with this item

                    if not _item_submitted:
                        logging.warning(f'[UsenetMigrate] All {len(candidate_urls)} candidate(s) exhausted for {name!r} — marking failed')
                        failed_items.append(torrent)
                        failed += 1

                with _usenet_migrate_lock:
                    _usenet_migrate_state.update({'submitted': submitted, 'not_found': not_found, 'failed': failed, 'skipped': skipped})

                time.sleep(0.3)  # avoid hammering indexers

            # Save not-found and failed items as reusable backup files
            base = path.replace('.json', '')
            if not_found_items:
                _nf_path = f'{base}_usenet_not_found.json'
                try:
                    with open(_nf_path, 'w', encoding='utf-8') as _f:
                        _json.dump(not_found_items, _f, indent=2)
                    logging.info(f'[UsenetMigrate] Saved {len(not_found_items)} not-found items to {_nf_path}')
                except Exception as _e:
                    logging.warning(f'[UsenetMigrate] Could not save not-found file: {_e}')
            if failed_items:
                _fail_path = f'{base}_usenet_failed.json'
                try:
                    with open(_fail_path, 'w', encoding='utf-8') as _f:
                        _json.dump(failed_items, _f, indent=2)
                    logging.info(f'[UsenetMigrate] Saved {len(failed_items)} failed items to {_fail_path}')
                except Exception as _e:
                    logging.warning(f'[UsenetMigrate] Could not save failed file: {_e}')

            with _usenet_migrate_lock:
                _usenet_migrate_state.update({
                    'status': 'done', 'current_name': '',
                    'submitted': submitted, 'not_found': not_found, 'failed': failed, 'skipped': skipped,
                })
            logging.info(f'[UsenetMigrate] Complete: submitted={submitted} skipped={skipped} not_found={not_found} failed={failed}')

        except Exception as e:
            logging.error(f'[UsenetMigrate] Error: {e}', exc_info=True)
            with _usenet_migrate_lock:
                _usenet_migrate_state.update({'status': 'error', 'error': str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'status': 'started'})


@debrid_manager_bp.route('/api/usenet/migrate/progress')
@admin_required
def api_usenet_migrate_progress():
    with _usenet_migrate_lock:
        return jsonify(dict(_usenet_migrate_state))


@debrid_manager_bp.route('/api/usenet/migrate/reset', methods=['POST'])
@admin_required
def api_usenet_migrate_reset():
    with _usenet_migrate_lock:
        if _usenet_migrate_state['status'] == 'running':
            # Signal thread to stop — keep status as 'running' until thread acknowledges
            _usenet_migrate_state['stop_requested'] = True
        else:
            # Not running, safe to reset immediately
            _usenet_migrate_state.update({
                'status': 'idle', 'current': 0, 'total': 0, 'current_name': '',
                'submitted': 0, 'not_found': 0, 'failed': 0, 'skipped': 0,
                'error': None, 'started_at': 0, 'stop_requested': False,
            })
    return jsonify({'success': True})


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
        if not provider:
            return jsonify({'error': 'No debrid provider configured'}), 503
        sub = provider.get_subscription_status()

        # Get traffic data via provider abstraction (handles RD and AllDebrid differences)
        traffic = {}
        try:
            traffic = provider.get_user_traffic() or {}
        except Exception as e:
            logging.warning(f"Usage traffic fetch failed: {e}")

        # RD returns date-keyed traffic history; AllDebrid doesn't have this concept
        traffic_details = traffic.get('traffic_details', {})

        today_utc   = datetime.utcnow().strftime("%Y-%m-%d")
        today_data  = traffic_details.get(today_utc, {})
        today_bytes = today_data.get('bytes', 0)

        history = []
        for date_str in sorted(traffic_details.keys(), reverse=True):
            day = traffic_details[date_str]
            b   = day.get('bytes', 0)
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

        total_all_bytes = sum(
            traffic_details[d].get('bytes', 0) for d in traffic_details
        )
        # Fallback: providers without traffic history may expose all-time bytes directly
        if not total_all_bytes:
            total_all_bytes = (
                traffic.get('total_bytes_downloaded', 0) or
                sub.get('total_bytes_downloaded', 0) or 0
            )

        try:
            active_count, max_dl = provider.get_active_downloads()
        except Exception:
            active_count, max_dl = 0, 0

        return jsonify({
            'success': True,
            'has_traffic_history': bool(traffic_details),
            'user': {
                'username':       sub.get('username', ''),
                'email':          sub.get('email', ''),
                'type':           sub.get('type', ''),
                'premium':        bool(sub.get('premium', False)),
                'expiration':     sub.get('expiration', ''),
                'days_remaining': sub.get('days_remaining'),
                'points':         sub.get('points', 0),  # None hides the row in JS
                'locale':         sub.get('locale', ''),
                # Premiumize-specific extras (ignored by other providers)
                'space_used':     sub.get('space_used'),
                'limit_used':     sub.get('limit_used'),
                # Torbox-specific extras (ignored by other providers)
                'created_at':     sub.get('created_at'),
                'cooldown_until': sub.get('cooldown_until'),
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
            # Debrid-Link specific — raw limits dict + live speeds (None for all other providers)
            'dl_limits':         traffic.get('limits', {}) if 'limits' in traffic else {},
            'dl_download_speed': traffic.get('download_speed') if 'download_speed' in traffic else None,
            'dl_upload_speed':   traffic.get('upload_speed')   if 'upload_speed'   in traffic else None,
            'dl_peers':          traffic.get('peers_connected') if 'peers_connected' in traffic else None,
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
                    if provider.PROVIDER_NAME == 'Real-Debrid':
                        threading.Thread(
                            target=_fetch_pages_bg,
                            args=(provider.api_key, 1, _gen),
                            daemon=True,
                        ).start()
                    else:
                        threading.Thread(
                            target=_fetch_all_bg,
                            args=(_gen,),
                            daemon=True,
                        ).start()
                    logging.info('[Reconcile] Force refresh — triggered background library re-fetch')
                except Exception as _e:
                    logging.warning(f'[Reconcile] Could not trigger library re-fetch: {_e}')

        _load_lib_cache_from_db()
        with _lib['lock']:
            stable = _lib['stable']

        if stable is None:
            return jsonify({
                'success': False,
                'error': 'Debrid library not yet loaded — visit the Torrents tab first, then retry.'
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

        # Use configured NAS paths if available; fall back to smart detection via rd_mount_prefixes
        from utilities.settings import get_nas_paths
        _configured_nas_paths = get_nas_paths()

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
                # Detect NAS / non-RD storage
                # Primary: use configured NAS path prefixes if available
                # Fallback: smart detection via rd_mount_prefixes
                _loc = row['location_on_disk'] or ''
                if _configured_nas_paths:
                    is_nas = any(_loc.startswith(p) for p in _configured_nas_paths)
                else:
                    _item_root = _loc.lstrip('/').split('/')[0]
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


# ---------------------------------------------------------------------------
# Bad Torrent Audit (climount __bad__ folder)
# ---------------------------------------------------------------------------

_BAD_TORRENT_AUDIT_STALE_AFTER = 3600  # 1 hour

_bad_torrent_audit_state = {
    'status': 'idle',   # idle | scanning | done | error | stopped
    'data':   None,
    'error':  None,
    'ts':     0,
    'lock':   threading.Lock(),
    'progress': 0,      # 0-100
    'progress_msg': '',
    'stop_requested': False,
}

_BTA_PERSIST_FILE = os.path.join(
    os.environ.get('USER_DB_CONTENT', '/user/db_content'),
    'bad_torrent_audit_results.json',
)

# Cache the bad folder path after first successful discovery so web workers
# never call os.path.isdir() on the FUSE mount (which can block indefinitely).
_bta_bad_folder_cache = {'path': None, 'checked': False}


def _bta_load_persisted():
    """Load persisted audit results from disk. Returns data dict or None."""
    try:
        with open(_BTA_PERSIST_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _bta_save_persisted(data):
    """Save audit results to disk."""
    try:
        os.makedirs(os.path.dirname(_BTA_PERSIST_FILE), exist_ok=True)
        with open(_BTA_PERSIST_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logging.warning(f"[BadTorrentAudit] Could not persist results: {e}")


def _resolve_bad_folder_path():
    """Derive the __bad__ candidate paths from settings WITHOUT touching the filesystem."""
    from utilities.settings import get_setting
    mount = (get_setting('Plex', 'mounted_file_location', '') or
             get_setting('File Management', 'original_files_path', ''))
    if not mount:
        return []
    _KNOWN_SUFFIXES = ('/__all__', '/shows', '/movies', '/default', '/realdebrid')
    debrid_root = mount.rstrip('/')
    for suffix in _KNOWN_SUFFIXES:
        if debrid_root.endswith(suffix):
            debrid_root = debrid_root[:-len(suffix)]
            break
    return [
        os.path.join(debrid_root, '__bad__'),
        os.path.join(mount, '__bad__'),
    ]


def _get_bad_folder_path():
    """Return the __bad__ folder path, using cached result after first discovery.

    The os.path.isdir() check is only ever done from background threads (scan start)
    or once at startup — never from a web worker poll. Web workers use the cached value.
    """
    if _bta_bad_folder_cache['path']:
        return _bta_bad_folder_cache['path']
    # Check filesystem — only call this from background threads
    for c in _resolve_bad_folder_path():
        try:
            if os.path.isdir(c):
                _bta_bad_folder_cache['path'] = c
                _bta_bad_folder_cache['checked'] = True
                return c
        except OSError:
            pass
    _bta_bad_folder_cache['checked'] = True
    return None


def _ffprobe_check(video_path, timeout=2):
    """
    Run ffprobe on video_path with a strict timeout.
    Returns ('ok', duration_s) if streams detected, ('broken', error_msg) otherwise.
    """
    import shutil
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return ('unknown', 'ffprobe not found')
    cmd = [
        ffprobe, '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=codec_type',
        '-read_intervals', '%+#1',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        video_path,
    ]
    try:
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        elapsed = round(time.time() - t0, 1)
        stdout = proc.stdout.decode('utf-8', 'ignore').strip()
        if stdout == 'video' or proc.returncode == 0 and stdout:
            return ('ok', elapsed)
        stderr = proc.stderr.decode('utf-8', 'ignore').strip()
        short_err = (stderr.splitlines()[-1] if stderr else 'no video stream').strip()[:120]
        return ('broken', short_err)
    except subprocess.TimeoutExpired:
        return ('broken', f'timeout after {timeout}s')
    except Exception as e:
        return ('broken', str(e)[:120])


_VIDEO_EXTS = frozenset(['.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.ts', '.m2ts'])


def _find_first_video(folder_path):
    """Return the path to the first video file found inside a __bad__ entry folder."""
    try:
        for entry in sorted(os.scandir(folder_path), key=lambda e: e.name):
            if entry.is_file(follow_symlinks=False):
                if os.path.splitext(entry.name)[1].lower() in _VIDEO_EXTS:
                    return entry.path
            elif entry.is_dir(follow_symlinks=False):
                # One level deep (season folders)
                try:
                    for sub in sorted(os.scandir(entry.path), key=lambda e: e.name):
                        if sub.is_file() and os.path.splitext(sub.name)[1].lower() in _VIDEO_EXTS:
                            return sub.path
                except OSError:
                    pass
    except OSError:
        pass
    return None


def _run_bad_torrent_audit_bg(bad_folder, resume_data=None):
    try:
        entries = sorted(os.listdir(bad_folder))
        total = len(entries)

        # Seed from resume data so we don't re-probe already-scanned entries
        already = {}
        if resume_data:
            for r in (resume_data.get('results') or []):
                already[r['name']] = r

        results = list(already.values())  # start with previously scanned
        broken_count = sum(1 for r in results if r['status'] == 'broken')
        stale_count  = sum(1 for r in results if r['status'] == 'stale')
        unknown_count = sum(1 for r in results if r['status'] not in ('broken', 'stale'))

        pending = [n for n in entries if n not in already]
        pending_total = len(pending)

        for i, name in enumerate(pending):
            should_stop = False
            with _bad_torrent_audit_state['lock']:
                if _bad_torrent_audit_state['stop_requested']:
                    ts_now = time.time()
                    stop_data = {
                        'results': results,
                        'stats': {
                            'total': len(results),
                            'broken': broken_count,
                            'stale': stale_count,
                            'unknown': unknown_count,
                        },
                        'bad_folder': bad_folder,
                        'scanned_at': ts_now,
                        'partial': True,
                    }
                    _bad_torrent_audit_state['status'] = 'stopped'
                    _bad_torrent_audit_state['data'] = stop_data
                    _bad_torrent_audit_state['error'] = None
                    _bad_torrent_audit_state['ts'] = ts_now
                    _bad_torrent_audit_state['progress'] = int((i / max(pending_total, 1)) * 100)
                    _bad_torrent_audit_state['progress_msg'] = ''
                    _bad_torrent_audit_state['stop_requested'] = False
                    should_stop = True
            if should_stop:
                _bta_save_persisted(stop_data)
                logging.info(f"[BadTorrentAudit] Scan stopped at {i}/{pending_total}: {broken_count} broken so far")
                return

            entry_path = os.path.join(bad_folder, name)
            if not os.path.isdir(entry_path):
                continue

            scanned_so_far = len(already) + i + 1
            pct = int((scanned_so_far / max(total, 1)) * 100)
            with _bad_torrent_audit_state['lock']:
                _bad_torrent_audit_state['progress'] = pct
                _bad_torrent_audit_state['progress_msg'] = f'Checking {scanned_so_far}/{total}: {name[:60]}'

            video_path = _find_first_video(entry_path)
            if not video_path:
                results.append({
                    'name': name,
                    'folder': entry_path,
                    'video_file': None,
                    'status': 'unknown',
                    'detail': 'no video file found',
                    'probe_time': None,
                })
                unknown_count += 1
                continue

            status, detail = _ffprobe_check(video_path)
            if status == 'ok':
                stale_count += 1
            elif status == 'broken':
                broken_count += 1
            else:
                unknown_count += 1

            results.append({
                'name': name,
                'folder': entry_path,
                'video_file': os.path.relpath(video_path, entry_path),
                'status': status,
                'detail': detail,
                'probe_time': detail if status == 'ok' else None,
            })

        ts_now = time.time()
        data = {
            'results': results,
            'stats': {
                'total': len(results),
                'broken': broken_count,
                'stale': stale_count,
                'unknown': unknown_count,
            },
            'bad_folder': bad_folder,
            'scanned_at': ts_now,
            'partial': False,
        }
        _bta_save_persisted(data)
        with _bad_torrent_audit_state['lock']:
            _bad_torrent_audit_state['status'] = 'done'
            _bad_torrent_audit_state['data'] = data
            _bad_torrent_audit_state['error'] = None
            _bad_torrent_audit_state['ts'] = ts_now
            _bad_torrent_audit_state['progress'] = 100
            _bad_torrent_audit_state['progress_msg'] = ''
        logging.info(f"[BadTorrentAudit] Scan complete: {broken_count} broken, {stale_count} stale, {unknown_count} unknown")

    except Exception as exc:
        logging.error(f"[BadTorrentAudit] Scan error: {exc}", exc_info=True)
        with _bad_torrent_audit_state['lock']:
            _bad_torrent_audit_state['status'] = 'error'
            _bad_torrent_audit_state['error'] = str(exc)
            _bad_torrent_audit_state['progress'] = 0


@debrid_manager_bp.route('/api/bad_torrent_audit')
@admin_required
def api_bad_torrent_audit():
    """Return bad torrent audit state. Never touches the FUSE mount directly."""
    # Use cached path or derive from settings — no os.path.isdir() here
    candidates = _resolve_bad_folder_path()
    if not candidates and not _bta_bad_folder_cache['path']:
        return jsonify({'success': False, 'error': 'No mount path configured'}), 400

    with _bad_torrent_audit_state['lock']:
        status = _bad_torrent_audit_state['status']
        data = _bad_torrent_audit_state['data']
        err = _bad_torrent_audit_state['error']
        pct = _bad_torrent_audit_state['progress']
        msg = _bad_torrent_audit_state['progress_msg']

    # If idle in this process but persisted results exist, load them (disk only, no FUSE)
    if status == 'idle' and data is None:
        persisted = _bta_load_persisted()
        if persisted:
            with _bad_torrent_audit_state['lock']:
                _bad_torrent_audit_state['data'] = persisted
                _bad_torrent_audit_state['status'] = 'stopped' if persisted.get('partial') else 'done'
            status = _bad_torrent_audit_state['status']
            data = persisted

    return jsonify({
        'success': True,
        'status': status,
        'data': data,
        'error': err,
        'progress': pct,
        'progress_msg': msg,
    })


def _run_bad_torrent_audit_wrapper(fresh):
    """Wrapper that resolves the bad folder path in the background thread (never in a web worker)."""
    bad_folder = _get_bad_folder_path()
    if not bad_folder:
        with _bad_torrent_audit_state['lock']:
            _bad_torrent_audit_state['status'] = 'error'
            _bad_torrent_audit_state['error'] = 'No __bad__ folder found — check mount path in settings'
            _bad_torrent_audit_state['progress'] = 0
        return
    resume_data = None
    if not fresh:
        resume_data = _bta_load_persisted()
        if resume_data:
            with _bad_torrent_audit_state['lock']:
                _bad_torrent_audit_state['data'] = resume_data
            skipped = len(resume_data.get('results') or [])
            logging.info(f"[BadTorrentAudit] Resuming scan, skipping {skipped} already-scanned entries")
        else:
            with _bad_torrent_audit_state['lock']:
                _bad_torrent_audit_state['data'] = None
    else:
        with _bad_torrent_audit_state['lock']:
            _bad_torrent_audit_state['data'] = None
    logging.info(f"[BadTorrentAudit] Scan started: {bad_folder} (fresh={fresh})")
    _run_bad_torrent_audit_bg(bad_folder, resume_data)


@debrid_manager_bp.route('/api/bad_torrent_audit/scan', methods=['POST'])
@admin_required
def api_bad_torrent_audit_scan():
    """Trigger a background bad torrent audit scan. Never touches the FUSE mount in the web worker."""
    req = request.get_json(silent=True) or {}
    fresh = req.get('fresh', False)

    with _bad_torrent_audit_state['lock']:
        if _bad_torrent_audit_state['status'] == 'scanning':
            return jsonify({'success': True, 'status': 'scanning', 'message': 'Scan already in progress'})
        _bad_torrent_audit_state['status'] = 'scanning'
        _bad_torrent_audit_state['error'] = None
        _bad_torrent_audit_state['progress'] = 0
        _bad_torrent_audit_state['stop_requested'] = False
        _bad_torrent_audit_state['progress_msg'] = 'Starting…'

    threading.Thread(target=_run_bad_torrent_audit_wrapper, args=(fresh,), daemon=True).start()
    return jsonify({'success': True, 'status': 'scanning', 'resumed': not fresh})


@debrid_manager_bp.route('/api/bad_torrent_audit/stop', methods=['POST'])
@admin_required
def api_bad_torrent_audit_stop():
    """Request the running scan to stop and preserve results so far."""
    with _bad_torrent_audit_state['lock']:
        if _bad_torrent_audit_state['status'] != 'scanning':
            return jsonify({'success': False, 'error': 'No scan in progress'})
        _bad_torrent_audit_state['stop_requested'] = True
    return jsonify({'success': True, 'message': 'Stop requested'})


# ---------------------------------------------------------------------------
# All-folder audit (__all__) — same ffprobe logic, different folder + state
# ---------------------------------------------------------------------------

_all_torrent_audit_state = {
    'status': 'idle',
    'data':   None,
    'error':  None,
    'ts':     0,
    'lock':   threading.Lock(),
    'progress': 0,
    'progress_msg': '',
    'stop_requested': False,
}

_ATA_PERSIST_FILE = os.path.join(
    os.environ.get('USER_DB_CONTENT', '/user/db_content'),
    'all_torrent_audit_results.json',
)


def _ata_load_persisted():
    try:
        with open(_ATA_PERSIST_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _ata_save_persisted(data):
    try:
        os.makedirs(os.path.dirname(_ATA_PERSIST_FILE), exist_ok=True)
        with open(_ATA_PERSIST_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logging.warning(f"[AllTorrentAudit] Could not persist results: {e}")


def _get_all_folder_path():
    """Derive the __all__ folder path from the same mount setting as __bad__."""
    from utilities.settings import get_setting
    mount = (get_setting('Plex', 'mounted_file_location', '') or
             get_setting('File Management', 'original_files_path', ''))
    if not mount:
        return None
    _KNOWN_SUFFIXES = ('/__all__', '/shows', '/movies', '/default', '/realdebrid')
    debrid_root = mount.rstrip('/')
    for suffix in _KNOWN_SUFFIXES:
        if debrid_root.endswith(suffix):
            debrid_root = debrid_root[:-len(suffix)]
            break
    return os.path.join(debrid_root, '__all__')


def _fetch_all_entries_from_climount(all_folder):
    """Fetch __all__ entry names from climount API (avoids blocking os.listdir on FUSE).
    Falls back to os.listdir if climount URL not configured.
    Returns list of entry names (folder names inside __all__).
    """
    import requests as _req
    from utilities.settings import get_setting
    url = get_setting('cli_mount', 'url', default='').strip().rstrip('/')
    if not url:
        logging.info('[AllTorrentAudit] No climount URL — falling back to os.listdir')
        return sorted(os.listdir(all_folder))
    all_entries = []
    page = 1
    while True:
        try:
            r = _req.get(f'{url}/api/browse/__all__?page={page}', timeout=15)
            r.raise_for_status()
            data = r.json()
            page_entries = data.get('entries', [])
            if not page_entries:
                break
            all_entries.extend(e['name'] for e in page_entries if e.get('name'))
            total_pages = data.get('total_pages', 1)
            logging.info(f'[AllTorrentAudit] cli_mount page {page}/{total_pages}: {len(page_entries)} entries')
            if page >= total_pages:
                break
            page += 1
        except Exception as e:
            logging.warning(f'[AllTorrentAudit] cli_mount API error on page {page}: {e} — falling back to os.listdir')
            return sorted(os.listdir(all_folder))
    return sorted(all_entries)


def _run_all_torrent_audit_bg(all_folder, resume_data=None, touch_only=False, ffprobe_timeout=0.5):
    """Background thread: probe each __all__ entry via FUSE mount to trigger climount's repair check."""
    try:
        with _all_torrent_audit_state['lock']:
            _all_torrent_audit_state['progress_msg'] = 'Fetching entry list from climount…'
        entries = _fetch_all_entries_from_climount(all_folder)
        total = len(entries)
        logging.info(f"[AllTorrentAudit] Got {total} entries, starting ffprobe scan")

        already = {}
        if resume_data:
            for r in (resume_data.get('results') or []):
                already[r['name']] = r

        results = list(already.values())
        broken_count  = sum(1 for r in results if r['status'] == 'broken')
        stale_count   = sum(1 for r in results if r['status'] == 'stale')
        unknown_count = sum(1 for r in results if r['status'] not in ('broken', 'stale'))

        pending = [n for n in entries if n not in already]
        pending_total = len(pending)

        for i, name in enumerate(pending):
            should_stop = False
            with _all_torrent_audit_state['lock']:
                if _all_torrent_audit_state['stop_requested']:
                    ts_now = time.time()
                    stop_data = {
                        'results': results,
                        'stats': {'total': len(results), 'broken': broken_count,
                                  'stale': stale_count, 'unknown': unknown_count},
                        'all_folder': all_folder,
                        'scanned_at': ts_now,
                        'partial': True,
                    }
                    _all_torrent_audit_state['status'] = 'stopped'
                    _all_torrent_audit_state['data'] = stop_data
                    _all_torrent_audit_state['error'] = None
                    _all_torrent_audit_state['ts'] = ts_now
                    _all_torrent_audit_state['progress'] = int((i / max(pending_total, 1)) * 100)
                    _all_torrent_audit_state['progress_msg'] = ''
                    _all_torrent_audit_state['stop_requested'] = False
                    should_stop = True
            if should_stop:
                _ata_save_persisted(stop_data)
                logging.info(f"[AllTorrentAudit] Scan stopped at {i}/{pending_total}")
                return

            entry_path = os.path.join(all_folder, name)
            scanned_so_far = len(already) + i + 1
            pct = int((scanned_so_far / max(total, 1)) * 100)
            with _all_torrent_audit_state['lock']:
                _all_torrent_audit_state['progress'] = pct
                _all_torrent_audit_state['progress_msg'] = f'Touching {scanned_so_far}/{total}: {name[:60]}' if touch_only else f'Checking {scanned_so_far}/{total}: {name[:60]}'

            # Persist every 100 items so resume works after restart
            if scanned_so_far % 100 == 0:
                _ata_save_persisted({
                    'results': results,
                    'stats': {'total': len(results), 'broken': broken_count,
                              'stale': stale_count, 'unknown': unknown_count},
                    'all_folder': all_folder,
                    'scanned_at': time.time(),
                    'partial': True,
                })

            if touch_only:
                # Just stat the entry to trigger climount's file check — no ffprobe
                try:
                    os.stat(entry_path)
                except Exception:
                    pass
                stale_count += 1  # count as touched
                results.append({'name': name, 'folder': entry_path, 'video_file': None,
                                 'status': 'touched', 'detail': 'touched', 'probe_time': None})
                continue

            # cli_mount names the folder same as the video file inside it —
            # construct the path directly to avoid os.scandir on the FUSE mount
            _, ext = os.path.splitext(name)
            if ext.lower() in _VIDEO_EXTS:
                video_path = os.path.join(entry_path, name)
            else:
                # Season pack — need to find the video file
                video_path = _find_first_video(entry_path)

            if not video_path:
                results.append({'name': name, 'folder': entry_path, 'video_file': None,
                                 'status': 'unknown', 'detail': 'no video file found', 'probe_time': None})
                unknown_count += 1
                continue

            status, detail = _ffprobe_check(video_path, timeout=ffprobe_timeout)
            if status == 'ok':
                stale_count += 1
            elif status == 'broken':
                broken_count += 1
            else:
                unknown_count += 1

            results.append({'name': name, 'folder': entry_path,
                             'video_file': os.path.relpath(video_path, entry_path),
                             'status': status, 'detail': detail,
                             'probe_time': detail if status == 'ok' else None})

        ts_now = time.time()
        data = {
            'results': results,
            'stats': {'total': len(results), 'broken': broken_count,
                      'stale': stale_count, 'unknown': unknown_count},
            'all_folder': all_folder,
            'scanned_at': ts_now,
            'partial': False,
        }
        _ata_save_persisted(data)
        with _all_torrent_audit_state['lock']:
            _all_torrent_audit_state['status'] = 'done'
            _all_torrent_audit_state['data'] = data
            _all_torrent_audit_state['error'] = None
            _all_torrent_audit_state['ts'] = ts_now
            _all_torrent_audit_state['progress'] = 100
            _all_torrent_audit_state['progress_msg'] = ''
        logging.info(f"[AllTorrentAudit] Scan complete: {broken_count} broken, {stale_count} stale, {unknown_count} unknown")

    except Exception as exc:
        logging.error(f"[AllTorrentAudit] Scan error: {exc}", exc_info=True)
        with _all_torrent_audit_state['lock']:
            _all_torrent_audit_state['status'] = 'error'
            _all_torrent_audit_state['error'] = str(exc)
            _all_torrent_audit_state['progress'] = 0


@debrid_manager_bp.route('/api/all_torrent_audit')
@admin_required
def api_all_torrent_audit():
    with _all_torrent_audit_state['lock']:
        status = _all_torrent_audit_state['status']
        data   = _all_torrent_audit_state['data']
        err    = _all_torrent_audit_state['error']
        pct    = _all_torrent_audit_state['progress']
        msg    = _all_torrent_audit_state['progress_msg']

    if status == 'idle' and data is None:
        persisted = _ata_load_persisted()
        if persisted:
            with _all_torrent_audit_state['lock']:
                _all_torrent_audit_state['data'] = persisted
                _all_torrent_audit_state['status'] = 'stopped' if persisted.get('partial') else 'done'
            status = _all_torrent_audit_state['status']
            data   = persisted

    return jsonify({'success': True, 'status': status, 'data': data,
                    'error': err, 'progress': pct, 'progress_msg': msg})


@debrid_manager_bp.route('/api/all_torrent_audit/scan', methods=['POST'])
@admin_required
def api_all_torrent_audit_scan():
    req   = request.get_json(silent=True) or {}
    fresh = req.get('fresh', False)

    with _all_torrent_audit_state['lock']:
        if _all_torrent_audit_state['status'] == 'scanning':
            return jsonify({'success': True, 'status': 'scanning', 'message': 'Scan already in progress'})
        _all_torrent_audit_state['status'] = 'scanning'
        _all_torrent_audit_state['error'] = None
        _all_torrent_audit_state['progress'] = 0
        _all_torrent_audit_state['stop_requested'] = False
        _all_torrent_audit_state['progress_msg'] = 'Starting…'

    def _run():
        all_folder = _get_all_folder_path()
        if not all_folder or not os.path.isdir(all_folder):
            with _all_torrent_audit_state['lock']:
                _all_torrent_audit_state['status'] = 'error'
                _all_torrent_audit_state['error'] = f'__all__ folder not found: {all_folder}'
            return
        resume_data = None
        if not fresh:
            resume_data = _ata_load_persisted()
        logging.info(f"[AllTorrentAudit] Scan started: {all_folder} (fresh={fresh})")
        _run_all_torrent_audit_bg(all_folder, resume_data)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'status': 'scanning', 'resumed': not fresh})


@debrid_manager_bp.route('/api/all_torrent_audit/stop', methods=['POST'])
@admin_required
def api_all_torrent_audit_stop():
    with _all_torrent_audit_state['lock']:
        if _all_torrent_audit_state['status'] != 'scanning':
            return jsonify({'success': False, 'error': 'No scan in progress'})
        _all_torrent_audit_state['stop_requested'] = True
    return jsonify({'success': True})


@debrid_manager_bp.route('/api/bad_torrent_audit/delete', methods=['POST'])
@admin_required
def api_bad_torrent_audit_delete():
    """Delete selected bad torrent entries from RD and optionally remove from __bad__."""
    req = request.get_json(silent=True) or {}
    names = req.get('names', [])  # folder names inside __bad__
    if not names or not isinstance(names, list):
        return jsonify({'success': False, 'error': 'names list required'}), 400

    # Use cached bad folder path — never call os.path.isdir in a web worker
    bad_folder = _bta_bad_folder_cache['path']
    if not bad_folder:
        return jsonify({'success': False, 'error': 'Bad folder path not cached yet — run a scan first'}), 400

    deleted = []
    failed = []

    import shutil, errno
    for name in names:
        entry_path = os.path.join(bad_folder, name)
        try:
            # Walk bottom-up: delete all files first, then each empty subdir,
            # then the root dir. This works on FUSE mounts that reject rmtree
            # with ENOTEMPTY on the parent even after children are gone.
            for dirpath, dirnames, filenames in os.walk(entry_path, topdown=False):
                for fname in filenames:
                    try:
                        os.remove(os.path.join(dirpath, fname))
                    except Exception:
                        pass
                try:
                    os.rmdir(dirpath)
                except Exception:
                    pass
            # Final attempt on the root in case walk missed it
            if os.path.exists(entry_path):
                try:
                    os.rmdir(entry_path)
                except Exception:
                    pass
            deleted.append(name)
        except FileNotFoundError:
            deleted.append(name)  # already gone
        except Exception as e:
            if not failed:
                logging.error(f"[BadTorrentAudit] delete failed for {entry_path!r}: {type(e).__name__}: {e}")
            failed.append({'name': name, 'error': str(e)})

    logging.info(f"[BadTorrentAudit] Mount delete: {len(deleted)} deleted, {len(failed)} failed")

    # Remove deleted entries from persisted results so results stay visible without rescan
    deleted_names = set(deleted)
    try:
        persisted = _bta_load_persisted()
        if persisted:
            persisted['results'] = [r for r in persisted.get('results', []) if r['name'] not in deleted_names]
            s = persisted['results']
            persisted['stats'] = {
                'total':   len(s),
                'broken':  sum(1 for r in s if r['status'] == 'broken'),
                'stale':   sum(1 for r in s if r['status'] == 'stale'),
                'unknown': sum(1 for r in s if r['status'] not in ('broken', 'stale')),
            }
            _bta_save_persisted(persisted)
            with _bad_torrent_audit_state['lock']:
                _bad_torrent_audit_state['data'] = persisted
                _bad_torrent_audit_state['status'] = 'stopped' if persisted.get('partial') else 'done'
        else:
            with _bad_torrent_audit_state['lock']:
                _bad_torrent_audit_state['status'] = 'idle'
                _bad_torrent_audit_state['data'] = None
    except Exception:
        with _bad_torrent_audit_state['lock']:
            _bad_torrent_audit_state['status'] = 'idle'
            _bad_torrent_audit_state['data'] = None

    return jsonify({
        'success': True,
        'deleted_rd': len(deleted),
        'failed_rd': len(failed),
        'no_rd_id': 0,
        'errors': [f['error'] for f in failed[:10]],
        'first_error': failed[0]['error'] if failed else None,
    })


@debrid_manager_bp.route('/api/climount/settings', methods=['GET'])
@admin_required
def api_climount_settings_get():
    from utilities.settings import get_setting
    url = get_setting('cli_mount', 'url', default='')
    return jsonify({'success': True, 'url': url})


@debrid_manager_bp.route('/api/climount/settings', methods=['POST'])
@admin_required
def api_climount_settings_save():
    from utilities.settings import set_setting
    req = request.get_json(silent=True) or {}
    url = (req.get('url') or '').strip().rstrip('/')
    set_setting('cli_mount', 'url', url)
    return jsonify({'success': True})


@debrid_manager_bp.route('/api/climount/test', methods=['POST'])
@admin_required
def api_climount_test():
    import requests as _requests
    from utilities.settings import get_setting
    req = request.get_json(silent=True) or {}
    url = (req.get('url') or '').strip().rstrip('/') or get_setting('cli_mount', 'url', default='')
    if not url:
        return jsonify({'success': False, 'error': 'No URL configured'})
    try:
        r = _requests.get(f'{url}/api/browse/__bad__', timeout=8)
        r.raise_for_status()
        data = r.json()
        total = data.get('total') if isinstance(data, dict) else None
        entries = data if isinstance(data, list) else data.get('entries', [])
        return jsonify({'success': True, 'count': total if total is not None else len(entries)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def _get_climount_hash_map(names):
    """Fetch hashes for the given folder names from climount's /api/browse/ endpoint.

    Returns a dict of name -> info_hash (only entries with non-empty hashes).
    Falls back to empty dict if climount URL is not configured or request fails.
    """
    import requests as _requests
    from utilities.settings import get_setting
    url = get_setting('cli_mount', 'url', default='').strip().rstrip('/')
    logging.debug(f'[BadTorrentAudit] climount url from settings: {url!r}')
    if not url:
        logging.debug('[BadTorrentAudit] No climount URL configured, skipping hash lookup')
        return {}
    try:
        # Fetch all pages — climount uses 1-based ?page= pagination with a fixed limit of 50
        all_entries = []
        page = 1
        while True:
            api_url = f'{url}/api/browse/__bad__?page={page}'
            logging.info(f'[BadTorrentAudit] Fetching climount page {page}: {api_url}')
            r = _requests.get(api_url, timeout=10)
            r.raise_for_status()
            data = r.json()
            page_entries = data if isinstance(data, list) else data.get('entries', [])
            if not page_entries:
                break
            all_entries.extend(page_entries)
            total_pages = data.get('total_pages', 1) if isinstance(data, dict) else 1
            logging.info(f'[BadTorrentAudit] climount page {page}/{total_pages}: got {len(page_entries)} entries (total so far: {len(all_entries)})')
            if page == 1:
                for e in page_entries[:3]:
                    logging.info(f'[BadTorrentAudit] sample entry: keys={list(e.keys())} name={e.get("name")!r} info_hash={e.get("info_hash")!r}')
            if page >= total_pages:
                break
            page += 1

        logging.info(f'[BadTorrentAudit] climount total entries fetched: {len(all_entries)}')
        name_set = set(names)
        result = {}
        for entry in all_entries:
            n = entry.get('name', '')
            h = entry.get('info_hash', '')
            if n in name_set and h:
                result[n] = h
        logging.info(f'[BadTorrentAudit] climount hash lookup: {len(result)}/{len(names)} matched')
        return result
    except Exception as e:
        logging.warning(f'[BadTorrentAudit] Could not fetch climount hashes: {e}')
        return {}


@debrid_manager_bp.route('/api/bad_torrent_audit/reinsert', methods=['POST'])
@admin_required
def api_bad_torrent_audit_reinsert():
    """Re-insert broken torrents back to the debrid service.

    Prefers hashes from climount's /api/browse/ endpoint (if URL configured).
    Falls back to matching against the RD torrent library by normalised filename.
    """
    req = request.get_json(silent=True) or {}
    names = req.get('names', [])
    provider_index = int(req.get('provider_index', 0))
    logging.info(f'[BadTorrentAudit] Reinsert request: {names} provider_index={provider_index}')
    if not names or not isinstance(names, list):
        return jsonify({'success': False, 'error': 'names list required'}), 400

    # --- Primary: climount hash lookup ---
    dcyp_hashes = _get_climount_hash_map(names)
    logging.info(f'[BadTorrentAudit] Reinsert: climount resolved {len(dcyp_hashes)}/{len(names)} hashes')

    import re as _re
    def _norm(s):
        return _re.sub(r'[\s\-_.]+', ' ', str(s)).strip().lower()

    # --- Fallback: library name matching ---
    missing = [n for n in names if n not in dcyp_hashes]
    logging.info(f'[BadTorrentAudit] Reinsert: {len(missing)} names need library fallback: {missing}')
    rd_map = {}
    try:
        all_providers = get_debrid_providers()
        provider = all_providers[provider_index] if provider_index < len(all_providers) else all_providers[0]
        logging.debug(f'[BadTorrentAudit] Got debrid provider: {type(provider).__name__}')
    except Exception as e:
        logging.error(f'[BadTorrentAudit] Failed to get debrid provider: {e}')
        return jsonify({'success': False, 'error': f'Failed to connect to debrid provider: {e}'}), 500

    if missing:
        try:
            rd_torrents = provider.get_torrents()
            logging.debug(f'[BadTorrentAudit] RD library has {len(rd_torrents) if rd_torrents else 0} torrents')
        except Exception as e:
            logging.warning(f'[BadTorrentAudit] Could not fetch RD torrents for fallback: {e}')
            rd_torrents = []
        for t in (rd_torrents or []):
            fn = t.get('filename', '') or t.get('name', '')
            if fn:
                rd_map[_norm(fn)] = {
                    'id': t.get('id', ''),
                    'hash': t.get('hash', ''),
                    'filename': fn,
                }
        logging.debug(f'[BadTorrentAudit] RD map built with {len(rd_map)} entries')
        for name in missing:
            match = rd_map.get(_norm(name))
            logging.debug(f'[BadTorrentAudit] RD fallback for {name!r}: norm={_norm(name)!r} match={match}')

    reinserted = []
    failed = []
    no_hash = []

    from urllib.parse import quote

    _RD_RATE_LIMIT_DELAY = 0.5   # seconds between addMagnet calls (RD allows 250/min)
    _RD_RATE_LIMIT_RETRY_DELAY = 10.0  # fallback seconds to wait after a 429

    for name in names:
        torrent_hash = dcyp_hashes.get(name, '')
        old_id = ''
        filename = name
        source = 'climount'
        if not torrent_hash:
            source = 'rd_fallback'
            match = rd_map.get(_norm(name))
            if match:
                torrent_hash = match['hash']
                filename     = match['filename']
                old_id       = match['id']

        logging.info(f'[BadTorrentAudit] reinsert: name={name!r} source={source} hash={torrent_hash!r}')

        if not torrent_hash:
            logging.warning(f'[BadTorrentAudit] No hash found for {name!r}')
            no_hash.append(name)
            continue

        magnet = f'magnet:?xt=urn:btih:{torrent_hash}&dn={quote(filename)}'

        # Pre-check: if the hash already exists on the target provider, skip the add
        existing_id = None
        try:
            if hasattr(provider, '_find_existing_torrent'):
                ex = provider._find_existing_torrent(torrent_hash)
                existing_id = str(ex.get('id', '')) if ex else None
            elif hasattr(provider, '_find_by_hash'):
                ex = provider._find_by_hash(torrent_hash)
                existing_id = str(ex.get('id', '')) if ex else None
            elif hasattr(provider, 'get_torrents'):
                # RD: scan torrent list for matching hash
                torrents = provider.get_torrents() or []
                for t in torrents:
                    if (t.get('hash') or '').lower() == torrent_hash.lower():
                        existing_id = str(t.get('id', ''))
                        break
        except Exception as ex_err:
            logging.debug(f'[BadTorrentAudit] Pre-check failed for {name!r}: {ex_err}')

        if existing_id:
            logging.info(f'[BadTorrentAudit] Hash already on {provider.PROVIDER_NAME} as id={existing_id!r}, skipping add for {name!r}')
            reinserted.append(name)
            time.sleep(_RD_RATE_LIMIT_DELAY)
            continue

        logging.info(f'[BadTorrentAudit] Adding magnet for {name!r}')

        try:
            new_id = provider.add_torrent(magnet)
            # None means provider found it already existed (e.g. RD duplicate 404)
            if new_id is None:
                logging.info(f'[BadTorrentAudit] add_torrent returned None (already exists) for {name!r}')
                reinserted.append(name)
                time.sleep(_RD_RATE_LIMIT_DELAY)
                continue
            logging.info(f'[BadTorrentAudit] add_torrent ok for {name!r}: new_id={new_id!r} provider={provider.PROVIDER_NAME}')
            if old_id and str(new_id) != str(old_id):
                try:
                    provider.remove_torrent(old_id)
                except Exception as ex:
                    logging.warning(f'[BadTorrentAudit] Could not remove old torrent {old_id}: {ex}')
            reinserted.append(name)
            time.sleep(_RD_RATE_LIMIT_DELAY)
        except Exception as e:
            err_str = str(e)
            if '451' in err_str:
                logging.info(f'[BadTorrentAudit] DMCA blocked (451) for {name!r}, skipping')
                failed.append({'name': name, 'error': 'DMCA blocked on Real-Debrid (451)'})
                time.sleep(_RD_RATE_LIMIT_DELAY)
                continue
            logging.error(f'[BadTorrentAudit] add_torrent failed for {name!r}: {err_str}')
            from debrid.base import RateLimitError as _RateLimitError
            if isinstance(e, _RateLimitError) or '429' in err_str or 'rate limit' in err_str.lower():
                # Use Retry-After header if available, else exponential backoff
                retry_after = _RD_RATE_LIMIT_RETRY_DELAY
                if hasattr(e, 'response') and e.response is not None:
                    ra = e.response.headers.get('Retry-After')
                    logging.warning(f'[BadTorrentAudit] 429 headers: {dict(e.response.headers)}')
                    retry_after = float(ra) if ra else _RD_RATE_LIMIT_RETRY_DELAY
                backoff = retry_after
                while True:
                    logging.warning(f'[BadTorrentAudit] 429 rate limit hit, sleeping {backoff}s then retrying {name!r}')
                    time.sleep(backoff)
                    try:
                        new_id = provider.add_torrent(magnet)
                        logging.info(f'[BadTorrentAudit] Retry ok for {name!r}: new_id={new_id!r}')
                        reinserted.append(name)
                        time.sleep(_RD_RATE_LIMIT_DELAY)
                        break
                    except Exception as e2:
                        err_str = str(e2)
                        if '451' in err_str:
                            failed.append({'name': name, 'error': 'DMCA blocked on Real-Debrid (451)'})
                            break
                        elif isinstance(e2, _RateLimitError) or '429' in err_str or 'rate limit' in err_str.lower():
                            retry_after2 = float(e2.response.headers.get('Retry-After', backoff * 2)) if hasattr(e2, 'response') and e2.response is not None else backoff * 2
                            backoff = min(retry_after2, 120)
                            continue
                        else:
                            logging.error(f'[BadTorrentAudit] Retry failed for {name!r}: {err_str}')
                            failed.append({'name': name, 'error': err_str})
                            break
                continue
            failed.append({'name': name, 'error': err_str})

    if reinserted:
        _invalidate_library_cache()

    logging.info(f"[BadTorrentAudit] Reinsert complete: {len(reinserted)} ok, {len(failed)} failed, {len(no_hash)} no hash")
    return jsonify({
        'success': True,
        'reinserted': len(reinserted),
        'failed': len(failed),
        'no_hash': len(no_hash),
        'errors': [f['error'] for f in failed[:10]],
        'first_error': failed[0]['error'] if failed else None,
    })


# ---------------------------------------------------------------------------
# Usenet Repair API
# ---------------------------------------------------------------------------

_repair_thread = None
_repair_progress = {}  # shared state for the running repair

_debrid_repair_thread = None
_debrid_repair_progress = {}  # shared state for the running debrid repair


@debrid_manager_bp.route('/api/usenet/repair/stats')
def usenet_repair_stats():
    """Stats for the Usenet tab: cli_mount health summary + 30-day repair activity counts + available versions."""
    try:
        from usenet.repair_engine import get_health_summary, get_available_versions, get_repair_version
        from database.nzb_repair_activity import get_repair_stats
        health = get_health_summary()
        activity = get_repair_stats(days=30, source='usenet')
        versions = get_available_versions()
        repair_version = get_repair_version()
        return jsonify(success=True, health=health, activity=activity, versions=versions, repair_version=repair_version)
    except Exception as e:
        logging.error(f'[UsenetRepair] stats error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/activity')
def usenet_repair_activity():
    """Paginated repair activity log."""
    try:
        from database.nzb_repair_activity import get_repair_activity
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        outcome = request.args.get('outcome') or None
        rows, total = get_repair_activity(limit=limit, offset=offset, outcome=outcome, source='usenet')
        return jsonify(success=True, rows=rows, total=total)
    except Exception as e:
        logging.error(f'[UsenetRepair] activity error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/scan_status')
def usenet_scan_status():
    """Check if cli_mount is currently running a health sweep (active_run != null and running)."""
    try:
        from usenet.climount_client import get_climount_client
        from routes.api_tracker import api as _api
        client = get_climount_client()
        if not client.is_enabled():
            return jsonify(success=True, is_scanning=False)
        r = _api.get(f'{client.base_url}/api/repair/status', headers=client._headers(), timeout=10)
        if r.status_code != 200:
            return jsonify(success=True, is_scanning=False)
        data = r.json()
        active_run = data.get('active_run') or data.get('value', {}).get('active_run')
        run_status = (active_run or {}).get('status', '') if active_run else ''
        is_scanning = active_run is not None and run_status in ('running', 'pending', '')
        return jsonify(success=True, is_scanning=is_scanning, active_run=active_run)
    except Exception as e:
        logging.debug(f'[UsenetRepair] scan_status error: {e}')
        return jsonify(success=True, is_scanning=False)


@debrid_manager_bp.route('/api/usenet/repair/health_scan', methods=['POST'])
def usenet_trigger_health_scan():
    """Trigger a health scan for NZB entries. ?full=true scans all, default scans unchecked only."""
    try:
        from usenet.repair_engine import trigger_health_scan
        full = request.args.get('full', 'false').lower() in ('true', '1', 'yes')
        ok = trigger_health_scan(full=full)
        scan_type = 'full' if full else 'partial'
        return jsonify(success=ok, message=f'{scan_type.capitalize()} health scan triggered' if ok else 'Failed to trigger scan')
    except Exception as e:
        logging.error(f'[UsenetRepair] health_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/stop', methods=['POST'])
def usenet_stop_health_scan():
    """Stop a running NZB health scan in cli_mount."""
    try:
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')
        r = api.post(f'{url}/api/repair/stop', headers=_dcy_headers(), timeout=10)
        if r.status_code == 200:
            return jsonify(success=True, message='Health scan stop requested')
        return jsonify(success=False, error=f'cli_mount returned HTTP {r.status_code}')
    except Exception as e:
        logging.error(f'[UsenetRepair] stop_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/broken')
def usenet_broken_items():
    """Return current broken items from cli_mount health."""
    try:
        from usenet.repair_engine import fetch_broken_items
        # annotate_mount=False for fast response — mount verification
        # is only needed by the actual repair pipeline, not display.
        items = fetch_broken_items(annotate_mount=False)
        return jsonify(success=True, items=items, count=len(items))
    except Exception as e:
        logging.error(f'[UsenetRepair] broken items error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/fix_single', methods=['POST'])
def usenet_fix_single():
    """Repair a single broken entry by entry_name."""
    try:
        body = request.get_json(silent=True) or {}
        entry_name = body.get('entry_name', '').strip()
        version_override = body.get('version_override') or None
        if not entry_name:
            return jsonify(success=False, error='entry_name required'), 400
        from usenet.repair_engine import repair_single_entry
        result = repair_single_entry(entry_name, version_override=version_override)
        return jsonify(success=True, result=result)
    except Exception as e:
        logging.error(f'[UsenetRepair] fix_single error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/run', methods=['POST'])
def usenet_run_repair():
    """Kick off a repair run in background. Accepts optional version_override in JSON body."""
    global _repair_thread, _repair_progress

    if _repair_thread and _repair_thread.is_alive():
        return jsonify(success=False, message='Repair already running')

    body = request.get_json(silent=True) or {}
    version_override = body.get('version_override') or None

    _repair_progress = {'status': 'running', 'summary': None, 'error': None, 'started_at': time.time(),
                        'version_override': version_override}

    def _run():
        global _repair_progress
        try:
            from usenet.repair_engine import run_repair
            summary = run_repair(triggered_by='manual', version_override=version_override)
            _repair_progress['status'] = 'done'
            _repair_progress['summary'] = summary
        except Exception as e:
            logging.error(f'[UsenetRepair] run_repair thread error: {e}', exc_info=True)
            _repair_progress['status'] = 'error'
            _repair_progress['error'] = str(e)

    _repair_thread = threading.Thread(target=_run, daemon=True, name='usenet-repair')
    _repair_thread.start()
    return jsonify(success=True, message='Repair started')


@debrid_manager_bp.route('/api/usenet/repair/progress')
def usenet_repair_progress():
    """Poll repair run progress."""
    global _repair_thread, _repair_progress
    is_running = bool(_repair_thread and _repair_thread.is_alive())
    return jsonify(
        success=True,
        is_running=is_running,
        **_repair_progress,
    )


@debrid_manager_bp.route('/api/usenet/repair/delete_all_broken', methods=['POST'])
def usenet_delete_all_broken():
    """Delete all broken NZBs from cli_mount, Plex, and reset CLI DB items to Wanted."""
    try:
        from usenet.repair_engine import (
            fetch_broken_items, _find_db_items_by_entry_name,
            _delete_from_provider, _delete_from_plex,
        )
        from database.database_writing import update_media_item_state
        from database.not_wanted_magnets import add_to_not_wanted_nzb_guid

        broken = fetch_broken_items()
        if not broken:
            return jsonify(success=True, message='No broken items found', deleted_cli_mount=0, deleted_plex=0, reset_db=0)

        deleted_climount = 0
        deleted_plex = 0
        reset_db = 0

        for entry in broken:
            info_hash = entry.get('info_hash', '')
            entry_name = entry.get('entry_name', '') or entry.get('name', '')

            # Delete from cli_mount first
            if _delete_from_provider(info_hash, entry_name):
                deleted_climount += 1

            # Find matching DB items
            db_items = _find_db_items_by_entry_name(entry_name) if entry_name else []

            if db_items:
                for item in db_items:
                    # Delete from Plex
                    if _delete_from_plex(item):
                        deleted_plex += 1
                    # Reset to Wanted so it re-scrapes
                    try:
                        update_media_item_state(item['id'], 'Wanted')
                        reset_db += 1
                    except Exception as _dbe:
                        logging.warning(f'[DeleteBroken] DB reset failed for item {item.get("id")}: {_dbe}')
                    # Blacklist broken NZB URL so it won't be re-submitted
                    nzb_url = item.get('filled_by_magnet') or item.get('link', '')
                    if nzb_url:
                        try:
                            add_to_not_wanted_nzb_guid(nzb_url)
                        except Exception:
                            pass

        msg = (f'Deleted {deleted_climount} from cli_mount, '
               f'{deleted_plex} from Plex, '
               f'{reset_db} items reset to Wanted.')
        logging.info(f'[DeleteBroken] {msg}')
        return jsonify(success=True, message=msg,
                       deleted_climount=deleted_climount,
                       deleted_plex=deleted_plex,
                       reset_db=reset_db)
    except Exception as e:
        logging.error(f'[DeleteBroken] Error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/settings', methods=['GET'])
def usenet_repair_settings_get():
    try:
        from usenet.repair_engine import get_repair_version
        return jsonify(success=True, repair_version=get_repair_version())
    except Exception as e:
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/settings', methods=['POST'])
def usenet_repair_settings_save():
    try:
        from usenet.repair_engine import set_repair_version
        body = request.get_json(silent=True) or {}
        set_repair_version(body.get('repair_version', ''))
        return jsonify(success=True)
    except Exception as e:
        logging.error(f'[UsenetRepair] save settings error: {e}')
        return jsonify(success=False, error=str(e))


# ---------------------------------------------------------------------------
# Debrid (RealDebrid torrent) Health & Repair API
# ---------------------------------------------------------------------------

@debrid_manager_bp.route('/api/debrid/repair/stats')
def debrid_repair_stats():
    """Stats for the Debrid repair tab: cli_mount torrent health summary + 30-day activity counts."""
    try:
        from usenet.debrid_repair_engine import get_health_summary, get_repair_stats
        health = get_health_summary()
        activity = get_repair_stats(days=30)
        return jsonify(success=True, health=health, activity=activity)
    except Exception as e:
        logging.error(f'[DebridRepair] stats error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/activity')
def debrid_repair_activity():
    """Paginated repair activity log for debrid entries."""
    try:
        from usenet.debrid_repair_engine import get_repair_activity
        limit = int(request.args.get('limit', 25))
        offset = int(request.args.get('offset', 0))
        outcome = request.args.get('outcome') or None
        rows, total = get_repair_activity(limit=limit, offset=offset, outcome=outcome)
        return jsonify(success=True, rows=rows, total=total)
    except Exception as e:
        logging.error(f'[DebridRepair] activity error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/scan_status')
def debrid_scan_status():
    """Check if cli_mount is currently running a health sweep."""
    try:
        from usenet.climount_client import get_climount_client
        from routes.api_tracker import api as _api
        client = get_climount_client()
        if not client.is_enabled():
            return jsonify(success=True, is_scanning=False)
        r = _api.get(f'{client.base_url}/api/repair/status', headers=client._headers(), timeout=10)
        if r.status_code != 200:
            return jsonify(success=True, is_scanning=False)
        data = r.json()
        active_run = data.get('active_run') or data.get('value', {}).get('active_run')
        run_status = (active_run or {}).get('status', '') if active_run else ''
        is_scanning = active_run is not None and run_status in ('running', 'pending', '')
        return jsonify(success=True, is_scanning=is_scanning, active_run=active_run)
    except Exception as e:
        logging.debug(f'[DebridRepair] scan_status error: {e}')
        return jsonify(success=True, is_scanning=False)


@debrid_manager_bp.route('/api/debrid/repair/health_scan', methods=['POST'])
def debrid_trigger_health_scan():
    """Trigger a health scan for torrent entries. ?full=true scans all, default scans unchecked only."""
    try:
        from usenet.debrid_repair_engine import trigger_health_scan
        full = request.args.get('full', 'false').lower() in ('true', '1', 'yes')
        ok = trigger_health_scan(full=full)
        scan_type = 'full' if full else 'partial'
        return jsonify(success=ok, message=f'{scan_type.capitalize()} health scan triggered' if ok else 'Failed to trigger scan')
    except Exception as e:
        logging.error(f'[DebridRepair] health_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/stop', methods=['POST'])
def debrid_stop_health_scan():
    """Stop a running torrent health scan in cli_mount."""
    try:
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')
        r = api.post(f'{url}/api/repair/stop', headers=_dcy_headers(), timeout=10)
        if r.status_code == 200:
            return jsonify(success=True, message='Health scan stop requested')
        return jsonify(success=False, error=f'cli_mount returned HTTP {r.status_code}')
    except Exception as e:
        logging.error(f'[DebridRepair] stop_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/broken')
def debrid_broken_items():
    """Return current broken torrent items from cli_mount health."""
    try:
        from usenet.debrid_repair_engine import fetch_broken_items
        items = fetch_broken_items()
        return jsonify(success=True, items=items, count=len(items))
    except Exception as e:
        logging.error(f'[DebridRepair] broken items error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/fix_single', methods=['POST'])
def debrid_fix_single():
    """Repair a single broken debrid entry. action: 'reinsert' | 'replace'."""
    try:
        body = request.get_json(silent=True) or {}
        entry_name = body.get('entry_name', '').strip()
        info_hash = body.get('info_hash', '').strip()
        action = body.get('action', 'replace')
        version_override = body.get('version_override') or None
        if not entry_name:
            return jsonify(success=False, error='entry_name required'), 400
        from usenet.debrid_repair_engine import reinsert_entry, replace_entry
        if action == 'reinsert':
            result = reinsert_entry(entry_name, info_hash)
        else:
            result = replace_entry(entry_name, info_hash, version_override=version_override)
        return jsonify(success=True, result=result)
    except Exception as e:
        logging.error(f'[DebridRepair] fix_single error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/run', methods=['POST'])
def debrid_run_repair():
    """Kick off a debrid repair run in background."""
    global _debrid_repair_thread, _debrid_repair_progress

    if _debrid_repair_thread and _debrid_repair_thread.is_alive():
        return jsonify(success=False, message='Debrid repair already running')

    body = request.get_json(silent=True) or {}
    version_override = body.get('version_override') or None

    _debrid_repair_progress = {'status': 'running', 'summary': None, 'error': None,
                                'started_at': time.time(), 'version_override': version_override}

    def _run():
        global _debrid_repair_progress
        try:
            # Clean ghosts before repair so they don't show up as actionable broken items
            try:
                from usenet.debrid_repair_engine import delete_ghost_health_records
                ghost_result = delete_ghost_health_records()
                if ghost_result['deleted']:
                    logging.info(f'[DebridRepair] Ghost cleanup before repair: deleted={ghost_result["deleted"]}')
            except Exception as _ge:
                logging.warning(f'[DebridRepair] Ghost cleanup failed (continuing): {_ge}')
            from usenet.debrid_repair_engine import run_repair
            summary = run_repair(triggered_by='manual', version_override=version_override)
            _debrid_repair_progress['status'] = 'done'
            _debrid_repair_progress['summary'] = summary
        except Exception as e:
            logging.error(f'[DebridRepair] run_repair thread error: {e}', exc_info=True)
            _debrid_repair_progress['status'] = 'error'
            _debrid_repair_progress['error'] = str(e)

    _debrid_repair_thread = threading.Thread(target=_run, daemon=True, name='debrid-repair')
    _debrid_repair_thread.start()
    return jsonify(success=True, message='Debrid repair started')


@debrid_manager_bp.route('/api/debrid/repair/progress')
def debrid_repair_progress():
    """Poll debrid repair run progress."""
    global _debrid_repair_thread, _debrid_repair_progress
    is_running = bool(_debrid_repair_thread and _debrid_repair_thread.is_alive())
    return jsonify(
        success=True,
        is_running=is_running,
        **_debrid_repair_progress,
    )


@debrid_manager_bp.route('/api/debrid/repair/clear_bad_flags', methods=['POST'])
def debrid_clear_bad_flags():
    """Clear Bad=true flag on all cli_mount torrent entries by calling the sync endpoint.
    This recovers entries that cli_mount marked as bad after failed repair attempts."""
    try:
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api
        import urllib.parse

        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')

        # Get all torrent entries from cli_mount
        r = api.get(f'{url}/api/repair/health', headers=_dcy_headers(), timeout=60)
        if r.status_code != 200:
            return jsonify(success=False, error=f'Health API returned {r.status_code}')

        data = r.json()
        entries = data if isinstance(data, list) else data.get('entries', [])
        torrent_entries = [e for e in entries if (e.get('protocol') or '').lower() == 'torrent']

        cleared = 0
        errors = 0
        for entry in torrent_entries:
            broken_files = entry.get('broken_files') or []
            info_hash = (entry.get('info_hash') or entry.get('hash') or
                        (broken_files[0].get('info_hash') if broken_files else '') or '')
            if not info_hash:
                continue
            try:
                sr = api.post(f'{url}/api/torrents/{info_hash}/sync',
                              headers=_dcy_headers(), timeout=15)
                if sr.status_code == 200:
                    cleared += 1
                else:
                    errors += 1
            except Exception:
                errors += 1

        msg = f'Cleared Bad flag on {cleared} entries ({errors} errors)'
        logging.info(f'[DebridRepair] {msg}')
        return jsonify(success=True, message=msg, cleared=cleared, errors=errors)
    except Exception as e:
        logging.error(f'[DebridRepair] clear_bad_flags error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/delete_ghost_entries', methods=['POST'])
def debrid_delete_ghost_entries():
    """Delete all ghost entries from cli_mount's repair health records."""
    try:
        from usenet.debrid_repair_engine import delete_ghost_health_records
        result = delete_ghost_health_records()
        msg = f'Deleted {result["deleted"]} ghost health records ({result["skipped"]} real entries skipped, {result["errors"]} errors)'
        return jsonify(success=True, message=msg, **result)
    except Exception as e:
        logging.error(f'[DebridRepair] delete_ghost_entries error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/delete_ghost_single', methods=['POST'])
def debrid_delete_ghost_single():
    """Delete a single ghost health record by entry name."""
    try:
        import urllib.parse as _up
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api
        body = request.get_json(silent=True) or {}
        entry_name = (body.get('entry_name') or '').strip()
        if not entry_name:
            return jsonify(success=False, error='entry_name required'), 400
        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')
        encoded = _up.quote(entry_name, safe='')
        r = api.delete(f'{url}/api/repair/health/{encoded}', headers=_dcy_headers(), timeout=15)
        if r.status_code == 200:
            logging.info(f'[DebridRepair] Deleted ghost health record: {entry_name!r}')
            return jsonify(success=True)
        return jsonify(success=False, error=f'cli_mount returned HTTP {r.status_code}')
    except Exception as e:
        logging.error(f'[DebridRepair] delete_ghost_single error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/delete_all_broken', methods=['POST'])
def debrid_delete_all_broken():
    """Delete all broken torrent entries from cli_mount, Plex, and reset CLI DB items to Wanted."""
    try:
        from usenet.debrid_repair_engine import delete_all_broken
        result = delete_all_broken()
        msg = (f'Deleted {result["deleted_climount"]} from cli_mount, '
               f'{result["deleted_plex"]} from Plex, '
               f'{result["reset_db"]} items reset to Wanted.')
        logging.info(f'[DebridRepair] {msg}')
        return jsonify(success=True, message=msg, **result)
    except Exception as e:
        logging.error(f'[DebridRepair] delete_all_broken error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))
