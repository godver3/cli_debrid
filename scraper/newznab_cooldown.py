"""Per-indexer cooldown for Newznab searches the indexer is refusing.

usenet/nzb_fetch_cooldown.py covers the .nzb download only. Searches had
nothing: a 429 cached an empty result for that one query, and every other title
kept hitting the indexer. With a large list that is thousands of refused
requests a day, which is how users got their indexer accounts banned. Once an
indexer says it is refusing, every search to it is skipped until the cooldown
lifts.

What counts as a refusal (checked 2026-10-24 against Prowlarr/Sonarr's
NewznabRssParser and HttpIndexerBase, and NNTmux's ApiController and
ApiV2Controller, which DrunkenSlug, NZBFinder, NinjaCentral and others run):
- HTTP 429: Retry-After, else one hour (Prowlarr's minimum backoff).
- <error> code 500 (spec and NNTmux "Request limit reached (x/y)"), 501
  (download limit), 429 (nzbgeek style), or limit wording in the description:
  Retry-After, else one hour. NNTmux also uses 500 for "Too many concurrent
  requests, retry shortly", which gets a short wait instead.
- <error> 100/101/102 (wrong key, suspended, not authorised) and 910 (API
  disabled): every request is refused the same way, so one hour. The key
  includes the API key, so a corrected key in settings retries at once.
- HTTP 503: "wait N seconds" when the body says so, an hour for limit wording,
  else five minutes (maintenance or a Cloudflare blip). NZBPlanet (newznab+
  0.2.3p, like altHUB) answers a bare 503 at its grab limit, so back-to-back
  refusals with no stated wait double the cooldown up to an hour; the first
  successful search resets it.
Only error bodies are read. A successful result list is never phrase-scanned,
because release titles ("Limited Series") would match.

Entries live in memory and are lost on restart, like the download cooldown.
"""

import hashlib
import logging
import re
import time
from threading import Lock
from typing import Mapping, Optional, Tuple

from usenet.nzb_fetch_cooldown import (
    _ATTR_RE,
    _LIMIT_PHRASE_RE,
    _NEWZNAB_ERROR_RE,
    _retry_after_seconds,
)

LIMIT_SECONDS = 60 * 60
ACCOUNT_SECONDS = 60 * 60
UNAVAILABLE_SECONDS = 5 * 60
SHORT_WAIT_SECONDS = 60
_SHORT_WAIT_MIN = 5
_SHORT_WAIT_MAX = 5 * 60

_LIMIT_CODES = {'429', '500', '501'}
_ACCOUNT_CODES = {'100', '101', '102', '910'}

# "You must wait 3 seconds", "retry in 2 minutes", NNTmux "retry shortly".
_WAIT_RE = re.compile(
    r'\b(?:wait|retry (?:in|after))\s+(\d+)\s*(s|secs?|seconds?|m|mins?|minutes?)\b',
    re.IGNORECASE,
)
_SHORT_WAIT_RE = re.compile(r'\b(?:concurrent|retry shortly|try again shortly)\b', re.IGNORECASE)

_until: dict = {}
_reason: dict = {}
_strikes: dict = {}
_skip_logged: dict = {}
_lock = Lock()


def indexer_key(endpoint: str, api_key: str) -> str:
    """One cooldown per indexer endpoint and API key."""
    digest = hashlib.sha256((api_key or '').encode()).hexdigest()[:10]
    return f'{(endpoint or "").rstrip("/").lower()}#{digest}'


def _stated_wait(text: str) -> Optional[int]:
    m = _WAIT_RE.search(text or '')
    if not m:
        return None
    seconds = int(m.group(1)) * (60 if m.group(2).lower().startswith('m') else 1)
    return max(_SHORT_WAIT_MIN, min(_SHORT_WAIT_MAX, seconds))


def classify_search_response(status_code: int, headers: Optional[Mapping], body: str) -> Optional[Tuple[int, str]]:
    """(cooldown seconds, short reason) when this search response is a refusal, else None.

    A wait the server stated (Retry-After, "wait N seconds") is in the reason as
    "asked to wait", which record() never escalates.
    """
    body = (body or '')[:2000]
    retry_after = _retry_after_seconds(headers)

    m = _NEWZNAB_ERROR_RE.search(body)
    if m:
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(m.group(1))}
        code = attrs.get('code', '').strip()
        desc = attrs.get('description', '').strip()
        label = f'error {code} "{desc}"' if desc else f'error {code}'
        if retry_after:
            label += ', asked to wait'
        if _SHORT_WAIT_RE.search(desc) or _stated_wait(desc):
            stated = retry_after or _stated_wait(desc)
            if stated and not retry_after:
                label += ', asked to wait'
            return stated or SHORT_WAIT_SECONDS, label
        if code in _LIMIT_CODES or _LIMIT_PHRASE_RE.search(desc):
            return retry_after or LIMIT_SECONDS, label
        if code in _ACCOUNT_CODES:
            return ACCOUNT_SECONDS, label
        # Parameter errors (200-203), no such item (300), unknown (900): not a refusal.
        if status_code not in (429, 503):
            return None

    stated = retry_after or _stated_wait(body)
    if status_code == 429:
        return (stated, 'HTTP 429, asked to wait') if stated else (LIMIT_SECONDS, 'HTTP 429')
    if status_code == 503:
        if stated:
            return stated, 'HTTP 503, asked to wait'
        if _LIMIT_PHRASE_RE.search(body):
            return LIMIT_SECONDS, 'HTTP 503 (limit)'
        return UNAVAILABLE_SECONDS, 'HTTP 503'
    return None


def record(key: str, seconds: int, reason: str) -> Tuple[bool, int]:
    """Start or extend a cooldown: (indexer was not already cooling down, seconds applied).

    A new refusal with no stated wait, before any search has succeeded since the
    last one, doubles the cooldown (capped at an hour).
    """
    now = time.monotonic()
    with _lock:
        current = _until.get(key, 0)
        is_new = current <= now
        if is_new:
            strikes = _strikes.get(key, 0)
            _strikes[key] = strikes + 1
            if 'asked to wait' not in reason and seconds < LIMIT_SECONDS:
                seconds = min(LIMIT_SECONDS, seconds * (2 ** strikes))
        until = now + seconds
        # A longer cooldown already in place is never shortened.
        if until > current:
            _until[key] = until
            _reason[key] = reason
        return is_new, seconds


def note_success(key: str) -> None:
    """A search went through: the next refusal starts from the base cooldown again."""
    with _lock:
        _strikes.pop(key, None)


def remaining(key: str) -> Optional[Tuple[int, str]]:
    """(seconds left, reason) while this indexer is cooling down, else None."""
    with _lock:
        until = _until.get(key)
        if until is None:
            return None
        left = until - time.monotonic()
        if left <= 0:
            _until.pop(key, None)
            _reason.pop(key, None)
            return None
        return int(left), _reason.get(key, '')


def first_skip_this_window(key: str) -> bool:
    """True the first time a search is skipped in this indexer's current cooldown."""
    with _lock:
        until = _until.get(key)
        if until is None:
            return True
        if _skip_logged.get(key) == until:
            return False
        _skip_logged[key] = until
        return True


def describe(seconds: int) -> str:
    if seconds < 120:
        return f'{seconds}s'
    return f'{seconds // 60} min'


def should_skip(instance: str, key: str) -> bool:
    """True (skip the request) while this indexer is refusing searches."""
    left = remaining(key)
    if left is None:
        return False
    if first_skip_this_window(key):
        seconds, reason = left
        logging.info(f"Newznab '{instance}': skipping searches while the indexer cools down "
                     f"after {reason} ({describe(seconds)} left)")
    return True


def note_refusal(instance: str, key: str, response) -> bool:
    """Start a cooldown when this search response is a limit/refusal. True if it was one."""
    if response is None:
        return False
    try:
        hit = classify_search_response(response.status_code, response.headers, response.text)
    except Exception:
        return False
    if not hit:
        return False
    seconds, reason = hit
    is_new, seconds = record(key, seconds, reason)
    if is_new:
        logging.warning(f"Newznab '{instance}' refused a search ({reason}); pausing all searches "
                        f"to it for {describe(seconds)}")
    return True


def reset_search_cooldowns() -> None:
    """Test/debug helper: clear all cooldowns."""
    with _lock:
        _until.clear()
        _reason.clear()
        _strikes.clear()
        _skip_logged.clear()
