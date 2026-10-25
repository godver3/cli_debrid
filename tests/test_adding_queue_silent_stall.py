"""Regression: Adding queue announcing N items then processing none (MaddGuru).

NZB rows already submitted to cli_mount used to stay in items_to_process, get
DEBUG-skipped every tick, and announce a phantom "Starting processing of N
available items" with zero per-item handler calls. Scraping then deferred any
sibling of those shows (return False) and aborted its whole batch.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_helpers():
    """Load partition helpers without importing the full AddingQueue stack."""
    path = os.path.join(ROOT, 'queues/adding_queue.py')
    source = open(path, encoding='utf-8').read()
    ns = {}
    stub = (
        "import logging\n"
        "from typing import Dict, List, Optional, Any, Tuple\n"
        "json = __import__('json')\n"
        "time = __import__('time')\n"
        "def get_debrid_provider(): return None\n"
        "def get_setting(*a, **k): return False\n"
        "class TorrentProcessor:\n    def __init__(self, *a, **k): pass\n"
        "class MediaMatcher:\n    def __init__(self, *a, **k): pass\n"
        "def accepts_uncached_now(*a, **k): return False\n"
        "def torrent_has_other_active_owner(*a, **k): return False\n"
        "def remove_unwanted_torrent(*a, **k): pass\n"
        "def cancel_superseded_nzb_job(*a, **k): pass\n"
        "def chosen_result_score_fields(*a, **k): return {}\n"
        "def record_grab(*a, **k): pass\n"
        "def queue_grab_trigger(*a, **k): return 'auto'\n"
    )
    start = source.index('def partition_adding_items')
    end = source.index('\nclass AddingQueue:')
    exec(stub + source[start:end], ns)
    return ns['partition_adding_items'], ns['adding_item_blocks_scraping']


partition_adding_items, adding_item_blocks_scraping = _load_helpers()


class PartitionAddingItemsTests(unittest.TestCase):
    def test_nzb_awaiting_held_aside_not_actionable(self):
        items = [
            {'id': 1, 'filled_by_torrent_id': 'nzb:job1'},
            {'id': 2, 'filled_by_torrent_id': 'nzb:job2'},
            {'id': 3, 'filled_by_torrent_id': ''},
            {'id': 4, 'filled_by_torrent_id': 'RDHASH'},
        ]
        actionable, awaiting, downloading, locked = partition_adding_items(items)
        self.assertEqual([i['id'] for i in actionable], [3, 4])
        self.assertEqual(awaiting, 2)
        self.assertEqual(downloading, 0)
        self.assertEqual(locked, 0)

    def test_downloading_nzb_counted_separately(self):
        items = [
            {'id': 1, 'filled_by_torrent_id': 'nzb:active'},
            {'id': 2, 'filled_by_torrent_id': 'nzb:waiting'},
        ]
        actionable, awaiting, downloading, locked = partition_adding_items(
            items, downloading_job_ids={'active'}
        )
        self.assertEqual(actionable, [])
        self.assertEqual(awaiting, 1)
        self.assertEqual(downloading, 1)

    def test_upgrade_locks_held_aside(self):
        items = [
            {'id': 10, 'filled_by_torrent_id': ''},
            {'id': 11, 'filled_by_torrent_id': ''},
        ]
        actionable, awaiting, downloading, locked = partition_adding_items(
            items, upgrade_locks={10}
        )
        self.assertEqual([i['id'] for i in actionable], [11])
        self.assertEqual(locked, 1)

    def test_all_nzb_awaiting_means_zero_actionable(self):
        """The MaddGuru signature: 73 NZB-awaiting rows → 0 actionable, not '73 available'."""
        items = [{'id': i, 'filled_by_torrent_id': f'nzb:j{i}'} for i in range(73)]
        actionable, awaiting, downloading, locked = partition_adding_items(items)
        self.assertEqual(actionable, [])
        self.assertEqual(awaiting, 73)

    def test_nzb_awaiting_does_not_block_scraping(self):
        self.assertFalse(adding_item_blocks_scraping({'filled_by_torrent_id': 'nzb:abc'}))
        self.assertTrue(adding_item_blocks_scraping({'filled_by_torrent_id': ''}))
        self.assertTrue(adding_item_blocks_scraping({'filled_by_torrent_id': 'RDHASH'}))


class ScrapingDeferralTests(unittest.TestCase):
    """A show deferred behind its Adding sibling must not hold the head of Scraping.

    update() re-sorts by (imdb_id, season, episode) every tick, so moving the
    deferred item to the back of the list was undone on the next tick: a show
    with 5+ episodes at the head used up every batch slot on deferrals.
    """

    def _queue(self, items):
        # The app imports the database package before the queues (main.py does); importing
        # queues.scraping_queue first runs into the app's import cycles, so do the same here.
        import database  # noqa: F401
        from queues.scraping_queue import ScrapingQueue
        q = ScrapingQueue()
        q.items = list(items)
        q._item_ids = {i['id'] for i in items}
        return q

    def test_deferred_items_skipped_even_after_resort(self):
        import time
        show_a = [{'id': n, 'imdb_id': 'tt1', 'season_number': 1, 'episode_number': n} for n in range(1, 7)]
        show_b = [{'id': 100, 'imdb_id': 'tt2', 'season_number': 1, 'episode_number': 1}]
        q = self._queue(show_a + show_b)
        for item in show_a:
            q._deferred_until[item['id']] = time.monotonic() + 60
        q.items.sort(key=lambda i: (i['imdb_id'], i['season_number'], i['episode_number']))
        self.assertEqual(q._next_item()['id'], 100)

    def test_all_deferred_returns_none_and_expiry_releases(self):
        q = self._queue([{'id': 1, 'imdb_id': 'tt1'}])
        q._deferred_until[1] = float('inf')
        self.assertIsNone(q._next_item())
        q._deferred_until[1] = 0  # expired
        self.assertEqual(q._next_item()['id'], 1)
        self.assertEqual(q._deferred_until, {})

    def test_deferrals_for_items_gone_from_queue_are_dropped(self):
        q = self._queue([{'id': 2, 'imdb_id': 'tt1'}])
        q._deferred_until[1] = float('inf')
        self.assertEqual(q._next_item()['id'], 2)
        self.assertNotIn(1, q._deferred_until)


if __name__ == '__main__':
    unittest.main()
