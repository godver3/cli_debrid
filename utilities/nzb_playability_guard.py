"""Conclusive playability classification for newly collected NZB media.

The general repair engine deliberately treats an inconclusive FUSE read as
readable because its caller may otherwise delete a healthy historical file.
That is the right destructive-repair bias, but the wrong admission rule for a
new item: Plex must not be told that an NZB is collected until one probe has
actually returned media data.
"""

import random
import time


HEALTHY = "healthy"
BROKEN = "broken"
UNKNOWN = "unknown"


def nzb_job_ready(job_status):
    """Return True only when cli_mount conclusively reports completion."""
    return bool(job_status and job_status.get("state") == "completed")


def classify_new_nzb(
    file_path,
    timeout=10,
    attempts=3,
    *,
    duration_reader=None,
    packet_reader=None,
    sleep_fn=time.sleep,
    fraction_reader=None,
):
    """Return ``healthy``, ``broken``, or ``unknown`` for a new NZB file.

    A successful attempt is conclusive. All-clean failures are conclusive.
    Any timeout/internal error without a success is unknown and must remain in
    Checking for a later retry; it is neither accepted nor destroyed.
    """
    if not file_path:
        return BROKEN

    if duration_reader is None or packet_reader is None:
        from usenet.repair_engine import _media_duration_seconds, _probe_readable_once

        duration_reader = duration_reader or _media_duration_seconds
        packet_reader = packet_reader or _probe_readable_once

    fraction_reader = fraction_reader or (lambda: random.uniform(0.2, 0.8))
    try:
        duration = duration_reader(file_path, timeout=timeout)
    except Exception:
        duration = None
    offset = duration * fraction_reader() if duration and duration > 1 else None

    results = []
    for attempt in range(max(1, attempts)):
        try:
            result = packet_reader(
                file_path,
                offset_seconds=offset,
                timeout=timeout,
            )
        except Exception:
            result = None
        if result is True:
            return HEALTHY
        results.append(result)
        if attempt < attempts - 1:
            sleep_fn(1.0)

    if any(result is None for result in results):
        return UNKNOWN
    return BROKEN
