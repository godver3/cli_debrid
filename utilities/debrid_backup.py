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


def fetch_tb_torrents(api_key: str) -> List[dict]:
    """Fetch all Torbox torrents (backup source)."""
    import requests
    try:
        r = requests.get(
            'https://api.torbox.app/v1/api/torrents/mylist',
            params={'bypass_cache': 'true'},
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30,
        )
        if r.status_code != 200:
            logging.error(f'{_LOG_TAG} TB /torrents/mylist returned {r.status_code}')
            return []
        data = r.json()
        torrents = data.get('data', [])
        result = []
        for t in (torrents if isinstance(torrents, list) else []):
            h = (t.get('hash') or '').lower()
            if not h:
                continue
            magnet = f"magnet:?xt=urn:btih:{h}&dn={t.get('name', '')}"
            result.append({'hash': h, 'magnet': magnet, 'name': t.get('name', ''), 'id': t.get('id')})
        return result
    except Exception as e:
        logging.error(f'{_LOG_TAG} TB fetch error: {e}')
        return []


def fetch_dl_torrents(api_key: str) -> List[dict]:
    """Fetch all Debrid-Link seedbox torrents (backup source)."""
    import requests
    try:
        result = []
        page = 0
        per_page = 100
        while True:
            r = requests.get(
                'https://debrid-link.com/api/v2/seedbox/list',
                params={'page': page, 'perPage': per_page},
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=30,
            )
            if r.status_code != 200:
                logging.error(f'{_LOG_TAG} DL /seedbox/list returned {r.status_code}')
                break
            data = r.json()
            items = data.get('value', [])
            if not isinstance(items, list):
                break
            for t in items:
                h = (t.get('hashString') or '').lower()
                if not h:
                    continue
                magnet = f"magnet:?xt=urn:btih:{h}&dn={t.get('name', '')}"
                result.append({'hash': h, 'magnet': magnet, 'name': t.get('name', ''), 'id': t.get('id')})
            pagination = data.get('pagination', {})
            next_page = pagination.get('next', -1)
            if next_page == -1 or len(items) < per_page:
                break
            page = next_page
        return result
    except Exception as e:
        logging.error(f'{_LOG_TAG} DL fetch error: {e}')
        return []


def fetch_pm_transfers(api_key: str) -> List[dict]:
    """Fetch all Premiumize transfers (backup source)."""
    import requests
    try:
        r = requests.get(
            'https://www.premiumize.me/api/transfer/list',
            params={'apikey': api_key},
            timeout=30,
        )
        if r.status_code != 200:
            logging.error(f'{_LOG_TAG} PM /transfer/list returned {r.status_code}')
            return []
        data = r.json()
        transfers = data.get('transfers', [])
        return transfers if isinstance(transfers, list) else []
    except Exception as e:
        logging.error(f'{_LOG_TAG} PM fetch error: {e}')
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
    pname = getattr(provider, 'PROVIDER_NAME', provider_name)
    if 'RealDebrid' in provider_name or pname == 'Real-Debrid':
        prefix = 'rd'
        torrents = fetch_rd_torrents(provider.api_key)
    elif 'AllDebrid' in provider_name or pname == 'AllDebrid':
        prefix = 'ad'
        torrents = fetch_ad_magnets(provider.api_key)
    elif 'Torbox' in provider_name or pname == 'Torbox':
        prefix = 'tb'
        torrents = fetch_tb_torrents(provider.api_key)
    elif 'Debrid-Link' in provider_name or pname == 'Debrid-Link':
        prefix = 'dl'
        torrents = fetch_dl_torrents(provider.api_key)
    elif 'Premiumize' in provider_name or pname == 'Premiumize':
        prefix = 'pm'
        torrents = fetch_pm_transfers(provider.api_key)
    else:
        return {'success': False, 'message': f'Unsupported provider: {pname}'}

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

    for prefix in ('rd', 'ad', 'tb', 'dl', 'pm'):
        slots = {}
        for s in ('1d', '3d', '7d'):
            info = slot_info(prefix, s)
            if info:
                slots[s] = info
        if slots:
            result['providers'][prefix] = slots

    # Include extra files (e.g. usenet_failed, usenet_not_found) that aren't scheduled slots
    slot_filenames = {f'{p}_backup_{s}.json' for p in ('rd','ad','tb','dl','pm') for s in ('1d','3d','7d')}
    extra = []
    for f in list_backup_files():
        fname = f['filename']
        if fname in slot_filenames or fname == _LOG_FILE:
            continue
        try:
            with open(os.path.join(backup_dir, fname), encoding='utf-8') as fh:
                import json as _json
                data = _json.load(fh)
                count = len(data) if isinstance(data, list) else None
        except Exception:
            count = None
        extra.append({
            'filename': fname,
            'size': f['size'],
            'modified': f['modified'],
            'count': count,
        })
    result['extra_files'] = extra

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
    pname = getattr(provider, 'PROVIDER_NAME', provider_name)
    if 'RealDebrid' in provider_name or pname == 'Real-Debrid':
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

    elif 'AllDebrid' in provider_name or pname == 'AllDebrid':
        # Get current hashes to skip already-present torrents
        current = fetch_ad_magnets(provider.api_key)
        current_hashes = {m.get('hash', '').lower() for m in current if m.get('hash')}

        import requests
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
                magnet = f'magnet:?xt=urn:btih:{item_hash}'
                r = requests.post(
                    'https://api.alldebrid.com/v4/magnet/upload',
                    params={'apikey': provider.api_key},
                    data={'magnets[]': magnet},
                    timeout=30,
                )
                resp = r.json()
                if resp.get('status') != 'success':
                    msg = resp.get('error', {}).get('message', 'unknown error') if isinstance(resp.get('error'), dict) else str(resp.get('error', 'unknown'))
                    failed.append({'hash': item_hash, 'reason': msg})
                    continue
                added.append(item_hash)
                time.sleep(0.2)
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

    elif 'Torbox' in provider_name or pname == 'Torbox':
        current = fetch_tb_torrents(provider.api_key)
        current_hashes = {t.get('hash', '').lower() for t in current if t.get('hash')}

        import requests
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
                magnet = f'magnet:?xt=urn:btih:{item_hash}'
                r = requests.post(
                    'https://api.torbox.app/v1/api/torrents/createtorrent',
                    data={'magnet': magnet},
                    headers={'Authorization': f'Bearer {provider.api_key}'},
                    timeout=30,
                )
                resp = r.json()
                if not resp.get('success'):
                    failed.append({'hash': item_hash, 'reason': resp.get('detail', 'unknown error')})
                    continue
                added.append(item_hash)
                time.sleep(0.25)
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

    elif 'Debrid-Link' in provider_name or pname == 'Debrid-Link':
        current = fetch_dl_torrents(provider.api_key)
        current_hashes = {t.get('hash', '').lower() for t in current if t.get('hash')}

        import requests
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
                magnet = f'magnet:?xt=urn:btih:{item_hash}'
                r = requests.post(
                    'https://debrid-link.com/api/v2/seedbox/add',
                    json={'url': magnet, 'async': True},
                    headers={'Authorization': f'Bearer {provider.api_key}'},
                    timeout=30,
                )
                resp = r.json()
                if not resp.get('success'):
                    failed.append({'hash': item_hash, 'reason': resp.get('error_description', 'unknown error')})
                    continue
                added.append(item_hash)
                time.sleep(0.3)
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

    elif 'Premiumize' in provider_name or pname == 'Premiumize':
        # Get current transfer hashes to avoid duplicates
        current = fetch_pm_transfers(provider.api_key)
        current_hashes = {(t.get('hash') or t.get('src', '').split('btih:')[-1].split('&')[0]).lower()
                          for t in current}

        added, skipped, failed = [], [], []
        for item in backup:
            # Premiumize backup stores transfers; extract hash from src or hash field
            item_hash = (item.get('hash') or '').lower()
            if not item_hash:
                src = item.get('src', '')
                if 'btih:' in src.lower():
                    item_hash = src.lower().split('btih:')[-1].split('&')[0]
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
                magnet = f'magnet:?xt=urn:btih:{item_hash}'
                r = requests.post(
                    'https://www.premiumize.me/api/transfer/create',
                    params={'apikey': provider.api_key},
                    data={'src': magnet},
                    timeout=30,
                )
                resp = r.json()
                if resp.get('status') != 'success':
                    failed.append({'hash': item_hash, 'reason': resp.get('message', 'unknown')})
                    continue
                added.append(item_hash)
                time.sleep(0.3)
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


# Provider IDs supported for cross-provider restore
RESTORE_PROVIDERS = {
    'realdebrid':  'Real-Debrid',
    'alldebrid':   'AllDebrid',
    'torbox':      'Torbox',
    'debridlink':  'Debrid-Link',
    'premiumize':  'Premiumize',
}


_CACHED_STATUSES = frozenset([
    'downloaded',   # Real-Debrid
    'Ready',        # AllDebrid
    'cached',       # Torbox / generic
    'complete',     # Debrid-Link
    'finished',     # Premiumize
])


def restore_from_file_to_provider(filename: str, provider_id: str, api_key: str,
                                   dry_run: bool = False, cached_only: bool = False,
                                   progress_cb=None) -> dict:
    """Restore a backup file to an explicitly specified provider using a given API key.

    provider_id must be one of the keys in RESTORE_PROVIDERS.
    The backup file can be from any provider — only the hash field is used.
    If cached_only=True, only items whose status indicates they were fully downloaded are restored.
    progress_cb(current, total, name) is called before each item is processed.
    """
    import requests as _req

    if provider_id not in RESTORE_PROVIDERS:
        return {'success': False, 'message': f'Unknown provider: {provider_id}'}
    if not api_key or not api_key.strip():
        return {'success': False, 'message': 'API key required'}

    api_key = api_key.strip()
    path = os.path.join(get_backup_dir(), filename)
    if not os.path.exists(path):
        return {'success': False, 'message': f'File not found: {filename}'}

    with open(path, encoding='utf-8') as f:
        backup = json.load(f)
    if not isinstance(backup, list):
        return {'success': False, 'message': 'Invalid backup format'}

    # Extract all hashes from backup (works for any source provider format)
    def _extract_hash(item):
        h = (item.get('hash') or '').lower()
        if not h:
            src = item.get('src', '') or item.get('magnet', '')
            if 'btih:' in src.lower():
                h = src.lower().split('btih:')[-1].split('&')[0]
        return h

    if cached_only:
        before = len(backup)
        hashes_to_check = [_extract_hash(item) for item in backup]
        hashes_to_check = [h for h in hashes_to_check if h]
        cached_hashes = set()
        try:
            if provider_id == 'torbox':
                chunk_size = 100
                for i in range(0, len(hashes_to_check), chunk_size):
                    chunk = hashes_to_check[i:i+chunk_size]
                    r = _req.get('https://api.torbox.app/v1/api/torrents/checkcached',
                                 params={'hash': ','.join(chunk), 'format': 'object'},
                                 headers={'Authorization': f'Bearer {api_key}'}, timeout=30)
                    if r.status_code == 200:
                        for h, info in (r.json().get('data') or {}).items():
                            if info: cached_hashes.add(h.lower())
            elif provider_id == 'alldebrid':
                chunk_size = 50
                for i in range(0, len(hashes_to_check), chunk_size):
                    chunk = hashes_to_check[i:i+chunk_size]
                    params = [('magnets[]', f'magnet:?xt=urn:btih:{h}') for h in chunk]
                    r = _req.get('https://api.alldebrid.com/v4.1/magnet/instant',
                                 params=[('agent', 'cli_debrid'), ('apikey', api_key)] + params, timeout=30)
                    if r.status_code == 200:
                        for m in (r.json().get('data') or {}).get('magnets', []):
                            if m.get('instant'):
                                h = (m.get('hash') or '').lower()
                                if h: cached_hashes.add(h)
            elif provider_id == 'premiumize':
                chunk_size = 100
                for i in range(0, len(hashes_to_check), chunk_size):
                    chunk = hashes_to_check[i:i+chunk_size]
                    params = [('items[]', h) for h in chunk] + [('apikey', api_key)]
                    r = _req.get('https://www.premiumize.me/api/cache/check', params=params, timeout=30)
                    if r.status_code == 200:
                        data = r.json()
                        responses = data.get('response', [])
                        for h, is_cached_val in zip(chunk, responses):
                            if is_cached_val: cached_hashes.add(h.lower())
            elif provider_id == 'debridlink':
                # DL has no batch cache check — add and check if instant (100% immediately)
                # Fall back to treating all as eligible; DL itself skips non-cached on add
                cached_hashes = set(hashes_to_check)
            else:
                cached_hashes = set(hashes_to_check)
        except Exception as _ce:
            logging.warning(f'[BackupRestore] cached_only check failed: {_ce}, falling back to all hashes')
            cached_hashes = set(hashes_to_check)
        backup = [item for item in backup if (_extract_hash(item) or '').lower() in cached_hashes]
        logging.info(f'[BackupRestore] cached_only: filtered {before} → {len(backup)} items (checked against {provider_id})')

    added, skipped, failed = [], [], []
    total_items = len(backup)
    _consecutive_failures = [0]
    _abort_reason = [None]
    _CONSECUTIVE_FAIL_LIMIT = 10  # abort if 10 non-skip items fail in a row

    def _progress(idx, item):
        name = item.get('filename') or item.get('name') or _extract_hash(item) or '?'
        if (idx + 1) % 500 == 0 or idx == 0:
            logging.info(f'[XPRestore] Progress: {idx + 1}/{total_items} — {name[:80]}')
        if progress_cb:
            progress_cb(idx + 1, total_items, name)

    def _record_fail(h, reason):
        failed.append({'hash': h, 'reason': reason})
        _consecutive_failures[0] += 1
        if _consecutive_failures[0] >= _CONSECUTIVE_FAIL_LIMIT and not _abort_reason[0]:
            _abort_reason[0] = reason
            logging.warning(f'[XPRestore] Aborting after {_CONSECUTIVE_FAIL_LIMIT} consecutive failures: {reason}')

    def _record_ok(h):
        added.append(h)
        _consecutive_failures[0] = 0

    def _should_abort():
        return _abort_reason[0] is not None

    if provider_id == 'realdebrid':
        current = fetch_rd_torrents(api_key)
        current_hashes = {t.get('hash', '').lower() for t in current if t.get('hash')}
        headers = {'Authorization': f'Bearer {api_key}'}
        try:
            hosts_r = _req.get('https://api.real-debrid.com/rest/1.0/torrents/availableHosts', headers=headers, timeout=15)
            host = hosts_r.json()[0]['host'] if hosts_r.status_code == 200 else 'real-debrid.com'
        except Exception:
            host = 'real-debrid.com'
        for idx, item in enumerate(backup):
            if _should_abort(): break
            _progress(idx, item)
            h = _extract_hash(item)
            if not h: _record_fail('?', 'no hash'); continue
            if h in current_hashes: skipped.append(h); continue
            if dry_run: added.append(h); continue
            try:
                r = _req.post('https://api.real-debrid.com/rest/1.0/torrents/addMagnet',
                              data={'host': host, 'magnet': f'magnet:?xt=urn:btih:{h}'},
                              headers=headers, timeout=30)
                torrent = r.json()
                if 'id' not in torrent:
                    _record_fail(h, torrent.get('error', 'no id returned')); continue
                _select_files_rd(torrent['id'], api_key, headers)
                _record_ok(h)
                time.sleep(0.5)
            except Exception as e:
                _record_fail(h, str(e))

    elif provider_id == 'alldebrid':
        current = fetch_ad_magnets(api_key)
        current_hashes = {m.get('hash', '').lower() for m in current if m.get('hash')}
        for idx, item in enumerate(backup):
            if _should_abort(): break
            _progress(idx, item)
            h = _extract_hash(item)
            if not h: _record_fail('?', 'no hash'); continue
            if h in current_hashes: skipped.append(h); continue
            if dry_run: added.append(h); continue
            try:
                r = _req.get('https://api.alldebrid.com/v4.1/magnet/upload',
                             params={'agent': 'cli_debrid', 'apikey': api_key, 'magnets[]': f'magnet:?xt=urn:btih:{h}'},
                             timeout=30)
                resp = r.json()
                if resp.get('status') != 'success':
                    _record_fail(h, resp.get('error', {}).get('message', 'unknown')); continue
                _record_ok(h)
                time.sleep(0.2)
            except Exception as e:
                _record_fail(h, str(e))

    elif provider_id == 'torbox':
        current = fetch_tb_torrents(api_key)
        current_hashes = {t.get('hash', '').lower() for t in current if t.get('hash')}
        for idx, item in enumerate(backup):
            if _should_abort(): break
            _progress(idx, item)
            h = _extract_hash(item)
            if not h: _record_fail('?', 'no hash'); continue
            if h in current_hashes: skipped.append(h); continue
            if dry_run: added.append(h); continue
            try:
                r = _req.post('https://api.torbox.app/v1/api/torrents/createtorrent',
                              data={'magnet': f'magnet:?xt=urn:btih:{h}'},
                              headers={'Authorization': f'Bearer {api_key}'}, timeout=30)
                logging.debug(f'[XPRestore] Torbox HTTP {r.status_code} body={r.text[:200]}')
                if r.status_code == 429 or (r.text and 'rate' in r.text.lower()):
                    reason = f'Rate limited (HTTP {r.status_code})'
                    try: reason = r.json().get('detail') or r.json().get('message') or reason
                    except Exception: pass
                    _record_fail(h, reason); continue
                if not r.text.strip():
                    _record_fail(h, f'Empty response (HTTP {r.status_code})'); continue
                resp = r.json()
                if not resp.get('success'):
                    reason = resp.get('detail') or resp.get('message') or resp.get('error') or str(resp)
                    _record_fail(h, reason); continue
                _record_ok(h)
                time.sleep(0.25)
            except Exception as e:
                _record_fail(h, str(e))

    elif provider_id == 'debridlink':
        logging.info(f'[XPRestore] DL: fetching current torrents')
        current = fetch_dl_torrents(api_key)
        logging.info(f'[XPRestore] DL: current library has {len(current)} torrents, backup has {len(backup)} items')
        current_hashes = {t.get('hash', '').lower() for t in current if t.get('hash')}
        for idx, item in enumerate(backup):
            if _should_abort(): break
            _progress(idx, item)
            h = _extract_hash(item)
            if not h: _record_fail('?', 'no hash'); continue
            if h in current_hashes: skipped.append(h); continue
            if dry_run: added.append(h); continue
            try:
                r = _req.post('https://debrid-link.com/api/v2/seedbox/add',
                              json={'url': f'magnet:?xt=urn:btih:{h}', 'async': True},
                              headers={'Authorization': f'Bearer {api_key}'}, timeout=30)
                if not r.text.strip():
                    reason = f'Empty response (HTTP {r.status_code})'
                    logging.warning(f'[XPRestore] DL empty response HTTP={r.status_code}')
                    _record_fail(h, reason); continue
                try:
                    resp = r.json()
                except Exception:
                    reason = f'HTTP {r.status_code}: {r.text[:200]}'
                    logging.warning(f'[XPRestore] DL non-JSON HTTP={r.status_code} body={r.text[:200]}')
                    _record_fail(h, reason); continue
                if not resp.get('success'):
                    reason = (resp.get('error_description')
                              or resp.get('error')
                              or resp.get('message')
                              or resp.get('detail')
                              or f'HTTP {r.status_code}: {r.text[:200]}')
                    logging.warning(f'[XPRestore] DL fail HTTP={r.status_code} resp={r.text[:300]}')
                    _record_fail(h, reason); continue
                _record_ok(h)
                time.sleep(0.3)
            except Exception as e:
                reason = f'{type(e).__name__}: {e}' if str(e) else type(e).__name__
                logging.warning(f'[XPRestore] DL exception for {h}: {reason}', exc_info=True)
                _record_fail(h, reason)

    elif provider_id == 'premiumize':
        current = fetch_pm_transfers(api_key)
        current_hashes = {(t.get('hash') or t.get('src', '').split('btih:')[-1].split('&')[0]).lower()
                          for t in current}
        for idx, item in enumerate(backup):
            if _should_abort(): break
            _progress(idx, item)
            h = _extract_hash(item)
            if not h: _record_fail('?', 'no hash'); continue
            if h in current_hashes: skipped.append(h); continue
            if dry_run: added.append(h); continue
            try:
                r = _req.post('https://www.premiumize.me/api/transfer/create',
                              params={'apikey': api_key},
                              data={'src': f'magnet:?xt=urn:btih:{h}'}, timeout=30)
                resp = r.json()
                if resp.get('status') != 'success':
                    _record_fail(h, resp.get('message', 'unknown')); continue
                _record_ok(h)
                time.sleep(0.3)
            except Exception as e:
                _record_fail(h, str(e))

    return {
        'success':      True,
        'total':        len(backup),
        'added':        len(added),
        'skipped':      len(skipped),
        'failed':       len(failed),
        'failures':     failed[:20],
        'aborted':      _abort_reason[0] is not None,
        'abort_reason': _abort_reason[0],
    }


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
    pname = getattr(provider, 'PROVIDER_NAME', provider_name)
    if 'RealDebrid' in provider_name or pname == 'Real-Debrid':
        return _cleanup_rd(provider.api_key, settings)
    elif 'AllDebrid' in provider_name or pname == 'AllDebrid':
        return _cleanup_ad(provider.api_key, settings)
    elif 'Torbox' in provider_name or pname == 'Torbox':
        return _cleanup_tb(provider.api_key, settings)
    elif 'Debrid-Link' in provider_name or pname == 'Debrid-Link':
        return _cleanup_dl(provider.api_key, settings)
    elif 'Premiumize' in provider_name or pname == 'Premiumize':
        return _cleanup_pm(provider.api_key, settings)
    else:
        return {'success': False, 'message': f'Unsupported provider: {pname}'}


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


def _cleanup_tb(api_key: str, settings: dict) -> dict:
    """Torbox cleanup — removes errored, stalled, and duplicate torrents."""
    import requests

    delete_errors  = settings.get('cleanup_delete_errors', True)
    delete_dupes   = settings.get('cleanup_delete_dupes', True)
    delete_stalled = settings.get('cleanup_delete_stalled', False)
    stalled_days   = settings.get('cleanup_stalled_days', 3)
    stalled_cutoff = stalled_days * 86400

    torrents = fetch_tb_torrents(api_key)
    now = time.time()

    deleted_errors  = []
    deleted_dupes   = []
    deleted_stalled = []
    delete_failures = []

    _TB_ERROR_STATES = frozenset({'error', 'failed'})

    headers = {'Authorization': f'Bearer {api_key}'}

    def _delete(torrent_id, bucket):
        try:
            r = requests.post(
                'https://api.torbox.app/v1/api/torrents/controltorrent',
                json={'operation': 'delete', 'torrent_id': int(torrent_id)},
                headers=headers,
                timeout=30,
            )
            if r.status_code == 200:
                bucket.append(torrent_id)
                return True
            delete_failures.append({'id': torrent_id, 'reason': f'HTTP {r.status_code}'})
        except Exception as e:
            delete_failures.append({'id': torrent_id, 'reason': str(e)})
        return False

    seen_hashes: dict = {}

    for t in torrents:
        tid   = t.get('id')
        state = (t.get('download_state') or '').lower()
        h     = (t.get('hash') or '').lower()

        if delete_errors and state in _TB_ERROR_STATES:
            _delete(tid, deleted_errors)
            continue

        if delete_stalled and state not in {'cached', 'completed', 'downloaded'}:
            added_ts = t.get('created_at')
            if added_ts:
                try:
                    from datetime import datetime
                    if isinstance(added_ts, str):
                        added_ts = datetime.fromisoformat(added_ts.replace('Z', '+00:00')).timestamp()
                    if (now - float(added_ts)) > stalled_cutoff:
                        _delete(tid, deleted_stalled)
                        continue
                except Exception:
                    pass

        if delete_dupes and h:
            if h in seen_hashes:
                _delete(tid, deleted_dupes)
                continue
            seen_hashes[h] = tid

    total = len(deleted_errors) + len(deleted_dupes) + len(deleted_stalled)

    log = _read_log()
    log['last_cleanup'] = now
    log.setdefault('cleanup_stats', {})['last'] = {
        'timestamp':       now,
        'deleted_errors':  len(deleted_errors),
        'deleted_dupes':   len(deleted_dupes),
        'deleted_stalled': len(deleted_stalled),
        'total':           total,
        'provider':        'tb',
    }
    _write_log(log)

    return {
        'success':         True,
        'provider':        'tb',
        'deleted_errors':  len(deleted_errors),
        'deleted_dupes':   len(deleted_dupes),
        'deleted_stalled': len(deleted_stalled),
        'total_deleted':   total,
        'message':         f'Cleanup complete: {total} torrents removed',
    }


def _cleanup_dl(api_key: str, settings: dict) -> dict:
    """Debrid-Link cleanup — removes errored, stalled, and duplicate torrents."""
    import requests

    delete_errors  = settings.get('cleanup_delete_errors', True)
    delete_dupes   = settings.get('cleanup_delete_dupes', True)
    delete_stalled = settings.get('cleanup_delete_stalled', False)
    stalled_days   = settings.get('cleanup_stalled_days', 3)
    stalled_cutoff = stalled_days * 86400

    torrents = fetch_dl_torrents(api_key)
    now = time.time()

    deleted_errors  = []
    deleted_dupes   = []
    deleted_stalled = []
    delete_failures = []

    headers = {'Authorization': f'Bearer {api_key}'}

    def _delete(torrent_id, bucket):
        try:
            r = requests.delete(
                f'https://debrid-link.com/api/v2/seedbox/{torrent_id}/remove',
                headers=headers,
                timeout=30,
            )
            if r.status_code == 200:
                resp = r.json()
                if resp.get('success'):
                    bucket.append(torrent_id)
                    logging.info(f'{_LOG_TAG} DL deleted torrent {torrent_id}')
                    return True
                delete_failures.append({'id': torrent_id, 'reason': resp.get('error_description', 'API returned success=false')})
            else:
                delete_failures.append({'id': torrent_id, 'reason': f'HTTP {r.status_code}'})
        except Exception as e:
            delete_failures.append({'id': torrent_id, 'reason': str(e)})
        return False

    # Note: fetch_dl_torrents returns normalized dicts {hash, magnet, name, id}
    # For error/stall detection we need the raw API — fetch directly here
    raw_torrents = []
    try:
        page = 0
        while True:
            r = requests.get(
                'https://debrid-link.com/api/v2/seedbox/list',
                params={'page': page, 'perPage': 100},
                headers=headers,
                timeout=30,
            )
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get('value', [])
            if not isinstance(items, list):
                break
            raw_torrents.extend(items)
            pagination = data.get('pagination', {})
            next_page = pagination.get('next', -1)
            if next_page == -1 or len(items) < 100:
                break
            page = next_page
    except Exception as e:
        logging.warning(f'{_LOG_TAG} DL cleanup: failed to fetch raw torrents: {e}')

    seen_hashes: dict = {}

    for t in raw_torrents:
        tid    = t.get('id')
        error  = int(t.get('error', 0) or 0)
        pct    = int(t.get('downloadPercent', 0) or 0)
        done   = bool(t.get('downloaded'))
        h      = (t.get('hashString') or '').lower()
        created = t.get('created', 0) or 0

        if delete_errors and error != 0:
            _delete(tid, deleted_errors)
            continue

        if delete_stalled and not done and pct < 100:
            try:
                if created and (now - float(created)) > stalled_cutoff:
                    _delete(tid, deleted_stalled)
                    continue
            except Exception:
                pass

        if delete_dupes and h:
            if h in seen_hashes:
                _delete(tid, deleted_dupes)
                continue
            seen_hashes[h] = tid

    total = len(deleted_errors) + len(deleted_dupes) + len(deleted_stalled)

    log = _read_log()
    log['last_cleanup'] = now
    log.setdefault('cleanup_stats', {})['last'] = {
        'timestamp':       now,
        'deleted_errors':  len(deleted_errors),
        'deleted_dupes':   len(deleted_dupes),
        'deleted_stalled': len(deleted_stalled),
        'total':           total,
        'provider':        'dl',
    }
    _write_log(log)

    return {
        'success':         True,
        'provider':        'dl',
        'deleted_errors':  len(deleted_errors),
        'deleted_dupes':   len(deleted_dupes),
        'deleted_stalled': len(deleted_stalled),
        'total_deleted':   total,
        'message':         f'Cleanup complete: {total} torrents removed',
    }


def _cleanup_pm(api_key: str, settings: dict) -> dict:
    """Premiumize cleanup — removes errored and duplicate transfers."""
    import requests
    now = time.time()

    try:
        r = requests.get(
            'https://www.premiumize.me/api/transfer/list',
            params={'apikey': api_key},
            timeout=30,
        )
        transfers = r.json().get('transfers', []) if r.status_code == 200 else []
    except Exception as e:
        return {'success': False, 'message': f'Failed to fetch Premiumize transfers: {e}'}

    deleted_errors = []
    deleted_dupes  = []

    # Delete errored/deleted transfers
    if settings.get('cleanup_delete_errors', True):
        for t in transfers:
            if (t.get('status') or '').lower() in ('error', 'deleted', 'timeout'):
                try:
                    requests.post(
                        'https://www.premiumize.me/api/transfer/delete',
                        params={'apikey': api_key},
                        data={'id': t['id']},
                        timeout=15,
                    )
                    deleted_errors.append(t['id'])
                except Exception:
                    pass

    # Delete duplicate transfers (same name, keep newest by id)
    if settings.get('cleanup_delete_dupes', True):
        from collections import defaultdict
        by_name: dict = defaultdict(list)
        for t in transfers:
            if (t.get('status') or '').lower() not in ('error', 'deleted', 'timeout'):
                by_name[t.get('name', '').lower()].append(t)
        for name, copies in by_name.items():
            if len(copies) > 1:
                copies.sort(key=lambda x: x.get('id', 0), reverse=True)
                for dup in copies[1:]:
                    try:
                        requests.post(
                            'https://www.premiumize.me/api/transfer/delete',
                            params={'apikey': api_key},
                            data={'id': dup['id']},
                            timeout=15,
                        )
                        deleted_dupes.append(dup['id'])
                    except Exception:
                        pass

    total = len(deleted_errors) + len(deleted_dupes)

    log = _read_log()
    log['last_cleanup'] = now
    log.setdefault('cleanup_stats', {})['last'] = {
        'timestamp':        now,
        'deleted_errors':   len(deleted_errors),
        'deleted_dupes':    len(deleted_dupes),
        'deleted_stalled':  0,
        'total':            total,
        'provider':         'pm',
    }
    _write_log(log)

    return {
        'success':          True,
        'provider':         'pm',
        'deleted_errors':   len(deleted_errors),
        'deleted_dupes':    len(deleted_dupes),
        'deleted_stalled':  0,
        'total_deleted':    total,
        'message':          f'Cleanup complete: {total} transfers removed',
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
