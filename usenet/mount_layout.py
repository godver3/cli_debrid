"""Filesystem helpers for NZB provider mount layouts.

NzbDAV stores jobs under <mount>/<category>/<job>/; Zurg (and other
flat-layout SAB mounts) store them as <mount>/<job>/. Targeted Plex scans
need to find either layout without treating sibling job folders on a flat
mount as categories.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple


def resolve_nzb_job_dir(mount: str, folder: str) -> Optional[str]:
    """Return the absolute path to an NZB job folder under ``mount``.

    Prefers a flat hit (``<mount>/<folder>``) so Zurg mounts resolve correctly
    and sibling job dirs are never walked as categories. Falls back to the
    nested NzbDAV layout (``<mount>/<category>/<folder>``).
    """
    if not mount or not folder:
        return None

    flat = os.path.join(mount, folder)
    if os.path.isdir(flat):
        return flat

    try:
        for category in os.listdir(mount):
            candidate = os.path.join(mount, category, folder)
            if os.path.isdir(candidate):
                return candidate
    except OSError:
        return None

    return None


_MEDIA_EXTS = ('.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.mpeg',
               '.mpg', '.m4v')

# A job folder with season subfolders ("Show.S01/Season 01/*.mkv") looks exactly
# like a category holding a job, so these names mark a folder as a job.
# Whole names only: a job called "Series.Name.S01" or "Extras.Movie" inside a
# category must not make the category look like a job.
_JOB_SUBDIR_RE = re.compile(
    r'^(?:(?:season|series)(?:[ ._-]*\d{1,4})?|specials?|extras?|featurettes?|subs?|subtitles?|s\d{1,2})$', re.I)

# Release-style names (dots, a year, SxxEyy, a resolution). Category names
# ("tv", "movies", "radarr-4k") don't look like this.
_RELEASE_NAME_RE = re.compile(r'\.|\b(?:19|20)\d{2}\b|\bS\d{1,2}(?:E\d{1,3})?\b|\b\d{3,4}p\b', re.I)


def dir_has_media(path: str, depth: int = 1) -> bool:
    """True when ``path`` holds a video file directly, or (depth > 0) in a
    subfolder. Stops at the first hit so a big job costs one short listing."""
    try:
        with os.scandir(path) as it:
            subdirs = []
            for e in it:
                if e.name.startswith('.'):
                    continue
                if e.is_dir():
                    subdirs.append(e.path)
                elif e.name.lower().endswith(_MEDIA_EXTS):
                    return True
    except OSError:
        return False
    return depth > 0 and any(dir_has_media(d, depth - 1) for d in subdirs)


def is_category_dir(path: str) -> bool:
    """A category is a folder with no media of its own whose child folders
    contain media (NzbDAV's <mount>/<category>/<job>)."""
    try:
        with os.scandir(path) as it:
            children = [e for e in it if not e.name.startswith('.') and e.is_dir()]
    except OSError:
        return False
    if not children or any(_JOB_SUBDIR_RE.match(c.name) for c in children):
        return False
    try:
        with os.scandir(path) as it:
            if any(e.is_file() and e.name.lower().endswith(_MEDIA_EXTS) for e in it):
                return False
    except OSError:
        return False
    return any(dir_has_media(c.path, depth=1) for c in children)


# cli_mount/built-in mount views of the same content, never categories or jobs.
_RESERVED_TOP = {'__all__', '__bad__', 'nzbs', 'torrents'}


def list_job_dirs(mount: str, known_jobs: Optional[set] = None) -> List[Tuple[str, str]]:
    """(relative path, absolute path) of every NZB job folder under ``mount``,
    for flat (<mount>/<job>) and nested (<mount>/<category>/<job>) layouts.

    ``known_jobs``: top-level names already recorded as jobs. Those that look
    like release names skip the category probe (one extra listing per folder);
    the rest are probed again, since an empty category or one whose only job
    is still uploading gets recorded as a job on first sight."""
    jobs: List[Tuple[str, str]] = []
    known_jobs = known_jobs or set()
    try:
        with os.scandir(mount) as it:
            tops = sorted((e.name, e.path) for e in it
                          if not e.name.startswith('.') and e.name not in _RESERVED_TOP and e.is_dir())
    except OSError:
        return jobs
    for name, path in tops:
        skip_probe = name in known_jobs and _RELEASE_NAME_RE.search(name)
        if not skip_probe and is_category_dir(path):
            try:
                with os.scandir(path) as it:
                    for e in sorted(it, key=lambda x: x.name):
                        if not e.name.startswith('.') and e.is_dir():
                            jobs.append((f'{name}/{e.name}', e.path))
            except OSError:
                continue
        else:
            jobs.append((name, path))
    return jobs
