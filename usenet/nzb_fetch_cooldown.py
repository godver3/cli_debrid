"""Short-lived cooldown for an indexer that is refusing NZB downloads.

When an indexer refuses an NZB download because of a limit (HTTP 429, a
newznab "request/download limit reached" error, or the same wording in a
plain/HTML/JSON body), the Adding queue used to treat it like any other
rejected result: nothing was recorded, the next scrape a few minutes later
ranked the same release first again, and each attempt fetched the URL three
times (pre-check, cli_mount's own URL fetch, direct-upload fallback). That kept
the indexer pinned at its limit and the item retrying.

Scope is deliberately narrow: ONLY the indexer's limit/unavailable answer to
the .nzb download itself. Missing articles, ffprobe failures, cli_mount
rejections and every other error keep their existing handling. Entries expire
and are never persisted — a refused download says nothing about whether the
release is healthy, so this is not the not-wanted list.
"""

import re
import time
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Mapping, Optional
from urllib.parse import urlparse

# A limit (daily/hourly grab or API cap) rarely lifts within minutes.
COOLDOWN_SECONDS = 30 * 60
# A bare 503 is as often a Cloudflare or maintenance blip as a limit.
UNAVAILABLE_COOLDOWN_SECONDS = 5 * 60
_RETRY_AFTER_MIN = 60
_RETRY_AFTER_MAX = 60 * 60

_until: dict = {}
_lock = Lock()

# Newznab reports errors as <error code="..." description="..."/>, often with HTTP 200.
_NEWZNAB_ERROR_RE = re.compile(r'<error\b([^>]*)>', re.IGNORECASE)
_ATTR_RE = re.compile(r'\b(code|description)\s*=\s*"([^"]*)"', re.IGNORECASE)
# Newznab spec: 500 "Request limit reached", 501 "Download limit reached".
# Some indexers (nzbgeek, drunkenslug) report 429 inside the XML as well.
_NEWZNAB_LIMIT_CODES = {'429', '500', '501'}
# Wording indexers, Prowlarr and NZBHydra2 use for a grab/API cap.
_LIMIT_PHRASE_RE = re.compile(
    r'\b(?:limits?|limited|quota|exceeded|too many (?:requests|downloads|grabs)'
    r'|rate[- ]?limit(?:ed)?|maximum (?:number|grabs|downloads|api))\b',
    re.IGNORECASE,
)
# Statuses whose non-NZB body may carry limit wording. 404/410 and 5xx error
# pages are left out: their HTML can mention "limit" for unrelated reasons.
_BODY_PHRASE_STATUSES = (200, 402, 403)

# Prowlarr proxies every indexer through one host as [/<urlbase>]/<indexerId>/download?...
_PROWLARR_PATH_RE = re.compile(r'/(\d+)/download\b')


def _indexer_key(url: str) -> str:
    """The indexer behind an NZB URL: its host, plus Prowlarr's per-indexer id."""
    try:
        parsed = urlparse(url)
    except Exception:
        return ''
    host = (parsed.netloc or '').lower()
    m = _PROWLARR_PATH_RE.search(parsed.path or '')
    return f'{host}/{m.group(1)}' if host and m else host


def _retry_after_seconds(headers: Optional[Mapping]) -> Optional[int]:
    """Retry-After (delta-seconds or HTTP-date), clamped to 1-60 minutes."""
    if not headers:
        return None
    try:
        value = headers.get('Retry-After') or headers.get('retry-after')
    except Exception:
        return None
    if not value:
        return None
    value = str(value).strip()
    try:
        seconds = int(value)
    except ValueError:
        try:
            seconds = int(parsedate_to_datetime(value).timestamp() - time.time())
        except Exception:
            return None
    return max(_RETRY_AFTER_MIN, min(_RETRY_AFTER_MAX, seconds))


def _newznab_error_is_limit(body: str) -> Optional[bool]:
    """True/False for a newznab <error>, None when the body has none."""
    m = _NEWZNAB_ERROR_RE.search(body or '')
    if not m:
        return None
    attrs = {k.lower(): v for k, v in _ATTR_RE.findall(m.group(1))}
    if attrs.get('code', '').strip() in _NEWZNAB_LIMIT_CODES:
        return True
    return bool(_LIMIT_PHRASE_RE.search(attrs.get('description', '')))


def classify_refusal(status_code: int, headers: Optional[Mapping], body: str) -> Optional[int]:
    """Cooldown in seconds when the response means the indexer is refusing downloads, else None.

    Call only for a response that is not an NZB.
    """
    body = (body or '')[:2000]
    retry_after = _retry_after_seconds(headers)
    newznab_limit = _newznab_error_is_limit(body)

    if status_code == 429:
        return retry_after or COOLDOWN_SECONDS
    if newznab_limit is True:
        return retry_after or COOLDOWN_SECONDS
    if status_code == 503:
        if newznab_limit is None and _LIMIT_PHRASE_RE.search(body):
            return retry_after or COOLDOWN_SECONDS
        return retry_after or UNAVAILABLE_COOLDOWN_SECONDS
    if newznab_limit is None and status_code in _BODY_PHRASE_STATUSES and _LIMIT_PHRASE_RE.search(body):
        return retry_after or COOLDOWN_SECONDS
    return None


def record_indexer_limit(url: str, seconds: int = COOLDOWN_SECONDS) -> None:
    """Put the indexer serving this NZB URL on cooldown for `seconds`."""
    key = _indexer_key(url)
    if key:
        with _lock:
            until = time.monotonic() + seconds
            # A longer cooldown already in place (e.g. a limit) is never shortened.
            if until > _until.get(key, 0):
                _until[key] = until


def cooldown_reason(url: str) -> Optional[str]:
    """Why a fresh download from this URL's indexer should wait, or None."""
    key = _indexer_key(url)
    if not key:
        return None
    with _lock:
        until = _until.get(key)
        if until is None:
            return None
        remaining = until - time.monotonic()
        if remaining <= 0:
            del _until[key]
            return None
    return f'indexer {key} is refusing downloads (cooling down, {max(1, int(remaining // 60))} min left)'


_skip_logged: dict = {}


def first_skip_this_window(item_id, url: str) -> bool:
    """True the first time an item skips this URL's indexer in its current cooldown.

    The Adding queue retries every tick while an indexer cools down; this keeps
    the "skipping" message to one line per item per cooldown window.
    """
    key = _indexer_key(url)
    with _lock:
        until = _until.get(key)
        if until is None:
            return True
        if _skip_logged.get((item_id, key)) == until:
            return False
        _skip_logged[(item_id, key)] = until
        if len(_skip_logged) > 10000:
            _skip_logged.clear()
        return True


def reset_fetch_cooldowns() -> None:
    """Test/debug helper: clear all cooldowns."""
    with _lock:
        _until.clear()
        _skip_logged.clear()
