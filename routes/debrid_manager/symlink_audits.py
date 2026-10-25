"""Symlink audits: broken/orphaned symlinks, junk symlinks, and the rclone file count.

Part of the Debrid Manager blueprint (routes/debrid_manager_routes.py).
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import logging
import time
import json
import re
from collections import defaultdict
from routes.models import admin_required
from flask import jsonify
from flask import request
from routes.debrid_manager_routes import (
    debrid_manager_bp,
)


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
    deleted, failed, errors, skipped = 0, 0, [], 0

    # Re-verify against the CURRENT filesystem state right before deleting -
    # the caller's path list came from a cached scan that may be stale (a new
    # symlink may have been created pointing at one of these paths since the
    # scan ran, e.g. by normal ongoing collection activity). Trusting the
    # cache blindly can delete a file a real, currently-active symlink now
    # depends on.
    current_linked_targets = set()
    for root, dirs, files in os.walk(symlink_dir, followlinks=False):
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                raw = os.readlink(full)
                target = raw if os.path.isabs(raw) else os.path.normpath(
                    os.path.join(os.path.dirname(full), raw))
                current_linked_targets.add(target)

    for p in paths:
        if not isinstance(p, str):
            continue
        # Safety: path must be under symlink_dir or rclone_dir
        if not (p.startswith(symlink_dir) or p.startswith(rclone_dir)):
            errors.append(f'Refused: {p}')
            failed += 1
            continue
        if p in current_linked_targets:
            skipped += 1
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

    return jsonify({'success': True, 'deleted': deleted, 'failed': failed, 'skipped': skipped, 'errors': errors[:20]})


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


_junk_audit_load_cache()


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


@debrid_manager_bp.route('/api/junk_symlink_audit')
@admin_required
def api_junk_symlink_audit():
    db_path, symlink_dir, mount_dir = _junk_audit_paths()
    if not symlink_dir:
        return jsonify({'success': False, 'error': 'Not in symlink mode'}), 400

    with _junk_symlink_audit_state['lock']:
        status = _junk_symlink_audit_state['status']
        data = _junk_symlink_audit_state['data']
        ts = _junk_symlink_audit_state['ts']
        err = _junk_symlink_audit_state['error']

    stale = bool(ts and (time.time() - ts) > _JUNK_AUDIT_STALE_AFTER)
    if status == 'idle' and not data and os.path.isdir(symlink_dir):
        with _junk_symlink_audit_state['lock']:
            if _junk_symlink_audit_state['status'] == 'idle':
                _junk_symlink_audit_state['status'] = 'scanning'
        threading.Thread(
            target=_run_junk_symlink_audit_bg,
            args=(db_path, symlink_dir, mount_dir),
            daemon=True,
        ).start()
        status = 'scanning'

    return jsonify({
        'success': True,
        'status': status,
        'data': data,
        'ts': ts,
        'stale': stale,
        'error': err,
        'paths': {'symlinks': symlink_dir, 'mount': mount_dir},
    })


@debrid_manager_bp.route('/api/junk_symlink_audit/scan', methods=['POST'])
@admin_required
def api_junk_symlink_audit_scan():
    db_path, symlink_dir, mount_dir = _junk_audit_paths()
    if not symlink_dir:
        return jsonify({'success': False, 'error': 'Not in symlink mode'}), 400
    if not os.path.isdir(symlink_dir):
        return jsonify({'success': False, 'error': f'Symlink directory not found: {symlink_dir}'}), 400
    if mount_dir and not os.path.isdir(mount_dir):
        return jsonify({'success': False, 'error': f'Mount directory not found: {mount_dir}'}), 400

    with _junk_symlink_audit_state['lock']:
        if _junk_symlink_audit_state['status'] == 'scanning':
            return jsonify({'success': True, 'status': 'scanning'})
        _junk_symlink_audit_state['status'] = 'scanning'
        _junk_symlink_audit_state['error'] = None

    threading.Thread(
        target=_run_junk_symlink_audit_bg,
        args=(db_path, symlink_dir, mount_dir),
        daemon=True,
    ).start()
    return jsonify({'success': True, 'status': 'scanning'})


@debrid_manager_bp.route('/api/junk_symlink_audit/apply', methods=['POST'])
@admin_required
def api_junk_symlink_audit_apply():
    """Apply junk audit fixes: symlinks, mount orphans, DB rescrape/delete."""
    from utilities.junk_symlink_audit import apply_junk_symlink_plan

    db_path, symlink_dir, mount_dir = _junk_audit_paths()
    if not symlink_dir:
        return jsonify({'success': False, 'error': 'Not in symlink mode'}), 400

    with _junk_symlink_audit_state['lock']:
        plan = _junk_symlink_audit_state.get('data')
    if not plan:
        return jsonify({'success': False, 'error': 'No audit results — run scan first'}), 400

    req = request.get_json(silent=True) or {}
    apply_all = req.get('apply_all', True)
    item_ids = req.get('item_ids')
    symlink_paths = req.get('symlink_paths')
    mount_paths = req.get('mount_paths')
    include_mount = req.get('include_mount', True)
    apply_db = req.get('apply_db', apply_all or bool(item_ids))
    # Default bulk fix to high-confidence junk only; pass 'review' or 'all' explicitly.
    confidence_tier = req.get('confidence_tier')
    if apply_all and confidence_tier is None:
        confidence_tier = 'high'
    elif confidence_tier == 'all':
        confidence_tier = None

    symlink_set = None if apply_all and not symlink_paths else (
        set(symlink_paths) if symlink_paths else (set() if not apply_all else None)
    )
    if item_ids and not symlink_paths:
        id_set = {int(i) for i in item_ids}
        symlink_set = {
            e['symlink'] for e in plan.get('bad_symlinks', []) + plan.get('review_symlinks', [])
            if e.get('item_id') in id_set
        }
    mount_set = None if not include_mount else (
        None if apply_all and not mount_paths else (
            set(mount_paths) if mount_paths else (set() if not apply_all else None)
        )
    )
    reset_ids = None
    delete_ids = None
    if not apply_db:
        reset_ids = set()
        delete_ids = set()
    elif item_ids:
        reset_ids = {int(i) for i in item_ids}
        delete_ids = {int(i) for i in item_ids}

    result = apply_junk_symlink_plan(
        plan,
        db_path=db_path,
        symlink_paths=symlink_set,
        mount_paths=mount_set,
        reset_item_ids=reset_ids,
        delete_item_ids=delete_ids,
        use_rescrape=True,
        confidence_tier=confidence_tier,
    )

    with _junk_symlink_audit_state['lock']:
        _junk_symlink_audit_state['status'] = 'idle'
        _junk_symlink_audit_state['data'] = None
        _junk_symlink_audit_state['ts'] = 0
    try:
        if os.path.exists(_JUNK_AUDIT_CACHE_FILE):
            os.remove(_JUNK_AUDIT_CACHE_FILE)
    except Exception:
        pass

    return jsonify({'success': True, **result})
