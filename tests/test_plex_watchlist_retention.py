#!/usr/bin/env python3

import asyncio
import importlib.util
import logging
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB_MODULE_NAMES = (
    'plexapi',
    'plexapi.myplex',
    'utilities.settings',
    'database.database_reading',
    'queues.config_manager',
    'cli_battery',
    'cli_battery.app',
    'cli_battery.app.direct_api',
    'cli_battery.app.database',
    'content_checkers.plex_token_manager',
    'feedparser',
    'aiohttp',
)


def _install_import_stubs():
    plexapi = types.ModuleType('plexapi')
    myplex = types.ModuleType('plexapi.myplex')

    class MyPlexAccount:
        def query(self, *args, **kwargs):
            return None

    myplex.MyPlexAccount = MyPlexAccount
    plexapi.myplex = myplex
    sys.modules['plexapi'] = plexapi
    sys.modules['plexapi.myplex'] = myplex

    settings = types.ModuleType('utilities.settings')
    settings.get_setting = lambda *args, **kwargs: kwargs.get('default')
    settings.get_all_settings = lambda: {}
    sys.modules['utilities.settings'] = settings

    database = types.ModuleType('database.database_reading')
    database.get_media_item_presence = lambda *args, **kwargs: None
    database.get_media_item_presence_overall = lambda *args, **kwargs: None
    sys.modules['database.database_reading'] = database

    config_manager = types.ModuleType('queues.config_manager')
    config_manager.load_config = lambda: {}
    sys.modules['queues.config_manager'] = config_manager

    cli_battery = types.ModuleType('cli_battery')
    app = types.ModuleType('cli_battery.app')
    app.trakt_client = types.SimpleNamespace()
    direct_api = types.ModuleType('cli_battery.app.direct_api')
    direct_api.DirectAPI = object
    battery_database = types.ModuleType('cli_battery.app.database')
    battery_database.DatabaseManager = object
    cli_battery.app = app
    sys.modules['cli_battery'] = cli_battery
    sys.modules['cli_battery.app'] = app
    sys.modules['cli_battery.app.direct_api'] = direct_api
    sys.modules['cli_battery.app.database'] = battery_database

    token_manager = types.ModuleType('content_checkers.plex_token_manager')
    token_manager.update_token_status = lambda *args, **kwargs: None
    token_manager.get_token_status = lambda *args, **kwargs: None
    sys.modules['content_checkers.plex_token_manager'] = token_manager

    if 'feedparser' not in sys.modules:
        feedparser = types.ModuleType('feedparser')
        feedparser.parse = lambda url: None
        sys.modules['feedparser'] = feedparser

    if 'aiohttp' not in sys.modules:
        sys.modules['aiohttp'] = types.ModuleType('aiohttp')


def _load_module(name, relative_path):
    missing = object()
    saved_modules = {
        module_name: sys.modules.get(module_name, missing)
        for module_name in (*STUB_MODULE_NAMES, name)
    }
    try:
        _install_import_stubs()
        path = os.path.join(ROOT, relative_path)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for module_name, saved_module in saved_modules.items():
            if saved_module is missing:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = saved_module


class FakePlexItem:
    def __init__(self, media_type):
        self.title = 'Test title'
        self.type = 'show' if media_type == 'tv' else 'movie'
        self.key = '/library/metadata/1'
        self._server = types.SimpleNamespace(url=lambda key: f'https://plex.test{key}')


class FakePlexAccount:
    username = 'tester'

    def __init__(self, item, removal_fails=False):
        self.item = item
        self.removal_fails = removal_fails
        self.removed = []

    def watchlist(self):
        return [self.item]

    def removeFromWatchlist(self, items):
        if self.removal_fails:
            raise RuntimeError('Plex removal failed')
        self.removed.extend(items)


class PlexWatchlistRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module(
            'content_checkers.plex_watchlist',
            'content_checkers/plex_watchlist.py',
        )

    def run_fetcher(self, state, media_type, keep_series, removal=True,
                    show_status='returning series', removal_fails=False):
        module = self.module
        item = FakePlexItem(media_type)
        account = FakePlexAccount(item, removal_fails)
        module.get_plex_client = lambda: (account, 'token')
        module.get_setting = lambda section, key, default=False: {
            'plex_watchlist_removal': removal,
            'plex_watchlist_keep_series': keep_series,
        }.get(key, default)
        module.get_media_item_presence_overall = lambda **kwargs: state
        module.get_show_status = lambda imdb_id: show_status

        async def fake_fetches(items, token):
            return [{
                'imdb_id': 'tt1234567',
                'tmdb_id': None,
                'media_type': media_type,
                'original_plex_item': item,
            }]

        module.run_async_fetches = fake_fetches
        with self.assertLogs(level=logging.INFO) as captured:
            batches = module.get_wanted_from_plex_watchlist({'1080p': True})
        returned = [entry for batch, _ in batches for entry in batch]
        return returned, account.removed, '\n'.join(captured.output)

    def test_partial_and_collected_series_with_keep_series_are_processed(self):
        for state in ('Partial', 'Collected'):
            with self.subTest(state=state):
                returned, removed, logs = self.run_fetcher(state, 'tv', True)
                self.assertEqual(len(returned), 1)
                self.assertEqual(removed, [])
                self.assertIn('Retained TV series processed: 1', logs)
                self.assertNotIn('already collected and kept', logs)

    def test_ongoing_series_without_keep_series_is_processed(self):
        for state in ('Partial', 'Collected'):
            with self.subTest(state=state):
                returned, removed, _ = self.run_fetcher(
                    state, 'tv', False, show_status='returning series')
                self.assertEqual(len(returned), 1)
                self.assertEqual(removed, [])

    def test_ended_series_and_collected_movie_are_removed(self):
        ended, ended_removed, _ = self.run_fetcher(
            'Collected', 'tv', False, show_status='ended')
        movie, movie_removed, _ = self.run_fetcher('Collected', 'movie', False)
        self.assertEqual(ended, [])
        self.assertEqual(movie, [])
        self.assertEqual(len(ended_removed), 1)
        self.assertEqual(len(movie_removed), 1)

    def test_removal_disabled_processes_collected_and_partial_items(self):
        for state in ('Collected', 'Partial'):
            with self.subTest(state=state):
                returned, removed, _ = self.run_fetcher(
                    state, 'movie', False, removal=False)
                self.assertEqual(len(returned), 1)
                self.assertEqual(removed, [])

    def test_removal_failure_keeps_item_in_processing_batch(self):
        returned, removed, _ = self.run_fetcher(
            'Collected', 'movie', False, removal_fails=True)
        self.assertEqual(len(returned), 1)
        self.assertEqual(removed, [])


class PlexRssRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module(
            'content_checkers.plex_rss_watchlist',
            'content_checkers/plex_rss_watchlist.py',
        )

    def run_fetcher(self, state, media_type, keep_series, removal=True,
                    show_status='returning series'):
        module = self.module
        entry = types.SimpleNamespace(
            title='Test title',
            guid='imdb://tt1234567',
            category='show' if media_type == 'tv' else 'movie',
        )
        module.feedparser.parse = lambda url: types.SimpleNamespace(
            bozo=False, entries=[entry])
        module.get_setting = lambda section, key, default=False: {
            'plex_watchlist_removal': removal,
            'plex_watchlist_keep_series': keep_series,
        }.get(key, default)
        module.get_media_item_presence_overall = lambda **kwargs: state
        module.get_show_status = lambda imdb_id: show_status
        with self.assertLogs(level=logging.INFO) as captured:
            batches = module.get_wanted_from_plex_rss(
                'https://plex.test/rss', {'1080p': True})
        returned = [entry for batch, _ in batches for entry in batch]
        return returned, '\n'.join(captured.output)

    def test_partial_and_collected_series_with_keep_series_are_processed(self):
        for state in ('Partial', 'Collected'):
            with self.subTest(state=state):
                returned, logs = self.run_fetcher(state, 'tv', True)
                self.assertEqual(len(returned), 1)
                self.assertIn('Retained TV series processed: 1', logs)
                self.assertNotIn('collected but kept', logs)

    def test_ongoing_series_without_keep_series_is_processed(self):
        for state in ('Partial', 'Collected'):
            with self.subTest(state=state):
                returned, _ = self.run_fetcher(
                    state, 'tv', False, show_status='returning series')
                self.assertEqual(len(returned), 1)

    def test_unknown_status_is_conservatively_processed(self):
        returned, _ = self.run_fetcher(
            'Collected', 'tv', False, show_status='')
        self.assertEqual(len(returned), 1)

    def test_ended_series_and_collected_movie_are_suppressed(self):
        ended, _ = self.run_fetcher(
            'Collected', 'tv', False, show_status='ended')
        movie, _ = self.run_fetcher('Collected', 'movie', False)
        self.assertEqual(ended, [])
        self.assertEqual(movie, [])

    def test_removal_disabled_processes_collected_and_partial_items(self):
        for state in ('Collected', 'Partial'):
            with self.subTest(state=state):
                returned, _ = self.run_fetcher(
                    state, 'movie', False, removal=False)
                self.assertEqual(len(returned), 1)


if __name__ == '__main__':
    unittest.main()
