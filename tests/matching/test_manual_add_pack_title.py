"""Tests for pack title resolution in manual add routes (add_to_debrid & magnet confirm)."""
import inspect
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_FRESH_PREFIXES = (
    'flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
    'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT', 'flask_login',
)


def _fresh_app_modules():
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401
    return patcher


class AddToDebridPackTitleTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.scraper_routes.add_to_debrid as mod
        self.mod = mod
        self.view = inspect.unwrap(mod.add_torrent_to_debrid)
        self.app = Flask(__name__)
        self.app.secret_key = 'test-secret'

    def test_multi_file_pack_named_after_episode_uses_common_folder_for_filled_by_title(self):
        folder = 'Detectorists.S01.1080p.WEBRip.Rus.Eng.Ozz'
        provider_name = 'Detectorists.S01E04.1080p.WEBRip.Rus.Eng.Ozz.mkv'
        files = [
            {'path': f'/{folder}/Detectorists.S01E0{i}.1080p.WEBRip.Rus.Eng.Ozz.mkv', 'bytes': 1000 + i}
            for i in range(1, 7)
        ]

        class FakeProvider:
            PROVIDER_NAME = 'Torbox'

            def add_torrent(self, magnet, temp_file=None):
                return 'tb_42'

            def get_torrent_info(self, tid):
                return {
                    'id': tid,
                    'status': 'downloaded',
                    'filename': provider_name,
                    'debrid_folder_name': folder,
                    'files': files,
                }

        added = []

        def fake_add_media_item(item, **kw):
            added.append(dict(item))
            return 101

        fake_checking = mock.MagicMock()

        def fake_get_setting(section, key=None, default=None):
            if (section, key) == ('Scraping', 'versions'):
                return {'1080p': {}}
            if (section, key) == ('File Management', 'file_collection_management'):
                return 'Symlinked/Local'
            return default

        with mock.patch.object(self.mod, 'current_user', SimpleNamespace(is_authenticated=False, role='admin')), \
             mock.patch.object(self.mod, 'is_user_system_enabled', return_value=False), \
             mock.patch.object(self.mod, 'get_setting', side_effect=fake_get_setting), \
             mock.patch('debrid.get_debrid_providers', return_value=[FakeProvider()]), \
             mock.patch.object(self.mod, 'get_torrent_history', return_value=[]), \
             mock.patch.object(self.mod, 'record_torrent_addition'), \
             mock.patch('metadata.metadata.get_metadata', return_value={'imdb_id': 'tt4082744', 'seasons': {}}), \
             mock.patch.object(self.mod, '_record_manual_grab'), \
             mock.patch('database.add_media_item', side_effect=fake_add_media_item), \
             mock.patch('queues.checking_queue.CheckingQueue', return_value=fake_checking):
            form = {
                'magnet_link': 'magnet:?xt=urn:btih:' + 'a' * 40,
                'title': 'Detectorists',
                'year': '2014',
                'media_type': 'tv',
                'season': '1',
                'episode': '1',
                'version': '1080p',
                'tmdb_id': '61428',
                'original_scraped_torrent_title': 'Detectorists S01 1080p WEBRip',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.view()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['filled_by_title'], folder)
        self.assertEqual(added[0]['debrid_folder_name'], folder)


class MagnetConfirmPackTitleTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.magnet_routes.confirm as mod
        self.mod = mod
        self.view = inspect.unwrap(mod.confirm_manual_assignment)
        self.app = Flask(__name__)
        self.app.secret_key = 'test-secret'

    def test_confirm_resolves_pack_title_from_torrent_info_when_provider_named_after_episode(self):
        folder = 'Pokemon.Complete.S01.1080p.WEBRip'
        provider_name = 'Pokemon.S01E75.1080p.WEBRip.mkv'

        class FakeProvider:
            def add_torrent(self, magnet):
                return 'tb_99'

            def get_torrent_info(self, tid):
                return {
                    'id': tid,
                    'filename': provider_name,
                    'files': [
                        {'path': f'/{folder}/Pokemon.S01E01.1080p.WEBRip.mkv'},
                        {'path': f'/{folder}/Pokemon.S01E75.1080p.WEBRip.mkv'},
                    ],
                }

        added = []

        def fake_add_media_item(item, **kw):
            added.append(dict(item))
            return len(added)

        meta = {
            'tmdb_id': '60572',
            'imdb_id': 'tt0168366',
            'title': 'Pokemon',
            'year': 1997,
            'genres': ['Animation'],
            'seasons': {
                1: {
                    'episodes': {
                        1: {'title': 'Ep 1', 'first_aired': '1997-04-01T00:00:00Z'},
                        75: {'title': 'Ep 75', 'first_aired': '1998-12-01T00:00:00Z'},
                    },
                    'episode_count': 2,
                }
            },
        }

        with mock.patch.object(self.mod, 'get_debrid_provider', return_value=FakeProvider()), \
             mock.patch.object(self.mod, 'add_media_item', side_effect=fake_add_media_item), \
             mock.patch.object(self.mod, 'update_torrent_tracking'), \
             mock.patch.object(self.mod, '_saved_episode_order', return_value='default'), \
             mock.patch('database.torrent_tracking.get_torrent_history', return_value=[]), \
             mock.patch('metadata.metadata.get_metadata', return_value=meta), \
             mock.patch('routes.settings_routes.get_enabled_notifications_for_category',
                        return_value=SimpleNamespace(json={'success': False})):
            form = {
                'magnet_link': 'magnet:?xt=urn:btih:' + 'b' * 40,
                'torrent_filename': provider_name,
                'torrent_id': 'tb_preview',
                'version': '1080p',
                'ep_60572_s01e01': 'Pokemon.S01E01.1080p.WEBRip.mkv',
                'ep_60572_s01e75': 'Pokemon.S01E75.1080p.WEBRip.mkv',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.view()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(added), 2)
        for row in added:
            self.assertEqual(row['filled_by_title'], folder)

    def test_confirm_keeps_single_file_torrent_filename_unchanged(self):
        provider_name = 'Pokemon.S01E75.1080p.WEBRip.mkv'

        class FakeProvider:
            def add_torrent(self, magnet):
                return 'tb_100'

            def get_torrent_info(self, tid):
                raise AssertionError('get_torrent_info should not be called for a single-file assignment')

        added = []

        def fake_add_media_item(item, **kw):
            added.append(dict(item))
            return 1

        meta = {
            'tmdb_id': '60572',
            'imdb_id': 'tt0168366',
            'title': 'Pokemon',
            'year': 1997,
            'genres': ['Animation'],
            'seasons': {1: {'episodes': {75: {'title': 'Ep 75', 'first_aired': '1998-12-01T00:00:00Z'}}, 'episode_count': 1}},
        }

        with mock.patch.object(self.mod, 'get_debrid_provider', return_value=FakeProvider()), \
             mock.patch.object(self.mod, 'add_media_item', side_effect=fake_add_media_item), \
             mock.patch.object(self.mod, 'update_torrent_tracking'), \
             mock.patch.object(self.mod, '_saved_episode_order', return_value='default'), \
             mock.patch('database.torrent_tracking.get_torrent_history', return_value=[]), \
             mock.patch('metadata.metadata.get_metadata', return_value=meta), \
             mock.patch('routes.settings_routes.get_enabled_notifications_for_category',
                        return_value=SimpleNamespace(json={'success': False})):
            form = {
                'magnet_link': 'magnet:?xt=urn:btih:' + 'c' * 40,
                'torrent_filename': provider_name,
                'torrent_id': 'tb_preview',
                'version': '1080p',
                'ep_60572_s01e75': provider_name,
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.view()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['filled_by_title'], provider_name)


if __name__ == '__main__':
    unittest.main()
