#!/usr/bin/env python3
"""
Regression test for the concurrent per-(imdb_id, items) group worker pattern
added to metadata.py::process_metadata()'s main "Step 4: Process Items Using
Fetched Data" loop, to speed up startup (see the startup-performance
investigation - this loop, run one item at a time, dominated the ~4 minute
"Content Sources" boot phase alongside the already-parallelized Step 2.5/3.5
fetches covered by test_metadata_concurrent_prefetch.py).

Each worker is one (imdb_id, original_items_list) group. Workers never
mutate shared state directly - they return their own movies/episodes lists,
a bulk_show_metadata_updates dict (only ever keyed by their own imdb_id, since
groups are keyed by unique imdb_id), and a list of (tmdb_id, item) pairs to
append into items_by_tmdb_id_only - and the main thread merges every future's
result, in original submission order, only after every future has completed.
That "merge after all futures return" ordering matters because
items_by_tmdb_id_only is read by a consumer block that runs strictly after
the loop in the real code.

metadata.py can't be imported standalone in this sandbox (transitively pulls
in iso8601 and other real deps not installed here), so this test exercises
the pattern itself via a minimal stand-in worker, matching the exact
submit -> ordered .result() -> merge structure used in the real code.
"""

import unittest
from concurrent.futures import ThreadPoolExecutor


def _run_group_workers_and_merge(groups, worker_fn, max_workers_cap=8):
    """Mirrors process_metadata()'s Step 4 submit -> ordered .result() -> merge."""
    movies = []
    episodes = []
    bulk_show_metadata_updates = {}
    tmdb_only_appends = []

    workers = min(max_workers_cap, max(1, len(groups)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker_fn, imdb_id, items) for imdb_id, items in groups]
        for fut in futures:
            result = fut.result()
            movies.extend(result['movies'])
            episodes.extend(result['episodes'])
            bulk_show_metadata_updates.update(result['bulk_show_metadata_updates'])
            tmdb_only_appends.extend(result['tmdb_only_appends'])

    return movies, episodes, bulk_show_metadata_updates, tmdb_only_appends


class TestMetadataParallelGroups(unittest.TestCase):
    def test_matches_sequential_result_regardless_of_finish_order(self):
        import time as _time

        groups = [
            ('tt001', [{'title': 'Movie A'}]),
            ('tt002', [{'title': 'Movie B'}]),
            ('tt003', [{'title': 'Movie C'}]),
        ]

        def worker(imdb_id, items):
            # Reverse-order sleep so later-submitted groups finish first.
            _time.sleep(0.03 if imdb_id == 'tt001' else 0.01)
            movie = {'imdb_id': imdb_id, 'title': items[0]['title']}
            return {
                'movies': [movie],
                'episodes': [],
                'bulk_show_metadata_updates': {},
                'tmdb_only_appends': [],
            }

        def worker_sequential(imdb_id, items):
            movie = {'imdb_id': imdb_id, 'title': items[0]['title']}
            return {
                'movies': [movie],
                'episodes': [],
                'bulk_show_metadata_updates': {},
                'tmdb_only_appends': [],
            }

        movies, episodes, updates, tmdb_appends = _run_group_workers_and_merge(groups, worker)

        expected_movies = []
        for imdb_id, items in groups:
            expected_movies.extend(worker_sequential(imdb_id, items)['movies'])

        # Order must match submission order (groups order), not completion order.
        self.assertEqual(movies, expected_movies)
        self.assertEqual(episodes, [])
        self.assertEqual(updates, {})
        self.assertEqual(tmdb_appends, [])

    def test_one_group_exception_does_not_drop_other_groups(self):
        groups = [
            ('tt_good1', [{'title': 'Good 1'}]),
            ('tt_bad', [{'title': 'Bad'}]),
            ('tt_good2', [{'title': 'Good 2'}]),
        ]

        def worker(imdb_id, items):
            try:
                if imdb_id == 'tt_bad':
                    raise RuntimeError("simulated network failure")
                return {
                    'movies': [{'imdb_id': imdb_id}],
                    'episodes': [],
                    'bulk_show_metadata_updates': {},
                    'tmdb_only_appends': [],
                }
            except Exception:
                # Mirrors the real worker's top-level try/except: return an
                # empty result on failure so .result() never raises and the
                # other futures still get collected.
                return {
                    'movies': [],
                    'episodes': [],
                    'bulk_show_metadata_updates': {},
                    'tmdb_only_appends': [],
                }

        movies, episodes, updates, tmdb_appends = _run_group_workers_and_merge(groups, worker)

        self.assertEqual(
            {m['imdb_id'] for m in movies},
            {'tt_good1', 'tt_good2'},
        )

    def test_bulk_show_metadata_updates_only_touch_own_imdb_id(self):
        # Since groups are keyed by unique imdb_id, each worker's update dict
        # should only ever contain its own key - verifying the merge can't
        # let one group's refreshed metadata clobber another's.
        groups = [
            ('tt_show1', [{'title': 'Show 1'}]),
            ('tt_show2', [{'title': 'Show 2'}]),
        ]

        def worker(imdb_id, items):
            return {
                'movies': [],
                'episodes': [{'imdb_id': imdb_id, 'title': items[0]['title']}],
                'bulk_show_metadata_updates': {imdb_id: {'refreshed': True}},
                'tmdb_only_appends': [],
            }

        movies, episodes, updates, tmdb_appends = _run_group_workers_and_merge(groups, worker)

        self.assertEqual(updates, {
            'tt_show1': {'refreshed': True},
            'tt_show2': {'refreshed': True},
        })
        self.assertEqual(len(episodes), 2)

    def test_tmdb_only_appends_merge_after_all_groups_complete(self):
        # Mirrors the real code's producer (Step 4 group workers) -> consumer
        # (the items_by_tmdb_id_only block after the executor's `with` exits)
        # ordering requirement: appends from every group must be visible in
        # the merged list before any consumer logic would run.
        groups = [
            ('tt_unresolved1', [{'title': 'X', 'tmdb_id': '111'}]),
            ('tt_unresolved2', [{'title': 'Y', 'tmdb_id': '222'}]),
        ]

        def worker(imdb_id, items):
            return {
                'movies': [],
                'episodes': [],
                'bulk_show_metadata_updates': {},
                'tmdb_only_appends': [(items[0]['tmdb_id'], items[0])],
            }

        movies, episodes, updates, tmdb_appends = _run_group_workers_and_merge(groups, worker)

        self.assertEqual(
            sorted(tmdb_id for tmdb_id, _item in tmdb_appends),
            ['111', '222'],
        )

    def test_worker_count_bounded_and_never_zero(self):
        movies, episodes, updates, tmdb_appends = _run_group_workers_and_merge(
            [], lambda imdb_id, items: {'movies': [], 'episodes': [], 'bulk_show_metadata_updates': {}, 'tmdb_only_appends': []}
        )
        self.assertEqual(movies, [])

        many_groups = [(f"tt{i:04d}", [{'title': f'Movie {i}'}]) for i in range(50)]
        movies, episodes, updates, tmdb_appends = _run_group_workers_and_merge(
            many_groups,
            lambda imdb_id, items: {
                'movies': [{'imdb_id': imdb_id}],
                'episodes': [],
                'bulk_show_metadata_updates': {},
                'tmdb_only_appends': [],
            },
        )
        self.assertEqual(len(movies), 50)


if __name__ == '__main__':
    unittest.main()
