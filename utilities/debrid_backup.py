"""
Debrid Backup Utility
Handles backup and restore of Real-Debrid and AllDebrid torrent libraries.

Backup strategy (rd.py-inspired):
  - Up to 3 rotating slots: 1d, 3d, 7d (user-configurable retention intervals)
  - On each scheduled run:
      1. If 3d slot is due (slot age >= slot_3d_hours), copy 1d -> 3d
      2. If 7d slot is due (slot age >= slot_7d_hours), copy 3d -> 7d
      3. Do fresh fetch and write to 1d
  - Backup log (backup_log.json) tracks timestamps + counts for each slot
      timestamp  = when the data was originally captured (used for UI display)
      rotated_at = when the slot was last written (used for rotation timing)
"""
import json
import logging
import os
import shutil
import time
from typing import Dict, List, Optional

_LOG_TAG = '[DEBRID_BACKUP]'
_LOG_FILE = 'backup_log.json'
_BACKUP_SUBDIR = 'debrid_backups'

# Slot filenames — keyed by provider prefix + slot label
_SLOT_1D = '{provider}_backup_1d.json'
_SLOT_3D = '{provider}_backup_3d.json'
_SLOT_7D = '{provider}_backup_7d.json'


def get_backup_dir() -> str:
    from utilities.settings import get_config_dir
    d = os.path.join(get_config_dir(), _BACKUP_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _log_path() -> str:
    return os.path.join(get_backup_dir(), _LOG_FILE)


def _read_log() -> dict:
    path = _log_path()
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_log(data: dict):
    path = _log_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f'{_LOG_TAG} Failed to write log: {e}')


def _slot_file(provider: str, slot: str) -> str:
    """Returns full path for a slot file. slot = '1d' | '3d' | '7d'."""
    filename = f'{provider}_backup_{slot}.json'
    return os.path.join(get_backup_dir(), filename)


def _get_settings() -> dict:
    """Returns backup + cleanup settings with defaults."""
    from utilities.settings import get_setting
    return {
        # Backup
        'enabled':           get_setting('Debrid Backup', 'enabled', default=False),
        'slot_1d_hours':     int(get_setting('Debrid Backup', 'slot_1d_hours', default=24)),
        'slot_3d_hours':     int(get_setting('Debrid Backup', 'slot_3d_hours', default=72)),
        'slot_7d_hours':     int(get_setting('Debrid Backup', 'slot_7d_hours', default=168)),
        # Cleanup
        'cleanup_enabled':       get_setting('Debrid Cleanup', 'enabled', default=False),
        'cleanup_delete_errors': get_setting('Debrid Cleanup', 'delete_errors', default=True),
        'cleanup_delete_dupes':  get_setting('Debrid Cleanup', 'delete_dupes', default=True),
        'cleanup_delete_stalled': get_setting('Debrid Cleanup', 'delete_stalled', default=False),
        'cleanup_stalled_days':  int(get_setting('Debrid Cleanup', 'stalled_days', default=3)),
    }


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_rd_torrents(api_key: str) -> List[dict]:
    """Paginate RD /torrents and return the full list."""
    import requests
    headers = {'Authorization': f'Bearer {api_key}'}
    torrents = []
    page = 1
    limit = 500
    while True:
        try:
            r = requests.get(
                'https://api.real-debrid.com/rest/1.0/torrents',
                headers=headers,
                params={'limit': limit, 'page': page},
                timeout=30,
            )
            if r.status_code == 429:
                logging.warning(f'{_LOG_TAG} RD rate limit on page {page}, retrying after 5s')
                time.sleep(5)
                continue
            if r.status_code != 200:
                logging.error(f'{_LOG_TAG} RD /torrents page {page} returned {r.status_code}')
                break
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            torrents.extend(batch)
            if len(batch) < limit:
                break
            page += 1
        except Exception as e:
            logging.error(f'{_LOG_TAG} RD fetch error at page {page}: {e}')
            break
    return torrents


def fetch_ad_magnets(api_key: str) -> List[dict]:
    """Fetch all AllDebrid magnets."""
    import requests
    try:
        r = requests.get(
            'https://api.alldebrid.com/v4.1/magnet/status',
            params={'apikey': api_key},
            timeout=30,
        )
        if r.status_code != 200:
            logging.error(f'{_LOG_TAG} AD /magnet/status returned {r.status_code}')
            return []
        data = r.json()
        magnets = data.get('data', {}).get('magnets', [])
        return magnets if isinstance(magnets, list) else []
    except Exception as e:
        logging.error(f'{_LOG_TAG} AD fetch error: {e}')
        return []


# ---------------------------------------------------------------------------
# Core backup logic
# ---------------------------------------------------------------------------

def _write_slot(provider: str, slot: str, torrents: List[dict]) -> int:
    """Write backup data to a slot file. Returns count written."""
    path = _slot_file(provider, slot)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(torrents, f, ensure_ascii=False)
    return len(torrents)


def _copy_slot(provider: str, src: str, dst: str):
    """Copy one slot file to another."""
    src_path = _slot_file(provider, src)
    dst_path = _slot_file(provider, dst)
    if os.path.exists(src_path):
        shutil.copy2(src_path, dst_path)


def run_backup(force: bool = False) -> dict:
    """
    Main backup entry point. Called by the scheduled task and by manual trigger.
    Returns a result dict with keys: success, provider, counts, message.
    """
    settings = _get_settings()
    if not settings['enabled'] and not force:
        return {'success': False, 'message': 'Backup disabled', 'skipped': True}

    try:
        from debrid import get_debrid_provider, ProviderUnavailableError
        provider = get_debrid_provider()
    except Exception as e:
        return {'success': False, 'message': f'Provider unavailable: {e}'}

    provider_name = type(provider).__name__
    if 'RealDebrid' in provider_name:
        prefix = 'rd'
        torrents = fetch_rd_torrents(provider.api_key)
    elif 'AllDebrid' in provider_name:
        prefix = 'ad'
        torrents = fetch_ad_magnets(provider.api_key)
    else:
        return {'success': False, 'message': f'Unsupported provider: {provider_name}'}

    if not torrents:
        return {'success': False, 'message': 'No torrents returned from provider'}

    log = _read_log()
    now = time.time()
    slot_log = log.get(prefix, {})

    # --- Rotation: promote slots before writing fresh 1d ---
    slot_1d_ts = slot_log.get('1d', {}).get('timestamp', 0)
    slot_3d_ts = slot_log.get('3d', {}).get('timestamp', 0)

    # rotated_at = when the slot was last written (used for timing).
    # Falls back to timestamp for backwards-compat with old log files that lack rotated_at.
    slot_3d_rotated = slot_log.get('3d', {}).get('rotated_at') or slot_log.get('3d', {}).get('timestamp', 0)
    slot_7d_rotated = slot_log.get('7d', {}).get('rotated_at') or slot_log.get('7d', {}).get('timestamp', 0)

    slot_3d_hours = settings['slot_3d_hours']
    slot_7d_hours = settings['slot_7d_hours']

    # Promote 3d -> 7d if due.
    # Timer uses rotated_at so the interval is measured from when 7d was last written,
    # not from the age of the data it contains.
    if slot_3d_ts and (not slot_7d_rotated or (now - slot_7d_rotated) >= slot_7d_hours * 3600):
        _copy_slot(prefix, '3d', '7d')
        slot_log['7d'] = {
            'timestamp':  slot_3d_ts,                         # data capture time (for UI display)
            'rotated_at': now,                                 # rotation time (for timer)
            'count':      slot_log.get('3d', {}).get('count', 0),
        }
        logging.info(f'{_LOG_TAG} Promoted 3d -> 7d slot for {prefix}')

    # Promote 1d -> 3d if due.
    if slot_1d_ts and (not slot_3d_rotated or (now - slot_3d_rotated) >= slot_3d_hours * 3600):
        _copy_slot(prefix, '1d', '3d')
        slot_log['3d'] = {
            'timestamp':  slot_1d_ts,                         # data capture time (for UI display)
            'rotated_at': now,                                 # rotation time (for timer)
            'count':      slot_log.get('1d', {}).get('count', 0),
        }
        logging.info(f'{_LOG_TAG} Promoted 1d -> 3d slot for {prefix}')

    # Write fresh 1d
    count = _write_slot(prefix, '1d', torrents)
    slot_log['1d'] = {'timestamp': now, 'rotated_at': now, 'count': count}
    logging.info(f'{_LOG_TAG} Wrote {count} torrents to 1d slot for {prefix}')

    log[prefix] = slot_log
    log['last_run'] = now
    _write_log(log)

    return {
        'success': True,
        'provider': prefix,
        'count': count,
        'message': f'Backed up {count} torrents to 1d slot',
    }


# ---------------------------------------------------------------------------
# Status / listing
# ---------------------------------------------------------------------------

def get_backup_status() -> dict:
    """Returns status dict for the backup tab UI."""
    log = _read_log()
    backup_dir = get_backup_dir()
    settings = _get_settings()

    def slot_info(prefix: str, slot: str) -> Optional[dict]:
        path = _slot_file(prefix, slot)
        slot_data = log.get(prefix, {}).get(slot, {})
        if not os.path.exists(path) and not slot_data:
            return None
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return {
            'slot':      slot,
            'timestamp': slot_data.get('timestamp'),
            'count':     slot_data.get('count'),
            'size':      size,
            'exists':    os.path.exists(path),
        }

    result = {
        'enabled':      bool(settings['enabled']),
        'last_run':     log.get('last_run'),
        'last_cleanup': log.get('last_cleanup'),
        'cleanup_stats': log.get('cleanup_stats', {}),
        'settings':     settings,
        'providers':    {},
    }

    for prefix in ('rd', 'ad'):
        slots = {}
        for s in ('1d', '3d', '7d'):
            info = slot_info(prefix, s)
            if info:
                slots[s] = info
        if slots:
            result['providers'][prefix] = slots

    return result


def list_backup_files() -> List[dict]:
    """List all backup files in the backup directory."""
    backup_dir = get_backup_dir()
    files = []
    for fname in sorted(os.listdir(backup_dir)):
        if not fname.endswith('.json') or fname == _LOG_FILE:
            continue
        path = os.path.join(backup_dir, fname)
        stat = os.stat(path)
        files.append({
            'filename': fname,
            'size':     stat.st_size,
            'modified': stat.st_mtime,
        })
    return files


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_from_file(filename: str, dry_run: bool = False) -> dict:
    """
    Restore torrents from a backup file.
    Skips hashes already present in the provider.
    Uses selectFiles heuristic: video files > 10% of largest file size.
    Returns: {success, added, skipped, failed, total}
    """
    import re
    import requests

    path = os.path.join(get_backup_dir(), filename)
    if not os.path.exists(path):
        return {'success': False, 'message': f'File not found: {filename}'}

    with open(path, encoding='utf-8') as f:
        backup = json.load(f)

    if not isinstance(backup, list):
        return {'success': False, 'message': 'Invalid backup format'}

    try:
        from debrid import get_debrid_provider
        provider = get_debrid_provider()
    except Exception as e:
        return {'success': False, 'message': f'Provider unavailable: {e}'}

    provider_name = type(provider).__name__
    if 'RealDebrid' in provider_name:
        # Get current hashes
        current = fetch_rd_torrents(provider.api_key)
        current_hashes = {t.get('hash', '').lower() for t in current if t.get('hash')}

        # Get available hosts
        headers = {'Authorization': f'Bearer {provider.api_key}'}
        hosts_r = requests.get(
            'https://api.real-debrid.com/rest/1.0/torrents/availableHosts',
            headers=headers, timeout=15
        )
        host = hosts_r.json()[0]['host'] if hosts_r.status_code == 200 else 'real-debrid.com'

        added = []
        skipped = []
        failed = []

        for item in backup:
            item_hash = (item.get('hash') or '').lower()
            if not item_hash:
                failed.append({'hash': '?', 'reason': 'no hash'})
                continue
            if item_hash in current_hashes:
                skipped.append(item_hash)
                continue
            if dry_run:
                added.append(item_hash)
                continue
            try:
                # Add via magnet
                magnet = f'magnet:?xt=urn:btih:{item_hash}'
                r = requests.post(
                    'https://api.real-debrid.com/rest/1.0/torrents/addMagnet',
                    data={'host': host, 'magnet': magnet},
                    headers=headers, timeout=30
                )
                torrent = r.json()
                if 'id' not in torrent:
                    failed.append({'hash': item_hash, 'reason': torrent.get('error', 'no id returned')})
                    continue

                torrent_id = torrent['id']
                # selectFiles heuristic
                _select_files_rd(torrent_id, provider.api_key, headers)
                added.append(item_hash)
                time.sleep(0.15)  # gentle rate limiting
            except Exception as e:
                failed.append({'hash': item_hash, 'reason': str(e)})

        return {
            'success': True,
            'total':   len(backup),
            'added':   len(added),
            'skipped': len(skipped),
            'failed':  len(failed),
            'failures': failed,
        }

    else:
        return {'success': False, 'message': f'Restore not implemented for {provider_name}'}


def _select_files_rd(torrent_id: str, api_key: str, headers: dict):
    """Select video files for a just-added RD torrent using size heuristic."""
    import re
    import requests

    r = requests.get(
        f'https://api.real-debrid.com/rest/1.0/torrents/info/{torrent_id}',
        headers=headers, timeout=20
    )
    info = r.json()
    files = info.get('files', [])
    if not files:
        requests.post(
            f'https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{torrent_id}',
            data={'files': 'all'}, headers=headers, timeout=15
        )
        return

    video_re = re.compile(r'\.(mkv|mp4|avi|wmv|m4v|ts)$', re.IGNORECASE)
    sample_re = re.compile(r'\bsample\b', re.IGNORECASE)

    sizes = [f['bytes'] for f in files]
    cutoff = (max(sizes) * 0.10) if len(sizes) > 1 else 0

    chosen = []
    for f in files:
        path = f.get('path', '')
        if video_re.search(path) and not sample_re.search(path):
            if f['bytes'] > cutoff:
                chosen.append(str(f['id']))

    files_param = ','.join(chosen) if chosen else 'all'
    requests.post(
        f'https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{torrent_id}',
        data={'files': files_param}, headers=headers, timeout=15
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

_ERROR_STATUSES = frozenset({'error', 'magnet_error', 'virus', 'dead', 'error_dl_timeout', 'error_dl_size'})


def _is_error_status(status: str) -> bool:
    s = (status or '').lower()
    return s in _ERROR_STATUSES or s.startswith('error_') or s.endswith('_error')


def run_cleanup(force: bool = False) -> dict:
    """
    Main cleanup entry point. Called by the scheduled task and by manual trigger.

    Rules (each independently togglable):
    1. delete_errors  — delete torrents with error/magnet_error/virus/dead status
    2. delete_dupes   — keep best copy per hash (most links → oldest; else highest progress)
    3. delete_stalled — delete torrents at 0% progress for > stalled_days days

    Returns: {success, deleted_errors, deleted_dupes, deleted_stalled, total_deleted, message}
    """
    import requests
    from datetime import datetime

    settings = _get_settings()
    if not settings['cleanup_enabled'] and not force:
        return {'success': False, 'message': 'Cleanup disabled', 'skipped': True}

    try:
        from debrid import get_debrid_provider, ProviderUnavailableError
        provider = get_debrid_provider()
    except Exception as e:
        return {'success': False, 'message': f'Provider unavailable: {e}'}

    provider_name = type(provider).__name__
    if 'RealDebrid' in provider_name:
        return _cleanup_rd(provider.api_key, settings)
    elif 'AllDebrid' in provider_name:
        return _cleanup_ad(provider.api_key, settings)
    else:
        return {'success': False, 'message': f'Unsupported provider: {provider_name}'}


def _cleanup_rd(api_key: str, settings: dict) -> dict:
    """Real-Debrid cleanup implementation."""
    import requests
    from datetime import datetime

    headers = {'Authorization': f'Bearer {api_key}'}
    delete_errors   = settings.get('cleanup_delete_errors', True)
    delete_dupes    = settings.get('cleanup_delete_dupes', True)
    delete_stalled  = settings.get('cleanup_delete_stalled', False)
    stalled_days    = settings.get('cleanup_stalled_days', 3)
    stalled_cutoff  = stalled_days * 86400

    torrents = fetch_rd_torrents(api_key)
    now = time.time()

    deleted_errors  = []
    deleted_dupes   = []
    deleted_stalled = []
    kept_ids        = set()

    def _rd_delete(torrent_id: str, reason: str):
        try:
            r = requests.delete(
                f'https://api.real-debrid.com/rest/1.0/torrents/delete/{torrent_id}',
                headers=headers, timeout=15
            )
            logging.info(f'{_LOG_TAG} Deleted RD torrent {torrent_id} ({reason}) — {r.status_code}')
            time.sleep(0.1)
            return True
        except Exception as e:
            logging.warning(f'{_LOG_TAG} Failed to delete {torrent_id}: {e}')
            return False

    def _parse_added(added_str: str) -> float:
        """Parse RD added timestamp string to unix ts."""
        try:
            return datetime.strptime(added_str, '%Y-%m-%dT%H:%M:%S.000Z').timestamp()
        except Exception:
            return 0.0

    # Pass 1: Error torrents
    surviving = []
    for t in torrents:
        if delete_errors and _is_error_status(t.get('status', '')):
            if _rd_delete(t['id'], 'error status'):
                deleted_errors.append(t['id'])
            continue
        surviving.append(t)

    # Pass 2: Stalled (0% for > stalled_days)
    if delete_stalled:
        still_surviving = []
        for t in surviving:
            if t.get('progress', -1) == 0:
                added_ts = _parse_added(t.get('added', ''))
                if added_ts and (now - added_ts) > stalled_cutoff:
                    if _rd_delete(t['id'], f'stalled {stalled_days}d'):
                        deleted_stalled.append(t['id'])
                    continue
            still_surviving.append(t)
        surviving = still_surviving

    # Pass 3: Duplicates (group by hash)
    if delete_dupes:
        by_hash: dict = {}
        for t in surviving:
            h = (t.get('hash') or '').lower()
            if not h:
                continue
            by_hash.setdefault(h, []).append(t)

        for h, copies in by_hash.items():
            if len(copies) < 2:
                continue
            # Prefer the copy with the most links (downloaded ones)
            has_links = [c for c in copies if c.get('links')]
            if has_links:
                # Among linked copies, keep oldest; delete the rest
                has_links.sort(key=lambda c: _parse_added(c.get('added', '')))
                keep = has_links[0]
                to_del = [c for c in copies if c['id'] != keep['id']]
            else:
                # No links: keep highest progress, delete the rest
                copies.sort(key=lambda c: c.get('progress', 0), reverse=True)
                keep = copies[0]
                to_del = copies[1:]

            for t in to_del:
                if _rd_delete(t['id'], 'duplicate'):
                    deleted_dupes.append(t['id'])

    total = len(deleted_errors) + len(deleted_dupes) + len(deleted_stalled)

    # Write last_cleanup to log
    log = _read_log()
    log['last_cleanup'] = now
    log.setdefault('cleanup_stats', {})['last'] = {
        'timestamp':        now,
        'deleted_errors':   len(deleted_errors),
        'deleted_dupes':    len(deleted_dupes),
        'deleted_stalled':  len(deleted_stalled),
        'total':            total,
        'provider':         'rd',
    }
    _write_log(log)

    return {
        'success':          True,
        'provider':         'rd',
        'deleted_errors':   len(deleted_errors),
        'deleted_dupes':    len(deleted_dupes),
        'deleted_stalled':  len(deleted_stalled),
        'total_deleted':    total,
        'message':          f'Cleanup complete: {total} torrents removed',
    }


def _cleanup_ad(api_key: str, settings: dict) -> dict:
    """AllDebrid cleanup implementation."""
    import requests

    delete_errors   = settings.get('cleanup_delete_errors', True)
    delete_dupes    = settings.get('cleanup_delete_dupes', True)
    delete_stalled  = settings.get('cleanup_delete_stalled', False)
    stalled_days    = settings.get('cleanup_stalled_days', 3)
    stalled_cutoff  = stalled_days * 86400

    magnets = fetch_ad_magnets(api_key)
    now = time.time()

    _AD_ERROR_STATUSES = frozenset({'Error', 'Virus', 'Dead', 'Timeout', 'Banned'})

    def _ad_delete(magnet_id, reason: str):
        try:
            r = requests.delete(
                'https://api.alldebrid.com/v4.1/magnet/delete',
                params={'apikey': api_key, 'id': magnet_id},
                timeout=15,
            )
            logging.info(f'{_LOG_TAG} Deleted AD magnet {magnet_id} ({reason}) — {r.status_code}')
            time.sleep(0.1)
            return True
        except Exception as e:
            logging.warning(f'{_LOG_TAG} Failed to delete AD {magnet_id}: {e}')
            return False

    deleted_errors  = []
    deleted_dupes   = []
    deleted_stalled = []

    surviving = []
    for m in magnets:
        status = m.get('status', '')
        if delete_errors and status in _AD_ERROR_STATUSES:
            if _ad_delete(m.get('id'), 'error status'):
                deleted_errors.append(m.get('id'))
            continue
        surviving.append(m)

    if delete_stalled:
        still_surviving = []
        for m in surviving:
            if m.get('downloaded', 1) == 0 and m.get('progress', -1) == 0:
                added_ts = m.get('uploadDate') or 0
                if added_ts and (now - added_ts) > stalled_cutoff:
                    if _ad_delete(m.get('id'), f'stalled {stalled_days}d'):
                        deleted_stalled.append(m.get('id'))
                    continue
            still_surviving.append(m)
        surviving = still_surviving

    if delete_dupes:
        by_hash: dict = {}
        for m in surviving:
            h = (m.get('hash') or '').lower()
            if not h:
                continue
            by_hash.setdefault(h, []).append(m)
        for h, copies in by_hash.items():
            if len(copies) < 2:
                continue
            # Keep the most complete (highest downloaded bytes), delete the rest
            copies.sort(key=lambda c: c.get('downloaded', 0) or 0, reverse=True)
            for m in copies[1:]:
                if _ad_delete(m.get('id'), 'duplicate'):
                    deleted_dupes.append(m.get('id'))

    total = len(deleted_errors) + len(deleted_dupes) + len(deleted_stalled)

    log = _read_log()
    log['last_cleanup'] = now
    log.setdefault('cleanup_stats', {})['last'] = {
        'timestamp':        now,
        'deleted_errors':   len(deleted_errors),
        'deleted_dupes':    len(deleted_dupes),
        'deleted_stalled':  len(deleted_stalled),
        'total':            total,
        'provider':         'ad',
    }
    _write_log(log)

    return {
        'success':          True,
        'provider':         'ad',
        'deleted_errors':   len(deleted_errors),
        'deleted_dupes':    len(deleted_dupes),
        'deleted_stalled':  len(deleted_stalled),
        'total_deleted':    total,
        'message':          f'Cleanup complete: {total} magnets removed',
    }


def get_cleanup_settings() -> dict:
    """Returns cleanup settings for the UI."""
    from utilities.settings import get_setting
    return {
        'enabled':        get_setting('Debrid Cleanup', 'enabled', default=False),
        'delete_errors':  get_setting('Debrid Cleanup', 'delete_errors', default=True),
        'delete_dupes':   get_setting('Debrid Cleanup', 'delete_dupes', default=True),
        'delete_stalled': get_setting('Debrid Cleanup', 'delete_stalled', default=False),
        'stalled_days':   int(get_setting('Debrid Cleanup', 'stalled_days', default=3)),
    }
