"""Whether Plex has been failing to answer cli_debrid.

In Plex mode an item is only collected once Plex adds it, so the Checking queue
needs to tell "Plex rejected this file" apart from "Plex couldn't be asked".
Callers that talk to Plex record failed contacts here; the Checking queue asks
recent_failure() before giving up on a finished download, and backs off from
retrying scans while Plex is failing. Whether Plex is busy scanning comes from
Plex itself (plex_functions.plex_is_scanning).

State is in memory only. After a restart nothing is known yet, which reads as
"no recent failure".
"""

import threading
import time
from typing import Optional

# A failed contact this recent means Plex is unreachable or timing out. Plex that
# answers some requests and times out on others is as stuck as Plex that's down.
FAILURE_WINDOW = 600

_lock = threading.Lock()
_last_fail = 0.0  # last failed contact with Plex (our clock)


def record_contact_failure() -> None:
    global _last_fail
    with _lock:
        _last_fail = time.time()


def recent_failure(window: float = FAILURE_WINDOW) -> Optional[float]:
    """Seconds since Plex last failed to answer, if that was within ``window``; else None."""
    with _lock:
        last_fail = _last_fail
    if not last_fail:
        return None
    age = time.time() - last_fail
    return age if age < window else None


def _reset_for_tests() -> None:
    global _last_fail
    with _lock:
        _last_fail = 0.0
