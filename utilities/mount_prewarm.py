"""
Plex next-episode prewarm for the built-in mount (cli_mount's feature).

Every 20 s, Plex sessions streaming from the mount are checked. Once an
episode passes the trigger point (default 70%), the next episode's first
part (default 20%, at most 256 MB) is fetched in the background into the
engine's caches, so it starts instantly. Plex says who is really watching,
so scanners and other readers never trigger it. Each episode is prewarmed
at most once per 6 hours.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional, Tuple

import requests

from utilities import mount_now_playing as np

POLL_SECONDS = 20
REMEMBER_SECONDS = 6 * 3600

_done = {}  # next episode ratingKey -> time prewarmed
_started = False
_lock = threading.Lock()


def _settings(get_setting: Callable) -> dict:
    bm = get_setting('Built-in Mount') or {}

    def num(k, d):
        try:
            return int(bm.get(k, d))
        except (TypeError, ValueError):
            return d
    return {'enabled': bool(bm.get('plex_prewarm', True)), 'trigger': num('prewarm_trigger_percent', 70),
            'percent': num('prewarm_percent', 20), 'max_mb': num('prewarm_max_mb', 256)}


def next_episode(url: str, token: str, s: dict, timeout: float = 5) -> Optional[dict]:
    """The episode after session s in its show (by season, then episode)."""
    show = s.get('grandparentRatingKey')
    if not show or s.get('parentIndex') is None or s.get('index') is None:
        return None
    try:
        r = requests.get(f'{url}/library/metadata/{show}/allLeaves',
                         headers={'X-Plex-Token': token, 'Accept': 'application/json'}, timeout=timeout)
        r.raise_for_status()
        eps = (r.json().get('MediaContainer') or {}).get('Metadata') or []
    except Exception as exc:
        logging.debug(f'[prewarm] episodes of {show}: {exc}')
        return None
    cur = (int(s['parentIndex']), int(s['index']))
    later = [e for e in eps if e.get('parentIndex') is not None and e.get('index') is not None
             and (int(e['parentIndex']), int(e['index'])) > cur and int(e['parentIndex']) > 0]
    return min(later, key=lambda e: (int(e['parentIndex']), int(e['index'])), default=None)


def mount_target(plex_file: str, mount_path: str) -> Optional[Tuple[str, str]]:
    """(job folder, file) of a Plex path served by the mount, else None."""
    if not plex_file:
        return None
    path = plex_file.replace('\\', '/')
    try:
        if os.path.lexists(plex_file):
            path = os.path.realpath(plex_file)
    except OSError:
        pass
    marker = '/__all__/'
    if path.startswith(mount_path.rstrip('/') + '/') or marker in path:
        rest = path.split(marker, 1)[1] if marker in path else path[len(mount_path.rstrip('/')) + 1:]
        parts = rest.split('/')
        if len(parts) >= 2:
            return parts[0], parts[-1]
    return None


def check_once(client, get_setting: Callable, now: Optional[float] = None) -> list:
    """One poll. Returns what was prewarmed: [(folder, file, bytes)]."""
    now = now or time.time()
    cfg = _settings(get_setting)
    if not cfg['enabled'] or cfg['percent'] <= 0 or cfg['max_mb'] <= 0:
        return []
    streams = client.get_streams()
    if not streams:
        return []
    url, token = np.plex_server(get_setting)
    lookup = np.metadata_lookup(url, token)
    for k in [k for k, t in _done.items() if now - t > REMEMBER_SECONDS]:
        _done.pop(k, None)
    out = []
    for s in np.plex_sessions(url, token):
        if s.get('type') != 'episode' or not s.get('duration'):
            continue
        if 100 * int(s.get('viewOffset') or 0) / int(s['duration']) < cfg['trigger']:
            continue
        if not any(np.match_stream(f, streams, client.mount_path) for f in np._session_files(s, lookup)):
            continue  # not playing from the mount
        nxt = next_episode(url, token, s)
        if not nxt or str(nxt.get('ratingKey')) in _done:
            continue
        item = lookup(str(nxt['ratingKey'])) or nxt
        for media in item.get('Media') or []:
            part = (media.get('Part') or [{}])[0]
            target = mount_target(part.get('file', ''), client.mount_path)
            if not target:
                continue
            job = client._job_by_folder(target[0])
            if not job:
                continue
            size = int(part.get('size') or 0)
            n = min(cfg['max_mb'] << 20, size * cfg['percent'] // 100) if size else cfg['max_mb'] << 20
            if client.prewarm(job['id'], target[1], n):
                _done[str(nxt['ratingKey'])] = now
                logging.info(f"[prewarm] {nxt.get('grandparentTitle', '')} S{int(nxt['parentIndex']):02d}"
                             f"E{int(nxt['index']):02d}: fetching first {n >> 20} MB of {target[1]}")
                out.append((target[0], target[1], n))
            break
    return out


def _loop(get_setting: Callable):
    from usenet.builtin_client import BuiltinClient
    while True:
        time.sleep(POLL_SECONDS)
        try:
            up = get_setting('Usenet Provider') or {}
            if up.get('enabled') and (up.get('provider') or '').strip().lower() == 'builtin':
                check_once(BuiltinClient(), get_setting)
        except Exception as exc:
            logging.debug(f'[prewarm] poll failed: {exc}')


def start(get_setting: Callable):
    """Start the poller once per process (cheap when the mount isn't used)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(get_setting,), name='mount-prewarm', daemon=True).start()
