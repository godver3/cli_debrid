"""In-flight request coalescing shared by scrapers with an ID-independent
sub-query (Newznab, Prowlarr).

These scrapers always fire both an ID-based and a title-based query per
search, and `scraper.py` separately fans a single episode/movie search out
across several title variants (translated title, anime romanized alias,
aliases) run concurrently. Because the ID-based query doesn't depend on
title at all, two of those concurrent title-variant threads end up issuing
the exact same ID-based request to the same indexer within milliseconds of
each other — racing past each scraper's own result cache, since that cache
is only populated *after* a request completes.

SingleFlightGuard closes that race: the first caller for a given key runs
the real work; any other caller for the same key that shows up while it's
still running waits for that result instead of repeating the work itself.
It intentionally has no opinion on a scraper's own caching policy (TTL,
whether errors get cached, etc.) — `fn` is expected to already implement
that end to end. This only prevents concurrent duplicate execution of `fn`
for the same key.
"""
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# How long a leader's result stays available for a waiter that wakes up
# late (e.g. scheduling jitter after event.set()). Not a results cache —
# just a grace window so a slow-to-wake waiter doesn't miss a result that
# was already produced for it.
_RESULT_GRACE_SECONDS = 10


class SingleFlightGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: Dict[str, threading.Event] = {}
        self._results: Dict[str, Tuple[float, Any]] = {}

    def _prune_results_locked(self, now: float) -> None:
        stale_keys = [k for k, (ts, _) in self._results.items() if now - ts > _RESULT_GRACE_SECONDS]
        for k in stale_keys:
            del self._results[k]

    def call(
        self,
        key: str,
        fn: Callable[[], Any],
        wait_timeout: Optional[float] = None,
        copy_fn: Optional[Callable[[Any], Any]] = None,
    ) -> Any:
        """
        Run fn() for this key, or wait for and reuse a concurrent caller's
        result if one is already running fn() for the same key.

        copy_fn, if given, is applied to a result handed to a *waiter* (not
        to the leader's own return value) — use it when fn() returns a
        mutable object that downstream code modifies in place, so a waiter
        never shares mutable state with the leader or other waiters.
        """
        with self._lock:
            now = time.monotonic()
            self._prune_results_locked(now)
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                am_leader = True
            else:
                am_leader = False

        if am_leader:
            try:
                result = fn()
                with self._lock:
                    self._results[key] = (time.monotonic(), result)
                return result
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                event.set()

        event.wait(timeout=wait_timeout)
        with self._lock:
            entry = self._results.get(key)
        if entry is not None:
            value = entry[1]
            return copy_fn(value) if copy_fn else value
        # The leader's call errored before storing a result, or we timed out
        # waiting for it — fall back to doing the work ourselves rather than
        # returning nothing.
        return fn()
