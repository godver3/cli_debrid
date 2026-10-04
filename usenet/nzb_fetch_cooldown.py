"""Short-lived cooldown for an indexer that is refusing NZB downloads.

When an indexer refuses an NZB download because of a limit (HTTP 429/503, or a
newznab "request/download limit reached" error), the Adding queue used to
treat it like any other rejected result: nothing was recorded, the next scrape
a few minutes later ranked the same release first again, and each attempt
fetched the URL three times (pre-check, cli_mount's own URL fetch, direct-upload
fallback). That kept the indexer pinned at its limit and the item retrying.

Scope is deliberately narrow: ONLY the indexer's limit/unavailable answer to
the .nzb download itself. Missing articles, ffprobe failures, cli_mount
rejections and every other error keep their existing handling. Entries expire
and are never persisted — a refused download says nothing about whether the
release is healthy, so this is not the not-wanted list.
"""

import re
import time
from threading import Lock
from typing import Optional
from urllib.parse import urlparse

COOLDOWN_SECONDS = 30 * 60

_until: dict = {}
_lock = Lock()

# Newznab reports limits as <error code="..." description="..."/>, often with HTTP 200.
_NEWZNAB_ERROR_RE = re.compile(r'<error\b[^>]*\bdescription="([^"]*)"', re.IGNORECASE)
_LIMIT_STATUS = (429, 503)


# Prowlarr proxies every indexer through one host as /<indexerId>/download?...
_PROWLARR_PATH_RE = re.compile(r'^/(\d+)/download\b')


def _indexer_key(url: str) -> str:
    """The indexer behind an NZB URL: its host, plus Prowlarr's per-indexer id."""
    try:
        parsed = urlparse(url)
    except Exception:
        return ''
    host = (parsed.netloc or '').lower()
    m = _PROWLARR_PATH_RE.match(parsed.path or '')
    return f'{host}/{m.group(1)}' if host and m else host


def is_indexer_limit_response(status_code: int, body: str) -> bool:
    """True when the response means the indexer is refusing all downloads for now."""
    if status_code in _LIMIT_STATUS:
        return True
    m = _NEWZNAB_ERROR_RE.search(body or '')
    return bool(m and 'limit' in m.group(1).lower())


def record_indexer_limit(url: str) -> None:
    """Put the indexer serving this NZB URL on cooldown."""
    key = _indexer_key(url)
    if key:
        with _lock:
            _until[key] = time.monotonic() + COOLDOWN_SECONDS


def cooldown_reason(url: str) -> Optional[str]:
    """Why a fresh download from this URL's indexer should wait, or None."""
    key = _indexer_key(url)
    if not key:
        return None
    with _lock:
        until = _until.get(key)
        if until is None:
            return None
        if until <= time.monotonic():
            del _until[key]
            return None
    return f'indexer {key} hit its download limit in the last {COOLDOWN_SECONDS // 60} min'


def reset_fetch_cooldowns() -> None:
    """Test/debug helper: clear all cooldowns."""
    with _lock:
        _until.clear()
