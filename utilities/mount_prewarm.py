"""
Show precache and stream protection for the built-in mount (both cli_mount
features, once Plex-only), from one poller over every media server the
mount serves: Plex, and Jellyfin or Emby (utilities/media_sessions.py).

Stream protection: every 10 s the mount files the media servers report
playing are posted to the engine, which keeps the playback reserve for them
alone while they play. A failed poll is posted as such, and the engine then
treats every read as playback again.

Show precache: every 20 s, sessions streaming from the mount are checked.
Once an episode passes the trigger point (default 70%), the next episode's
first part (default 20%, at most 256 MB) is fetched in the background into
the engine's caches, so it starts instantly. The media server says who is
really watching, so scanners and other readers never trigger it. Each
episode is precached at most once per 6 hours.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional, Tuple

import requests

from utilities import media_sessions as ms
from utilities import mount_now_playing as np
from utilities.paths import safe_realpath

POLL_SECONDS = 20          # prewarm
PROTECT_SECONDS = 10       # playback protection (engine forgets a set after 45 s)
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
        logging.debug(f'[show precache] episodes of {show}: {exc}')
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
            path = safe_realpath(plex_file)
    except OSError:
        pass
    marker = '/__all__/'
    if path.startswith(mount_path.rstrip('/') + '/') or marker in path:
        rest = path.split(marker, 1)[1] if marker in path else path[len(mount_path.rstrip('/')) + 1:]
        parts = rest.split('/')
        if len(parts) >= 2:
            return parts[0], parts[-1]
    return None


def name_candidates(plex_file: str) -> list:
    """Names to look a Plex file up by on the mount: its own basename, then
    the release filename a cli_debrid symlink name carries after its last
    "(" - "... - (Release.mkv" (movie template), "... - (Release.mkv).mkv"
    (episode template) or "... (Release.mkv)". From cli_mount's
    mountBasenameCandidates, which only knew the last shape."""
    base = (plex_file or '').replace('\\', '/').rsplit('/', 1)[-1]
    if not base:
        return []
    out = [base]
    i = base.rfind('(')
    if i >= 0:
        inner = base[i + 1:].strip()
        close = inner.rfind(')')
        if close >= 0:
            inner = inner[:close].strip()
        if inner and '.' in inner and inner != base:
            out.append(inner)
    return out


def resolve_mount_file(client, plex_file: str) -> Optional[Tuple[str, str]]:
    """(job id, file) on the mount for a file Plex reports. By path first
    (the symlink's target, or a path inside the mount); when Plex sees the
    file at a path this process can't map (another container path, a
    virtual folder), by name, as cli_mount did."""
    target = mount_target(plex_file, client.mount_path)
    if target:
        job = client._job_by_folder(target[0])
        if job:
            return job['id'], target[1]
    found = client.find_file(name_candidates(plex_file))
    if found:
        return found['job_id'], found['file']
    return None


def check_once(client, get_setting: Callable, now: Optional[float] = None) -> list:
    """One poll. Returns what was prewarmed: [(job id, file, bytes)]."""
    now = now or time.time()
    cfg = _settings(get_setting)
    if not cfg['enabled'] or cfg['percent'] <= 0 or cfg['max_mb'] <= 0:
        return []
    streams = client.get_streams()
    if not streams:
        return []
    lookup = ms.lookup(get_setting)
    for k in [k for k, t in _done.items() if now - t > REMEMBER_SECONDS]:
        _done.pop(k, None)
    out = []
    for s in ms.sessions(get_setting):
        if s.get('type') != 'episode' or not s.get('duration'):
            continue
        if 100 * int(s.get('viewOffset') or 0) / int(s['duration']) < cfg['trigger']:
            continue
        if not any(np.match_stream(f, streams, client.mount_path) for f in np._session_files(s, lookup)):
            continue  # not playing from the mount
        nxt = ms.next_episode(get_setting, s)
        if not nxt or str(nxt.get('ratingKey')) in _done:
            continue
        # Jellyfin/Emby episodes come with their files; Plex's are looked up.
        item = nxt if str(nxt.get('ratingKey')).startswith(ms.JF) else (lookup(str(nxt['ratingKey'])) or nxt)
        for media in item.get('Media') or []:
            part = (media.get('Part') or [{}])[0]
            found = resolve_mount_file(client, part.get('file', ''))
            if not found:
                continue
            job_id, file = found
            size = int(part.get('size') or 0)
            n = min(cfg['max_mb'] << 20, size * cfg['percent'] // 100) if size else cfg['max_mb'] << 20
            if client.prewarm(job_id, file, n):
                _done[str(nxt['ratingKey'])] = now
                logging.info(f"[show precache] {nxt.get('grandparentTitle', '')} S{int(nxt['parentIndex']):02d}"
                             f"E{int(nxt['index']):02d}: fetching first {n >> 20} MB of {file}")
                out.append((job_id, file, n))
            break
    return out


_protect_state = {'last': None}  # last posted (ok, files) - logged on change only


def protect_once(client, get_setting: Callable) -> Optional[list]:
    """One playback-protection poll. Returns the protected files posted,
    or None when protection is off / nothing was posted."""
    bm = get_setting('Built-in Mount') or {}
    if not bm.get('plex_playback_protection', True):
        return None
    streams = client.get_streams()
    if not streams:
        files, ok, err = [], True, ''  # nothing open: no need to ask Plex
    else:
        sessions, err = ms.sessions_checked(get_setting)
        ok = not err
        lookup = ms.lookup(get_setting) if ok else None
        files, seen = [], set()
        for s in sessions:
            for f in np._session_files(s, lookup):
                st = np.match_stream(f, streams, client.mount_path)
                if st and (st.get('job_id'), st.get('file')) not in seen:
                    seen.add((st.get('job_id'), st.get('file')))
                    files.append({'job_id': st.get('job_id'), 'file': st.get('file')})
                    break
    if not client.set_protected(files, ok, err):
        return None
    state = (ok, tuple(sorted((f['job_id'], f['file']) for f in files)))
    if state != _protect_state['last']:
        _protect_state['last'] = state
        if not ok:
            logging.warning(f'[stream protection] media server poll failed ({err}) - every mount read counts as playback until it works')
        elif files:
            logging.info(f"[stream protection] reserve kept for {len(files)} playing stream(s): "
                         + ', '.join(f['file'] for f in files))
        else:
            logging.debug('[stream protection] nothing playing from the mount')
    return files


def _loop(get_setting: Callable):
    from usenet.builtin_client import BuiltinClient
    last_prewarm = 0.0
    while True:
        time.sleep(PROTECT_SECONDS)
        try:
            up = get_setting('Usenet Provider') or {}
            if not (up.get('enabled') and (up.get('provider') or '').strip().lower() == 'builtin'):
                continue
            client = BuiltinClient()
            try:
                protect_once(client, get_setting)
            except Exception as exc:
                logging.debug(f'[stream protection] poll failed: {exc}')
            if time.monotonic() - last_prewarm >= POLL_SECONDS:
                last_prewarm = time.monotonic()
                check_once(client, get_setting)
        except Exception as exc:
            logging.debug(f'[show precache] poll failed: {exc}')


def start(get_setting: Callable):
    """Start the poller once per process (cheap when the mount isn't used)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(get_setting,), name='mount-prewarm', daemon=True).start()
