"""
Playback sessions from every media server the mount serves: Plex, and
Jellyfin or Emby (same API). Stream protection, show precache and Now
playing all read sessions through here.

Jellyfin/Emby sessions are turned into the shape of a Plex /status/sessions
entry (type, title, grandparentTitle, parentIndex, index, viewOffset and
duration in ms, Media[].Part[].file, User, Player), so the code that matches
sessions to mount files works the same for every server. Their keys carry a
"jf:" prefix so they never collide with Plex ratingKeys.
"""

import logging
from typing import Callable, List, Optional, Tuple

import requests

from utilities import mount_now_playing as np
from utilities.jellyfin_auth import jellyfin_auth_headers

JF = 'jf:'

#: Where the mount's Jellyfin/Emby URL/key come from, first match wins: the
#: Mount page's own fields, then the Jellyfin/Emby connection in Settings.
JELLYFIN_SOURCES = (
    ('Built-in Mount', 'jellyfin_url', 'jellyfin_token', 'Mount page'),
    ('Debug', 'emby_jellyfin_url', 'emby_jellyfin_token', 'Settings → Jellyfin/Emby'),
)


def jellyfin_server_source(get_setting: Callable) -> tuple:
    """(url, key, where each came from) of the Jellyfin/Emby server."""
    url = token = url_src = token_src = ''
    for section, url_key, token_key, label in JELLYFIN_SOURCES:
        u = (get_setting(section, url_key, '') or '')
        t = (get_setting(section, token_key, '') or '')
        if not url and isinstance(u, str) and u.strip():
            url, url_src = u.strip().rstrip('/'), label
        if not token and isinstance(t, str) and t.strip():
            token, token_src = t.strip(), label
    return url, token, url_src, token_src


def jellyfin_server(get_setting: Callable) -> tuple:
    url, token, _, _ = jellyfin_server_source(get_setting)
    return url, token


def any_server(get_setting: Callable) -> bool:
    return all(np.plex_server(get_setting)) or all(jellyfin_server(get_setting))


# ---- Jellyfin / Emby -------------------------------------------------------

def _jf_get(url: str, token: str, path: str, timeout: float = 5):
    r = requests.get(f'{url}{path}', headers={**jellyfin_auth_headers(token), 'Accept': 'application/json'},
                     timeout=timeout)
    r.raise_for_status()
    return r.json()


def _ticks_ms(v) -> int:
    try:
        return int(v or 0) // 10_000
    except (TypeError, ValueError):
        return 0


def _jf_item_files(item: dict) -> List[dict]:
    """Media list (Plex shape) of a Jellyfin item: its versions' paths."""
    parts, seen = [], set()
    for src in item.get('MediaSources') or []:
        p = src.get('Path')
        if p and p not in seen:
            seen.add(p)
            parts.append({'file': p, 'size': int(src.get('Size') or 0)})
    if item.get('Path') and item['Path'] not in seen:
        parts.insert(0, {'file': item['Path'], 'size': 0})
    return [{'Part': [p]} for p in parts]


def _jf_item(item: dict) -> dict:
    """A Jellyfin item in Plex metadata shape."""
    kind = (item.get('Type') or '').lower()
    return {
        'type': kind,
        'ratingKey': JF + str(item.get('Id') or ''),
        'title': item.get('Name') or '',
        'year': item.get('ProductionYear'),
        'grandparentTitle': item.get('SeriesName') or '',
        'grandparentRatingKey': JF + str(item['SeriesId']) if item.get('SeriesId') else None,
        'parentIndex': item.get('ParentIndexNumber'),
        'index': item.get('IndexNumber'),
        'duration': _ticks_ms(item.get('RunTimeTicks')),
        'Media': _jf_item_files(item),
    }


def jellyfin_sessions_checked(url: str, token: str, timeout: float = 5) -> tuple:
    """(sessions in Plex shape, error): error is '' when the server answered."""
    if not url or not token:
        return [], 'no Jellyfin/Emby URL/API key set'
    try:
        raw = _jf_get(url, token, '/Sessions', timeout) or []
    except Exception as exc:
        logging.debug(f'[now playing] Jellyfin/Emby sessions failed: {exc}')
        return [], str(exc) or type(exc).__name__
    out = []
    for s in raw:
        item = s.get('NowPlayingItem')
        if not item:
            continue
        ps = s.get('PlayState') or {}
        d = _jf_item(item)
        # The version being played: with several versions merged into one
        # item, MediaSourceId is the one playing (its own item id).
        msid = ps.get('MediaSourceId')
        if msid and msid != item.get('Id'):
            try:
                found = (_jf_get(url, token, f'/Items?Ids={msid}&Fields=Path,MediaSources', timeout) or {}).get('Items') or []
                if found:
                    d['Media'] = _jf_item_files(found[0]) + d['Media']
            except Exception as exc:
                logging.debug(f'[now playing] Jellyfin/Emby version {msid}: {exc}')
        method = (ps.get('PlayMethod') or '').lower()
        d.update({
            'viewOffset': _ticks_ms(ps.get('PositionTicks')),
            'sessionKey': JF + str(s.get('Id') or ''),
            'User': {'title': s.get('UserName') or ''},
            'Player': {'title': s.get('DeviceName') or '', 'product': s.get('Client') or '',
                       'state': 'paused' if ps.get('IsPaused') else 'playing'},
            'TranscodeSession': ({'videoDecision': 'transcode'} if method == 'transcode'
                                 else {'videoDecision': 'copy'} if method == 'directstream' else None),
            '_server': 'jellyfin',
        })
        out.append(d)
    return out, ''


def jellyfin_next_episode(url: str, token: str, s: dict, timeout: float = 5) -> Optional[dict]:
    """The episode after session s in its series (Plex metadata shape, with
    the files), or None."""
    series = (s.get('grandparentRatingKey') or '')
    if not series.startswith(JF) or s.get('parentIndex') is None or s.get('index') is None:
        return None
    try:
        eps = (_jf_get(url, token, f'/Shows/{series[len(JF):]}/Episodes?Fields=Path,MediaSources', timeout)
               or {}).get('Items') or []
    except Exception as exc:
        logging.debug(f'[show precache] episodes of {series}: {exc}')
        return None
    cur = (int(s['parentIndex']), int(s['index']))
    later = [e for e in eps if e.get('ParentIndexNumber') is not None and e.get('IndexNumber') is not None
             and (int(e['ParentIndexNumber']), int(e['IndexNumber'])) > cur and int(e['ParentIndexNumber']) > 0]
    if not later:
        return None
    return _jf_item(min(later, key=lambda e: (int(e['ParentIndexNumber']), int(e['IndexNumber']))))


def jellyfin_lookup(url: str, token: str, timeout: float = 5) -> Callable:
    cache = {}

    def get(key: str) -> dict:
        if key not in cache:
            try:
                items = (_jf_get(url, token, f'/Items?Ids={key[len(JF):]}&Fields=Path,MediaSources', timeout)
                         or {}).get('Items') or []
                cache[key] = _jf_item(items[0]) if items else {}
            except Exception as exc:
                logging.debug(f'[now playing] Jellyfin/Emby item {key}: {exc}')
                cache[key] = {}
        return cache[key]
    return get


def jellyfin_test(url: str, token: str) -> dict:
    """For the Mount page's Test button."""
    if not url or not token:
        return {'ok': False, 'error': 'No Jellyfin/Emby URL/API key: enter them here, or set them in Settings.'}
    try:
        r = requests.get(f'{url}/System/Info', headers={**jellyfin_auth_headers(token), 'Accept': 'application/json'},
                         timeout=8)
        if r.status_code in (401, 403):
            return {'ok': False, 'error': f'Jellyfin/Emby refused the API key ({r.status_code}).', 'url': url}
        r.raise_for_status()
        info = r.json() or {}
    except Exception as exc:
        return {'ok': False, 'error': f'Jellyfin/Emby not reachable at {url}: {exc}', 'url': url}
    sessions, err = jellyfin_sessions_checked(url, token)
    return {'ok': not err, 'error': err, 'url': url, 'sessions': len(sessions),
            'server': ' '.join(x for x in (info.get('ServerName'), info.get('ProductName') or '',
                                           info.get('Version')) if x)}


# ---- every server ----------------------------------------------------------

def sessions_checked(get_setting: Callable, timeout: float = 5) -> Tuple[List[dict], str]:
    """Sessions of every configured server, and the errors of the ones that
    didn't answer ('' = all answered). No server configured is an error."""
    sessions, errors, asked = [], [], False
    url, token = np.plex_server(get_setting)
    if url and token:
        asked = True
        got, err = np.plex_sessions_checked(url, token, timeout)
        sessions += got
        if err:
            errors.append(f'Plex: {err}')
    url, token = jellyfin_server(get_setting)
    if url and token:
        asked = True
        got, err = jellyfin_sessions_checked(url, token, timeout)
        sessions += got
        if err:
            errors.append(f'Jellyfin/Emby: {err}')
    if not asked:
        return [], 'no media server set (Plex, or Jellyfin/Emby)'
    return sessions, '; '.join(errors)


def sessions(get_setting: Callable) -> List[dict]:
    return sessions_checked(get_setting)[0]


def lookup(get_setting: Callable) -> Callable:
    """Session/item key -> metadata (files) on the server it came from."""
    p_url, p_token = np.plex_server(get_setting)
    j_url, j_token = jellyfin_server(get_setting)
    plex = np.metadata_lookup(p_url, p_token) if p_url and p_token else (lambda k: {})
    jf = jellyfin_lookup(j_url, j_token) if j_url and j_token else (lambda k: {})
    return lambda key: jf(key) if str(key).startswith(JF) else plex(key)


def next_episode(get_setting: Callable, s: dict) -> Optional[dict]:
    """The next episode after session s, with its files (Media/Part)."""
    if s.get('_server') == 'jellyfin':
        url, token = jellyfin_server(get_setting)
        return jellyfin_next_episode(url, token, s)
    from utilities.mount_prewarm import next_episode as plex_next
    url, token = np.plex_server(get_setting)
    return plex_next(url, token, s)
