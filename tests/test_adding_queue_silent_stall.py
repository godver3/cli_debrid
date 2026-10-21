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


if __name__ == '__main__':
    unittest.main()
