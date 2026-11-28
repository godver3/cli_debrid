"""Season 0 (Specials) preservation across scraper routes."""
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


class SelectMediaSeasonZeroTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.scraper_routes.search as search_mod
        self.search_mod = search_mod
        self.select_media_view = inspect.unwrap(search_mod.select_media)
        self.select_episode_view = inspect.unwrap(search_mod.select_episode)
        self.app = Flask(__name__)
        self.app.secret_key = 'test-secret'

    def test_select_media_keeps_season_zero_for_tv_episode(self):
        captured = {}

        def fake_process(media_id, title, year, media_type, season, episode, multi, version, genres, **kw):
            captured.update(media_type=media_type, season=season, episode=episode, multi=multi)
            return ([], [])

        with mock.patch.object(self.search_mod, 'current_user', SimpleNamespace(is_authenticated=False, role='admin')), \
             mock.patch.object(self.search_mod, 'process_media_selection', side_effect=fake_process), \
             mock.patch('metadata.metadata.get_imdb_id_if_missing', return_value='tt1234567'):
            form = {
                'media_id': '1396', 'title': 'Show', 'year': '2008', 'media_type': 'tv',
                'season': '0', 'episode': '1', 'multi': 'false', 'version': '1080p',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.select_media_view()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured['season'], 0)
        self.assertEqual(captured['episode'], 1)
        self.assertFalse(captured['multi'])

    def test_select_media_runs_nzb_season_aggregate_for_season_zero_pack(self):
        captured = {}
        agg_called = {}

        def fake_process(media_id, title, year, media_type, season, episode, multi, version, genres, **kw):
            captured.update(season=season, episode=episode, multi=multi)
            return ([], [])

        def fake_agg(**kw):
            agg_called.update(kw)
            return []

        settings = {
            ('Scrapers', None): {
                'nzb1': {'type': 'Newznab', 'enabled': True, 'url': 'https://nzb.test', 'api_key': 'k'}
            }
        }
        with mock.patch.object(self.search_mod, 'current_user', SimpleNamespace(is_authenticated=False, role='admin')), \
             mock.patch.object(self.search_mod, 'process_media_selection', side_effect=fake_process), \
             mock.patch.object(self.search_mod, 'get_setting', lambda s, k=None, d=None: settings.get((s, k), d)), \
             mock.patch('metadata.metadata.get_imdb_id_if_missing', return_value='tt1234567'), \
             mock.patch('metadata.metadata.get_episode_count_for_seasons', return_value=4) as ep_cnt, \
             mock.patch('scraper.newznab.scrape_newznab_season_aggregate', side_effect=fake_agg):
            form = {
                'media_id': '1396', 'title': 'Show', 'year': '2008', 'media_type': 'tv',
                'season': '0', 'episode': '', 'multi': 'true', 'version': '1080p',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.select_media_view()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured['season'], 0)
        self.assertIsNone(captured['episode'])
        ep_cnt.assert_called_once_with('tt1234567', [0])
        self.assertEqual(agg_called.get('season'), 0)

    def test_select_media_movie_keeps_no_season(self):
        captured = {}

        def fake_process(media_id, title, year, media_type, season, episode, multi, version, genres, **kw):
            captured.update(season=season, episode=episode)
            return ([], [])

        with mock.patch.object(self.search_mod, 'current_user', SimpleNamespace(is_authenticated=False, role='admin')), \
             mock.patch.object(self.search_mod, 'process_media_selection', side_effect=fake_process), \
             mock.patch('metadata.metadata.get_imdb_id_if_missing', return_value='tt0111161'):
            form = {
                'media_id': '278', 'title': 'Movie', 'year': '1994', 'media_type': 'movie',
                'season': '0', 'episode': '0', 'multi': 'false', 'version': '1080p',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.select_media_view()
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(captured['season'])
        self.assertIsNone(captured['episode'])

    def test_select_episode_passes_int_zero_for_specials(self):
        captured = {}

        def fake_scrape(media_id, title, year, season, allow_specials=False):
            captured.update(season=season, allow_specials=allow_specials)
            return {'episode_results': [{'episode_num': 1, 'season_num': 0}]}

        with mock.patch.object(self.search_mod, 'current_user', SimpleNamespace(is_authenticated=False, role='admin')), \
             mock.patch.object(self.search_mod, 'web_scrape_tvshow', side_effect=fake_scrape), \
             mock.patch('utilities.web_scraper.get_available_versions', return_value=['1080p']):
            form = {
                'media_id': '1396', 'title': 'Show', 'year': '2008',
                'season': '0', 'allow_specials': 'true',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.select_episode_view()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured['season'], 0)
        self.assertIsInstance(captured['season'], int)


class AddToDebridSeasonZeroTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.scraper_routes.add_to_debrid as atd
        self.atd = atd
        self.view = inspect.unwrap(atd.add_torrent_to_debrid)
        self.app = Flask(__name__)
        settings = {
            ('Scraping', 'versions'): {'1080p': {}},
            ('File Management', 'file_collection_management'): 'Symlinked/Local',
            ('Debrid Provider', 'enable_debrid_naming'): False,
        }
        p = mock.patch.object(atd, 'get_setting', lambda s, k=None, d=None: settings.get((s, k), d))
        p.start()
        self.addCleanup(p.stop)

    def test_nzb_single_keeps_season_zero_for_tv_and_none_for_movie(self):
        captured = []

        def fake_nzb(**kw):
            captured.append(kw)
            from flask import jsonify
            return jsonify({'success': True})

        with mock.patch.object(self.atd, '_add_nzb_to_usenet', side_effect=fake_nzb), \
             mock.patch('metadata.metadata.get_metadata', return_value={'genres': []}):
            tv_form = {
                'protocol': 'nzb', 'nzb_url': 'https://nzb.test/1',
                'title': 'Show', 'year': '2008', 'media_type': 'tv',
                'season': '0', 'episode': '0', 'version': '1080p', 'tmdb_id': '1396',
            }
            with self.app.test_request_context(method='POST', data=tv_form):
                resp = self.view()
            self.assertEqual(resp.status_code, 200)

            movie_form = {
                'protocol': 'nzb', 'nzb_url': 'https://nzb.test/2',
                'title': 'Movie', 'year': '2020', 'media_type': 'movie',
                'season': '0', 'episode': '0', 'version': '1080p', 'tmdb_id': '278',
            }
            with self.app.test_request_context(method='POST', data=movie_form):
                resp2 = self.view()
            self.assertEqual(resp2.status_code, 200)

        self.assertEqual(captured[0]['season'], 0)
        self.assertEqual(captured[0]['episode'], 0)
        self.assertIsNone(captured[1]['season'])
        self.assertIsNone(captured[1]['episode'])

    def test_debrid_add_keeps_season_zero_and_episode_zero_in_db_item(self):
        added_items = []
        recorded_items = []
        fake_provider = mock.Mock()
        fake_provider.PROVIDER_NAME = 'RealDebrid'
        fake_provider.supports_direct_cache_check = True
        fake_provider.add_torrent.return_value = 'T1'
        fake_provider.get_torrent_info.return_value = {
            'status': 'downloaded',
            'filename': 'Show.S00E00.1080p',
            'files': [{'path': '/Show.S00E00.1080p/Show.S00E00.mkv', 'bytes': 1000}],
        }
        meta = {
            'imdb_id': 'tt1234567',
            'genres': ['Drama'],
            'seasons': {
                '0': {
                    'episodes': {
                        '0': {'title': 'Special Pilot', 'first_aired': '2008-01-01T00:00:00Z'}
                    }
                }
            },
        }
        fake_processor = mock.Mock()
        fake_processor.process_content.return_value = (True, 'ok')
        fake_queue = mock.Mock()

        with mock.patch('debrid.get_debrid_providers', return_value=[fake_provider]), \
             mock.patch.object(self.atd, 'get_torrent_history', return_value=[]), \
             mock.patch.object(self.atd, 'record_torrent_addition', side_effect=lambda **kw: recorded_items.append(kw['item_data'])), \
             mock.patch.object(self.atd, 'ContentProcessor', return_value=fake_processor), \
             mock.patch.object(self.atd, '_record_manual_grab'), \
             mock.patch.object(self.atd, 'is_user_system_enabled', return_value=False), \
             mock.patch('metadata.metadata.get_metadata', return_value=meta), \
             mock.patch('database.add_media_item', side_effect=lambda item, **kw: (added_items.append(item) or 42)), \
             mock.patch('queues.checking_queue.CheckingQueue', return_value=fake_queue):
            form = {
                'magnet_link': 'magnet:?xt=urn:btih:' + 'a' * 40,
                'title': 'Show', 'year': '2008', 'media_type': 'tv',
                'season': '0', 'episode': '0', 'version': '1080p', 'tmdb_id': '1396',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.view()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(recorded_items[0]['season'], 0)
        self.assertEqual(recorded_items[0]['episode'], 0)
        self.assertEqual(len(added_items), 1)
        self.assertEqual(added_items[0]['season_number'], 0)
        self.assertEqual(added_items[0]['episode_number'], 0)
        self.assertEqual(added_items[0]['episode_title'], 'Special Pilot')

    def test_debrid_add_season_zero_pack_processes_specials_episodes(self):
        added_items = []
        fake_provider = mock.Mock()
        fake_provider.PROVIDER_NAME = 'RealDebrid'
        fake_provider.supports_direct_cache_check = True
        fake_provider.add_torrent.return_value = 'T2'
        fake_provider.get_torrent_info.return_value = {
            'status': 'downloaded',
            'filename': 'Show.S00.1080p',
            'files': [{'path': '/Show.S00.1080p/Show.S00E01.mkv', 'bytes': 1000}],
        }
        meta = {
            'imdb_id': 'tt1234567',
            'genres': ['Drama'],
            'seasons': {
                0: {
                    'episodes': {
                        1: {'title': 'OVA 1', 'first_aired': '2008-01-01T00:00:00Z'}
                    }
                }
            },
        }
        fake_processor = mock.Mock()
        fake_processor.process_content.return_value = (True, 'ok')
        fake_matcher = mock.Mock()
        fake_matcher._parse_file_info.return_value = {'path': 'Show.S00E01.mkv'}
        fake_matcher.find_best_match_from_parsed.return_value = ('Show.S00E01.mkv', 100)
        fake_queue = mock.Mock()

        with mock.patch('debrid.get_debrid_providers', return_value=[fake_provider]), \
             mock.patch.object(self.atd, 'get_torrent_history', return_value=[]), \
             mock.patch.object(self.atd, 'record_torrent_addition'), \
             mock.patch.object(self.atd, 'ContentProcessor', return_value=fake_processor), \
             mock.patch.object(self.atd, 'MediaMatcher', return_value=fake_matcher), \
             mock.patch.object(self.atd, '_record_manual_grab'), \
             mock.patch.object(self.atd, 'is_user_system_enabled', return_value=False), \
             mock.patch('metadata.metadata.get_metadata', return_value=meta), \
             mock.patch('database.add_media_item', side_effect=lambda item, **kw: (added_items.append(item) or 99)), \
             mock.patch('queues.checking_queue.CheckingQueue', return_value=fake_queue):
            form = {
                'magnet_link': 'magnet:?xt=urn:btih:' + 'b' * 40,
                'title': 'Show', 'year': '2008', 'media_type': 'tv',
                'season': '0', 'episode': '', 'version': '1080p', 'tmdb_id': '1396',
            }
            with self.app.test_request_context(method='POST', data=form):
                resp = self.view()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['message'], 'Successfully processed season pack')
        self.assertEqual(len(added_items), 1)
        self.assertEqual(added_items[0]['season_number'], 0)
        self.assertEqual(added_items[0]['episode_number'], 1)
        self.assertEqual(added_items[0]['episode_title'], 'OVA 1')


class RunScrapeSeasonZeroTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.scraper_routes.tester as tester_mod
        self.tester_mod = tester_mod
        self.view = inspect.unwrap(tester_mod.run_scrape)
        self.app = Flask(__name__)

    def test_run_scrape_keeps_season_zero_and_movie_none(self):
        calls = []

        def fake_scrape(imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres, skip_cache):
            calls.append((media_type, season, episode))
            return ([], [])

        cfg = {'Scraping': {'versions': {'1080p': {}}}}
        with mock.patch.object(self.tester_mod, 'scrape', side_effect=fake_scrape), \
             mock.patch.object(self.tester_mod, 'load_config', return_value=cfg), \
             mock.patch.object(self.tester_mod, 'save_config'):
            ep_body = {
                'title': 'Show', 'year': 2008, 'movie_or_episode': 'episode',
                'version': '1080p', 'season': 0, 'episode': 0, 'skip_cache_check': True,
            }
            with self.app.test_request_context(method='POST', json=ep_body):
                resp = self.view()
            self.assertEqual(resp.status_code, 200)

            mv_body = {
                'title': 'Movie', 'year': 2020, 'movie_or_episode': 'movie',
                'version': '1080p', 'season': 0, 'episode': 0, 'skip_cache_check': True,
            }
            with self.app.test_request_context(method='POST', json=mv_body):
                resp2 = self.view()
            self.assertEqual(resp2.status_code, 200)

        self.assertEqual(calls[0], ('episode', 0, 0))
        self.assertEqual(calls[2], ('movie', None, None))


if __name__ == '__main__':
    unittest.main()
