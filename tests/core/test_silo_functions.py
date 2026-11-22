"""utilities/silo_functions.py: Silo as the Symlinked/Local media server."""
import unittest
from unittest import mock

import database  # noqa: F401  (app import order)
from utilities import silo_functions as silo
from queues.runner import task_registry as reg


SETTINGS = {
    ('File Management', 'file_collection_management'): 'Symlinked/Local',
    ('File Management', 'media_server_type'): 'silo',
    ('File Management', 'symlinked_files_path'): '/mnt/symlinked',
    ('Debug', 'silo_url'): 'http://silo:8090/',
    ('Debug', 'silo_api_key'): 'sa_test',
}

LIBS = [
    {'id': '1', 'name': 'Movies', 'type': 'movies', 'paths': ['/media/Movies'], 'enabled': True},
    {'id': '2', 'name': 'TV', 'type': 'series', 'paths': ['/media/TV Shows'], 'enabled': True},
]


def _get(overrides=None):
    vals = {**SETTINGS, **(overrides or {})}
    return lambda section, key, default=None: vals.get((section, key), default)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class SiloTestBase(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(silo, 'get_setting', side_effect=_get())
        p.start()
        self.addCleanup(p.stop)
        silo._lib_cache.update(at=0.0, key=None, items=None)


class ModeTests(SiloTestBase):
    def test_silo_mode_needs_symlink_mode_and_credentials(self):
        self.assertTrue(silo.is_silo_mode())
        self.assertEqual(silo.silo_config(), ('http://silo:8090', 'sa_test'))
        with mock.patch.object(silo, 'get_setting', side_effect=_get({('Debug', 'silo_api_key'): ''})):
            self.assertFalse(silo.is_silo_mode())
        with mock.patch.object(silo, 'get_setting',
                               side_effect=_get({('File Management', 'file_collection_management'): 'Plex'})):
            self.assertFalse(silo.is_silo_mode())


class PathTests(SiloTestBase):
    def test_same_path_when_silo_sees_the_symlink_folder(self):
        libs = [{'id': '1', 'type': 'movies', 'paths': ['/mnt/symlinked/Movies']}]
        self.assertEqual(silo.to_silo_path('/mnt/symlinked/Movies/A (2020)/a.mkv', libs),
                         '/mnt/symlinked/Movies/A (2020)/a.mkv')

    def test_maps_by_library_folder_name(self):
        self.assertEqual(silo.to_silo_path('/mnt/symlinked/TV Shows/Show/Season 01/e1.mkv', LIBS),
                         '/media/TV Shows/Show/Season 01/e1.mkv')
        self.assertEqual(silo.to_silo_path('/mnt/symlinked/Movies', LIBS), '/media/Movies')

    def test_maps_a_root_named_like_the_symlink_folder(self):
        libs = [{'id': '1', 'type': 'mixed', 'paths': ['/data/symlinked']}]
        self.assertEqual(silo.to_silo_path('/mnt/symlinked/Movies/A/a.mkv', libs),
                         '/data/symlinked/Movies/A/a.mkv')

    def test_no_library_holds_it(self):
        self.assertIsNone(silo.to_silo_path('/mnt/symlinked/Anime/X/x.mkv', LIBS))
        self.assertIsNone(silo.to_silo_path('/elsewhere/Movies/x.mkv', LIBS))

    def test_prefix_is_not_a_folder_match(self):
        libs = [{'id': '1', 'type': 'movies', 'paths': ['/media/Movies']}]
        self.assertIsNone(silo.to_silo_path('/media/Movies4K/a.mkv', libs))


class ScanTests(SiloTestBase):
    def test_scan_posts_mapped_path_with_bearer_key(self):
        calls = []

        def fake(method, url, headers=None, **kw):
            calls.append((method, url, kw.get('json'), headers))
            if url.endswith('/api/v2/libraries'):
                return _Resp(200, {'items': LIBS})
            return _Resp(202, {'status': 'accepted', 'mode': 'subtree'})
        with mock.patch.object(silo.requests, 'request', side_effect=fake):
            ok = silo.silo_update_item({'full_path': '/mnt/symlinked/Movies/A (2020)', 'type': 'movie'})
        self.assertTrue(ok)
        method, url, body, headers = calls[-1]
        self.assertEqual((method, url), ('POST', 'http://silo:8090/api/v2/scan'))
        self.assertEqual(body, {'path': '/media/Movies/A (2020)'})
        self.assertEqual(headers['Authorization'], 'Bearer sa_test')

    def test_unmapped_path_scans_libraries_of_the_type(self):
        bodies = []

        def fake(method, url, headers=None, **kw):
            if url.endswith('/api/v2/libraries'):
                return _Resp(200, {'items': LIBS})
            bodies.append(kw.get('json'))
            return _Resp(202, {'mode': 'library'})
        with mock.patch.object(silo.requests, 'request', side_effect=fake):
            self.assertTrue(silo.scan_path('/mnt/symlinked/Anime/X/x.mkv', 'episode'))
        self.assertEqual(bodies, [{'library_id': '2'}])

    def test_refused_key_does_not_fall_back(self):
        bodies = []

        def fake(method, url, headers=None, **kw):
            if url.endswith('/api/v2/libraries'):
                return _Resp(200, {'items': LIBS})
            bodies.append(kw.get('json'))
            return _Resp(403, {'detail': 'not an administrator'})
        with mock.patch.object(silo.requests, 'request', side_effect=fake):
            self.assertFalse(silo.scan_path('/mnt/symlinked/Movies/A/a.mkv', 'movie'))
        self.assertEqual(len(bodies), 1)

    def test_remove_rescans_the_removed_file_path(self):
        bodies = []

        def fake(method, url, headers=None, **kw):
            if url.endswith('/api/v2/libraries'):
                return _Resp(200, {'items': LIBS})
            bodies.append(kw.get('json'))
            return _Resp(202, {'mode': 'file'})
        with mock.patch.object(silo.requests, 'request', side_effect=fake):
            self.assertTrue(silo.remove_file_from_silo('Show', '/mnt/symlinked/TV Shows/Show/S01/e1.mkv', 'Pilot'))
        self.assertEqual(bodies, [{'path': '/media/TV Shows/Show/S01/e1.mkv'}])


    def test_gone_path_walks_up_to_an_existing_folder(self):
        bodies = []

        def fake(method, url, headers=None, **kw):
            if url.endswith('/api/v2/libraries'):
                return _Resp(200, {'items': LIBS})
            body = kw.get('json')
            bodies.append(body)
            if body.get('path') in ('/media/Movies/A (2020)/a.mkv', '/media/Movies/A (2020)'):
                return _Resp(400, {'detail': 'Path does not exist'})
            return _Resp(202, {'mode': 'library'})
        with mock.patch.object(silo.requests, 'request', side_effect=fake):
            self.assertTrue(silo.remove_file_from_silo('A', '/mnt/symlinked/Movies/A (2020)/a.mkv'))
        self.assertEqual(bodies, [{'path': '/media/Movies/A (2020)/a.mkv'}, {'path': '/media/Movies/A (2020)'},
                                  {'path': '/media/Movies'}])

    def test_other_refusal_scans_the_owning_library_not_a_guessed_type(self):
        bodies = []

        def fake(method, url, headers=None, **kw):
            if url.endswith('/api/v2/libraries'):
                return _Resp(200, {'items': LIBS})
            body = kw.get('json')
            bodies.append(body)
            return _Resp(400, {'detail': 'unsupported media extension'}) if 'path' in body else _Resp(202, {})
        with mock.patch.object(silo.requests, 'request', side_effect=fake):
            self.assertTrue(silo.scan_path('/mnt/symlinked/Movies/A/a.txt'))
        self.assertEqual(bodies[-1], {'library_id': '1'})


class CatalogTests(SiloTestBase):
    def test_pages_through_catalog_and_shapes_like_emby(self):
        pages = [
            {'items': [{'type': 'movie', 'title': 'A', 'year': 2020,
                        'versions': [{'file_path': '/media/Movies/A/a.mkv'}, {'file_path': '/media/Movies/A/a-4k.mkv'}]},
                       {'type': 'series', 'title': 'Show', 'versions': []}],
             'page': {'has_more': True, 'next_cursor': 'c1'}},
            {'items': [{'type': 'episode', 'title': 'Pilot', 'series_title': 'Show', 'season_number': 1,
                        'episode_number': 1, 'versions': [{'file_path': '/media/TV Shows/Show/e1.mkv'}]}],
             'page': {'has_more': False}},
        ]
        seen = []

        def fake(method, url, headers=None, params=None, **kw):
            seen.append(dict(params))
            return _Resp(200, pages.pop(0))
        with mock.patch.object(silo.requests, 'request', side_effect=fake):
            lib = silo.get_collected_from_silo()
        self.assertEqual([m['location'] for m in lib['movies']], ['/media/Movies/A/a.mkv', '/media/Movies/A/a-4k.mkv'])
        self.assertEqual(lib['episodes'][0]['title'], 'Show')
        self.assertEqual(lib['episodes'][0]['location'], '/media/TV Shows/Show/e1.mkv')
        self.assertEqual(seen[0]['include_file_paths'], 'true')
        self.assertEqual(seen[1]['cursor'], 'c1')

    def test_failure_returns_none(self):
        with mock.patch.object(silo.requests, 'request', return_value=_Resp(403, {'detail': 'scope'})):
            self.assertIsNone(silo.get_collected_from_silo())


class ConnectionTests(SiloTestBase):
    def test_messages(self):
        with mock.patch.object(silo.requests, 'request', return_value=_Resp(200, {'state': 'available'})):
            self.assertEqual(silo.test_connection(), (True, 'Connected to Silo'))
        with mock.patch.object(silo.requests, 'request', return_value=_Resp(401, {'detail': 'bad key'})):
            ok, msg = silo.test_connection()
            self.assertFalse(ok)
            self.assertIn('admin API key', msg)
        self.assertFalse(silo.test_connection('', '')[0])


class DispatchTests(unittest.TestCase):
    def test_plex_update_item_goes_to_silo_first(self):
        from utilities.plex_functions import update
        with mock.patch('utilities.silo_functions.is_silo_mode', return_value=True), \
                mock.patch('utilities.silo_functions.silo_update_item', return_value=True) as up:
            self.assertTrue(update.plex_update_item({'full_path': '/mnt/symlinked/Movies/A'}))
        up.assert_called_once()

    def test_remove_file_from_plex_goes_to_silo_first(self):
        from utilities.plex_functions import removal
        with mock.patch('utilities.silo_functions.is_silo_mode', return_value=True), \
                mock.patch('utilities.silo_functions.remove_file_from_silo', return_value=True) as rm:
            self.assertTrue(removal.remove_file_from_plex('A', '/mnt/symlinked/Movies/A/a.mkv'))
        rm.assert_called_once_with('A', '/mnt/symlinked/Movies/A/a.mkv', None)


class ReversePathTests(SiloTestBase):
    def test_maps_silo_path_back_under_the_symlink_folder(self):
        self.assertEqual(silo.from_silo_path('/media/TV Shows/Show/S01/e1.mkv', LIBS, '/mnt/symlinked'),
                         '/mnt/symlinked/TV Shows/Show/S01/e1.mkv')

    def test_round_trip(self):
        local = '/mnt/symlinked/Movies/A (2020)/a.mkv'
        self.assertEqual(silo.from_silo_path(silo.to_silo_path(local, LIBS), LIBS, '/mnt/symlinked'), local)

    def test_next_episode_ids(self):
        self.assertEqual(silo.next_episode_ids('episode-tvdb-73244-1-9'),
                         ['episode-tvdb-73244-1-10', 'episode-tvdb-73244-2-1'])
        self.assertEqual(silo.next_episode_ids('movie-tmdb-5'), [])
        self.assertEqual(silo.next_episode_ids('episode-x-y-z'), [])


SESSION = {'session_id': 's1', 'media_item_id': 'episode-tvdb-73244-1-1', 'media_file_id': '2',
           'media_type': 'episode', 'position_seconds': 30, 'file_duration': 60, 'season_number': 1,
           'episode_number': 1, 'series_title': 'The Office (US)', 'username': 'admin',
           'video_decision': 'direct', 'is_paused': False, 'client_name': 'Web', 'client_label': 'Web 1.0'}
CATALOG = {'content_id': 'episode-tvdb-73244-1-1', 'type': 'episode', 'title': 'Pilot',
           'series_id': 'series-tvdb-73244', 'series_title': 'The Office (US)', 'season_number': 1,
           'episode_number': 1, 'versions': [{'file_id': '9', 'file_path': '/media/TV Shows/Office/e1-4k.mkv'},
                                             {'file_id': '2', 'file_path': '/media/TV Shows/Office/e1.mkv'}]}


class SessionTests(unittest.TestCase):
    def _get(self, over=None):
        cfg = {('File Management', 'file_collection_management'): 'Symlinked/Local',
               ('File Management', 'media_server_type'): 'silo',
               ('File Management', 'symlinked_files_path'): '/mnt/symlinked',
               ('Debug', 'silo_url'): 'http://silo', ('Debug', 'silo_api_key'): 'sa_x', **(over or {})}
        return lambda s, k=None, d=None: cfg.get((s, k), d)

    def test_silo_session_in_plex_shape_playing_version_first(self):
        from utilities import media_sessions as ms
        with mock.patch.object(silo, 'admin_sessions', return_value=[SESSION]), \
                mock.patch.object(silo, 'catalog_items', return_value=[CATALOG]), \
                mock.patch.object(silo, 'get_libraries', return_value=LIBS):
            got, err = ms.sessions_checked(self._get())
        self.assertEqual(err, '')
        s = got[0]
        self.assertEqual((s['ratingKey'], s['type'], s['parentIndex'], s['index']),
                         ('silo:episode-tvdb-73244-1-1', 'episode', 1, 1))
        self.assertEqual((s['viewOffset'], s['duration']), (30000, 60000))
        self.assertEqual(s['Media'][0]['Part'][0]['file'], '/mnt/symlinked/TV Shows/Office/e1.mkv')
        self.assertEqual(s['Player']['state'], 'playing')
        self.assertEqual(s['_server'], 'silo')

    def test_silo_unreachable_is_reported(self):
        from utilities import media_sessions as ms
        with mock.patch.object(silo, 'admin_sessions', side_effect=RuntimeError('down')):
            got, err = ms.sessions_checked(self._get())
        self.assertEqual(got, [])
        self.assertIn('Silo: down', err)

    def test_not_asked_when_silo_is_not_the_server(self):
        from utilities import media_sessions as ms
        get = self._get({('File Management', 'media_server_type'): 'jellyfin'})
        self.assertEqual(ms.silo_server(get), ('', ''))

    def test_next_episode(self):
        from utilities import media_sessions as ms
        nxt = dict(CATALOG, content_id='episode-tvdb-73244-1-2', episode_number=2,
                   versions=[{'file_id': '3', 'file_path': '/media/TV Shows/Office/e2.mkv'}])
        with mock.patch.object(silo, 'catalog_items', return_value=[nxt]) as cat, \
                mock.patch.object(silo, 'get_libraries', return_value=LIBS):
            got = ms.next_episode(self._get(), {'_server': 'silo', 'ratingKey': 'silo:episode-tvdb-73244-1-1'})
        self.assertEqual(cat.call_args[0][2], ['episode-tvdb-73244-1-2', 'episode-tvdb-73244-2-1'])
        self.assertEqual(got['index'], 2)
        self.assertEqual(got['Media'][0]['Part'][0]['file'], '/mnt/symlinked/TV Shows/Office/e2.mkv')


class ViewerHistoryTests(SiloTestBase):
    def test_sync_stores_completed_plays_per_profile(self):
        import tempfile
        from utilities import viewer_history as vh
        history = {'items': [
            {'user_id': '1', 'username': 'admin', 'profile_id': 'p1', 'profile_name': 'Ryan',
             'media_item_id': 'episode-tvdb-73244-1-1', 'completed': True, 'ended_at': '2026-11-22T22:40:01Z'},
            {'user_id': '2', 'username': 'kid', 'profile_id': 'p2', 'profile_name': 'Kid',
             'media_item_id': 'movie-tmdb-10', 'completed': False, 'ended_at': '2026-11-22T22:41:01Z'}],
            'page': {'has_more': False}}
        items = {'episode-tvdb-73244-1-1': {'content_id': 'episode-tvdb-73244-1-1', 'type': 'episode',
                                            'series_id': 'series-tvdb-73244'},
                 'series-tvdb-73244': {'content_id': 'series-tvdb-73244', 'type': 'series',
                                       'title': 'The Office (US)', 'tmdb_id': '2316', 'imdb_id': 'tt0386676'},
                 'movie-tmdb-10': {'content_id': 'movie-tmdb-10', 'type': 'movie', 'title': 'M', 'tmdb_id': '10'}}

        def fake(method, url, headers=None, params=None, **kw):
            if url.endswith('/admin/playback-history'):
                return _Resp(200, history)
            if url.endswith('/admin/users'):
                return _Resp(200, {'items': [{'id': '1', 'role': 'admin'}, {'id': '2', 'role': 'user'}]})
            ids = [v for k, v in params if k == 'content_ids']
            return _Resp(200, {'items': [items[i] for i in ids if i in items]})
        with tempfile.TemporaryDirectory() as d, mock.patch.dict('os.environ', {'USER_DB_CONTENT': d}), \
                mock.patch.object(silo.requests, 'request', side_effect=fake):
            self.assertEqual(vh.sync_silo(), (2, 1))
            viewers = vh.get_viewers()
            plays = vh.get_plays('silo:p1')
        self.assertEqual([v['id'] for v in viewers], ['silo:p1'])  # p2 has no completed plays
        self.assertTrue(viewers[0]['is_main'])
        self.assertEqual(plays[0][:4], ('tv', '2316', 'tt0386676', 'The Office (US)'))


class PlexOnlyPathsSkipSiloTests(unittest.TestCase):
    """Places that only checked for a Jellyfin URL treated Silo as Plex."""
    def _settings(self):
        vals = {('File Management', 'file_collection_management'): 'Symlinked/Local',
                ('File Management', 'media_server_type'): 'silo',
                ('File Management', 'plex_url_for_symlink'): 'http://stale-plex:32400',
                ('File Management', 'plex_token_for_symlink'): 't'}
        return lambda s, k=None, default=None, **kw: vals.get((s, k), kw.get('default', default))

    def test_subtitle_info_does_not_ask_a_leftover_plex(self):
        from utilities import plex_subtitle_info as psi
        with mock.patch('utilities.settings.get_setting', side_effect=self._settings()):
            self.assertIsNone(psi.server())

    def test_dead_version_cleanup_is_plex_only(self):
        from utilities.plex_functions import scan_cleanup
        with mock.patch.object(scan_cleanup, 'get_setting', side_effect=self._settings()):
            self.assertFalse(scan_cleanup.start_dead_plex_version_cleanup({'location_on_disk': '/x/a.mkv'}))


class TaskDefaultTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch('utilities.set_supervisor_env.is_limited_environment', return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_silo_symlink_mode(self):
        sections = {'File Management': {'file_collection_management': 'Symlinked/Local', 'media_server_type': 'silo'},
                    'Debug': {'silo_url': 'http://silo', 'silo_api_key': 'sa_x'}}
        on = reg.default_enabled(lambda s, k=None, d=None: sections.get(s, {}).get(k, d))
        self.assertIn('task_verify_symlinked_files', on)
        self.assertNotIn('task_plex_movie_boxsets', on)
        self.assertNotIn('task_plex_smart_collection_posters', on)

    def test_silo_turns_off_poster_overlays(self):
        sections = {'File Management': {'file_collection_management': 'Symlinked/Local', 'media_server_type': 'silo'},
                    'Overlay Settings': {'overlays_enabled': True}}
        on = reg.default_enabled(lambda s, k=None, d=None: sections.get(s, {}).get(k, d))
        self.assertNotIn('task_overlay_sync', on)


if __name__ == '__main__':
    unittest.main()
