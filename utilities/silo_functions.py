"""Silo media server (github.com/Silo-Server/silo-server) for Symlinked/Local mode.

Uses Silo's native admin API with an admin API key (`sa_...`):
- POST /api/v2/scan {path}: scan a new symlink's folder (Silo widens a file path
  to its folder). Silo has no item delete: a removed file leaves the library
  when the nearest folder that still exists is rescanned (a gone path is a 400).
- GET  /api/v2/admin/catalog/items?include_file_paths=true: the library's files,
  for the symlink verification task.
- GET  /api/v2/libraries: library roots, to translate cli_debrid's symlink paths
  into the paths Silo sees when the two run in different containers.
"""
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from utilities.settings import get_setting

_TIMEOUT = 15
_LIB_TTL = 300
_lib_cache: Dict[str, Any] = {'at': 0.0, 'key': None, 'items': None}
_lib_lock = threading.Lock()


def silo_config() -> Tuple[str, str]:
    url = (get_setting('Debug', 'silo_url', '') or '').strip().rstrip('/')
    key = (get_setting('Debug', 'silo_api_key', '') or '').strip()
    return url, key


def is_silo_selected() -> bool:
    """Symlinked/Local mode with Silo picked as the media server."""
    return (get_setting('File Management', 'file_collection_management', '') == 'Symlinked/Local'
            and (get_setting('File Management', 'media_server_type', 'plex') or '').lower() == 'silo')


def is_silo_mode() -> bool:
    """Silo is the media server and has a URL and API key."""
    url, key = silo_config()
    return bool(url and key) and is_silo_selected()


def _headers(key: str) -> dict:
    return {'Authorization': f'Bearer {key}', 'Accept': 'application/json'}


def _request(method: str, path: str, url: str = None, key: str = None, headers_extra: dict = None,
             **kw) -> requests.Response:
    if url is None or key is None:
        url, key = silo_config()
    kw.setdefault('timeout', _TIMEOUT)
    return requests.request(method, f'{url}{path}', headers={**_headers(key), **(headers_extra or {})}, **kw)


def _problem(r: requests.Response) -> str:
    """Short error text from Silo's RFC 9457 problem document."""
    try:
        body = r.json()
        return body.get('detail') or body.get('title') or r.text[:200]
    except Exception:
        return r.text[:200]


def test_connection(url: str = None, key: str = None) -> Tuple[bool, str]:
    """(ok, message). Checks the key is an admin key that can scan."""
    if url is None or key is None:
        url, key = silo_config()
    url = (url or '').strip().rstrip('/')
    if not url or not key:
        return False, 'Silo URL and API key are required'
    try:
        r = _request('GET', '/api/v2/scan/capabilities', url, key)
    except requests.RequestException as e:
        return False, f'Could not reach Silo: {e}'
    if r.status_code in (401, 403):
        return False, f'Silo refused the API key ({r.status_code}): {_problem(r)}. Use an admin API key (Settings → API Keys).'
    if r.status_code == 404:
        return False, 'Silo has no scan API here. Check the URL, and that Silo runs in integrated or api mode.'
    if r.status_code != 200:
        return False, f'Silo returned HTTP {r.status_code}: {_problem(r)}'
    state = (r.json() or {}).get('state', '')
    if state and state != 'available':
        return False, f'Silo scanning is {state}'
    return True, 'Connected to Silo'


# ---------------------------------------------------------------------------
# Libraries and path translation
# ---------------------------------------------------------------------------

def get_libraries(force: bool = False, url: str = None, key: str = None) -> List[dict]:
    if url is None or key is None:
        url, key = silo_config()
    with _lib_lock:
        fresh = time.time() - _lib_cache['at'] < _LIB_TTL and _lib_cache['key'] == (url, key)
        if fresh and not force and _lib_cache['items'] is not None:
            return _lib_cache['items']
    r = _request('GET', '/api/v2/libraries', url, key)
    r.raise_for_status()
    items = [lib for lib in (r.json() or {}).get('items') or [] if lib.get('enabled', True)]
    with _lib_lock:
        _lib_cache.update(at=time.time(), key=(url, key), items=items)
    return items


def _norm(path: str) -> str:
    p = (path or '').replace('\\', '/')
    while '//' in p:
        p = p.replace('//', '/')
    return p.rstrip('/') or '/'


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip('/') + '/')


def to_silo_path(local_path: str, libraries: Optional[List[dict]] = None) -> Optional[str]:
    """The path Silo knows `local_path` by, or None when no library holds it.

    Same path when Silo sees the folders where cli_debrid does. Otherwise the
    part below the symlink folder is moved onto a library root: a root named
    like the first folder under it (/mnt/symlinked/Movies/X → /media/Movies/X),
    or a root named like the symlink folder itself (/mnt/symlinked/... →
    /data/symlinked/...)."""
    if libraries is None:
        libraries = get_libraries()
    p = _norm(local_path)
    roots = [_norm(r) for lib in libraries for r in lib.get('paths') or []]
    for root in roots:
        if _under(p, root):
            return p
    base = _norm(get_setting('File Management', 'symlinked_files_path', '') or '')
    if base == '/' or not _under(p, base):
        return None
    rel = p[len(base):].lstrip('/')
    first, _, rest = rel.partition('/')
    for root in roots:
        name = root.rsplit('/', 1)[-1]
        if first and name == first:
            return f'{root}/{rest}' if rest else root
    for root in roots:
        if root.rsplit('/', 1)[-1] == base.rsplit('/', 1)[-1]:
            return f'{root}/{rel}' if rel else root
    return None


def from_silo_path(silo_path: str, libraries: List[dict], symlink_base: str = None) -> str:
    """The path cli_debrid knows a Silo file path by: the reverse of
    to_silo_path. Unchanged when it exists here as is (same paths)."""
    p = _norm(silo_path)
    if os.path.lexists(p):
        return p
    if symlink_base is None:
        symlink_base = get_setting('File Management', 'symlinked_files_path', '') or ''
    base = _norm(symlink_base)
    if base == '/':
        return p
    candidates = []
    for lib in libraries:
        for r in lib.get('paths') or []:
            root = _norm(r)
            if not _under(p, root):
                continue
            rest = p[len(root):].lstrip('/')
            name = root.rsplit('/', 1)[-1]
            candidates.append(f'{base}/{name}/{rest}' if rest else f'{base}/{name}')
            if name == base.rsplit('/', 1)[-1]:
                candidates.append(f'{base}/{rest}' if rest else base)
    for c in candidates:
        if os.path.lexists(c):
            return c
    return candidates[0] if candidates else p


def _owning_library(silo_path: str, libraries: List[dict]) -> Tuple[Optional[dict], str]:
    """(library, root) of the deepest library root holding silo_path."""
    best, best_root = None, ''
    for lib in libraries:
        for r in lib.get('paths') or []:
            root = _norm(r)
            if _under(silo_path, root) and len(root) > len(best_root):
                best, best_root = lib, root
    return best, best_root


def _library_ids_for_type(libraries: List[dict], item_type: str) -> List[str]:
    want = 'movies' if item_type == 'movie' else 'series'
    return [str(l['id']) for l in libraries if (l.get('type') or '') in (want, 'mixed')]


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------

def _post_scan(body: dict) -> Tuple[bool, int, str]:
    try:
        r = _request('POST', '/api/v2/scan', json=body)
    except requests.RequestException as e:
        return False, 0, str(e)
    if r.status_code == 202:
        return True, 202, (r.json() or {}).get('mode', '')
    return False, r.status_code, _problem(r)


def scan_path(local_path: str, item_type: str = '') -> bool:
    """Ask Silo to scan `local_path` (a file or folder). When the path can't be
    placed in a Silo library, scans the libraries of the item's type instead.
    Silo coalesces repeat requests for a scope that is already scanning."""
    if not local_path:
        return False
    try:
        libraries = get_libraries()
    except Exception as e:
        logging.error(f'[Silo] Could not list libraries: {e}')
        return False
    silo_path = to_silo_path(local_path, libraries)
    if silo_path:
        lib, root = _owning_library(silo_path, libraries)
        # Silo answers 400 "Path does not exist" for a removed file or folder, so
        # walk up to the nearest folder that still exists; its subtree scan
        # drops what is gone. Never above the library root.
        target = silo_path
        while True:
            ok, status, info = _post_scan({'path': target})
            if ok:
                logging.info(f'[Silo] Scan accepted ({info}) for {target}')
                return True
            if status in (401, 403):
                logging.warning(f'[Silo] Scan of {target} refused (HTTP {status}): {info}')
                return False
            if status == 400 and 'not exist' in info.lower() and root and target != root:
                target = target.rsplit('/', 1)[0] or '/'
                continue
            logging.warning(f'[Silo] Scan of {target} refused (HTTP {status}): {info}')
            break
        if lib:
            ids = [str(lib['id'])]
        else:
            ids = _library_ids_for_type(libraries, item_type or 'episode')
    else:
        logging.warning(f"[Silo] No Silo library holds {local_path}. Check that Silo's library folders "
                        f"cover the symlink folder; scanning the whole library instead.")
        ids = _library_ids_for_type(libraries, item_type or 'episode')
    accepted = False
    for lib_id in ids:
        ok, status, info = _post_scan({'library_id': lib_id})
        if ok:
            accepted = True
            logging.info(f'[Silo] Library {lib_id} scan accepted')
        else:
            logging.warning(f'[Silo] Library {lib_id} scan refused (HTTP {status}): {info}')
    return accepted


def silo_update_item(item: Dict[str, Any]) -> bool:
    """Same contract as emby_update_item: scan the folder of a new or changed file."""
    path = item.get('full_path') or item.get('location_on_disk') or item.get('location')
    if not path:
        logging.error('[Silo] No path to scan')
        return False
    return scan_path(path, item.get('type', ''))


def remove_file_from_silo(item_title: str, item_path: str, episode_title: str = None) -> bool:
    """Same contract as remove_file_from_emby. Silo has no item delete: once the
    file is gone, a scan of the nearest folder that still exists drops it.
    Silo keeps everything when a library root is empty (it treats that as an
    offline mount), so the last item of a library stays until another lands."""
    if not item_path:
        return False
    label = f'{item_title} - {episode_title}' if episode_title else item_title
    logging.info(f'[Silo] Rescanning {item_path} so Silo drops {label}')
    if os.path.lexists(item_path):
        logging.info(f'[Silo] {item_path} still exists; the rescan will keep it')
    return scan_path(item_path, 'episode' if episode_title else '')


# ---------------------------------------------------------------------------
# Library contents (symlink verification)
# ---------------------------------------------------------------------------

def _epoch(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
    except ValueError:
        return None


def get_collected_from_silo() -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Every movie and episode file in Silo, in the shape of
    get_collected_from_emby (title, year, imdb_id, tmdb_id, type, location,
    addedAt, genres, release_date, and for episodes the season/episode and
    the show's ids). `location` is the path cli_debrid sees, mapped back
    from Silo's path. One entry per file. None on failure."""
    url, key = silo_config()
    if not url or not key:
        return None
    raw: List[dict] = []
    series: Dict[str, dict] = {}
    cursor = None
    pages = 0
    try:
        while True:
            params = {'limit': 200, 'include_file_paths': 'true'}
            if cursor:
                params['cursor'] = cursor
            r = _request('GET', '/api/v2/admin/catalog/items', url, key, params=params, timeout=60)
            if r.status_code != 200:
                logging.error(f'[Silo] Catalog listing failed (HTTP {r.status_code}): {_problem(r)}')
                return None
            body = r.json() or {}
            for it in body.get('items') or []:
                if it.get('type') == 'series':
                    series[it.get('content_id')] = it
                elif it.get('type') in ('movie', 'episode'):
                    raw.append(it)
            page = body.get('page') or {}
            cursor = page.get('next_cursor')
            pages += 1
            if not page.get('has_more') or not cursor:
                break
        libraries = get_libraries(url=url, key=key)
    except Exception as e:
        logging.error(f'[Silo] Catalog listing error: {e}')
        return None
    base = get_setting('File Management', 'symlinked_files_path', '') or ''
    movies: List[dict] = []
    episodes: List[dict] = []
    for it in raw:
        for v in it.get('versions') or []:
            if not v.get('file_path'):
                continue
            common = {'addedAt': _epoch(it.get('added_at')), 'guid': it.get('content_id'),
                      'ratingKey': it.get('content_id'), 'type': it.get('type'),
                      'location': from_silo_path(v['file_path'], libraries, base)}
            if it.get('type') == 'movie':
                movies.append({**common, 'title': it.get('title'), 'year': it.get('year'),
                               'imdb_id': it.get('imdb_id'), 'tmdb_id': it.get('tmdb_id'),
                               'genres': it.get('genres') or [], 'release_date': it.get('release_date')})
            else:
                show = series.get(it.get('series_id')) or {}
                episodes.append({**common, 'title': show.get('title') or it.get('series_title'),
                                 'episode_title': it.get('title'),
                                 'season_number': it.get('season_number'), 'episode_number': it.get('episode_number'),
                                 'year': show.get('year'), 'show_year': show.get('year'),
                                 'release_date': it.get('air_date'),
                                 'imdb_id': show.get('imdb_id'), 'tmdb_id': show.get('tmdb_id'),
                                 'episode_imdb_id': it.get('imdb_id') or show.get('imdb_id'),
                                 'episode_tmdb_id': it.get('tmdb_id') or show.get('tmdb_id'),
                                 'genres': show.get('genres') or []})
    logging.info(f'[Silo] Catalog: {len(movies)} movie files, {len(episodes)} episode files ({pages} pages)')
    return {'movies': movies, 'episodes': episodes}


# ---------------------------------------------------------------------------
# Playback sessions (stream protection, show precache, Now playing)
# ---------------------------------------------------------------------------

def admin_sessions(url: str, key: str, timeout: float = 5) -> List[dict]:
    """Every active playback session (GET /api/v2/admin/sessions). Raises on
    a failed request so the caller can report the server as unreachable."""
    out, cursor = [], None
    for _ in range(20):
        params = {'limit': 200}
        if cursor:
            params['cursor'] = cursor
        r = _request('GET', '/api/v2/admin/sessions', url, key, params=params, timeout=timeout)
        r.raise_for_status()
        body = r.json() or {}
        out += body.get('items') or []
        cursor = (body.get('page') or {}).get('next_cursor')
        if not (body.get('page') or {}).get('has_more') or not cursor:
            break
    return out


def catalog_items(url: str, key: str, content_ids: List[str], timeout: float = 5) -> List[dict]:
    """Catalog entries (with file paths) of these content ids; ids that are
    not in the catalog are left out."""
    ids = [str(c) for c in content_ids if c]
    if not ids:
        return []
    params = [('content_ids', c) for c in ids[:200]] + [('include_file_paths', 'true'), ('limit', '200')]
    r = _request('GET', '/api/v2/admin/catalog/items', url, key, params=params, timeout=timeout)
    r.raise_for_status()
    return (r.json() or {}).get('items') or []


def next_episode_ids(content_id: str) -> List[str]:
    """Candidate ids of the episode after `content_id`, for Silo's episode ids
    `episode-<provider>-<series>-<season>-<episode>`: the next episode of the
    season, then the first of the next season. [] for any other shape."""
    parts = str(content_id or '').split('-')
    if len(parts) < 5 or parts[0] != 'episode':
        return []
    try:
        season, episode = int(parts[-2]), int(parts[-1])
    except ValueError:
        return []
    stem = '-'.join(parts[:-2])
    return [f'{stem}-{season}-{episode + 1}', f'{stem}-{season + 1}-1']


# ---------------------------------------------------------------------------
# Item details (subtitle tracks)
# ---------------------------------------------------------------------------

def primary_profile_id(url: str, key: str, timeout: float = 15) -> Optional[str]:
    """An admin account's primary profile without a PIN: catalog item details
    need a profile, and an admin key may act as its own account's profile."""
    r = _request('GET', '/api/v2/admin/users', url, key, timeout=timeout)
    r.raise_for_status()
    for user in (r.json() or {}).get('items') or []:
        if user.get('role') != 'admin' or user.get('enabled') is False:
            continue
        pr = _request('GET', f"/api/v2/admin/users/{user['id']}/profiles", url, key, timeout=timeout)
        if pr.status_code != 200:
            continue
        profiles = (pr.json() or {}).get('items') or []
        for p in sorted(profiles, key=lambda p: not p.get('is_primary')):
            if not (p.get('pin_protected') or p.get('has_pin')):
                return str(p['id'])
    return None


def catalog_detail(url: str, key: str, content_id: str, profile_id: str, timeout: float = 30) -> Optional[dict]:
    """GET /api/v2/catalog/items/{id} as `profile_id`; None when it's not there."""
    from urllib.parse import quote
    r = _request('GET', f'/api/v2/catalog/items/{quote(content_id, safe="")}', url, key,
                 timeout=timeout, headers_extra={'X-Profile-Id': profile_id})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json() or {}
