"""Checking-queue period backstop for NZB items under fail-closed playability.

#499's Plex-mode ffprobe gate returns None for incomplete jobs and inconclusive
probes, so items stay in Checking instead of being force-collected. This module
owns the matching eviction path: once time in Checking exceeds
``checking_queue_period``, blacklist each item's NZB GUID and return the group
to Wanted so they are never stuck forever.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, List


def checking_period_exceeded(time_in_queue: float, limit: float) -> bool:
    """Return True when an item/group has outlived its Checking allowance."""
    return time_in_queue > limit


def apply_nzb_checking_timeout(
    *,
    torrent_id: str,
    items: Iterable[dict],
    time_in_queue: float,
    limit: float,
    add_to_not_wanted_nzb_guid: Callable[[str], Any],
    move_to_wanted: Callable[[dict, str], Any],
    contains_item_id: Callable[[Any], bool],
    from_state: str = "Checking",
) -> bool:
    """If ``time_in_queue`` exceeds ``limit`` for an NZB torrent, blacklist and Wanted.

    Returns True when the backstop fired. Non-NZB torrent IDs are ignored (the
    debrid path still removes the torrent itself in CheckingQueue).
    """
    if not str(torrent_id).startswith("nzb:"):
        return False
    if not checking_period_exceeded(time_in_queue, limit):
        return False

    item_list: List[dict] = list(items)
    for item in item_list:
        url = item.get("filled_by_magnet") or ""
        if not url:
            continue
        try:
            add_to_not_wanted_nzb_guid(url)
        except Exception:
            pass

    for item in item_list:
        if contains_item_id(item["id"]):
            move_to_wanted(item, from_state)
    return True
