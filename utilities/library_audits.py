"""Debrid Manager library audits: the background workers and their state.

Five audits, each run in a background thread and polled by the Debrid
Manager page through routes/debrid_manager_routes.py:

- symlink audit: symlinks whose targets are missing from the mount, plus a
  count of rclone mount entries;
- junk symlink audit: symlinks pointing at samples/extras/junk files;
- battery audit: shows in the metadata battery with missing or bad IDs;
- bad torrent audit: mount entries whose video file fails an ffprobe check;
- all torrent audit: every mount entry checked against the database.

Each keeps its progress in a module-level state dict that the routes read.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor


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


_JUNK_AUDIT_STALE_AFTER = 24 * 3600


_JUNK_AUDIT_CACHE_FILE = os.path.join(
    os.environ.get('USER_DB_CONTENT', '/user/db_content'),
    'junk_symlink_audit_cache.json',
)


_junk_symlink_audit_state = {
    'status': 'idle',
    'data': None,
    'error': None,
    'ts': 0,
    'lock': threading.Lock(),
}


def _junk_audit_db_path():
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        return conn.execute('PRAGMA database_list').fetchone()[2]
    finally:
        conn.close()


def _junk_audit_paths():
    from utilities.settings import get_setting
    file_mgmt = get_setting('File Management', 'file_collection_management', 'Plex')
    if file_mgmt != 'Symlinked/Local':
        return None, None, None
    mount = get_setting('File Management', 'original_files_path', '')
    symlinks = get_setting('File Management', 'symlinked_files_path', '')
    db_path = _junk_audit_db_path()
    return db_path, symlinks, mount


def _junk_audit_load_cache():
    try:
        if os.path.exists(_JUNK_AUDIT_CACHE_FILE):
            with open(_JUNK_AUDIT_CACHE_FILE, 'r') as f:
                saved = json.load(f)
            data, ts = saved.get('data'), saved.get('ts', 0)
            if data and ts:
                with _junk_symlink_audit_state['lock']:
                    _junk_symlink_audit_state['status'] = 'done'
                    _junk_symlink_audit_state['data'] = data
                    _junk_symlink_audit_state['ts'] = ts
                logging.info(f"[JunkAudit] Loaded cached results (age: {(time.time()-ts)/3600:.1f}h)")
    except Exception as exc:
        logging.warning(f"[JunkAudit] Could not load cache: {exc}")


def _junk_audit_save_cache(data, ts):
    try:
        with open(_JUNK_AUDIT_CACHE_FILE, 'w') as f:
            json.dump({'data': data, 'ts': ts}, f)
    except Exception as exc:
        logging.warning(f"[JunkAudit] Could not save cache: {exc}")


def _run_junk_symlink_audit_bg(db_path, symlink_dir, mount_dir):
    from utilities.junk_symlink_audit import build_junk_symlink_plan
    try:
        plan = build_junk_symlink_plan(
            db_path=db_path,
            symlink_root=symlink_dir,
            mount_path=mount_dir,
        )
        ts_now = time.time()
        plan['scanned_at'] = ts_now
        with _junk_symlink_audit_state['lock']:
            _junk_symlink_audit_state['status'] = 'done'
            _junk_symlink_audit_state['data'] = plan
            _junk_symlink_audit_state['error'] = None
            _junk_symlink_audit_state['ts'] = ts_now
        _junk_audit_save_cache(plan, ts_now)
        logging.info(
            "[JunkAudit] Scan done: likely=%s review=%s mount=%s db_resets=%s dup_del=%s",
            plan['stats']['bad_symlinks'], plan['stats']['review_symlinks'],
            plan['stats']['mount_files'], plan['stats']['db_resets'],
            plan['stats']['db_duplicate_deletes'],
        )
    except Exception as exc:
        logging.error(f"[JunkAudit] Scan failed: {exc}", exc_info=True)
        with _junk_symlink_audit_state['lock']:
            _junk_symlink_audit_state['status'] = 'error'
            _junk_symlink_audit_state['error'] = str(exc)


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
