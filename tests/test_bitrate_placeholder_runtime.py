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

from database import database_reading, database_writing
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

    def test_episode_placeholder_metadata_is_unknown(self):
        # Unknown, not a guess: filter_results skips the bitrate check for None.
        self.assertIsNone(self._run('episode', None, 1))
        self.assertIsNone(self._run('episode', None, '1'))
        self.assertIsNone(self._run('episode', None, None))

    def test_movie_placeholder_metadata_is_unknown(self):
        self.assertIsNone(self._run('movie', None, 1))
        self.assertIsNone(self._run('movie', None, 0))

    def test_line_of_fire_bitrate_now_passes(self):
        runtime = self._run('episode', None, 42)
        mbps = file_processing.calculate_bitrate(1.81, runtime) / 1000
        self.assertLess(mbps, 18.0)


class TvdbShowRuntimeTests(unittest.TestCase):
    """Root cause: defaultSeasonType (1 = Aired Order) was used as the runtime fallback."""

    def _runtime(self, raw):
        from cli_battery.app import tvdb_client
        base = {'name': 'Below', 'firstAired': '2026-10-08', 'defaultSeasonType': 1}
        return tvdb_client._build_show_dict({**base, **raw}, 'tt35934755', 1).get('runtime')

    def test_missing_average_runtime_is_none_not_season_type(self):
        self.assertIsNone(self._runtime({'averageRuntime': None}))
        self.assertIsNone(self._runtime({}))

    def test_average_runtime_used(self):
        self.assertEqual(self._runtime({'averageRuntime': 42}), 42)


class RefreshedEpisodeRuntimeTests(unittest.TestCase):
    def _get(self, metadata, season=1, episode=2):
        from metadata.metadata import _refreshed_episode_runtime
        return _refreshed_episode_runtime(metadata, season, episode)

    def test_prefers_episode_runtime(self):
        md = {'runtime': '53', 'seasons': {1: {'episodes': {2: {'runtime': 50}}}}}
        self.assertEqual(self._get(md), 50)

    def test_falls_back_to_show_runtime(self):
        md = {'runtime': '42', 'seasons': {1: {'episodes': {2: {'runtime': 0}}}}}
        self.assertEqual(self._get(md), 42)

    def test_string_keys(self):
        md = {'seasons': {'1': {'episodes': {'2': {'runtime': 44}}}}}
        self.assertEqual(self._get(md), 44)

    def test_placeholder_or_missing_returns_none(self):
        self.assertIsNone(self._get({'runtime': '1', 'seasons': {1: {'episodes': {2: {'runtime': None}}}}}))
        self.assertIsNone(self._get({}))
        self.assertIsNone(self._get(None))


class UpdateReleaseDateRuntimeTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.remove, self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE media_items (id INTEGER PRIMARY KEY, release_date TEXT, state TEXT, '
                     'last_updated TEXT, airtime TEXT, runtime INTEGER)')
        conn.execute("INSERT INTO media_items VALUES (1, '2026-10-08', 'Unreleased', NULL, '21:00', 1)")
        conn.commit()
        conn.close()

        def _connect():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        patcher = mock.patch.object(database_writing, 'get_db_connection', _connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runtime(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT runtime FROM media_items WHERE id = 1').fetchone()[0]
        finally:
            conn.close()

    def test_runtime_written_when_given(self):
        database_writing.update_release_date_and_state(1, '2026-10-08', 'Wanted', runtime=55)
        self.assertEqual(self._runtime(), 55)

    def test_runtime_untouched_when_omitted(self):
        database_writing.update_release_date_and_state(1, '2026-10-08', 'Wanted')
        self.assertEqual(self._runtime(), 1)


class FilterResultsUnknownRuntimeTests(unittest.TestCase):
    """End to end through the real bitrate filter, with min and max bitrate both set."""

    VERSION = {"similarity_threshold": 0.85, "similarity_threshold_anime": 0.8, "max_resolution": "2160p",
               "resolution_wanted": "<=", "min_size_gb": 0.01, "max_size_gb": None,
               "min_bitrate_mbps": 5.0, "max_bitrate_mbps": 18.0}
    TITLE = "Line.of.Fire.2026.S01E01.Pilot.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb"

    def _passes(self, runtime, size=3.06):
        from PTT import parse_title
        from scraper.functions.filter_results import filter_results
        info = parse_title(self.TITLE)
        result = {"title": self.TITLE, "original_title": self.TITLE, "size": size, "parsed_info": info,
                  "scraper_type": "Newznab", "scraper_instance": "NZBGeek_1",
                  "nzb_url": "https://indexer.test/x", "protocol": "nzb"}
        passed, _ = filter_results([result], "321958", "Line of Fire", 2026, "episode", 1, 1, False,
                                   self.VERSION, runtime, 8, {1: 8}, ["Drama"], imdb_id="tt39365612")
        return bool(passed)

    def test_placeholder_runtime_rejected_everything(self):
        self.assertFalse(self._passes(1))  # the original bug: 438 Mbps

    def test_unknown_runtime_skips_bitrate_check(self):
        self.assertTrue(self._passes(None))
        self.assertTrue(self._passes(None, size=0.5))  # would fail min bitrate under any guess

    def test_real_runtime_still_filters(self):
        self.assertTrue(self._passes(42))
        self.assertFalse(self._passes(42, size=0.5))  # 1.6 Mbps < 5 min


if __name__ == '__main__':
    unittest.main()
