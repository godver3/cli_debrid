"""Direct Mount run with Jellyfin or Emby (File Management.plex_mode_media_server
= 'jellyfin'): the server reads the mount, and cli_debrid tracks the library
through its API, as it does through Plex or Silo.

API used (same on Jellyfin and Emby, API key in Debug.emby_jellyfin_token):
- GET  /Library/VirtualFolders: library roots, to place cli_debrid's mount paths
  in the server's view (they may be mounted elsewhere in its container).
- POST /Library/Media/Updated: scan a new or removed release's folder.
- POST /Items/{library}/Refresh: fallback when a path fits no library.
- GET  /Users/{user}/Items: the library's movies and episodes with their files
  (all of them, or the newest first for the recently-added scan).
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from utilities import media_server_paths as paths
from utilities.direct_mount import is_direct_mount_jellyfin
from utilities.jellyfin_auth import jellyfin_auth_headers
from utilities.settings import get_setting

_TIMEOUT = 15
_LIB_TTL = 300
_lib_cache: Dict[str, Any] = {'at': 0.0, 'key': None, 'items': None}
_lib_lock = threading.Lock()

RECENT_HOURS = 48
_PAGE = 200

# File names whose extension marks a file (anything else is a folder, since
# release folders are full of dots: "Show.S01E01.1080p.WEB-DL.x264-GRP")
_FILE_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.ts',
              '.m2ts', '.iso', '.strm', '.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.sup', '.nfo'}

# find_item_id's whole-library lookup (file name -> item id), reused for a while
_ID_MAP_TTL = 600
_id_map: Dict[str, Any] = {'at': 0.0, 'map': None}
_id_map_lock = threading.Lock()


def config(get=None) -> Tuple[str, str]:
    get = get or get_setting
    url = (get('Debug', 'emby_jellyfin_url', '') or '').strip().rstrip('/')
    token = (get('Debug', 'emby_jellyfin_token', '') or '').strip()
    return url, token


def is_jellyfin_direct(get=None) -> bool:
    """Direct Mount with Jellyfin/Emby, and its URL and API key are set."""
    url, token = config(get)
    return bool(url and token) and is_direct_mount_jellyfin(get)


def local_base(get=None) -> str:
    """The mount's top folder: cli_debrid's Direct Mount paths are under it."""
    get = get or get_setting
    mount = (get('Plex', 'mounted_file_location', '') or '').rstrip('/')
    return mount[:-len('/__all__')] if mount.endswith('/__all__') else mount


def _headers(token: str) -> dict:
    return {**jellyfin_auth_headers(token), 'Accept': 'application/json'}


def _request(method: str, path: str, url: str = None, token: str = None, **kw) -> requests.Response:
    if url is None or token is None:
        url, token = config()
    kw.setdefault('timeout', _TIMEOUT)
    return requests.request(method, f'{url}{path}', headers=_headers(token), **kw)


def test_connection(url: str = None, token: str = None) -> Tuple[bool, str]:
    """(ok, message): server name and version, or why it didn't answer."""
    if url is None or token is None:
        url, token = config()
    if not url or not token:
        return False, 'Jellyfin/Emby URL or API key is not set'
    try:
        r = _request('GET', '/System/Info', url.rstrip('/'), token)
    except requests.RequestException as e:
        return False, f'Could not reach {url}: {e}'
    if r.status_code in (401, 403):
        return False, f'The API key was refused (HTTP {r.status_code})'
    if r.status_code != 200:
        return False, f'HTTP {r.status_code} from {url}'
    info = r.json() or {}
    # Jellyfin names itself ("Jellyfin Server"); Emby's info has no ProductName
    product = info.get('ProductName') or 'Emby'
    return True, f"{info.get('ServerName') or 'Server'} ({product} {info.get('Version', '')})".strip()


def get_libraries(force: bool = False, url: str = None, token: str = None) -> List[dict]:
    """Library roots as [{'id', 'name', 'type', 'paths'}] (cached 5 minutes)."""
    if url is None or token is None:
        url, token = config()
    with _lib_lock:
        fresh = time.time() - _lib_cache['at'] < _LIB_TTL and _lib_cache['key'] == (url, token)
        if fresh and not force and _lib_cache['items'] is not None:
            return _lib_cache['items']
    r = _request('GET', '/Library/VirtualFolders', url, token)
    r.raise_for_status()
    items = [{'id': f.get('ItemId') or f.get('Id'), 'name': f.get('Name'),
              'type': (f.get('CollectionType') or '').lower(), 'paths': f.get('Locations') or []}
             for f in (r.json() or []) if f.get('ItemId') or f.get('Id')]
    with _lib_lock:
        _lib_cache.update(at=time.time(), key=(url, token), items=items)
    return items


def to_server_path(local_path: str, libraries: Optional[List[dict]] = None) -> Optional[str]:
    if libraries is None:
        libraries = get_libraries()
    return (paths.to_server_path(local_path, libraries, local_base())
            or paths.mirrored_server_path(local_path, libraries, local_base()))


def from_server_path(server_path: str, libraries: List[dict]) -> str:
    return paths.from_server_path(server_path, libraries, local_base())


# ---------------------------------------------------------------------------
# Scans and removals
# ---------------------------------------------------------------------------

def _post_media_updated(server_path: str, update_type: str) -> Tuple[bool, int]:
    try:
        r = _request('POST', '/Library/Media/Updated',
                     json={'Updates': [{'Path': server_path, 'UpdateType': update_type}]})
    except requests.RequestException as e:
        logging.warning(f'[Jellyfin/Emby] Scan request for {server_path} failed: {e}')
        return False, 0
    return r.status_code in (200, 204), r.status_code


def _refresh_libraries(ids: List[str]) -> bool:
    accepted = False
    for lib_id in ids:
        try:
            r = _request('POST', f'/Items/{lib_id}/Refresh',
                         params={'Recursive': 'true', 'MetadataRefreshMode': 'Default',
                                 'ImageRefreshMode': 'Default'})
        except requests.RequestException as e:
            logging.warning(f'[Jellyfin/Emby] Library {lib_id} refresh failed: {e}')
            continue
        if r.status_code in (200, 204):
            accepted = True
            logging.info(f'[Jellyfin/Emby] Library {lib_id} refresh accepted')
        else:
            logging.warning(f'[Jellyfin/Emby] Library {lib_id} refresh refused (HTTP {r.status_code})')
    return accepted


def _is_file_path(local_path: str) -> bool:
    """Whether `local_path` names a file (else a folder). Asks the filesystem
    while the path exists; a removed one counts as a file only with a media or
    subtitle extension."""
    if os.path.isdir(local_path):
        return False
    if os.path.lexists(local_path):
        return True
    return os.path.splitext(local_path)[1].lower() in _FILE_EXTS


def scan_path(local_path: str, item_type: str = '', removed: bool = False) -> bool:
    """Ask the server to scan the folder of `local_path` (a file or folder in
    cli_debrid's view). A release in the mount's all-folder is scanned through
    the library folder that mirrors it. When no library holds the path, the
    libraries of the item's type are refreshed instead."""
    if not local_path:
        return False
    try:
        libraries = get_libraries()
    except Exception as e:
        logging.error(f'[Jellyfin/Emby] Could not list libraries: {e}')
        return False
    server_path = to_server_path(local_path, libraries)
    if server_path:
        folder = server_path.rsplit('/', 1)[0] if _is_file_path(local_path) else server_path
        ok, status = _post_media_updated(folder, 'Deleted' if removed else 'Created')
        if ok:
            logging.info(f'[Jellyfin/Emby] Scan accepted for {folder}')
            return True
        if status in (401, 403):
            logging.warning(f'[Jellyfin/Emby] Scan of {folder} refused (HTTP {status}): check the API key')
            return False
        lib, _root = paths.owning_library(server_path, libraries)
        ids = [str(lib['id'])] if lib else paths.library_ids_for_type(libraries, item_type or 'episode')
    else:
        logging.warning(f"[Jellyfin/Emby] No library holds {local_path}. Point the libraries at the mount's "
                        f"Movies / TV Shows folders; refreshing the whole library instead.")
        ids = paths.library_ids_for_type(libraries, item_type or 'episode')
    return _refresh_libraries(ids)


def update_item(item: Dict[str, Any]) -> bool:
    """Same contract as emby_update_item: scan the folder of a new or changed file."""
    path = item.get('full_path') or item.get('location_on_disk') or item.get('location')
    if not path:
        logging.error('[Jellyfin/Emby] No path to scan')
        return False
    return scan_path(path, item.get('type', ''))


def remove_file(item_title: str, item_path: str, episode_title: str = None) -> bool:
    """Same contract as remove_file_from_emby: tell the server the file is gone
    so its folder is rescanned and the item drops out."""
    if not item_path or not os.path.isabs(item_path):
        label = f'{item_title} - {episode_title}' if episode_title else item_title
        logging.info(f'[Jellyfin/Emby] No folder to rescan for {label} ({item_path}); it drops out on the next scan')
        return False
    return scan_path(item_path, 'episode' if episode_title else '', removed=True)


# ---------------------------------------------------------------------------
# Library contents
# ---------------------------------------------------------------------------

def _user_id(session: requests.Session, url: str, headers: dict) -> Optional[str]:
    r = session.get(f'{url}/Users', headers=headers, params={'isDisabled': 'false'}, timeout=30)
    r.raise_for_status()
    users = r.json() or []
    admins = [u for u in users if (u.get('Policy') or {}).get('IsAdministrator')]
    pick = (admins or users or [None])[0]
    return pick.get('Id') if pick else None


def _date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = value.replace('Z', '+00:00')
        if '.' in v:  # trim fractional seconds past microseconds (Jellyfin sends 7 digits)
            head, _, tail = v.partition('.')
            digits = ''.join(ch for ch in tail if ch.isdigit())
            zone = tail[len(digits):]
            v = f'{head}.{digits[:6]}{zone}'
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_items(hours: Optional[int] = None) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Movies and episodes in the server's libraries, in the shape of
    get_collected_from_emby with locations in cli_debrid's view. With `hours`,
    only what it added in that window (newest first, stops at the cutoff).
    None on failure."""
    from utilities.emby_functions import _process_emby_episode, _process_emby_movie
    url, token = config()
    if not url or not token:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None
    headers = _headers(token)
    fields = 'Path,ProviderIds,DateCreated,ProductionYear,Genres,ParentIndexNumber,IndexNumber,PremiereDate,MediaSources,SeriesId'
    raw: List[dict] = []
    try:
        with requests.Session() as session:
            user = _user_id(session, url, headers)
            if not user:
                logging.error('[Jellyfin/Emby] No user to read the library with')
                return None
            start = 0
            while True:
                r = session.get(f'{url}/Users/{user}/Items', headers=headers, timeout=60, params={
                    'IncludeItemTypes': 'Movie,Episode', 'Recursive': 'true', 'Fields': fields,
                    'SortBy': 'DateCreated', 'SortOrder': 'Descending',
                    # placeholders for episodes the server knows of but has no file for
                    'IsMissing': 'false',
                    'StartIndex': start, 'Limit': _PAGE})
                r.raise_for_status()
                page = (r.json() or {}).get('Items') or []
                done = not page
                for it in page:
                    created = _date(it.get('DateCreated'))
                    if cutoff and created and created < cutoff:
                        done = True
                        break
                    raw.append(it)
                if done or len(page) < _PAGE:
                    break
                start += _PAGE
            series_ids = sorted({it.get('SeriesId') for it in raw if it.get('Type') == 'Episode' and it.get('SeriesId')})
            shows: Dict[str, dict] = {}
            for i in range(0, len(series_ids), 100):
                r = session.get(f'{url}/Users/{user}/Items', headers=headers, timeout=60, params={
                    'Ids': ','.join(series_ids[i:i + 100]), 'Fields': 'ProviderIds,Genres,ProductionYear'})
                r.raise_for_status()
                for show in (r.json() or {}).get('Items') or []:
                    shows[show.get('Id')] = show
    except requests.RequestException as e:
        logging.error(f'[Jellyfin/Emby] Library listing failed: {e}')
        return None
    movies, episodes = [], []
    for item in raw:
        for it in _versions(item):
            if it.get('Type') == 'Movie':
                entry = _process_emby_movie(it)
                if entry:
                    movies.append(entry)
            elif it.get('Type') == 'Episode':
                entry = _process_emby_episode(it, shows.get(it.get('SeriesId')) or {'Name': it.get('SeriesName')})
                if entry:
                    episodes.append(entry)
    return _localize({'movies': movies, 'episodes': episodes})


def _versions(item: dict) -> List[dict]:
    """One copy of the item per file. A movie or episode with several versions
    (grouped by the server, or merged by hand) lists every file in MediaSources;
    reading only the first would make the full scan drop the others' rows."""
    sources = [s for s in item.get('MediaSources') or [] if s.get('Path')]
    if len(sources) < 2:
        return [item]
    seen, out = set(), []
    for src in sources:
        if src['Path'] in seen:
            continue
        seen.add(src['Path'])
        out.append({**item, 'MediaSources': [src], 'Path': src['Path']})
    return out


def get_recent_items(hours: int = RECENT_HOURS) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Movies and episodes the server added in the last `hours`: Direct Mount's
    recently-added scan. None on failure."""
    return _read_items(hours=hours)


def get_all_items() -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Every movie and episode file: Direct Mount's full library scan. None on failure."""
    return _read_items()


def _localize(library: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """Server file paths → cli_debrid's paths."""
    try:
        libraries = get_libraries()
    except Exception as e:
        logging.warning(f'[Jellyfin/Emby] Could not list libraries to map paths: {e}')
        return library
    for entry in library.get('movies', []) + library.get('episodes', []):
        if entry.get('location'):
            entry['location'] = from_server_path(entry['location'], libraries)
    logging.info(f"[Jellyfin/Emby] Library: {len(library.get('movies', []))} movie files, "
                 f"{len(library.get('episodes', []))} episode files")
    return library


def collect_from_jellyfin(items: List[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    """collect_from_plex for Jellyfin/Emby: collect the Checking items whose
    file the server already has. Returns (files it has, items no longer in
    Checking), or None when it didn't answer. Reads the last week's additions."""
    names = {os.path.basename(i.get('filled_by_file') or '').lower() for i in items if i.get('filled_by_file')}
    names.discard('')
    if not names:
        return 0, 0
    library = get_recent_items(hours=7 * 24)
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
# Subtitles
# ---------------------------------------------------------------------------

def _ids_by_name(library: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
    return {os.path.basename(e.get('location') or '').lower(): str(e['ratingKey'])
            for e in library['movies'] + library['episodes'] if e.get('location') and e.get('ratingKey')}


def find_item_id(local_path: str) -> Optional[str]:
    """The server's item id for a file (by file name): recent additions first,
    then the whole library (read once and reused for _ID_MAP_TTL seconds).
    None when it doesn't list the file."""
    name = os.path.basename(local_path or '').lower()
    if not name:
        return None
    recent = get_recent_items(hours=7 * 24)
    if recent is None:
        return None
    found = _ids_by_name(recent).get(name)
    if found:
        return found
    with _id_map_lock:
        if _id_map['map'] is not None and time.time() - _id_map['at'] < _ID_MAP_TTL:
            return _id_map['map'].get(name)
    library = get_all_items()
    if library is None:
        return None
    ids = _ids_by_name(library)
    with _id_map_lock:
        _id_map.update(at=time.time(), map=ids)
    return ids.get(name)


def upload_subtitle(local_path: str, content: bytes, lang_code: str, fmt: str = 'srt',
                    item_id: Optional[str] = None) -> bool:
    """Add a subtitle to the server's item for `local_path` through its API
    (POST /Videos/{id}/Subtitles), for a read-only mount where no sidecar file
    can be written. True when the server took it."""
    import base64
    data = content if isinstance(content, bytes) else (content or '').encode('utf-8')
    if not data:
        return False
    item_id = item_id or find_item_id(local_path)
    if not item_id:
        logging.warning(f'[Jellyfin/Emby subs] {os.path.basename(local_path)} is not in the library yet; subtitle not uploaded')
        return False
    body = {'Language': lang_code, 'Format': fmt, 'IsForced': False, 'IsHearingImpaired': False,
            'Data': base64.b64encode(data).decode('ascii')}
    try:
        r = _request('POST', f'/Videos/{item_id}/Subtitles', json=body, timeout=60)
    except requests.RequestException as e:
        logging.warning(f'[Jellyfin/Emby subs] upload for {os.path.basename(local_path)} failed: {e}')
        return False
    if r.status_code in (200, 204):
        logging.info(f'[Jellyfin/Emby subs] uploaded {lang_code} subtitle to item {item_id}')
        return True
    name = os.path.basename(local_path)
    if r.status_code == 404:
        # Emby has no subtitle upload endpoint
        logging.warning(f'[Jellyfin/Emby subs] {name}: this server takes no subtitle uploads (Emby); '
                        f'subtitles reach it as sidecar files when the mount accepts them')
    elif r.status_code == 500:
        # Jellyfin saves an uploaded subtitle next to the video by default,
        # which fails on a read-only mount
        logging.warning(f'[Jellyfin/Emby subs] {name}: upload refused (HTTP 500). On a read-only mount, turn off '
                        f'"Save subtitles into media folders" in the Jellyfin library\'s settings so Jellyfin '
                        f'keeps uploaded subtitles in its own data folder')
    else:
        logging.warning(f'[Jellyfin/Emby subs] upload for {name} refused: HTTP {r.status_code} {(r.text or "")[:200]}')
    return False


def pick_up_sidecar(local_path: str, item_type: str = '') -> bool:
    """A sidecar was written next to the file: rescan its folder and refresh the
    item, so the server lists it (the mount sends no file-change events, and a
    folder scan alone doesn't make Emby re-read an item it already has)."""
    scanned = scan_path(local_path, item_type)
    item_id = find_item_id(local_path)
    if not item_id:
        return scanned
    try:
        r = _request('POST', f'/Items/{item_id}/Refresh',
                     # Emby refuses ImageRefreshMode=None (HTTP 400); Default works on both
                     params={'Recursive': 'false', 'MetadataRefreshMode': 'Default', 'ImageRefreshMode': 'Default',
                             'ReplaceAllMetadata': 'false', 'ReplaceAllImages': 'false'})
    except requests.RequestException as e:
        logging.warning(f'[Jellyfin/Emby subs] refresh of item {item_id} failed: {e}')
        return scanned
    if r.status_code in (200, 204):
        logging.info(f'[Jellyfin/Emby subs] refreshed item {item_id} to pick up its new subtitle file')
        return True
    logging.warning(f'[Jellyfin/Emby subs] refresh of item {item_id} refused (HTTP {r.status_code})')
    return scanned
