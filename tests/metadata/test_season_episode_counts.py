"""get_all_season_episode_counts counts episodes, not version rows.

Every requested or fallback version is its own media_items row. Counting rows
inflated a season (104 rows for Dragon Ball Kai's 98-episode season 1) and
shifted absolute episode numbers for every later season.
"""
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


class SeasonEpisodeCountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other tests leave stub database modules in sys.modules; use the real ones.
        cls._modules = mock.patch.dict(sys.modules)
        cls._modules.start()
        for name in list(sys.modules):
            if name == 'database' or name.startswith('database.'):
                del sys.modules[name]
        global database_reading
        database_reading = importlib.import_module('database.database_reading')

    @classmethod
    def tearDownClass(cls):
        cls._modules.stop()

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.remove, self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE media_items (id INTEGER PRIMARY KEY, tmdb_id TEXT, type TEXT, version TEXT, '
                     'season_number INT, episode_number INT, release_date TEXT, airtime TEXT, episode_title TEXT)')
        rows = [('61709', 'episode', '1080p', 1, ep) for ep in range(1, 99)]
        rows += [('61709', 'episode', '720 and below', 1, ep) for ep in (29, 31, 46)]
        rows += [('61709', 'episode', '2160p', 1, ep) for ep in range(1, 99)]
        rows += [('61709', 'episode', '1080p', 2, ep) for ep in range(1, 62)]
        conn.executemany('INSERT INTO media_items (tmdb_id, type, version, season_number, episode_number) '
                         'VALUES (?,?,?,?,?)', rows)
        conn.commit()
        conn.close()

        def _connect():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        patcher = mock.patch.object(database_reading, 'get_db_connection', _connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_counts_distinct_episodes(self):
        self.assertEqual(database_reading.get_all_season_episode_counts('61709'), {1: 98, 2: 61})


if __name__ == '__main__':
    unittest.main()
