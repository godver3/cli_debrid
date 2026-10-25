"""Plex trash: items in Plex's trash, purging them, deleting single parts, forced scans.

Part of the Debrid Manager blueprint (routes/debrid_manager_routes.py).
"""
from routes.models import admin_required
from flask import jsonify
import logging
from flask import request
import time
from debrid import get_debrid_provider
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from utilities.jellyfin_auth import jellyfin_auth_headers
from routes.debrid_manager_routes import (
    _fetch_all_bg,
    _fetch_pages_bg,
    _lib,
    _load_lib_cache_from_db,
    _plex_trash_items_cache,
    debrid_manager_bp,
)


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
            headers = {**jellyfin_auth_headers(jf_token), 'Content-Type': 'application/json'}
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
