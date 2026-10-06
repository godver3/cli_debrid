"""Placeholder runtimes (TVDB's 1-minute value for unreleased shows) must not
drive the bitrate filter. Repro: Line of Fire (2026) episodes stored runtime=1,
so a 1.81GB 42-minute episode was rejected as 259 Mbps."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import database_reading
from scraper.functions import file_processing


class _DbCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'CREATE TABLE media_items (id INTEGER PRIMARY KEY, tmdb_id TEXT, type TEXT, '
            'season_number INTEGER, episode_number INTEGER, runtime INTEGER)'
        )
        conn.commit()
        conn.close()

        def _connect():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        patcher = mock.patch.object(database_reading, 'get_db_connection', _connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.remove, self.db_path)

    def insert(self, rows):
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            'INSERT INTO media_items (tmdb_id, type, season_number, episode_number, runtime) VALUES (?, ?, ?, ?, ?)',
            rows,
        )
        conn.commit()
        conn.close()


class GetEpisodeRuntimeTests(_DbCase):
    def test_placeholder_only_rows_return_none(self):
        self.insert([('321958', 'episode', 1, e, 1) for e in range(2, 8)]
                    + [('321958', 'episode', 1, 1, None)])
        self.assertIsNone(database_reading.get_episode_runtime('321958'))

    def test_placeholders_excluded_from_average(self):
        self.insert([('1', 'episode', 1, 1, 42), ('1', 'episode', 1, 2, 44), ('1', 'episode', 1, 3, 1)])
        self.assertEqual(database_reading.get_episode_runtime('1'), 43)

    def test_season_zero_specials_excluded(self):
        self.insert([('2', 'episode', 1, 1, 60), ('2', 'episode', 0, 1, 10), ('2', 'episode', 0, 2, 12)])
        self.assertEqual(database_reading.get_episode_runtime('2'), 60)

    def test_movie_placeholder_returns_none(self):
        self.insert([('3', 'movie', None, None, 1)])
        self.assertIsNone(database_reading.get_movie_runtime('3'))


class MediaInfoForBitrateTests(unittest.TestCase):
    def _run(self, media_type, db_runtime, metadata_runtime):
        with mock.patch.object(file_processing, 'get_episode_runtime', return_value=db_runtime), \
             mock.patch.object(file_processing, 'get_movie_runtime', return_value=db_runtime), \
             mock.patch.object(file_processing, 'get_episode_count', return_value=8), \
             mock.patch('metadata.metadata.get_metadata', return_value={'runtime': metadata_runtime, 'seasons': {}}):
            item = {'title': 'x', 'media_type': media_type, 'tmdb_id': '321958'}
            return file_processing.get_media_info_for_bitrate([item])[0]['runtime']

    def test_episode_falls_back_to_show_metadata_runtime(self):
        self.assertEqual(self._run('episode', None, 42), 42)

    def test_episode_placeholder_metadata_uses_default(self):
        self.assertEqual(self._run('episode', None, 1), 30)
        self.assertEqual(self._run('episode', None, '1'), 30)

    def test_movie_placeholder_metadata_uses_default(self):
        self.assertEqual(self._run('movie', None, 1), 100)

    def test_line_of_fire_bitrate_now_passes(self):
        runtime = self._run('episode', None, 42)
        mbps = file_processing.calculate_bitrate(1.81, runtime) / 1000
        self.assertLess(mbps, 18.0)


if __name__ == '__main__':
    unittest.main()
