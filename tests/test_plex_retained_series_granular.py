#!/usr/bin/env python3
"""Database regression tests for retained Plex series with granular versions."""

import os
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_wanted_items_module():
    """Load wanted_items without importing the dependency-heavy database package."""
    missing = object()
    parent_attributes = {}
    for parent_name, attribute in (
        ('utilities', 'settings'),
        ('content_checkers', 'trakt'),
        ('queues', 'config_manager'),
        ('metadata', 'metadata'),
    ):
        parent = __import__(parent_name)
        parent_attributes[(parent_name, attribute)] = getattr(parent, attribute, missing)
    module_names = (
        'database',
        'database.core',
        'database.manual_blacklist',
        'database.wanted_items_retained_series_test',
        'content_checkers.trakt',
        'queues.config_manager',
        'utilities.settings',
        'metadata.metadata',
    )
    saved = {name: sys.modules.get(name, missing) for name in module_names}
    try:
        database_package = types.ModuleType('database')
        database_package.__path__ = [os.path.join(ROOT, 'database')]
        sys.modules['database'] = database_package

        core_path = os.path.join(ROOT, 'database', 'core.py')
        core_spec = importlib.util.spec_from_file_location('database.core', core_path)
        core_module = importlib.util.module_from_spec(core_spec)
        sys.modules['database.core'] = core_module
        core_spec.loader.exec_module(core_module)

        manual_blacklist = types.ModuleType('database.manual_blacklist')
        manual_blacklist.is_blacklisted = lambda *args, **kwargs: False
        sys.modules['database.manual_blacklist'] = manual_blacklist

        trakt = types.ModuleType('content_checkers.trakt')
        trakt.fetch_items_from_trakt = lambda *args, **kwargs: []
        trakt.load_imdb_trakt_cache = lambda: {}
        trakt.save_imdb_trakt_cache = lambda cache: None
        sys.modules['content_checkers.trakt'] = trakt

        config_manager = types.ModuleType('queues.config_manager')
        config_manager.load_config = lambda: {}
        sys.modules['queues.config_manager'] = config_manager

        settings = types.ModuleType('utilities.settings')
        settings.get_setting = lambda section, key, default=None: default
        sys.modules['utilities.settings'] = settings

        metadata = types.ModuleType('metadata.metadata')
        metadata.get_show_airtime_by_imdb_id = lambda *args, **kwargs: None
        metadata.get_tmdb_id_and_media_type = lambda *args, **kwargs: (None, None)
        sys.modules['metadata.metadata'] = metadata

        wanted_path = os.path.join(ROOT, 'database', 'wanted_items.py')
        wanted_spec = importlib.util.spec_from_file_location(
            'database.wanted_items_retained_series_test', wanted_path
        )
        module = importlib.util.module_from_spec(wanted_spec)
        sys.modules[wanted_spec.name] = module
        wanted_spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        for (parent_name, attribute), previous in parent_attributes.items():
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if previous is missing:
                try:
                    delattr(parent, attribute)
                except AttributeError:
                    pass
            else:
                setattr(parent, attribute, previous)


wanted_items = _load_wanted_items_module()


class RetainedSeriesGranularTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        conn = self.connect()
        conn.execute(
            """
            CREATE TABLE media_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imdb_id TEXT,
                tmdb_id TEXT,
                title TEXT,
                year INTEGER,
                release_date DATE,
                state TEXT,
                type TEXT,
                episode_title TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                filled_by_torrent_id TEXT,
                airtime TEXT,
                last_updated TIMESTAMP,
                sleep_cycles INTEGER DEFAULT 0,
                version TEXT,
                genres TEXT,
                runtime INTEGER,
                country TEXT,
                blacklisted_date TIMESTAMP,
                requested_season BOOLEAN DEFAULT FALSE,
                content_source TEXT,
                content_source_detail TEXT,
                source_position INTEGER,
                selected_folder TEXT,
                selected_folder_is_custom BOOLEAN DEFAULT FALSE,
                tags TEXT,
                ghostlisted BOOLEAN DEFAULT FALSE,
                plex_labels TEXT,
                content_sources TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO media_items (
                imdb_id, tmdb_id, title, year, release_date, state, type,
                season_number, episode_number, episode_title, version,
                ghostlisted
            ) VALUES (?, ?, ?, ?, ?, ?, 'episode', ?, ?, ?, ?, 0)
            """,
            (
                'tt1234567', '7654321', 'Retained Show', 2024,
                '2024-01-01', 'Collected', 1, 1, 'Existing Episode', '1080p',
            ),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def connect(self, *args, **kwargs):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def settings(section, key, default=None):
        if section == 'Debug' and key == 'enable_granular_version_additions':
            return True
        return default

    @staticmethod
    def episode(number, monitor_missing_only=True):
        return {
            'imdb_id': 'tt1234567',
            'tmdb_id': '7654321',
            'title': 'Retained Show',
            'year': 2024,
            'release_date': '2024-01-01',
            'media_type': 'episode',
            'season_number': 1,
            'episode_number': number,
            'episode_title': f'Episode {number}',
            'genres': [],
            'content_source': 'My Plex Watchlist_1',
            'monitor_missing_episodes_only': monitor_missing_only,
        }

    def rows_for_episode(self, number):
        conn = self.connect()
        try:
            return conn.execute(
                """
                SELECT version, state FROM media_items
                WHERE imdb_id = ? AND season_number = 1 AND episode_number = ?
                ORDER BY version
                """,
                ('tt1234567', number),
            ).fetchall()
        finally:
            conn.close()

    def run_add(self, items, unblacklist=False):
        notification_response = MagicMock()
        notification_response.json = {'success': False}
        metadata = types.ModuleType('metadata.metadata')
        metadata.get_show_airtime_by_imdb_id = lambda *args, **kwargs: None
        metadata.get_tmdb_id_and_media_type = lambda *args, **kwargs: (None, None)

        plex_labels = types.ModuleType('utilities.plex_label_manager')
        plex_labels.is_plex_labels_enabled_anywhere = lambda: False

        routes = types.ModuleType('routes')
        routes.__path__ = []
        notifications = types.ModuleType('routes.notifications')
        notifications.send_notifications = lambda *args, **kwargs: None
        settings_routes = types.ModuleType('routes.settings_routes')
        settings_routes.get_enabled_notifications_for_category = lambda *args, **kwargs: notification_response
        extensions = types.ModuleType('routes.extensions')
        extensions.app = types.SimpleNamespace(app_context=lambda: nullcontext())
        database_reading = types.ModuleType('database.database_reading')
        database_reading.get_all_media_items = lambda: []
        queue_manager = types.ModuleType('queues.queue_manager')
        queue_manager.QueueManager = lambda: types.SimpleNamespace(process_wanted=lambda: None)

        runtime_stubs = {
            'metadata.metadata': metadata,
            'utilities.plex_label_manager': plex_labels,
            'routes': routes,
            'routes.notifications': notifications,
            'routes.settings_routes': settings_routes,
            'routes.extensions': extensions,
            'database.database_reading': database_reading,
            'queues.queue_manager': queue_manager,
        }

        with patch.dict(sys.modules, runtime_stubs), \
             patch.object(wanted_items, 'get_db_connection', side_effect=self.connect), \
             patch.object(wanted_items, 'load_config', return_value={'Content Sources': {}}), \
             patch.object(wanted_items, 'load_imdb_trakt_cache', return_value={}), \
             patch.object(wanted_items, 'save_imdb_trakt_cache'), \
             patch.object(wanted_items, 'is_blacklisted', return_value=False), \
             patch('utilities.settings.get_setting', side_effect=self.settings):
            wanted_items.add_wanted_items(
                items,
                {'1080p': True, '2160p': True},
                unblacklist=unblacklist,
            )

    def test_retained_monitor_skips_new_version_for_existing_episode(self):
        self.run_add([self.episode(1), self.episode(2)])

        self.assertEqual(
            [('1080p', 'Collected')],
            [(row['version'], row['state']) for row in self.rows_for_episode(1)],
        )
        self.assertEqual(
            [('1080p', 'Wanted'), ('2160p', 'Wanted')],
            [(row['version'], row['state']) for row in self.rows_for_episode(2)],
        )

    def test_normal_granular_source_still_adds_missing_version(self):
        self.run_add([self.episode(1, monitor_missing_only=False)])

        self.assertEqual(
            [('1080p', 'Collected'), ('2160p', 'Wanted')],
            [(row['version'], row['state']) for row in self.rows_for_episode(1)],
        )

    def test_retained_monitor_can_unblacklist_with_global_granular_enabled(self):
        conn = self.connect()
        conn.execute(
            "UPDATE media_items SET state = 'Blacklisted' WHERE episode_number = 1"
        )
        conn.commit()
        conn.close()

        self.run_add([self.episode(1)], unblacklist=True)

        self.assertEqual(
            [('1080p', 'Wanted')],
            [(row['version'], row['state']) for row in self.rows_for_episode(1)],
        )

    def test_metadata_expansion_preserves_monitor_marker(self):
        with open(os.path.join(ROOT, 'metadata', 'metadata.py'), encoding='utf-8') as source_file:
            source = source_file.read()

        create_episode_start = source.index('def create_episode_item(')
        create_episode_end = source.index('\ndef _get_local_timezone', create_episode_start)
        create_episode = source[create_episode_start:create_episode_end]
        self.assertIn(
            "'monitor_missing_episodes_only': bool(show_item.get('monitor_missing_episodes_only', False))",
            create_episode,
        )


if __name__ == '__main__':
    unittest.main()
