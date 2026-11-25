"""When a movie with no air time is first searched, and its release-day window.

A movie's release date has no time of day, and digital releases usually appear
during that day. So a movie is first searched at 00:00 on its release date
plus Queue.movie_airtime_offset (it used to be 00:00 on the *next* day plus the
offset, which waited most of a day after releases were out).

The first 24 hours from that start are the movie's release-day window, which
ends when the old rule would have started searching. Inside it, a search that
finds nothing sends the movie to Sleeping whatever its wake limit, it wakes
every RELEASE_DAY_RETRY, and those wakes don't count toward the wake limit. So
starting early can't get a movie blacklisted any sooner than before.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from utilities.settings import get_setting

RELEASE_DAY_WINDOW = timedelta(hours=24)
RELEASE_DAY_RETRY = timedelta(hours=1)


def movie_offset_hours() -> float:
    try:
        return float(get_setting("Queue", "movie_airtime_offset", "0"))
    except (TypeError, ValueError):
        return 0.0


def _base_release_date(item: Dict) -> Optional[datetime]:
    """The date a movie is searched from: the physical date when its version
    requires one, otherwise the release date."""
    date_str = item.get('release_date')
    version_settings = (get_setting('Scraping', 'versions', {}) or {}).get(item.get('version'), {}) or {}
    if version_settings.get('require_physical_release'):
        date_str = item.get('physical_release_date')
    if not date_str or str(date_str).lower() in ('unknown', 'none'):
        return None
    try:
        return datetime.strptime(str(date_str)[:10], '%Y-%m-%d')
    except ValueError:
        return None


def release_day_window(item: Dict) -> Optional[Tuple[datetime, datetime]]:
    """(start, end) of a movie's release-day window, or None when it has none
    (not a movie, has an air time, or no usable date)."""
    if item.get('type') != 'movie' or item.get('airtime'):
        return None
    base = _base_release_date(item)
    if base is None:
        return None
    start = base + timedelta(hours=movie_offset_hours())
    return start, start + RELEASE_DAY_WINDOW


def in_release_day_window(item: Dict, now: Optional[datetime] = None) -> bool:
    try:
        window = release_day_window(item)
    except Exception as e:
        logging.debug(f"Release-day window check failed for item {item.get('id')}: {e}")
        return False
    if window is None:
        return False
    now = now or datetime.now()
    return window[0] <= now < window[1]
