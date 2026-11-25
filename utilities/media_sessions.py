"""
Playback sessions from every media server the mount serves: Plex,
Jellyfin or Emby (same API), and Silo. Stream protection, show precache and Now
playing all read sessions through here.

Jellyfin/Emby sessions are turned into the shape of a Plex /status/sessions
entry (type, title, grandparentTitle, parentIndex, index, viewOffset and
duration in ms, Media[].Part[].file, User, Player), so the code that matches
sessions to mount files works the same for every server. Their keys carry a
"jf:" prefix so they never collide with Plex ratingKeys. Silo sessions get
the same treatment with a "silo:" prefix, and Silo's file paths are mapped
back to the paths cli_debrid sees.
"""

import logging
from typing import Callable, List, Optional, Tuple

import requests

from utilities import mount_now_playing as np
from utilities.jellyfin_auth import jellyfin_auth_headers

JF = 'jf:'
SILO = 'silo:'

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


def silo_server(get_setting: Callable) -> tuple:
    """(url, key) of Silo when it is the media server (either mode), else ('', '')."""
    from utilities.silo_functions import is_silo_selected
    if not is_silo_selected(get_setting):
        return '', ''
    url = (get_setting('Debug', 'silo_url', '') or '').strip().rstrip('/')
    key = (get_setting('Debug', 'silo_api_key', '') or '').strip()
    return (url, key) if url and key else ('', '')


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


# ---- Silo ------------------------------------------------------------------

def _silo_item(item: dict, libraries: list, symlink_base: str, playing_file_id=None) -> dict:
    """A Silo catalog entry in Plex metadata shape, its files mapped to local
    paths (the one playing first)."""
    from utilities.silo_functions import from_silo_path
    versions = sorted(item.get('versions') or [], key=lambda v: str(v.get('file_id')) != str(playing_file_id))
    media = [{'Part': [{'file': from_silo_path(v['file_path'], libraries, symlink_base),
                        'size': int(v.get('file_size') or 0)}]}
             for v in versions if v.get('file_path')]
    return {
        'type': item.get('type') or '',
        'ratingKey': SILO + str(item.get('content_id') or ''),
        'title': item.get('title') or '',
        'year': item.get('year'),
        'grandparentTitle': item.get('series_title') or '',
        'grandparentRatingKey': SILO + str(item['series_id']) if item.get('series_id') else None,
        'parentIndex': item.get('season_number'),
        'index': item.get('episode_number'),
        'Media': media,
    }


def silo_sessions_checked(get_setting: Callable, timeout: float = 5) -> tuple:
    """(sessions in Plex shape, error): error is '' when Silo answered."""
    from utilities import silo_functions as silo
    url, key = silo_server(get_setting)
    if not url:
        return [], 'Silo is not the media server, or has no URL/API key'
    try:
        raw = silo.admin_sessions(url, key, timeout)
        libraries = silo.get_libraries(url=url, key=key) if raw else []
        items = {i.get('content_id'): i for i in silo.catalog_items(
            url, key, list({str(s.get('media_item_id')) for s in raw if s.get('media_item_id')}), timeout)}
    except Exception as exc:
        logging.debug(f'[now playing] Silo sessions failed: {exc}')
        return [], str(exc) or type(exc).__name__
    from utilities.silo_functions import local_base
    base = local_base(get_setting)
    out = []
    for s in raw:
        item = items.get(str(s.get('media_item_id')))
        if not item:
            continue
        d = _silo_item(item, libraries, base, s.get('media_file_id'))
        decision = (s.get('video_decision') or s.get('play_method') or '').lower()
        d.update({
            'duration': int(float(s.get('file_duration') or 0) * 1000),
            'viewOffset': int(float(s.get('position_seconds') or 0) * 1000),
            'sessionKey': SILO + str(s.get('session_id') or ''),
            'User': {'title': s.get('username') or s.get('profile_name') or ''},
            'Player': {'title': s.get('client_label') or s.get('device_platform') or '',
                       'product': s.get('client_name') or '',
                       'state': 'paused' if s.get('is_paused') else 'playing'},
            'TranscodeSession': ({'videoDecision': 'transcode'} if 'transcode' in decision
                                 else {'videoDecision': 'copy'} if decision in ('copy', 'remux', 'direct_stream', 'directstream')
                                 else None),
            '_server': 'silo',
        })
        out.append(d)
    return out, ''


def silo_next_episode(get_setting: Callable, s: dict, timeout: float = 5) -> Optional[dict]:
    """The episode after session s (Plex metadata shape, with its files), or None."""
    from utilities import silo_functions as silo
    url, key = silo_server(get_setting)
    cur = str(s.get('ratingKey') or '')
    if not url or not cur.startswith(SILO):
        return None
    candidates = silo.next_episode_ids(cur[len(SILO):])
    try:
        found = {i.get('content_id'): i for i in silo.catalog_items(url, key, candidates, timeout)}
        libraries = silo.get_libraries(url=url, key=key) if found else []
    except Exception as exc:
        logging.debug(f'[show precache] Silo next episode of {cur}: {exc}')
        return None
    from utilities.silo_functions import local_base
    base = local_base(get_setting)
    for c in candidates:
        if c in found:
            return _silo_item(found[c], libraries, base)
    return None


def silo_lookup(get_setting: Callable, timeout: float = 5) -> Callable:
    from utilities import silo_functions as silo
    url, key = silo_server(get_setting)
    from utilities.silo_functions import local_base
    base = local_base(get_setting)
    cache = {}

    def get(k: str) -> dict:
        if k not in cache:
            try:
                items = silo.catalog_items(url, key, [k[len(SILO):]], timeout)
                cache[k] = _silo_item(items[0], silo.get_libraries(url=url, key=key), base) if items else {}
            except Exception as exc:
                logging.debug(f'[now playing] Silo item {k}: {exc}')
                cache[k] = {}
        return cache[k]
    return get


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
    if all(silo_server(get_setting)):
        asked = True
        got, err = silo_sessions_checked(get_setting, timeout)
        sessions += got
        if err:
            errors.append(f'Silo: {err}')
    if not asked:
        return [], 'no media server set (Plex, Jellyfin/Emby or Silo)'
    return sessions, '; '.join(errors)


def sessions(get_setting: Callable) -> List[dict]:
    return sessions_checked(get_setting)[0]


def lookup(get_setting: Callable) -> Callable:
    """Session/item key -> metadata (files) on the server it came from."""
    p_url, p_token = np.plex_server(get_setting)
    j_url, j_token = jellyfin_server(get_setting)
    plex = np.metadata_lookup(p_url, p_token) if p_url and p_token else (lambda k: {})
    jf = jellyfin_lookup(j_url, j_token) if j_url and j_token else (lambda k: {})
    sl = silo_lookup(get_setting) if all(silo_server(get_setting)) else (lambda k: {})
    return lambda key: (jf(key) if str(key).startswith(JF) else sl(key) if str(key).startswith(SILO)
                        else plex(key))


def next_episode(get_setting: Callable, s: dict) -> Optional[dict]:
    """The next episode after session s, with its files (Media/Part)."""
    if s.get('_server') == 'silo':
        return silo_next_episode(get_setting, s)
    if s.get('_server') == 'jellyfin':
        url, token = jellyfin_server(get_setting)
        return jellyfin_next_episode(url, token, s)
    from utilities.mount_prewarm import next_episode as plex_next
    url, token = np.plex_server(get_setting)
    return plex_next(url, token, s)
