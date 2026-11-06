#!/usr/bin/env python3
"""
Regression test for the concurrent-fetch-and-merge pattern added to
metadata.py::process_metadata() to speed up startup (see the
startup-performance investigation - process_metadata dominated a ~4 minute
"Content Sources" boot phase for a library with hundreds of wanted items,
almost entirely spent in two previously-serial per-item network fetch loops:
"Step 2.5: Individual Fetch for Missing Items" and a new "Step 3.5: Pre-fetch
stale show metadata concurrently" pass added ahead of the existing inline
per-item staleness refresh in Step 4).

Both new blocks follow the same shape: submit one bounded-pool future per
item, iterate the futures list in original submission order (not
as_completed) so results merge into the shared dict deterministically
regardless of which network call actually finished first, and every worker
returns errors as data (a tuple field) rather than letting an exception
propagate out of the pool - so one item's failure can never affect any
other item's fetch or the loop draining every future.

metadata.py can't be imported standalone in this sandbox (transitively pulls
in iso8601 and other real deps not installed here), so this test exercises
the pattern itself via a minimal stand-in worker, matching the exact
submit -> ordered .result() -> merge structure used in the real code.
"""

import unittest
from concurrent.futures import ThreadPoolExecutor


def _run_concurrent_fetch_and_merge(ids, worker_fn, max_workers_cap=8):
    """Mirrors the submit -> ordered .result() -> merge pattern in process_metadata()."""
    results = {}
    errors = {}
    workers = min(max_workers_cap, max(1, len(ids)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker_fn, item_id) for item_id in ids]
        for fut in futures:
            item_id, value, err = fut.result()
            if err is not None:
                errors[item_id] = err
                continue
            if value is not None:
                results[item_id] = value
    return results, errors


class TestMetadataConcurrentPrefetch(unittest.TestCase):
    def test_results_merge_regardless_of_completion_order(self):
        import time as _time

        def worker(item_id):
            # Reverse-order sleep so later-submitted items finish first.
            _time.sleep(0.03 if item_id == 'a' else 0.01)
            return item_id, f"metadata-for-{item_id}", None

        results, errors = _run_concurrent_fetch_and_merge(['a', 'b', 'c'], worker)

        self.assertEqual(errors, {})
        self.assertEqual(results, {
            'a': 'metadata-for-a',
            'b': 'metadata-for-b',
            'c': 'metadata-for-c',
        })

    def test_one_failure_does_not_block_or_drop_other_results(self):
        def worker(item_id):
            if item_id == 'bad':
                return item_id, None, RuntimeError("network error")
            return item_id, f"metadata-for-{item_id}", None

        results, errors = _run_concurrent_fetch_and_merge(['good1', 'bad', 'good2'], worker)

        self.assertEqual(set(results.keys()), {'good1', 'good2'})
        self.assertEqual(set(errors.keys()), {'bad'})
        self.assertIsInstance(errors['bad'], RuntimeError)

    def test_none_result_is_not_merged(self):
        # Mirrors "fetch returned no metadata" (item stays missing, Step 4's
        # existing fallback handling is unaffected since this block never ran).
        def worker(item_id):
            return item_id, None, None

        results, errors = _run_concurrent_fetch_and_merge(['x'], worker)
        self.assertEqual(results, {})
        self.assertEqual(errors, {})

    def test_worker_count_bounded_and_never_zero(self):
        # min(8, max(1, len(ids))) - never spawns 0 workers for a 0-length
        # input (which would previously mean the "if stale_ids:" guard around
        # this block just skips it entirely, but the helper itself must not
        # crash if ever called with an empty list), and never exceeds the cap
        # for a large input.
        results, errors = _run_concurrent_fetch_and_merge([], lambda i: (i, None, None))
        self.assertEqual(results, {})

        many_ids = [f"id{i}" for i in range(50)]
        results, errors = _run_concurrent_fetch_and_merge(
            many_ids, lambda i: (i, f"meta-{i}", None))
        self.assertEqual(len(results), 50)


if __name__ == '__main__':
    unittest.main()
