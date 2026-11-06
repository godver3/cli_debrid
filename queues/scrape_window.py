"""The alternate scrape time strategy's window, shared by the Scraping and Unreleased queues.

With Debug > use_alternate_scrape_time_strategy on, items are scraped in a 24-hour window
ending at Debug > alternate_scrape_time_24h. Each queue decides what "strategy off" means
for it; this module only answers whether an item falls in the window.
"""
from datetime import datetime, time as dt_time, timedelta

from utilities.settings import get_setting


def alternate_scrape_anchor_time() -> dt_time:
    anchor_str = get_setting('Debug', 'alternate_scrape_time_24h', '00:00')
    try:
        return datetime.strptime(anchor_str, '%H:%M').time()
    except Exception:
        return dt_time(0, 0)


def in_alternate_scrape_window(item, now=None, anchor_time=None) -> bool:
    """Whether the item's release (date + airtime) falls in the window, or before it."""
    if now is None:
        now = datetime.now()
    if anchor_time is None:
        anchor_time = alternate_scrape_anchor_time()
    # The window ends at today's anchor even when that's still ahead of now (both copies
    # this replaced did the same in either case).
    anchor_dt = now.replace(hour=anchor_time.hour, minute=anchor_time.minute, second=0, microsecond=0)
    window_start = anchor_dt - timedelta(hours=24)

    release_date_str = item.get('release_date')
    airtime_str = item.get('airtime')
    if not release_date_str or release_date_str.lower() in ['unknown', 'none']:
        return False
    try:
        release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
        airtime = dt_time(0, 0)
        if airtime_str:
            for fmt in ('%H:%M:%S', '%H:%M'):
                try:
                    airtime = datetime.strptime(airtime_str, fmt).time()
                    break
                except ValueError:
                    continue
        item_dt = datetime.combine(release_date, airtime)
    except Exception:
        return False
    # Items older than the window are always allowed
    if item_dt < window_start:
        return True
    return window_start <= item_dt <= anchor_dt
