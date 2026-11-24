"""
Host-path checks for onboarding: is a folder really shared with the host
(and so with the media server), and does the media server see it at the
same path?

cli_debrid, the mount and the media server must all see the mount and the
symlink folder at exactly the same paths. Inside Docker the usual mistakes
are a folder that was never mapped (it lives in the container's own layer,
invisible to everyone else and gone on recreate), a built-in mount without
`rshared` (the engine's FUSE mount never reaches the host), and a media
server given the folder under a different path. The first two show in
/proc/self/mountinfo; the last is checked by asking the media server to
list the folder. Silo may see the symlink folder at another path (cli_debrid
maps it by folder name), so for Silo each library folder is checked.

Every check returns (ok, message, warning): warning=True means "could not
tell / probably fine", never a hard failure.
"""

import base64
import logging
import os
import uuid
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests

Result = Tuple[bool, str, bool]

MARKER_PREFIX = 'clid-path-check-'


# ---- /proc/self/mountinfo --------------------------------------------------

def _unescape(s: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \ooo
    out, i = [], 0
    while i < len(s):
        if s[i] == '\\' and s[i + 1:i + 4].isdigit() and len(s[i + 1:i + 4]) == 3:
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)


def parse_mountinfo(text: str) -> List[Dict]:
    """Entries of a mountinfo file: mount_point, root, fstype, source,
    propagation ('shared', 'slave', 'private')."""
    out = []
    for line in text.splitlines():
        parts = line.split(' ')
        if '-' not in parts or len(parts) < 10:
            continue
        sep = parts.index('-')
        optional = parts[6:sep]
        prop = 'private'
        if any(o.startswith('shared:') for o in optional):
            prop = 'shared'
        elif any(o.startswith('master:') for o in optional):
            prop = 'slave'
        out.append({'root': _unescape(parts[3]), 'mount_point': _unescape(parts[4]),
                    'fstype': parts[sep + 1] if len(parts) > sep + 1 else '',
                    'source': _unescape(parts[sep + 2]) if len(parts) > sep + 2 else '',
                    'propagation': prop})
    return out


def read_mountinfo() -> Optional[List[Dict]]:
    try:
        with open('/proc/self/mountinfo') as f:
            return parse_mountinfo(f.read())
    except OSError:
        return None


def in_container() -> bool:
    return os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv')


def covering_mount(path: str, entries: List[Dict], skip_fuse: bool = False) -> Optional[Dict]:
    """The mount a path lives on (the last-mounted of the longest matching
    mount point). skip_fuse looks beneath a FUSE mount, at the folder it was
    mounted over (the built-in mount sits on top of its bind mount)."""
    path = os.path.normpath(path)
    best, best_len = None, -1
    for e in entries:
        mp = e['mount_point']
        if skip_fuse and e['fstype'].startswith('fuse') and mp == path:
            continue
        if path == mp or path.startswith(mp.rstrip('/') + '/') or mp == '/':
            if len(mp) >= best_len:
                best, best_len = e, len(mp)
    return best


def host_mapping(path: str, label: str, need_shared: bool = False, entries: Optional[List[Dict]] = None) -> Optional[Result]:
    """Is path a folder mapped in from the host (and shared, when
    need_shared)? None when not in a container or mountinfo is unreadable."""
    if entries is None:
        if not in_container():
            return None
        entries = read_mountinfo()
    if not entries:
        return None
    m = covering_mount(path, entries, skip_fuse=need_shared)
    if m is None or m['mount_point'] == '/':
        return False, (f"{path} is not mapped to a folder on the host: it lives inside cli_debrid's container, so "
                       f"your media server can't see it and it's lost when the container is recreated. Add "
                       f"`- {path}:{path}{':rshared' if need_shared else ''}` to cli_debrid's volumes in docker-compose."), False
    if need_shared and m['propagation'] != 'shared':
        return False, (f"{m['mount_point']} is mapped without `:rshared`, so the built-in mount stays inside this "
                       f"container and Plex sees an empty folder. Use `- {m['mount_point']}:{m['mount_point']}:rshared` "
                       f"in docker-compose (and `:rslave` for the media server)."), False
    return True, f"{label} is a host folder ({m['mount_point']}{', shared' if need_shared else ''})", False


# ---- what the media server sees ----------------------------------------------

def plex_list(url: str, token: str, path: str, timeout: int = 10) -> Optional[Set[str]]:
    """Names Plex sees in a folder (its library folder browser). Empty set
    for a missing or empty folder; None when Plex couldn't be asked."""
    key = quote(base64.b64encode(path.encode()).decode(), safe='')
    try:
        r = requests.get(f"{url.rstrip('/')}/services/browse/{key}", params={'includeFiles': 1},
                         headers={'X-Plex-Token': token, 'Accept': 'application/json'}, timeout=timeout)
        if r.status_code != 200:
            return None
        mc = (r.json() or {}).get('MediaContainer') or {}
    except (requests.RequestException, ValueError) as exc:
        logging.debug(f'[path check] Plex browse {path}: {exc}')
        return None
    return {os.path.basename((p.get('path') or '').rstrip('/')) or p.get('title', '')
            for p in (mc.get('Path') or []) + (mc.get('File') or [])}


def jellyfin_list(url: str, token: str, path: str, timeout: int = 10) -> Optional[Set[str]]:
    """Names Jellyfin/Emby sees in a folder (admin API). Empty set when the
    server says the folder isn't there; None when it couldn't be asked."""
    from utilities.jellyfin_auth import jellyfin_auth_headers
    try:
        r = requests.get(f"{url.rstrip('/')}/Environment/DirectoryContents",
                         params={'path': path, 'includeFiles': 'true', 'includeDirectories': 'true'},
                         headers=jellyfin_auth_headers(token), timeout=timeout)
    except requests.RequestException as exc:
        logging.debug(f'[path check] Jellyfin list {path}: {exc}')
        return None
    if r.status_code in (401, 403):
        return None
    if r.status_code != 200:
        return set()  # 400/404/500: the folder doesn't exist on the server
    try:
        return {i.get('Name') or os.path.basename((i.get('Path') or '').rstrip('/')) for i in r.json() or []}
    except ValueError:
        return None


def silo_list(url: str, key: str, path: str, timeout: int = 10) -> Optional[Set[str]]:
    """Folder names Silo sees in a folder (admin filesystem browser). Empty
    set when Silo says the folder isn't there; None when it couldn't be asked."""
    names, cursor = set(), None
    for _ in range(50):
        params = {'path': path, 'limit': 200}
        if cursor:
            params['cursor'] = cursor
        try:
            r = requests.get(f"{url.rstrip('/')}/api/v2/admin/filesystem/browse", params=params,
                             headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'},
                             timeout=timeout)
        except requests.RequestException as exc:
            logging.debug(f'[path check] Silo browse {path}: {exc}')
            return None
        if r.status_code == 404:
            return set()
        if r.status_code != 200:
            return None
        try:
            body = r.json() or {}
        except ValueError:
            return None
        names |= {i.get('name') or os.path.basename((i.get('path') or '').rstrip('/')) for i in body.get('items') or []}
        cursor = (body.get('page') or {}).get('next_cursor')
        if not (body.get('page') or {}).get('has_more') or not cursor:
            return names
    return names


def _lister(server: Dict):
    if server.get('kind') == 'plex':
        return lambda p: plex_list(server['url'], server['token'], p)
    if server.get('kind') == 'silo':
        return lambda p: silo_list(server['url'], server['token'], p)
    return lambda p: jellyfin_list(server['url'], server['token'], p)


_TYPE_FOLDERS = (('movies_folder_name', 'Movies'), ('tv_shows_folder_name', 'TV Shows'),
                 ('anime_movies_folder_name', 'Anime Movies'), ('anime_tv_shows_folder_name', 'Anime TV Shows'),
                 ('documentary_movies_folder_name', 'Documentary Movies'),
                 ('documentary_tv_shows_folder_name', 'Documentary TV Shows'))


def type_folder_names(config: Dict) -> Set[str]:
    """The folders cli_debrid makes inside the symlink folder (Movies, TV Shows, ...)."""
    dbg = config.get('Debug', {}) or {}
    return {(dbg.get(k) or default).strip() for k, default in _TYPE_FOLDERS}


def silo_sees_symlinks(server: Dict, symlinks: str, folder_names: Optional[Set[str]] = None) -> List[Tuple[str, Result]]:
    """One row per Silo library inside the symlink folder: a marker folder made
    in the library's folder here must show up in Silo's listing of the
    library root. Silo may see the folder at another path (cli_debrid maps it
    by folder name), so the roots come from Silo's library list."""
    from utilities import silo_functions as silo
    try:
        libraries = silo.get_libraries(force=True, url=server['url'], key=server['token'])
    except Exception as exc:
        return [('Silo libraries', (True, f"Couldn't list Silo's libraries ({exc}); check them by hand", True))]
    rows = []
    for lib in libraries:
        if (lib.get('type') or '') not in ('movies', 'series', 'mixed'):
            continue
        for root in lib.get('paths') or []:
            local = silo.from_silo_path(root, libraries, symlinks)
            # Silo's root is inside the symlink folder when it is the same path,
            # its local counterpart exists, or it is named like one of the type
            # folders cli_debrid will make there. Any other root is unrelated.
            same_path = root.rstrip('/') == symlinks.rstrip('/') or root.startswith(symlinks.rstrip('/') + '/')
            named = root.rstrip('/').rsplit('/', 1)[-1] in (folder_names or {n for _, n in _TYPE_FOLDERS})
            if not (same_path or os.path.isdir(local) or named):
                continue
            label = f"Silo library {lib.get('name') or lib.get('id')}"
            if not os.path.isdir(local):
                rows.append((label, (True, f"{local} doesn't exist yet (it's made with the first symlink), so Silo's "
                                           f"view of it ({root}) can't be checked yet", True)))
                continue
            marker = os.path.join(local, MARKER_PREFIX + uuid.uuid4().hex[:8])
            try:
                os.makedirs(marker)
            except OSError as exc:
                rows.append((label, (False, f"Can't write to {local}: {exc}", True)))
                continue
            try:
                seen = silo_list(server['url'], server['token'], root)
            finally:
                try:
                    os.rmdir(marker)
                except OSError:
                    pass
            if seen is None:
                rows.append((label, (True, f"Couldn't ask Silo what it sees in {root}; check by hand", True)))
            elif os.path.basename(marker) in seen:
                rows.append((label, (True, f"Silo sees {local} at {root}", False)))
            else:
                rows.append((label, (False, f"Silo's folder {root} isn't cli_debrid's {local}. Give Silo the symlink "
                                            f"folder and point this library at the matching folder inside it.", False)))
    if not rows:
        names = ', '.join(f"{lib.get('name')} ({', '.join(lib.get('paths') or [])})" for lib in libraries) or 'none'
        rows.append(('Silo libraries', (False, f"No Silo movie or TV library is inside the symlink folder {symlinks}. "
                                               f"Point Silo's libraries at its Movies and TV Shows folders. Silo's "
                                               f"libraries now: {names}", False)))
    return rows


def server_sees_folder(server: Dict, path: str, label: str) -> Result:
    """Write-test: a marker folder created here must show up in the media
    server's listing of the same path."""
    name = server.get('name', 'Media server')
    marker = os.path.join(path, MARKER_PREFIX + uuid.uuid4().hex[:8])
    try:
        os.makedirs(marker)
    except OSError as exc:
        return False, f"Can't write to {path}: {exc}", True
    try:
        seen = _lister(server)(path)
    finally:
        try:
            os.rmdir(marker)
        except OSError:
            pass
    if seen is None:
        return True, f"Couldn't ask {name} what it sees in {path}; check by hand that it has the same folder", True
    if os.path.basename(marker) in seen:
        return True, f"{name} sees the {label} at {path}", False
    return False, (f"{name} doesn't see the {label} at {path}. Give {name} the same folder at the same path "
                   f"(Docker: `- {path}:{path}` in its volumes)."), False


def server_sees_mount(server: Dict, path: str) -> Result:
    """Compare what cli_debrid and the media server list in the mount
    (read-only, so no marker): at least one shared name, or both empty."""
    name = server.get('name', 'Media server')
    try:
        local = {n for n in os.listdir(path) if not n.startswith('.')}
    except OSError as exc:
        return False, f"cli_debrid can't list {path}: {exc}", True
    if not local:
        return True, f"{path} is empty here, so there's nothing to compare with {name} yet", True
    seen = _lister(server)(path)
    if seen is None:
        return True, f"Couldn't ask {name} what it sees in {path}; check by hand that it has the same folder", True
    if local & seen:
        return True, f"{name} sees the mount at {path}", False
    return False, (f"{name} sees {'nothing' if not seen else 'different files'} at {path}, but cli_debrid sees "
                   f"{len(local)} item(s). Give {name} the mount at the same path (Docker: `- {path}:{path}:rslave`)."), False


def media_server(config: Dict) -> Optional[Dict]:
    """The media server onboarding set up, as {kind, name, url, token}."""
    fm = config.get('File Management', {}) or {}
    if fm.get('file_collection_management') == 'Symlinked/Local':
        if (fm.get('media_server_type') or '').lower() == 'silo':
            dbg = config.get('Debug', {}) or {}
            if dbg.get('silo_url') and dbg.get('silo_api_key'):
                return {'kind': 'silo', 'name': 'Silo', 'url': dbg['silo_url'], 'token': dbg['silo_api_key']}
            return None
        if fm.get('plex_url_for_symlink') and fm.get('plex_token_for_symlink'):
            return {'kind': 'plex', 'name': 'Plex', 'url': fm['plex_url_for_symlink'], 'token': fm['plex_token_for_symlink']}
        dbg = config.get('Debug', {}) or {}
        if dbg.get('emby_jellyfin_url') and dbg.get('emby_jellyfin_token'):
            return {'kind': 'jellyfin', 'name': 'Jellyfin / Emby', 'url': dbg['emby_jellyfin_url'],
                    'token': dbg['emby_jellyfin_token']}
        return None
    if (fm.get('plex_mode_media_server') or '').lower() == 'silo':
        dbg = config.get('Debug', {}) or {}
        if dbg.get('silo_url') and dbg.get('silo_api_key'):
            return {'kind': 'silo', 'name': 'Silo', 'url': dbg['silo_url'], 'token': dbg['silo_api_key']}
        return None
    if (fm.get('plex_mode_media_server') or '').lower() == 'jellyfin':
        dbg = config.get('Debug', {}) or {}
        if dbg.get('emby_jellyfin_url') and dbg.get('emby_jellyfin_token'):
            return {'kind': 'jellyfin', 'name': 'Jellyfin / Emby', 'url': dbg['emby_jellyfin_url'],
                    'token': dbg['emby_jellyfin_token']}
        return None
    plex = config.get('Plex', {}) or {}
    if plex.get('url') and plex.get('token'):
        return {'kind': 'plex', 'name': 'Plex', 'url': plex['url'], 'token': plex['token']}
    return None


def mount_root(path: str) -> str:
    """The mount's top folder for a files path like /mnt/clid/__all__."""
    p = path.rstrip('/')
    return os.path.dirname(p) if os.path.basename(p) == '__all__' else p


def onboarding_checks(config: Dict, builtin_mount_path: Optional[str] = None) -> List[Tuple[str, Result]]:
    """(name, result) rows for onboarding's final check."""
    rows = []
    fm = config.get('File Management', {}) or {}
    files_path = (fm.get('original_files_path') or '').strip()
    symlinks = (fm.get('symlinked_files_path') or '').strip() if fm.get('file_collection_management') == 'Symlinked/Local' else ''

    if builtin_mount_path:
        r = host_mapping(builtin_mount_path, 'Built-in mount folder', need_shared=True)
        if r:
            rows.append(('Mount shared with host', r))
    elif files_path:
        r = host_mapping(mount_root(files_path), 'Mount folder')
        if r and not r[0]:
            rows.append(('Mount on host', (False, r[1], True)))  # an external mount may be reached another way
    if symlinks:
        r = host_mapping(symlinks, 'Symlink folder')
        if r:
            rows.append(('Symlink folder on host', r))

    server = media_server(config)
    if server:
        if files_path and os.path.isdir(mount_root(files_path)):
            rows.append((f"{server['name']} sees the mount", server_sees_mount(server, mount_root(files_path))))
        if symlinks and os.path.isdir(symlinks):
            if server['kind'] == 'silo':
                rows += silo_sees_symlinks(server, symlinks, type_folder_names(config))
            else:
                rows.append((f"{server['name']} sees the symlinks", server_sees_folder(server, symlinks, 'symlink folder')))
    return rows
