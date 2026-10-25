"""Library reconcile: debrid torrents vs. the database, and the bulk fixes it offers.

Part of the Debrid Manager blueprint (routes/debrid_manager_routes.py).
"""
from concurrent.futures import ThreadPoolExecutor
from routes.models import admin_required
from concurrent.futures import as_completed
from debrid import get_debrid_provider
from utilities.jellyfin_auth import jellyfin_auth_headers
from flask import jsonify
import logging
import os
from flask import request
import threading
import time
from routes.debrid_manager_routes import (
    _fetch_all_bg,
    _fetch_pages_bg,
    _lib,
    _load_lib_cache_from_db,
    _reconcile_cache,
    debrid_manager_bp,
)
from routes.debrid_manager.symlink_audits import _RCLONE_COUNT_STALE, _rclone_count_state, _run_rclone_count_bg, _symlink_audit_state


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
                _jf_headers = {**jellyfin_auth_headers(_jf_token), 'Accept': 'application/json'}
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
