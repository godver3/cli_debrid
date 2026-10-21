"""In-process registry of (torrent/NZB job ID, filename) pairs that ffprobe
has confirmed are unplayable this session. Sibling-reuse (both the debrid
and NZB paths) picks a candidate purely by "does this pack contain my
episode" - it has no awareness that a sibling episode already proved a
specific file inside this exact torrent is dead moments ago via a
playability check, so without this it would keep reusing (and re-failing)
that same file for every remaining sibling that maps to it.

Keyed per-file, not per-torrent: a season pack can be a mix of playable and
broken files (partial corruption, mismuxed episodes, etc.), so one bad file
must not block reuse of the same torrent for a *different* file that's
actually fine.

Deliberately in-memory only, not persisted: this only needs to cover
already-in-flight items for the remainder of this run. Separately,
add_to_not_wanted/add_to_not_wanted_urls (called alongside this) already
handles the permanent, cross-restart blacklist for fresh scrapes.
"""
import os
import threading
from typing import Optional

_lock = threading.Lock()
_bad_files: set[tuple[str, str]] = set()


def _key(torrent_id, filename) -> Optional[tuple]:
    if not torrent_id or not filename:
        return None
    return (str(torrent_id), os.path.basename(str(filename)))


def mark_torrent_unplayable(torrent_id, filename=None) -> None:
    """filename should be the specific file within torrent_id that failed
    (item['filled_by_file']). Kept optional for backwards compatibility, but
    without it this can't scope to just that file — callers should always
    pass it when available."""
    key = _key(torrent_id, filename)
    if key is None:
        return
    with _lock:
        _bad_files.add(key)


def is_torrent_known_unplayable(torrent_id, filename) -> bool:
    key = _key(torrent_id, filename)
    if key is None:
        return False
    with _lock:
        return key in _bad_files


def has_any_known_unplayable_file(torrent_id) -> bool:
    """Job-level check for reuse sites (NZB) that decide whether to reuse a
    job before the specific file within it is resolved, so a per-file check
    isn't possible at that point - blocks reuse if ANY file in this job has
    been proven dead this session. Coarser than is_torrent_known_unplayable
    (a genuinely mixed pack could over-block a fine sibling file), but it's
    what the NZB reuse decision point can actually act on."""
    if not torrent_id:
        return False
    tid = str(torrent_id)
    with _lock:
        return any(t == tid for t, _ in _bad_files)
