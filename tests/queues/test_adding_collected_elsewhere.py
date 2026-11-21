"""A manual grab that reaches Adding before its release id is in memory must
not be "restored" to Collected from the episode's old copy.

Live 2026-11-21 (Dragon Ball Kai S02E03): the manual NZB add set the row to
Adding and wrote its NZB id a moment later. The Adding tick saw no id in its
in-memory copy, found the old (wrong) copy of the same episode + version
Collected, and marked the new row Collected: no health check, no symlink.
"""
import os
import sqlite3
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    source = open(os.path.join(ROOT, 'queues/adding_queue.py'), encoding='utf-8').read()
    ns = {}
    start = source.index('def collected_elsewhere_check')
    end = source.index('\nclass AddingQueue:')
    exec("from typing import Dict, Any, Tuple\n" + source[start:end], ns)
    return ns['collected_elsewhere_check']


check = _load()
EP = {'imdb_id': 'tt1409055', 'season_number': 2, 'episode_number': 3, 'version': '720 and below', 'type': 'episode'}


class CollectedElsewhereTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id, season_number, episode_number, '
                        'version, type, state, filled_by_torrent_id)')
        # The old copy of the episode, still Collected.
        self.db.execute("INSERT INTO media_items VALUES (1, 'tt1409055', 2, 3, '720 and below', 'episode', 'Collected', 'nzb:old')")

    def test_row_with_its_own_release_is_not_restored(self):
        self.db.execute("INSERT INTO media_items VALUES (2, 'tt1409055', 2, 3, '720 and below', 'episode', 'Adding', 'nzb:new')")
        self.assertEqual(check(self.db, dict(EP, id=2)), ('nzb:new', 0))

    def test_row_without_a_release_is_restored(self):
        self.db.execute("INSERT INTO media_items VALUES (2, 'tt1409055', 2, 3, '720 and below', 'episode', 'Adding', NULL)")
        self.assertEqual(check(self.db, dict(EP, id=2)), ('', 1))

    def test_itself_does_not_count(self):
        self.db.execute("DELETE FROM media_items")
        self.db.execute("INSERT INTO media_items VALUES (2, 'tt1409055', 2, 3, '720 and below', 'episode', 'Collected', NULL)")
        self.assertEqual(check(self.db, dict(EP, id=2)), ('', 0))

    def test_other_version_does_not_count(self):
        self.db.execute("INSERT INTO media_items VALUES (2, 'tt1409055', 2, 3, '1080p', 'episode', 'Adding', NULL)")
        self.assertEqual(check(self.db, dict(EP, id=2, version='1080p')), ('', 0))


if __name__ == '__main__':
    unittest.main()
