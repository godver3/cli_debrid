"""Torrent audits: cli_mount's __bad__ folder and the whole library, probed with ffprobe; deleting or reinserting what they find; cli_mount connection settings.

Part of the Debrid Manager blueprint (routes/debrid_manager_routes.py).
"""
import threading
import os
import json
import logging
import subprocess
import time
from routes.models import admin_required
from flask import jsonify
from flask import request
from debrid import get_debrid_providers
from routes.debrid_manager_routes import (
    _invalidate_library_cache,
    debrid_manager_bp,
)


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
