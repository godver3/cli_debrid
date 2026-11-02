"""What cli_debrid has recently seen of the Plex server.

In Plex mode an item is only collected once Plex adds it, so the Checking queue
needs to tell "Plex rejected this file" apart from "Plex hasn't got to it yet".
Callers that talk to Plex record what they saw here; the Checking queue asks
plex_stalled_since() before giving up on a finished download.

State is in memory only. After a restart nothing is known yet, which reads as
"not stalled", the same behavior as before this module existed.
"""

import threading
import time
from typing import Dict, Iterable, List, Optional

# A failed contact this recent means Plex is unreachable or timing out. Plex that
# answers some requests and times out on others is as stuck as Plex that's down.
FAILURE_WINDOW = 600

_lock = threading.Lock()
_last_fail = 0.0                        # last failed contact with Plex (our clock)
_added_ats: Dict[str, List[float]] = {}  # library key -> addedAt values in its last recently-added listing (Plex's clock)
_listing_seen_at = 0.0                  # when a recently-added listing was last read (our clock)


def record_contact_failure() -> None:
    global _last_fail
    with _lock:
        _last_fail = time.time()


def record_recently_added(library_key, added_ats: Iterable) -> None:
    """Record one library's recently-added listing (the addedAt of each entry)."""
    global _listing_seen_at
    values = []
    for value in added_ats:
        try:
            values.append(float(value or 0))
        except (TypeError, ValueError):
            continue
    with _lock:
        _added_ats[str(library_key)] = values
        _listing_seen_at = time.time()


def plex_stalled_since(since: float, waiting_ahead: int) -> Optional[str]:
    """Why Plex looks stuck for a release waiting since ``since``, or None.

    ``waiting_ahead`` is how many releases finished before this one and are still
    waiting for Plex. Plex works through scans roughly in order, so if it has added
    fewer items since ``since`` than that, it hasn't reached this one yet. If it has
    added more, it went past this release and the file is the likely problem.
    """
    now = time.time()
    with _lock:
        last_fail, listed_at = _last_fail, _listing_seen_at
        added_since = sum(1 for values in _added_ats.values() for v in values if v > since)
    if last_fail and now - last_fail < FAILURE_WINDOW:
        return "Plex is unreachable or timing out"
    if listed_at > since and waiting_ahead > 0 and added_since < waiting_ahead:
        return (f"Plex is behind (added {added_since} item(s) since this finished, "
                f"{waiting_ahead} finished earlier and are still waiting)")
    return None


def _reset_for_tests() -> None:
    global _last_fail, _listing_seen_at
    with _lock:
        _last_fail = _listing_seen_at = 0.0
        _added_ats.clear()
