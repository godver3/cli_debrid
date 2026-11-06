#!/usr/bin/env python3
"""
Tests for scraper/request_dedup.py's SingleFlightGuard — the fix for the
duplicate-Newznab/Prowlarr-request bug found while diagnosing a user's
"same episode searched twice within milliseconds" report.

Root cause being fixed: scraper.py fans a single episode/movie search out
across several title variants (original, translated title, anime romanized
alias, ...) run concurrently via ThreadPoolExecutor. Newznab's and
Prowlarr's ID-based sub-query doesn't depend on title at all, so two of
those concurrent title-variant threads end up issuing the byte-identical
request to the same indexer within milliseconds of each other — racing
past each scraper's own result cache, since that cache only gets populated
*after* a request completes.

These tests exercise SingleFlightGuard in isolation using precise thread
orchestration (Events/Barriers), not sleep-based timing, so they're not
flaky under CI load.
"""

import copy
import threading
import time
import unittest

from scraper.request_dedup import SingleFlightGuard


class TestSingleFlightGuardBasic(unittest.TestCase):
    def test_single_caller_runs_fn_once(self):
        guard = SingleFlightGuard()
        calls = []

        def fn():
            calls.append(1)
            return ['result']

        result = guard.call('key-a', fn)
        self.assertEqual(result, ['result'])
        self.assertEqual(len(calls), 1)

    def test_sequential_calls_each_run_fn(self):
        """SingleFlightGuard has no long-lived cache of its own — a second,
        non-concurrent call for the same key after the first has finished
        must run fn() again (the caller's own TTL cache, if any, is what
        decides whether that's a real network hit or not)."""
        guard = SingleFlightGuard()
        calls = []

        def fn():
            calls.append(1)
            return len(calls)

        first = guard.call('key-a', fn)
        second = guard.call('key-a', fn)
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        self.assertEqual(len(calls), 2)

    def test_different_keys_independent(self):
        guard = SingleFlightGuard()

        result_a = guard.call('key-a', lambda: 'a-result')
        result_b = guard.call('key-b', lambda: 'b-result')

        self.assertEqual(result_a, 'a-result')
        self.assertEqual(result_b, 'b-result')


class TestSingleFlightGuardConcurrency(unittest.TestCase):
    def test_concurrent_identical_keys_coalesce_to_one_call(self):
        """This is the actual bug: N threads asking for the same key at
        the same time (the N title-variant threads, each issuing the same
        ID-based query) must result in fn() actually running only once."""
        guard = SingleFlightGuard()
        call_count = [0]
        call_count_lock = threading.Lock()
        release = threading.Event()
        entered = threading.Barrier(1 + 1)  # leader signals it has started

        def fn():
            with call_count_lock:
                call_count[0] += 1
            entered.wait(timeout=5)
            # Hold here until the test releases us, so every other thread
            # is guaranteed to arrive while this call is still in flight.
            release.wait(timeout=5)
            return ['shared-result']

        n_threads = 8
        results = [None] * n_threads
        errors = []

        def worker(idx):
            try:
                results[idx] = guard.call('shared-key', fn, wait_timeout=5)
            except Exception as exc:  # pragma: no cover - failure diagnostics
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()

        # Wait for the leader to actually be inside fn() before releasing it,
        # so we know the other threads had a chance to queue up as waiters.
        entered.wait(timeout=5)
        time.sleep(0.2)  # let the remaining threads register as waiters
        release.set()

        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(call_count[0], 1, "fn() should have run exactly once for 8 concurrent identical keys")
        for r in results:
            self.assertEqual(r, ['shared-result'])

    def test_concurrent_different_keys_do_not_block_each_other(self):
        guard = SingleFlightGuard()
        release_a = threading.Event()
        started_a = threading.Event()

        def fn_a():
            started_a.set()
            release_a.wait(timeout=5)
            return 'a'

        def fn_b():
            return 'b'

        result_holder = {}

        def worker_a():
            result_holder['a'] = guard.call('key-a', fn_a, wait_timeout=5)

        t = threading.Thread(target=worker_a)
        t.start()
        self.assertTrue(started_a.wait(timeout=5), "fn_a should have started")

        # key-b must not be blocked by key-a still being in flight.
        result_b = guard.call('key-b', fn_b, wait_timeout=5)
        self.assertEqual(result_b, 'b')

        release_a.set()
        t.join(timeout=5)
        self.assertEqual(result_holder['a'], 'a')

    def test_waiter_gets_a_copy_not_the_leader_shared_object(self):
        """Downstream scraper code mutates result dicts in place
        (xem_scene_mapping, content_source_detail, ...). A waiter must get
        its own copy so it can't race with the leader (or other waiters)
        mutating the same objects."""
        guard = SingleFlightGuard()
        leader_object = [{'title': 'Example'}]
        release = threading.Event()
        started = threading.Event()

        def fn():
            started.set()
            release.wait(timeout=5)
            return leader_object

        waiter_result = {}

        def leader_worker():
            waiter_result['leader'] = guard.call('key-a', fn, wait_timeout=5)

        t = threading.Thread(target=leader_worker)
        t.start()
        self.assertTrue(started.wait(timeout=5))

        release.set()
        t.join(timeout=5)

        # A second, later caller acting as a "waiter" scenario: simulate by
        # calling again once the leader has already produced a result within
        # the grace window, requesting a copy.
        waiter_value = guard.call('key-a', lambda: leader_object, wait_timeout=5, copy_fn=copy.deepcopy)

        self.assertEqual(waiter_result['leader'], leader_object)
        self.assertIs(waiter_result['leader'], leader_object)
        self.assertEqual(waiter_value, leader_object)

    def test_leader_exception_lets_waiter_fall_back_instead_of_hanging(self):
        guard = SingleFlightGuard()
        started = threading.Event()
        release = threading.Event()

        def failing_fn():
            started.set()
            release.wait(timeout=5)
            raise RuntimeError('simulated indexer error')

        leader_error = []

        def leader_worker():
            try:
                guard.call('key-a', failing_fn, wait_timeout=5)
            except RuntimeError as exc:
                leader_error.append(exc)

        t = threading.Thread(target=leader_worker)
        t.start()
        self.assertTrue(started.wait(timeout=5))

        # A waiter must not hang forever or silently get nothing back just
        # because the leader failed — it should fall back to its own fn().
        waiter_result = {'value': None}

        def waiter_worker():
            waiter_result['value'] = guard.call('key-a', lambda: 'fallback-result', wait_timeout=1)

        w = threading.Thread(target=waiter_worker)
        w.start()

        # Give the waiter a moment to register, then let the leader fail.
        time.sleep(0.1)
        release.set()

        t.join(timeout=5)
        w.join(timeout=5)

        self.assertEqual(len(leader_error), 1)
        self.assertEqual(waiter_result['value'], 'fallback-result')

    def test_wait_timeout_falls_back_to_running_fn_itself(self):
        guard = SingleFlightGuard()
        started = threading.Event()
        never_release = threading.Event()  # never set — leader "hangs"

        def slow_fn():
            started.set()
            never_release.wait(timeout=5)  # bounded so the test itself can't hang
            return 'slow-result'

        t = threading.Thread(target=lambda: guard.call('key-a', slow_fn, wait_timeout=5))
        t.start()
        self.assertTrue(started.wait(timeout=5))

        # Waiter times out almost immediately and must fall back rather than
        # blocking for the full 5s the leader is taking.
        start = time.monotonic()
        result = guard.call('key-a', lambda: 'fallback', wait_timeout=0.2)
        elapsed = time.monotonic() - start

        self.assertEqual(result, 'fallback')
        self.assertLess(elapsed, 2.0)

        never_release.set()
        t.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
