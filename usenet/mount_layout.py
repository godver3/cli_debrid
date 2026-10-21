"""Filesystem helpers for NZB provider mount layouts.

NzbDAV stores jobs under <mount>/<category>/<job>/; Zurg (and other
flat-layout SAB mounts) store them as <mount>/<job>/. Targeted Plex scans
need to find either layout without treating sibling job folders on a flat
mount as categories.
"""

from __future__ import annotations

import os
from typing import Optional


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
