"""Shared skip bookkeeping for the Plex watchlist fetchers (no heavy imports)."""
import logging
from typing import Dict, List

NO_ID_REASON = 'no IMDb ID, and no TMDb ID that converts to one (Plex has no usable external id for it yet; retried next run)'


def fetch_error_reason(error) -> str:
    """Skip reason for an item whose detail fetch failed ('HTTP429', 'Timeout', ...)."""
    return f"detail fetch failed ({str(error)[:60]})"


class SkippedItems:
    """Collects skipped watchlist titles by reason, so the end-of-fetch summary says *why*.

    A rate-limited fetch (HTTP429) and an item Plex has no ids for need different fixes, so they
    are logged as separate INFO lines instead of one combined list.
    """
    def __init__(self):
        self.by_reason: Dict[str, List[str]] = {}

    def add(self, title, reason: str) -> None:
        self.by_reason.setdefault(reason, []).append(title)

    def __len__(self) -> int:
        return sum(len(titles) for titles in self.by_reason.values())

    def log(self, log_prefix: str = '') -> None:
        for reason, titles in self.by_reason.items():
            shown = ', '.join(f"'{t}'" for t in titles[:25])
            more = f" (+{len(titles) - 25} more)" if len(titles) > 25 else ''
            logging.info(f"{log_prefix}Skipped {len(titles)} watchlist item(s) - {reason}: {shown}{more}")
