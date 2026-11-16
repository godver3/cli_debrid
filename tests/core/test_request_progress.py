"""utilities/request_progress.py: the command bar's simplified per-request log feed."""
import os
import tempfile
import unittest

from utilities import request_progress as rp

_LINES = [
    "2026-11-15 10:11:40,000 - queue_manager.py:_move_item_to_queue:916 - INFO - [item 7] episode_X_tt1_S01E01_1080p: Wanted -> Scraping",
    "2026-11-15 10:11:43,117 - queue_manager.py:_move_item_to_queue:916 - INFO - [item 1] episode_X_tt1_S01E01_1080p: Wanted -> Scraping",
    "2026-11-15 10:11:43,438 - scraper_manager.py:run_scraper:196 - INFO - [item 1] Scraper Torrentio_1 (Torrentio) call took 0.16s, found 70 results.",
    "2026-11-15 10:11:44,262 - scraper_manager.py:run_scraper:196 - INFO - [item 1] Scraper Jackett_1 (Jackett) call took 0.99s, found 30 results.",
    "2026-11-15 10:11:46,426 - filter_results.py:filter_results:1042 - INFO - [item 1] Rejected: Resolution '2160p' doesn't match",
    "2026-11-15 10:11:46,931 - queue_manager.py:_move_item_to_queue:916 - INFO - [item 1] episode_X_tt1_S01E01_1080p: Scraping -> Adding after 3s in Scraping",
    "2026-11-15 10:11:58,811 - torrent_processor.py:_process_nzb_result:1366 - INFO - [item 1] [X] Submitting NZB to cli_mount: X.S01E01.1080p-GRP",
    "2026-11-15 10:11:59,802 - torrent_processor.py:_process_nzb_result:1372 - WARNING - [item 1] [X] cli_mount server missing segments for 'X.S01E01.1080p-GRP' — adding NZB URL",
    "2026-11-15 10:12:02,351 - torrent_processor.py:_process_nzb_result:1424 - INFO - [item 1] [X] NZB submitted successfully, job_id=abc",
    "2026-11-15 10:12:51,563 - log_context.py:log_state_change:123 - INFO - [item 1] episode_X_tt1_S01E01_1080p: Checking -> Wanted",
    "2026-11-15 10:12:51,565 - queue_manager.py:_move_item_to_queue:916 - INFO - [item 1] episode_X_tt1_S01E01_1080p: Checking -> Wanted after 18s in Checking",
    "2026-11-15 10:13:00,000 - queue_manager.py:_move_item_to_queue:916 - DEBUG - [item 1] episode_X: Wanted -> Scraping",
    "2026-11-15 10:14:11,000 - log_context.py:log_state_change:123 - INFO - [item 1] episode_X_tt1_S01E01_1080p: Checking -> Collected",
]
_ITEMS = [{'id': 1, 'type': 'episode', 'season_number': 1, 'episode_number': 1, 'version': '1080p', 'state': 'Collected'}]


class RequestProgressTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.log')
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(_LINES) + '\n')
        self.addCleanup(os.remove, self.path)

    def events(self, since='2026-11-15 10:11:41,000', items=_ITEMS):
        return [e['text'] for e in rp.events_for(items, since, self.path)]

    def test_feed_is_short_and_readable(self):
        self.assertEqual(self.events(), [
            'Wanted → Scraping',
            'searched 2 sources: 100 results',
            'Scraping → Adding (3s)',
            'trying NZB X.S01E01.1080p-GRP',
            '✗ missing articles: X.S01E01.1080p-GRP',
            'NZB accepted, downloading',
            'Checking → Wanted: download rejected, searching again',
            'Checking → Collected: ✓ in your library',
        ])

    def test_other_items_and_older_lines_are_left_out(self):
        self.assertNotIn('Wanted → Scraping', self.events(since='2026-11-15 10:11:44,000'))
        self.assertEqual(self.events(items=[dict(_ITEMS[0], id=99)]), [])

    def test_many_items_skip_per_search_detail(self):
        items = _ITEMS + [dict(_ITEMS[0], id=i, episode_number=i) for i in range(2, 6)]
        texts = self.events(items=items)
        self.assertFalse(any(t.startswith(('searched', 'trying')) for t in texts))
        self.assertIn('✗ missing articles: X.S01E01.1080p-GRP', texts)

    def test_long_show_keeps_only_outcomes(self):
        items = _ITEMS + [dict(_ITEMS[0], id=i, episode_number=i) for i in range(100, 125)]
        texts = self.events(items=items)
        self.assertNotIn('Wanted → Scraping', texts)
        self.assertIn('Checking → Collected: ✓ in your library', texts)
        self.assertIn('Checking → Wanted: download rejected, searching again', texts)

    def test_episode_labels(self):
        evs = rp.events_for(_ITEMS, '2026-11-15 10:11:41,000', self.path)
        self.assertEqual({e['item'] for e in evs}, {'S01E01'})

    def test_summary(self):
        self.assertTrue(rp.summarize(_ITEMS)['done'])
        self.assertFalse(rp.summarize([dict(_ITEMS[0], state='Checking')])['done'])
        self.assertFalse(rp.summarize([])['done'])

    def test_timestamps(self):
        self.assertTrue(rp.valid_ts(rp.now_ts()))
        self.assertFalse(rp.valid_ts("2026-11-15' OR 1=1"))


if __name__ == '__main__':
    unittest.main()
