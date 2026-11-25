"""Paths between cli_debrid and a media server that may see the folders elsewhere.

Silo and Jellyfin/Emby (Direct Mount or Symlinked/Local) report their library
roots; these helpers move a path between cli_debrid's view and the server's.
`libraries` is a list of {'id', 'name', 'type', 'paths': [server roots]}.
`base` is the folder cli_debrid's paths are relative to: the symlink folder in
Symlinked/Local mode, the mount's top folder in Direct Mount.
"""
import os
from typing import List, Optional, Tuple


def norm(path: str) -> str:
    p = (path or '').replace('\\', '/')
    while '//' in p:
        p = p.replace('//', '/')
    return p.rstrip('/') or '/'


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip('/') + '/')


def _roots(libraries: List[dict]) -> List[str]:
    return [norm(r) for lib in libraries for r in lib.get('paths') or []]


def to_server_path(local_path: str, libraries: List[dict], base: str) -> Optional[str]:
    """The path the server knows `local_path` by, or None when no library holds it.

    Same path when the server sees the folders where cli_debrid does. Otherwise
    the part below `base` is moved onto a library root: a root named like the
    first folder under it (/mnt/symlinked/Movies/X → /media/Movies/X), or a
    root named like the base folder itself (/mnt/symlinked/... → /data/symlinked/...)."""
    p = norm(local_path)
    roots = _roots(libraries)
    for root in roots:
        if under(p, root):
            return p
    base = norm(base)
    if base == '/' or not under(p, base):
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


def from_server_path(server_path: str, libraries: List[dict], base: str) -> str:
    """The path cli_debrid knows a server path by: the reverse of to_server_path.
    Unchanged when it exists here as is (same paths)."""
    p = norm(server_path)
    if os.path.lexists(p):
        return p
    base = norm(base)
    if base == '/':
        return p
    candidates = []
    for lib in libraries:
        for r in lib.get('paths') or []:
            root = norm(r)
            if not under(p, root):
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


def owning_library(server_path: str, libraries: List[dict]) -> Tuple[Optional[dict], str]:
    """(library, root) of the deepest library root holding server_path."""
    best, best_root = None, ''
    for lib in libraries:
        for r in lib.get('paths') or []:
            root = norm(r)
            if under(server_path, root) and len(root) > len(best_root):
                best, best_root = lib, root
    return best, best_root


def mirrored_server_path(local_path: str, libraries: List[dict], base: str) -> Optional[str]:
    """The server's path for a file or folder in the mount's all-folder, through
    the library folder that mirrors it (<mount>/__all__/<release> is read by a
    library on <mount>/TV Shows/<release>), as Direct Mount's own scan does."""
    from utilities.plex_functions.sections_scan import _mirrored_library_path
    roots = {}
    for lib in libraries:
        for r in lib.get('paths') or []:
            roots[from_server_path(r, libraries, base).rstrip('/')] = norm(r)
    directory = local_path if os.path.isdir(local_path) else os.path.dirname(local_path)
    mirrored = _mirrored_library_path(os.path.abspath(directory), list(roots))
    if not mirrored:
        return None
    if directory != local_path:
        mirrored = os.path.join(mirrored, os.path.basename(local_path))
    return to_server_path(mirrored, libraries, base)


def library_ids_for_type(libraries: List[dict], item_type: str, movie_types=('movies',),
                         show_types=('series', 'tvshows'), mixed=('mixed',)) -> List[str]:
    want = movie_types if item_type == 'movie' else show_types
    return [str(l['id']) for l in libraries if (l.get('type') or '') in tuple(want) + tuple(mixed)]
