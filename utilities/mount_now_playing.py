"""
"Now playing" for the built-in mount: Plex sessions whose file is served by
the mount, with the Plex user, player and whether it streams from usenet or
debrid. Sessions playing anything else are left out.

A Plex session is matched to an open file on the mount (engine /streams):
  1. the session's file resolves (symlink) to <mount>/.../<folder>/<file>;
  2. else its path ends with <folder>/<file> (Plex mode: Plex sees the
     mount under its own prefix);
  3. else its file name contains the mount file's name (symlink templates
     that keep the original filename, when the symlink isn't visible here).
"""

import logging
import os
import threading
from typing import Callable, List, Optional

import requests
from utilities.paths import safe_realpath


#: Where the mount's Plex URL/token come from, first match wins: the Mount
#: page's own fields, then Plex mode's, then the Symlink Plex settings.
PLEX_SOURCES = (
    ('Built-in Mount', 'plex_url', 'plex_token', 'Mount page'),
    ('Plex', 'url', 'token', 'Settings → Plex'),
    ('File Management', 'plex_url_for_symlink', 'plex_token_for_symlink', 'Settings → Symlink Plex'),
)


def plex_server_source(get_setting: Callable) -> tuple:
    """(url, token, where each came from) of the Plex server the mount asks.
    With Silo as the media server, only the Mount page's own Plex fields count:
    a Plex URL left in Settings from before Silo isn't polled."""
    url = token = url_src = token_src = ''
    sources = PLEX_SOURCES
    try:
        from utilities.silo_functions import is_silo_selected
        from utilities.direct_mount import is_direct_mount_without_plex
        if is_silo_selected(get_setting) or is_direct_mount_without_plex(get_setting):
            sources = PLEX_SOURCES[:1]
    except Exception:
        pass
    for section, url_key, token_key, label in sources:
        if not url and (get_setting(section, url_key, '') or '').strip():
            url, url_src = get_setting(section, url_key, '').strip().rstrip('/'), label
        if not token and (get_setting(section, token_key, '') or '').strip():
            token, token_src = get_setting(section, token_key, '').strip(), label
    return url, token, url_src, token_src


def plex_server(get_setting: Callable) -> tuple:
    url, token, _, _ = plex_server_source(get_setting)
    return url, token


def plex_sessions(url: str, token: str, timeout: float = 5) -> List[dict]:
    return plex_sessions_checked(url, token, timeout)[0]


def plex_sessions_checked(url: str, token: str, timeout: float = 5) -> tuple:
    """(sessions, error): error is '' when Plex answered."""
    if not url or not token:
        return [], 'no Plex URL/token set'
    try:
        r = requests.get(f'{url}/status/sessions', headers={'X-Plex-Token': token, 'Accept': 'application/json'},
                         timeout=timeout)
        r.raise_for_status()
        return (r.json().get('MediaContainer') or {}).get('Metadata') or [], ''
    except Exception as exc:
        logging.debug(f'[now playing] Plex sessions failed: {exc}')
        return [], str(exc) or type(exc).__name__


def _session_files(s: dict, lookup: Optional[Callable] = None) -> List[str]:
    """The file(s) a session may be playing. Some sessions (transcodes)
    omit the path; then the item's own parts are looked up, the session's
    media version first."""
    files = [p['file'] for m in s.get('Media') or [] for p in m.get('Part') or [] if p.get('file')]
    if files or lookup is None or not s.get('ratingKey'):
        return files
    media_ids = {str(m.get('id')) for m in s.get('Media') or [] if m.get('id') is not None}
    item = lookup(str(s['ratingKey'])) or {}
    mine, rest = [], []
    for m in item.get('Media') or []:
        for p in m.get('Part') or []:
            if p.get('file'):
                (mine if str(m.get('id')) in media_ids else rest).append(p['file'])
    return mine + rest


def metadata_lookup(url: str, token: str, timeout: float = 5) -> Callable:
    """ratingKey -> Plex metadata item (memoised for one now-playing call)."""
    cache = {}

    def get(key: str) -> dict:
        if key not in cache:
            try:
                r = requests.get(f'{url}/library/metadata/{key}',
                                 headers={'X-Plex-Token': token, 'Accept': 'application/json'}, timeout=timeout)
                r.raise_for_status()
                cache[key] = ((r.json().get('MediaContainer') or {}).get('Metadata') or [{}])[0]
            except Exception as exc:
                logging.debug(f'[now playing] metadata {key} failed: {exc}')
                cache[key] = {}
        return cache[key]
    return get


def match_stream(plex_file: str, streams: List[dict], mount_path: str) -> Optional[dict]:
    if not plex_file:
        return None
    real = ''
    try:
        if os.path.lexists(plex_file):
            real = safe_realpath(plex_file)
    except OSError:
        pass
    norm = plex_file.replace('\\', '/')
    base = norm.rsplit('/', 1)[-1]
    for st in streams:
        tail = f"{st.get('folder', '')}/{st.get('file', '')}"
        if real and real.startswith(mount_path.rstrip('/') + '/') and real.endswith('/' + tail):
            return st
    for st in streams:
        if norm.endswith('/' + f"{st.get('folder', '')}/{st.get('file', '')}"):
            return st
    for st in streams:
        name = st.get('file') or ''
        stem = name.rsplit('.', 1)[0]
        if stem and len(stem) >= 8 and stem in base:
            return st
    return None


_CODECS = {'avc': 'H.264', 'hevc': 'HEVC', 'av1': 'AV1', 'xvid': 'XviD', 'mpeg': 'MPEG'}
_NETWORKS = {'Amazon': 'AMZN', 'Netflix': 'NF', 'Disney': 'DSNP', 'Apple TV': 'ATVP', 'HBO Max': 'HMAX',
             'Hulu': 'HULU', 'Paramount+': 'PMTP', 'Peacock': 'PCOK'}


def scene_tags(release: str) -> List[str]:
    """Short scene tags of a release name: 2160p, WEB-DL, AMZN, HEVC, DV, Atmos, 7.1, group..."""
    if not release:
        return []
    try:
        from PTT import parse_title
        p = parse_title(release) or {}
    except Exception:
        return []
    tags = [p.get('resolution'), p.get('quality'), _NETWORKS.get(p.get('network') or '', p.get('network'))]
    tags += list(p.get('hdr') or [])
    tags.append(_CODECS.get(p.get('codec') or '', (p.get('codec') or '').upper() or None))
    tags.append(p.get('bit_depth'))
    tags += list(p.get('audio') or [])[:2]
    tags += list(p.get('channels') or [])[:1]
    tags.append(p.get('group'))
    out = []
    for t in tags:
        t = str(t or '').strip()
        if t and t not in out:
            out.append(t)
    return out


def _title(s: dict) -> tuple:
    if s.get('type') == 'episode':
        se = ''
        if s.get('parentIndex') is not None and s.get('index') is not None:
            se = f"S{int(s['parentIndex']):02d}E{int(s['index']):02d}"
        return s.get('grandparentTitle') or s.get('title', ''), ' · '.join(x for x in (se, s.get('title', '')) if x)
    year = f" ({s['year']})" if s.get('year') else ''
    return f"{s.get('title', '')}{year}", ''


# Plex session key -> the mount stream it was last matched to. A file that is
# fully cached is read to the end and closed by Plex while playback goes on
# from Plex's own buffer; the session is still shown from this memory until
# Plex ends it.
_remembered: dict = {}
_remembered_lock = threading.Lock()


def has_remembered() -> bool:
    with _remembered_lock:
        return bool(_remembered)


def _session_key(s: dict) -> str:
    return str(s.get('sessionKey') or (s.get('Session') or {}).get('id') or '')


def now_playing(sessions: List[dict], streams: List[dict], mount_path: str,
                lookup: Optional[Callable] = None) -> List[dict]:
    out = []
    live_keys = set()
    for s in sessions:
        key = _session_key(s)
        if key:
            live_keys.add(key)
        st = next((m for m in (match_stream(f, streams, mount_path) for f in _session_files(s, lookup)) if m), None)
        buffered = False
        with _remembered_lock:
            if st is not None and key:
                _remembered[key] = dict(st)
            elif st is None and key in _remembered:
                st, buffered = _remembered[key], True
        if st is None:
            continue  # not from the mount
        title, subtitle = _title(s)
        dur = int(s.get('duration') or 0)
        off = int(s.get('viewOffset') or 0)
        player = s.get('Player') or {}
        ts = s.get('TranscodeSession') or {}
        out.append({
            'user': (s.get('User') or {}).get('title') or 'Unknown user',
            'player': player.get('title') or player.get('product') or '',
            'state': player.get('state') or '',
            'title': title,
            'subtitle': subtitle,
            'type': s.get('type', ''),
            'progress': round(100 * off / dur, 1) if dur else None,
            'position_ms': off,
            'duration_ms': dur,
            'decision': (ts.get('videoDecision') or 'direct play') if ts else 'direct play',
            'protocol': st.get('protocol') or 'usenet',
            'source': st.get('folder', ''),
            'file': st.get('file', ''),
            'tags': scene_tags(st.get('folder') or st.get('file') or ''),
            'rate_bytes': 0 if buffered else int(st.get('rate_bytes') or 0),
            'bytes_read': int(st.get('bytes_read') or 0),
            'buffered': buffered,
        })
    with _remembered_lock:
        for key in [k for k in _remembered if k not in live_keys]:
            del _remembered[key]
    return out
