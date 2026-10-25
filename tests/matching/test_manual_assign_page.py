"""Manual assign page helpers.

The page shows why each episode got its file (S/E, absolute number, title,
largest file), what each file's name says it holds, and can rebuild the
whole screen in another episode order from the file list it already has
(/magnet/rematch_manual_assignment), without adding the torrent again.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities.manual_assign_episodes import file_position_label  # noqa: E402

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT')


def _fresh_app_modules():
    # Other tests leave stand-ins for flask and project modules in sys.modules
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401  (app import order; routes first is circular)
    return patcher


class FilePositionLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(file_position_label({'seasons': [2], 'episodes': [5]}), 'S02E05')
        self.assertEqual(file_position_label({'seasons': [1], 'episodes': [2, 1]}), 'S01E01-02')
        self.assertEqual(file_position_label({'seasons': [], 'episodes': [7]}), 'E07')
        self.assertEqual(file_position_label({'seasons': [1, 2], 'episodes': [3]}), 'E03')
        self.assertEqual(file_position_label({'seasons': [13], 'episodes': [1]}, [422]), '#422')
        self.assertEqual(file_position_label({}, [2, 1]), '#001-002')
        self.assertEqual(file_position_label({'seasons': [1], 'episodes': []}), '')
        self.assertEqual(file_position_label({}), '')


SEASONS = {1: {'episodes': {1: {'title': 'Pilot'}, 2: {'title': 'The Second One'},
                            3: {'title': 'A Very Particular Title'}, 4: {'title': 'Episode 4'}}}}


def _video(path, size=1_000_000_000):
    return {'id': 1, 'path': path, 'filename': os.path.basename(path), 'bytes': size}


class BuildAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        import routes.magnet_routes as mr
        self.mr = mr
        for patcher in [
            mock.patch('metadata.metadata.get_metadata',
                       return_value={'imdb_id': 'tt0000001', 'tmdb_id': '1', 'title': 'Show', 'genres': []}),
            mock.patch.object(mr.trakt_client, 'get_show_seasons_and_episodes', return_value=(SEASONS, 'test')),
            mock.patch.object(mr, '_saved_episode_order', return_value='default'),
            mock.patch.object(mr, '_library_episode_rows', return_value=0),
            mock.patch.object(mr, '_mark_items_in_library'),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _build(self, files, order=None):
        return self.mr._build_assignment(files, '1', 'tv', 'Show', '2020', '1080p', 'TID', 'magnet:?xt=urn:btih:x',
                                         'seasons', ['1'], None, None, order)

    def test_reasons_and_file_labels(self):
        files = [_video('Show/Show.S01E01.1080p.mkv'), _video('Show/Show.S01E02.1080p.mkv'),
                 _video('Show/A Very Particular Title.mkv'), _video('Show/NCOP 01.mkv')]
        items, order = self._build(files)
        self.assertEqual(order, 'default')
        by_episode = {i['episode_number']: i for i in items}
        self.assertEqual(by_episode[1]['match_reason'], 'se')
        self.assertEqual(by_episode[2]['suggested_file_path'], 'Show.S01E02.1080p.mkv')
        self.assertEqual(by_episode[3]['match_reason'], 'title')
        self.assertGreaterEqual(by_episode[3]['match_score'], 85)
        self.assertFalse(by_episode[4]['assigned'])
        self.assertNotIn('match_reason', by_episode[4])
        by_name = {f['filename']: f for f in files}
        self.assertEqual(by_name['Show.S01E01.1080p.mkv']['label'], 'S01E01')
        self.assertEqual(by_name['NCOP 01.mkv']['skip_reason'], 'OP/ED extra')

    def test_double_episode_file_is_an_se_match_on_both(self):
        items, _ = self._build([_video('Show/Show.S01E01-E02.1080p.mkv')])
        by_episode = {i['episode_number']: i for i in items}
        self.assertEqual([by_episode[e].get('match_reason') for e in (1, 2)], ['se', 'se'])
        self.assertEqual(by_episode[2]['suggested_file_path'], 'Show.S01E01-E02.1080p.mkv')

    def test_blocked_order_raises(self):
        self.mr._library_episode_rows.return_value = 3
        with self.assertRaises(self.mr._AssignmentError) as raised:
            self._build([_video('Show.S01E01.mkv')], order='tvdb:dvd')
        self.assertEqual(raised.exception.status, 409)


class RematchRouteTests(unittest.TestCase):
    CONTEXT = {'tmdb_id': '1', 'media_type': 'tv', 'title': 'Show', 'year': '2020', 'version': '1080p',
               'selection_type': 'seasons', 'selected_seasons': ['1'], 'season': None, 'episode': None,
               'torrent_id': 'TID', 'magnet_link': 'magnet:?xt=urn:btih:x', 'torrent_filename': 'Show S01',
               'is_torrent_file': False,
               'video_files': [{'id': 1, 'path': 'Show/Show.S01E01.mkv', 'filename': 'Show.S01E01.mkv',
                                'bytes': 5, 'label': 'S01E01'}]}

    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.magnet_routes as mr
        import routes.models as models
        self.mr = mr
        app = Flask(__name__)
        app.register_blueprint(mr.magnet_bp, url_prefix='/magnet')
        self.client = app.test_client()
        for patcher in [mock.patch.object(models, 'is_user_system_enabled', return_value=False),
                        mock.patch.object(mr, '_saved_episode_order', return_value='default')]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_rebuilds_from_the_posted_files_in_the_new_order(self):
        built = [{'item_key': 'ep_1_s01e01', 'imdb_id': 'tt0000001', 'type': 'episode'}]
        with mock.patch.object(self.mr, '_build_assignment',
                               return_value=(built, 'tvdb:dvd')) as build:
            response = self.client.post('/magnet/rematch_manual_assignment',
                                        json={'context': self.CONTEXT, 'episode_order': 'tvdb:dvd'})
        data = response.get_json()
        self.assertTrue(data['success'], data)
        args = build.call_args.args
        # Only the file fields the debrid gave are carried over, labels are recomputed
        self.assertEqual(args[0], [{'id': 1, 'path': 'Show/Show.S01E01.mkv', 'filename': 'Show.S01E01.mkv', 'bytes': 5}])
        self.assertEqual(args[-1], 'tvdb:dvd')
        token = data['redirect_url'].split('token=')[1]
        stored = self.mr._assignment_store.pop(token)
        self.assertEqual(stored['episode_order'], 'tvdb:dvd')  # saved on the show at confirm
        self.assertEqual(stored['current_episode_order'], 'tvdb:dvd')
        self.assertEqual(stored['torrent_id'], 'TID')

    def test_rejects_bad_requests(self):
        bad_order = self.client.post('/magnet/rematch_manual_assignment',
                                     json={'context': self.CONTEXT, 'episode_order': 'bogus'})
        self.assertEqual(bad_order.status_code, 400)
        movie = self.client.post('/magnet/rematch_manual_assignment',
                                 json={'context': dict(self.CONTEXT, media_type='movie'), 'episode_order': 'default'})
        self.assertEqual(movie.status_code, 400)
        no_files = self.client.post('/magnet/rematch_manual_assignment',
                                    json={'context': dict(self.CONTEXT, video_files=[]), 'episode_order': 'default'})
        self.assertEqual(no_files.status_code, 400)

    def test_blocked_order_is_reported(self):
        with mock.patch.object(self.mr, '_build_assignment',
                               side_effect=self.mr._AssignmentError('Change it on the show page', 409)):
            response = self.client.post('/magnet/rematch_manual_assignment',
                                        json={'context': self.CONTEXT, 'episode_order': 'tvdb:dvd'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('show page', response.get_json()['error'])


class PageDataTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        import routes.magnet_routes as mr
        self.mr = mr

    def test_compact_items_and_files(self):
        data = self.mr._assignment_page_data({
            'target_items': [{'item_key': 'ep_1_s01e01', 'type': 'episode', 'season_number': 1, 'episode_number': 1,
                              'episode_title': 'Pilot', 'suggested_file_path': 'a.mkv', 'match_reason': 'se',
                              'in_library': True, 'genres': 'x', '_matcher_data': {}}],
            'video_files': [{'filename': 'a.mkv', 'path': 'S/a.mkv', 'bytes': 1, 'label': 'S01E01', 'id': 9}],
            'current_episode_order': 'tvdb:dvd',
            'rematch_context': dict(RematchRouteTests.CONTEXT),
        })
        self.assertEqual(data['items'][0]['file'], 'a.mkv')
        self.assertEqual(data['items'][0]['reason'], 'se')
        self.assertNotIn('_matcher_data', data['items'][0])
        self.assertNotIn('id', data['files'][0])
        self.assertNotIn('video_files', data['context'])
        self.assertTrue(data['is_show'])
        self.assertEqual(data['current_order'], 'tvdb:dvd')


if __name__ == '__main__':
    unittest.main()
