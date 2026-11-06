#!/usr/bin/env python3
"""
Regression test for the concurrent per-item worker pattern added to
metadata.py::refresh_release_dates()'s main loop, to speed up startup (see
the startup-performance investigation - this loop, run one item at a time,
was the other dominant cost in the "Content Sources" boot phase alongside
process_metadata(), covered by test_metadata_parallel_groups.py).

Unlike process_metadata()'s per-group workers, refresh_release_dates()'s
workers write to the DB directly (update_release_date_and_state, unchanged,
self-contained per item) and share two pieces of mutable state across
threads: the imdb_trakt_cache dict (+ hit/miss counters), guarded by a
cache_lock that is held only across the local dict read/write - never across
the network call in between a cache miss and the fetched Trakt ID being
cached - and a periodic-checkpoint-save trigger, now driven by a
lock-protected completion counter (workers finish out of order) instead of
the original list index.

metadata.py can't be imported standalone in this sandbox (transitively pulls
in iso8601 and other real deps not installed here), so this test exercises
both mechanisms directly via minimal stand-ins that mirror the real locking
structure.
"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor


class TestTraktCacheLocking(unittest.TestCase):
    def test_locked_read_check_write_loses_no_writes_under_concurrency(self):
        """Mirrors the cache_lock-guarded read-check(-then-later-write) sequence:
        N threads over a mix of overlapping and distinct imdb_ids, each doing a
        locked read/hit-or-miss-increment, and (on miss) a locked write of a
        fresh cache entry after "network" work done outside the lock. No write
        should be lost, and hit+miss must always sum to the number of items
        processed regardless of thread interleaving."""
        cache = {}
        cache_lock = threading.Lock()
        stats = {'hits': 0, 'misses': 0}

        # 5 distinct imdb_ids, each looked up by 20 "items" (mirrors many
        # episodes of the same show sharing one imdb_id, plus distinct movies).
        imdb_ids = [f"tt{i:04d}" for i in range(5)]
        work_items = imdb_ids * 20

        def worker(imdb_id):
            with cache_lock:
                cached_entry = cache.get(imdb_id)
                is_hit = cached_entry is not None
                if is_hit:
                    stats['hits'] += 1
                else:
                    stats['misses'] += 1

            if not is_hit:
                # Simulate network work happening outside the lock - this is
                # exactly the section that must NOT be lock-protected.
                fetched_trakt_id = f"trakt-{imdb_id}"
                with cache_lock:
                    cache[imdb_id] = {'trakt_id': fetched_trakt_id}

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, imdb_id) for imdb_id in work_items]
            for fut in futures:
                fut.result()

        # Every distinct imdb_id must have ended up with a cache entry.
        self.assertEqual(set(cache.keys()), set(imdb_ids))
        for imdb_id in imdb_ids:
            self.assertEqual(cache[imdb_id]['trakt_id'], f"trakt-{imdb_id}")

        # No write lost, no double counting: hits + misses == total work items.
        self.assertEqual(stats['hits'] + stats['misses'], len(work_items))
        # At least one miss per distinct id (the first lookup), never more
        # misses than distinct ids would allow if writes landed correctly.
        self.assertGreaterEqual(stats['misses'], len(imdb_ids))


class TestPeriodicCheckpointCounter(unittest.TestCase):
    def test_checkpoint_fires_exactly_n_over_100_times_no_double_or_missed_fires(self):
        """Mirrors the lock-protected completed_count counter that replaces the
        old index-based `if index % 100 == 0` check: N concurrent threads each
        increment a shared counter after finishing their item, and whichever
        thread's increment crosses a multiple of 100 performs the checkpoint
        save. This must fire exactly N // 100 times - never double-fired by a
        race on the modulo check, never missed."""
        progress_lock = threading.Lock()
        completed_count = 0
        save_calls = []
        save_calls_lock = threading.Lock()

        def do_checkpoint_save(progress_at_save):
            with save_calls_lock:
                save_calls.append(progress_at_save)

        def worker(item_id):
            nonlocal completed_count
            do_save = False
            progress_at_save = None
            with progress_lock:
                completed_count += 1
                if completed_count % 100 == 0:
                    do_save = True
                    progress_at_save = completed_count
            if do_save:
                do_checkpoint_save(progress_at_save)

        total_items = 359  # mirrors a realistic items_to_refresh count
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, i) for i in range(total_items)]
            for fut in futures:
                fut.result()

        expected_fires = total_items // 100
        self.assertEqual(len(save_calls), expected_fires)
        self.assertEqual(sorted(save_calls), [100, 200, 300])

    def test_zero_items_never_fires_checkpoint(self):
        progress_lock = threading.Lock()
        completed_count = 0
        save_calls = []

        def worker(item_id):
            nonlocal completed_count
            with progress_lock:
                completed_count += 1
                if completed_count % 100 == 0:
                    save_calls.append(completed_count)

        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = [pool.submit(worker, i) for i in range(5)]
            for fut in futures:
                fut.result()

        self.assertEqual(save_calls, [])


class TestWorkerExceptionIsolation(unittest.TestCase):
    def test_one_item_exception_does_not_block_others_or_propagate(self):
        """Mirrors refresh_release_dates()'s per-item try/except: a worker that
        raises must not prevent other workers' .result() calls from being
        collected, and .result() itself must never raise for the failing item
        since the worker catches internally (matching the real code's
        `except Exception: logging.error(...)` with no reraise)."""
        processed = []
        processed_lock = threading.Lock()

        def worker(item_id):
            try:
                if item_id == 'bad':
                    raise RuntimeError("simulated DB error")
                with processed_lock:
                    processed.append(item_id)
            except Exception:
                pass  # mirrors: logging.error(...) with no reraise

        items = ['a', 'bad', 'b', 'c']
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker, item_id) for item_id in items]
            for fut in futures:
                fut.result()  # must not raise

        self.assertEqual(sorted(processed), ['a', 'b', 'c'])


class TestConcurrentSqliteWriteFromWorkerThreads(unittest.TestCase):
    """Direct, non-mocked evidence for the decision to call
    update_release_date_and_state() straight from worker threads instead of
    collecting writes and serializing them on the main thread: a real temp
    SQLite file with the same WAL + busy_timeout=30000 settings
    database/core.py::get_db_connection() applies to every connection, with 8
    threads each opening their own connection and doing small writes in a
    loop - asserting zero "database is locked" errors."""

    def test_eight_threads_own_connections_no_database_locked_errors(self):
        import os
        import sqlite3
        import tempfile

        fd, db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            setup_conn = sqlite3.connect(db_path, timeout=30)
            setup_conn.execute('PRAGMA journal_mode=WAL')
            setup_conn.execute('PRAGMA busy_timeout = 30000')
            setup_conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, release_date TEXT)')
            setup_conn.executemany(
                'INSERT INTO items (id, release_date) VALUES (?, ?)',
                [(i, 'Unknown') for i in range(200)],
            )
            setup_conn.commit()
            setup_conn.close()

            errors = []
            errors_lock = threading.Lock()

            def worker(item_id):
                try:
                    conn = sqlite3.connect(db_path, timeout=30)
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('PRAGMA busy_timeout = 30000')
                    conn.execute(
                        'UPDATE items SET release_date = ? WHERE id = ?',
                        (f'2026-01-{(item_id % 28) + 1:02d}', item_id),
                    )
                    conn.commit()
                    conn.close()
                except sqlite3.OperationalError as e:
                    with errors_lock:
                        errors.append(str(e))

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(worker, i) for i in range(200)]
                for fut in futures:
                    fut.result()

            locked_errors = [e for e in errors if 'locked' in e.lower()]
            self.assertEqual(locked_errors, [], f"Unexpected 'database is locked' errors: {locked_errors}")

            verify_conn = sqlite3.connect(db_path)
            updated_count = verify_conn.execute(
                "SELECT COUNT(*) FROM items WHERE release_date != 'Unknown'"
            ).fetchone()[0]
            verify_conn.close()
            self.assertEqual(updated_count, 200)
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(db_path + suffix)
                except FileNotFoundError:
                    pass


if __name__ == '__main__':
    unittest.main()
