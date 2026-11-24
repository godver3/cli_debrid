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


def silo_kind(get=None) -> Optional[str]:
    """Which way Silo is used, or None when it isn't the media server:
    'symlink': Symlinked/Local mode with Media Server Type = Silo; Silo reads
               the symlink folder.
    'library': Plex mode with its media server set to Silo
               (File Management.plex_mode_media_server); Silo reads the mount
               and cli_debrid tracks the library through Silo, as it does
               through Plex. A separate setting from media_server_type, which
               Plex mode never shows, so an old value there can't switch it.
    `get` is a get_setting-like (section, key, default) callable."""
    get = get or get_setting
    mode = get('File Management', 'file_collection_management', 'Plex')
    if mode == 'Symlinked/Local' and (get('File Management', 'media_server_type', 'plex') or '').lower() == 'silo':
        return 'symlink'
    if mode == 'Plex' and (get('File Management', 'plex_mode_media_server', 'plex') or '').lower() == 'silo':
        return 'library'
    return None


def is_silo_selected(get=None) -> bool:
    """Silo is the media server (either mode)."""
    return silo_kind(get) is not None


def is_silo_library_mode(get=None) -> bool:
    """Plex mode run with Silo instead of Plex."""
    return silo_kind(get) == 'library'


def is_silo_mode(get=None) -> bool:
    """Silo is the media server and has a URL and API key."""
    get = get or get_setting
    url = (get('Debug', 'silo_url', '') or '').strip()
    key = (get('Debug', 'silo_api_key', '') or '').strip()
    return bool(url and key) and is_silo_selected(get)


def local_base(get=None) -> str:
    """The folder cli_debrid's paths for Silo are relative to: the symlink
    folder in Symlinked/Local mode, the mount's top folder in Plex mode."""
    get = get or get_setting
    if silo_kind(get) == 'library':
        mount = (get('Plex', 'mounted_file_location', '') or '').rstrip('/')
        return mount[:-len('/__all__')] if mount.endswith('/__all__') else mount
    return get('File Management', 'symlinked_files_path', '') or ''


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


# Path translation is shared with Jellyfin/Emby (utilities/media_server_paths.py);
# these wrappers keep Silo's names and use Silo's base folder.
from utilities import media_server_paths as _paths

_norm = _paths.norm
_under = _paths.under


def to_silo_path(local_path: str, libraries: Optional[List[dict]] = None) -> Optional[str]:
    """The path Silo knows `local_path` by, or None when no library holds it
    (see media_server_paths.to_server_path)."""
    if libraries is None:
        libraries = get_libraries()
    return _paths.to_server_path(local_path, libraries, local_base())


def from_silo_path(silo_path: str, libraries: List[dict], symlink_base: str = None) -> str:
    """The path cli_debrid knows a Silo file path by: the reverse of
    to_silo_path. Unchanged when it exists here as is (same paths)."""
    return _paths.from_server_path(silo_path, libraries, local_base() if symlink_base is None else symlink_base)


def _owning_library(silo_path: str, libraries: List[dict]) -> Tuple[Optional[dict], str]:
    """(library, root) of the deepest library root holding silo_path."""
    return _paths.owning_library(silo_path, libraries)


def _mirrored_silo_path(local_path: str, libraries: List[dict]) -> Optional[str]:
    """Silo's path for a file or folder in the mount's all-folder, through the
    library folder that mirrors it (as Plex mode's own scan does)."""
    return _paths.mirrored_server_path(local_path, libraries, local_base())


def _library_ids_for_type(libraries: List[dict], item_type: str) -> List[str]:
    return _paths.library_ids_for_type(libraries, item_type, movie_types=('movies',), show_types=('series',))


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
    if not silo_path:
        # Plex mode: the release is in the mount's all-folder (<mount>/__all__/<release>)
        # while Silo's library reads a sibling folder that mirrors it (<mount>/shows/<release>).
        silo_path = _mirrored_silo_path(local_path, libraries)
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
    if not os.path.isabs(item_path):
        # A bare file name (Plex removal matches by name) has no folder to rescan
        logging.info(f'[Silo] No folder to rescan for {label} ({item_path}); Silo drops it on its next scan')
        return False
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


def get_collected_from_silo(added_after: Optional[str] = None) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Every movie and episode file in Silo, in the shape of
    get_collected_from_emby (title, year, imdb_id, tmdb_id, type, location,
    addedAt, genres, release_date, and for episodes the season/episode and
    the show's ids). `location` is the path cli_debrid sees, mapped back
    from Silo's path. One entry per file. None on failure.
    added_after (ISO time): only items Silo first saw since then (Plex mode's
    recent scan). Their shows are read too, for the episodes' ids."""
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
            if added_after:
                params['added_after'] = added_after
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
        missing = sorted({it.get('series_id') for it in raw if it.get('type') == 'episode'
                          and it.get('series_id') and it.get('series_id') not in series})
        for start in range(0, len(missing), 200):
            for show in catalog_items(url, key, missing[start:start + 200], timeout=60):
                series[show.get('content_id')] = show
        libraries = get_libraries(url=url, key=key)
    except Exception as e:
        logging.error(f'[Silo] Catalog listing error: {e}')
        return None
    base = local_base()
    movies: List[dict] = []
    episodes: List[dict] = []
    for it in raw:
        for v in it.get('versions') or []:
            if not v.get('file_path'):
                continue
            common = {'addedAt': _epoch(it.get('added_at')), 'guid': it.get('content_id'),
                      'ratingKey': it.get('content_id'), 'type': it.get('type'),
                      'media_file_id': v.get('file_id'),
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


# ---------------------------------------------------------------------------
# Plex mode run with Silo (silo_kind() == 'library')
# ---------------------------------------------------------------------------

RECENT_HOURS = 48


def get_recent_from_silo(hours: int = RECENT_HOURS) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """What Silo added in the last `hours`: Plex mode's recently-added scan."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return get_collected_from_silo(added_after=since)


def collect_from_silo(items: List[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    """collect_from_plex for Silo: collect the Checking items whose file Silo
    already has. Returns (files Silo has, items no longer in Checking), or
    None when Silo didn't answer. Reads what Silo added in the last week."""
    names = {os.path.basename(i.get('filled_by_file') or '').lower() for i in items if i.get('filled_by_file')}
    names.discard('')
    if not names:
        return 0, 0
    library = get_recent_from_silo(hours=7 * 24)
    if library is None:
        return None
    entries = [e for e in library['movies'] + library['episodes']
               if os.path.basename(e.get('location') or '').lower() in names]
    found = len({os.path.basename(e['location']).lower() for e in entries})
    if not entries:
        return found, 0
    from database import add_collected_items
    from database.core import get_db_connection
    add_collected_items(entries, recent=True)
    ids = [i['id'] for i in items if i.get('id') is not None]
    if not ids:
        return found, 0
    conn = get_db_connection()
    try:
        still = conn.execute(
            f"SELECT COUNT(*) FROM media_items WHERE id IN ({','.join('?' * len(ids))}) AND state = 'Checking'",
            ids).fetchone()[0]
    finally:
        conn.close()
    return found, len(ids) - still


# ---------------------------------------------------------------------------
# Subtitles (Direct Mount run with Silo)
# ---------------------------------------------------------------------------

_profile_cache: Dict[str, Any] = {'key': None, 'id': None}


def _profile_id(url: str, key: str) -> Optional[str]:
    """An admin's primary profile, cached: user-level endpoints act as a profile."""
    if _profile_cache['key'] != (url, key):
        _profile_cache.update(key=(url, key), id=primary_profile_id(url, key))
    return _profile_cache['id']


def find_file_id(local_path: str) -> Optional[str]:
    """Silo's media file id for a file (by file name): what Silo added in the
    last week first, then the whole catalog. None when Silo doesn't list it."""
    name = os.path.basename(local_path or '').lower()
    if not name:
        return None
    for read in (lambda: get_recent_from_silo(hours=7 * 24), get_collected_from_silo):
        library = read()
        if library is None:
            return None
        for entry in library['movies'] + library['episodes']:
            if os.path.basename(entry.get('location') or '').lower() == name and entry.get('media_file_id'):
                return str(entry['media_file_id'])
    return None


def upload_subtitle(local_path: str, content: bytes, lang_code: str, fmt: str = 'srt',
                    file_id: Optional[str] = None, release_name: Optional[str] = None) -> bool:
    """Store a subtitle for the file in Silo (POST /api/v2/subtitles/upload),
    for a read-only mount where no sidecar file can be written. Silo keeps the
    subtitle itself. True when Silo took it."""
    data = content if isinstance(content, bytes) else (content or '').encode('utf-8')
    if not data:
        return False
    url, key = silo_config()
    if not url or not key:
        return False
    file_id = file_id or find_file_id(local_path)
    if not file_id:
        logging.warning(f'[Silo subs] {os.path.basename(local_path)} is not in Silo yet; subtitle not uploaded')
        return False
    try:
        profile = _profile_id(url, key)
        stem = os.path.splitext(os.path.basename(local_path))[0]
        form = {'media_file_id': file_id, 'language': lang_code}
        if release_name:
            form['release_name'] = release_name
        r = _request('POST', '/api/v2/subtitles/upload', url, key,
                     headers_extra={'X-Profile-Id': profile} if profile else None,
                     data=form, files={'file': (f'{stem}.{lang_code}.{fmt}', data, 'application/octet-stream')},
                     timeout=60)
    except requests.RequestException as e:
        logging.warning(f'[Silo subs] upload for {os.path.basename(local_path)} failed: {e}')
        return False
    if r.status_code in (200, 201):
        logging.info(f'[Silo subs] uploaded {lang_code} subtitle to file {file_id}')
        return True
    logging.warning(f'[Silo subs] upload for {os.path.basename(local_path)} refused (HTTP {r.status_code}): {_problem(r)}')
    return False


def pick_up_sidecar(local_path: str, item_type: str = '') -> bool:
    """A sidecar was written next to the file: rescan its folder so Silo lists it."""
    return scan_path(local_path, item_type)
