#!/usr/bin/env python3
"""
Contract tests for Bazarr Sonarr series-id consistency.

Loads production helpers from routes/bazarr_spoofing_routes.py without
importing routes/__init__.py (avoids full Flask app bootstrap).
"""

import importlib.util
import os
import sys
import types
import unittest
import unittest.mock
from unittest.mock import MagicMock


def _load_spoofing_module():
    """Import bazarr_spoofing_routes.py directly with light dependency stubs."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    # Stub heavy deps before loading the module file
    flask_stub = MagicMock()
    flask_stub.Blueprint = MagicMock(return_value=MagicMock())
    flask_stub.jsonify = MagicMock()
    flask_stub.request = MagicMock()
    flask_stub.Response = MagicMock()
    sys.modules['flask'] = flask_stub

    if 'utilities.settings' not in sys.modules:
        settings_mod = types.ModuleType('utilities.settings')
        settings_mod.get_setting = MagicMock(return_value='')
        settings_mod.set_setting = MagicMock()
        settings_mod.load_config = MagicMock(return_value={})
        sys.modules['utilities.settings'] = settings_mod
        # Ensure parent package exists
        if 'utilities' not in sys.modules:
            utilities_pkg = types.ModuleType('utilities')
            utilities_pkg.__path__ = [os.path.join(root, 'utilities')]
            sys.modules['utilities'] = utilities_pkg

    if 'utilities.release_parser' not in sys.modules:
        rp = types.ModuleType('utilities.release_parser')

        class _ReleaseParser:
            @staticmethod
            def parse_with_guessit(_s):
                return {}

            @staticmethod
            def parse_with_regex(_s):
                return {}

            @staticmethod
            def extract_release_group(_s):
                return ''

        rp.ReleaseParser = _ReleaseParser
        sys.modules['utilities.release_parser'] = rp

    if 'database.core' not in sys.modules:
        if 'database' not in sys.modules:
            db_pkg = types.ModuleType('database')
            db_pkg.__path__ = [os.path.join(root, 'database')]
            sys.modules['database'] = db_pkg
        db_core = types.ModuleType('database.core')
        db_core.get_db_connection = MagicMock()
        sys.modules['database.core'] = db_core

    module_path = os.path.join(root, 'routes', 'bazarr_spoofing_routes.py')
    spec = importlib.util.spec_from_file_location(
        'bazarr_spoofing_routes_under_test', module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spoof = _load_spoofing_module()


class TestNormalizeShowId(unittest.TestCase):
    def test_prefers_imdb_over_tmdb(self):
        self.assertEqual(
            spoof.normalize_show_id('tt0066626', '1922'),
            'tt0066626',
        )

    def test_empty_imdb_falls_through_to_tmdb(self):
        self.assertEqual(spoof.normalize_show_id('', '1922'), '1922')
        self.assertEqual(spoof.normalize_show_id('   ', 1922), '1922')
        self.assertEqual(spoof.normalize_show_id(None, '1922'), '1922')

    def test_both_missing(self):
        self.assertEqual(spoof.normalize_show_id('', ''), '')
        self.assertEqual(spoof.normalize_show_id(None, None), '')


class TestSonarrSeriesId(unittest.TestCase):
    def test_uses_tmdb_when_present(self):
        item = {
            'show_id': 'tt0066626',
            'imdb_id': 'tt0066626',
            'tmdb_id': '1922',
            'title': 'All in the Family',
        }
        self.assertEqual(spoof.sonarr_series_id(item), 1922)

    def test_hashes_when_no_tmdb(self):
        item = {
            'show_id': 'tt1234567',
            'imdb_id': 'tt1234567',
            'tmdb_id': None,
            'title': 'No Tmdb Show',
        }
        expected = spoof.generate_unique_id('tt1234567', 'series')
        self.assertEqual(spoof.sonarr_series_id(item), expected)
        self.assertNotEqual(expected, 0)

    def test_create_series_resource_matches_sonarr_series_id(self):
        item = {
            'show_id': 'tt0066626',
            'imdb_id': 'tt0066626',
            'tmdb_id': 1922,
            'title': 'All in the Family',
            'year': 1971,
        }
        resource = spoof.create_series_resource(item, [])
        self.assertEqual(resource['id'], spoof.sonarr_series_id(item))
        self.assertEqual(resource['id'], 1922)


class TestEpisodeFileIdContract(unittest.TestCase):
    def test_episode_file_id_differs_from_episode_id(self):
        item = {
            'id': 42,
            'imdb_id': 'tt0066626',
            'tmdb_id': '1922',
            'title': 'All in the Family',
            'episode_title': 'Pilot',
            'season_number': 1,
            'episode_number': 1,
            'file_path': '/tv/All in the Family/Season 01/S01E01.mkv',
            'collected_at': '2024-01-15T10:30:00Z',
            'version': '1080p',
        }
        result = spoof.create_episode_resource(item, series_id=1922, series_title='All in the Family')
        self.assertNotEqual(result['id'], result['episodeFileId'])
        self.assertIn('episodeFile', result)
        self.assertEqual(result['episodeFile']['id'], result['episodeFileId'])
        self.assertEqual(result['episodeFile']['seriesId'], 1922)
        self.assertTrue(result['hasFile'])


class TestSignalRSeriesIdMatch(unittest.TestCase):
    """SignalR must reuse create_series_resource id for episodeFile.seriesId."""

    def test_series_and_episode_file_share_same_series_id_with_tmdb(self):
        media_item = {
            'id': 100,
            'imdb_id': 'tt0066626',
            'tmdb_id': '1922',
            'title': 'All in the Family',
            'year': 1971,
            'season_number': 1,
            'episode_number': 1,
            'file_path': '/tv/All in the Family/Season 01/S01E01.mkv',
            'collected_at': '2024-01-15T10:30:00Z',
            'version': '1080p',
        }
        show_id = spoof.normalize_show_id(media_item.get('imdb_id'), media_item.get('tmdb_id'))
        series_info = {
            'show_id': show_id,
            'imdb_id': media_item.get('imdb_id'),
            'tmdb_id': media_item.get('tmdb_id'),
            'title': media_item.get('title'),
            'year': media_item.get('year'),
        }
        series_resource = spoof.create_series_resource(series_info, [media_item])
        series_id = series_resource['id']
        episode_file = spoof.create_episode_file_resource(media_item, series_id)

        self.assertEqual(series_id, 1922)
        self.assertEqual(episode_file['seriesId'], series_id)
        # Old bug: hashed series id would not equal TMDB
        hashed = spoof.generate_unique_id(show_id, 'series')
        self.assertNotEqual(series_id, hashed)

    def test_series_and_episode_file_share_same_series_id_without_tmdb(self):
        media_item = {
            'id': 101,
            'imdb_id': 'tt1111111',
            'tmdb_id': None,
            'title': 'Hash Only Show',
            'year': 2020,
            'season_number': 1,
            'episode_number': 1,
            'file_path': '/tv/Hash Only Show/Season 01/S01E01.mkv',
            'collected_at': '2024-01-15T10:30:00Z',
        }
        show_id = spoof.normalize_show_id(media_item.get('imdb_id'), media_item.get('tmdb_id'))
        series_info = {
            'show_id': show_id,
            'imdb_id': media_item.get('imdb_id'),
            'tmdb_id': media_item.get('tmdb_id'),
            'title': media_item.get('title'),
            'year': media_item.get('year'),
        }
        series_resource = spoof.create_series_resource(series_info, [media_item])
        series_id = series_resource['id']
        episode_file = spoof.create_episode_file_resource(media_item, series_id)

        self.assertEqual(series_id, spoof.generate_unique_id(show_id, 'series'))
        self.assertEqual(episode_file['seriesId'], series_id)


class TestEmptyImdbEpisodeGrouping(unittest.TestCase):
    def test_normalize_keys_align_for_empty_imdb(self):
        """Series list and episode map must use the same key when imdb is ''."""
        series_row = {
            'imdb_id': '',
            'tmdb_id': '1922',
            'title': 'All in the Family',
        }
        episode_row = {
            'imdb_id': '',
            'tmdb_id': '1922',
            'title': 'All in the Family',
            'season_number': 1,
            'episode_number': 1,
        }
        series_key = spoof.normalize_show_id(
            series_row.get('imdb_id'), series_row.get('tmdb_id')
        )
        ep_key = spoof.normalize_show_id(
            episode_row.get('imdb_id'), episode_row.get('tmdb_id')
        )
        self.assertEqual(series_key, ep_key)
        self.assertEqual(series_key, '1922')

        # Simulate ep_map lookup used by GET /api/v3/series
        ep_map = {}
        ep_map.setdefault(ep_key, []).append(episode_row)
        self.assertEqual(len(ep_map.get(series_key, [])), 1)


class TestGetSeriesByIdLookup(unittest.TestCase):
    def test_hashed_id_resolved_via_collected_series_fallback(self):
        series = {
            'show_id': 'tt9990001',
            'imdb_id': 'tt9990001',
            'tmdb_id': None,
            'title': 'Hash Show',
            'year': 2021,
            'genres': None,
            'runtime': 40,
        }
        hashed_id = spoof.sonarr_series_id(series)

        with unittest.mock.patch.object(
            spoof, 'get_collected_series', return_value=[series]
        ):
            # Mock DB tmdb lookup to miss
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = None
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)

            with unittest.mock.patch.object(
                spoof, 'get_db_connection', return_value=mock_conn
            ):
                found = spoof.get_series_by_id(hashed_id)

        self.assertIsNotNone(found)
        self.assertEqual(found['imdb_id'], 'tt9990001')
        self.assertEqual(found['show_id'], 'tt9990001')


if __name__ == '__main__':
    unittest.main()
