"""Process-lifetime cache of NZB releases with a definitive missing-segments result.

Indexers frequently list the same underlying release under multiple GUIDs
(the same upload re-indexed, or duplicate indexers carrying the same feed).
The not-wanted store (database/not_wanted_magnets.py) already blacklists a
specific GUID/URL once it's confirmed dead — but a *different* GUID for the
exact same release never matches that blacklist, so it pays the same
expensive server-side segment-availability check again (a real submission
attempt to cli_mount/nzbdav, not a cheap local lookup).

This cache is keyed on the normalized release TITLE instead, so a repeat of
the same underlying release short-circuits before ever reaching cli_mount —
without touching the existing per-GUID not-wanted behavior, which still
applies to the specific GUID that was actually tried.

Deliberately in-memory and bounded (not persisted): a missing-segments result
can become stale as usenet retention/repost activity changes, and this is
meant to suppress a burst of duplicate GUIDs within one run, not to remember
forever. Only a *definitive* missing-segments result (cli_mount/nzbdav's own
ARTICLE_NOT_FOUND-style report) populates it — timeouts and other
indeterminate errors must never be cached here, since those aren't proof the
release is actually dead.
"""

import re
from collections import OrderedDict
from threading import Lock

_MAX_ENTRIES = 2000
_dead_releases: "OrderedDict[str, None]" = OrderedDict()
_lock = Lock()


def _normalize_release_title(title: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (title or '').lower())


def is_release_known_dead(title: str) -> bool:
    """True if this release's title matches one already confirmed dead this session."""
    key = _normalize_release_title(title)
    if not key:
        return False
    with _lock:
        return key in _dead_releases


def mark_release_dead(title: str) -> None:
    """Record a definitive missing-segments result for this release title.

    Only call this for a conclusive result (e.g. ARTICLE_NOT_FOUND / cli_mount's
    own last_missing_segments flag) — never for a timeout or other inconclusive
    provider error, which is not proof the release is actually dead.
    """
    key = _normalize_release_title(title)
    if not key:
        return
    with _lock:
        _dead_releases[key] = None
        _dead_releases.move_to_end(key)
        while len(_dead_releases) > _MAX_ENTRIES:
            _dead_releases.popitem(last=False)


def reset_dead_release_cache() -> None:
    """Test/debug helper: clear the cache."""
    with _lock:
        _dead_releases.clear()
