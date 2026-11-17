"""
Debrid streaming through the built-in mount.

cli_debrid owns the debrid accounts; the engine only streams:
  - sync_once registers the account's finished torrents with the engine
    (POST /debrid/jobs) and removes ones gone from the account, so the mount
    mirrors the account like cli_mount/Zurg do: <mount>/__all__/<torrent>/<files>;
  - link() answers the engine's link callback (/mount/internal/link) with a
    fresh download URL for one file.

Providers opt in with supports_mount_links (Real-Debrid and TorBox today).
File refs are "<provider key>:<torrent id>:<file id>".
"""

import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

SYNC_SECONDS = 120
ADD_PER_PASS = 50  # provider API calls per pass stay modest

_KEYS = {'RealDebridProvider': 'rd', 'TorboxProvider': 'tb'}
_started = False
_lock = threading.Lock()


def provider_key(p) -> str:
    name = type(p).__name__
    return _KEYS.get(name) or name.lower().replace('provider', '') or 'debrid'


def providers() -> List:
    """Configured debrid providers that can hand out per-file links."""
    try:
        from debrid import get_debrid_providers
        return [p for p in get_debrid_providers() if p is not None and getattr(p, 'supports_mount_links', False)]
    except Exception as exc:
        logging.debug(f'[mount debrid] no providers: {exc}')
        return []


def _job_id(key: str, torrent_id: str) -> str:
    return f'{key}-{torrent_id}'


def unique_names(files: List[Dict]) -> List[Dict]:
    """Flatten torrent paths to file names unique within the folder: plain
    base names, falling back to the path joined with ' - ' on clashes."""
    base = [os.path.basename((f.get('name') or '').replace('\\', '/')) for f in files]
    out, used = [], set()
    for f, b in zip(files, base):
        name = b
        if not name or base.count(b) > 1 or name in used:
            parts = [x for x in (f.get('name') or '').replace('\\', '/').split('/') if x]
            name = ' - '.join(parts[-3:]) or f"file {f.get('file_id')}"
        while name in used:
            name = f"{name} ({f.get('file_id')})"
        used.add(name)
        out.append({**f, 'name': name})
    return out


def link(ref: str) -> Tuple[str, int]:
    """(url, ttl seconds) for an engine file ref. Raises on failure."""
    try:
        key, torrent_id, file_id = ref.split(':', 2)
    except ValueError:
        raise ValueError(f'bad file reference {ref!r}')
    for p in providers():
        if provider_key(p) == key:
            return p.mount_link(torrent_id, file_id)
    raise ValueError(f'no configured debrid provider for {key!r}')


def _finished(t: dict) -> bool:
    st = str(t.get('status') or '').lower()
    return st in ('downloaded', 'cached', 'completed', 'torrentstatus.cached') or st.endswith('cached')


def sync_once(engine, provs: Optional[List] = None) -> Dict[str, int]:
    """Mirror the debrid accounts into the engine. Returns counts."""
    provs = providers() if provs is None else provs
    out = {'added': 0, 'removed': 0, 'skipped': 0, 'errors': 0}
    if not provs:
        return out
    code, jobs = engine._call('GET', '/jobs', timeout=20)
    if code != 200 or not isinstance(jobs, list):
        return out
    have = {j['id'] for j in jobs if j.get('protocol') == 'debrid'}
    budget = ADD_PER_PASS
    for p in provs:
        key = provider_key(p)
        try:
            torrents = p.list_active_torrents() or []
        except Exception as exc:
            logging.warning(f'[mount debrid] listing {key} failed: {exc}')
            out['errors'] += 1
            continue
        if not torrents:
            continue  # never empty the mount because a listing came back empty
        on_account = {_job_id(key, str(t.get('id'))) for t in torrents if t.get('id')}
        for t in torrents:
            jid = _job_id(key, str(t.get('id')))
            if jid in have or not _finished(t) or budget <= 0:
                continue
            budget -= 1
            try:
                found = p.mount_files(str(t['id']))
            except Exception as exc:
                logging.debug(f'[mount debrid] {key} {t.get("id")}: {exc}')
                out['errors'] += 1
                continue
            if not found:
                out['skipped'] += 1
                continue
            folder, files = found
            files = unique_names([f for f in files if int(f.get('size') or 0) > 0])
            body = {'id': jid, 'name': folder, 'files': [
                {'name': f['name'], 'size': int(f['size']), 'ref': f"{key}:{t['id']}:{f['file_id']}"} for f in files]}
            c, resp = engine._call('POST', '/debrid/jobs', timeout=30, json=body)
            if c == 201:
                out['added'] += 1
            elif c != 409:
                out['errors'] += 1
                logging.warning(f'[mount debrid] engine refused {folder}: {c} {resp}')
        for jid in sorted(h for h in have if h.startswith(key + '-') and h not in on_account):
            if engine._call('DELETE', f'/jobs/{jid}', timeout=20)[0] in (204, 404):
                out['removed'] += 1
    if out['added'] or out['removed']:
        logging.info(f"[mount debrid] synced: {out['added']} added, {out['removed']} removed")
    return out


def _loop(get_setting):
    from usenet.builtin_client import BuiltinClient
    while True:
        time.sleep(SYNC_SECONDS)
        try:
            up = get_setting('Usenet Provider') or {}
            if up.get('enabled') and (up.get('provider') or '').strip().lower() == 'builtin' \
                    and (get_setting('Built-in Mount') or {}).get('debrid_streaming', True):
                sync_once(BuiltinClient())
        except Exception as exc:
            logging.debug(f'[mount debrid] sync failed: {exc}')


def start(get_setting):
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(get_setting,), name='mount-debrid-sync', daemon=True).start()
