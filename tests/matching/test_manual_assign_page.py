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


class GapFillTests(unittest.TestCase):
    """Episodes a torrent has no file for can be filled from a second magnet
    (/magnet/fill_gaps builds a screen for just those) or added to Wanted at confirm."""

    CONTEXT = {k: v for k, v in RematchRouteTests.CONTEXT.items() if k != 'video_files'}

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

    def test_episode_list_parsing(self):
        self.assertEqual(self.mr._episode_list(['1:492', '1:1001', 'x:1', '2:', None]), [(1, 492), (1, 1001)])

    def test_build_assignment_for_an_episode_list(self):
        with mock.patch('metadata.metadata.get_metadata',
                        return_value={'imdb_id': 'tt0000001', 'tmdb_id': '1', 'title': 'Show', 'genres': []}), \
                mock.patch.object(self.mr.trakt_client, 'get_show_seasons_and_episodes', return_value=(SEASONS, 'test')), \
                mock.patch.object(self.mr, '_library_episode_rows', return_value=0), \
                mock.patch.object(self.mr, '_mark_items_in_library'):
            items, _ = self.mr._build_assignment(
                [_video('Show.S01E03.mkv'), _video('Show.S01E01.mkv')], '1', 'tv', 'Show', '2020', '1080p', 'T2',
                'magnet:?b', 'episode_list', ['1:2', '1:3'], None, None, None)
        self.assertEqual([(i['season_number'], i['episode_number']) for i in items], [(1, 2), (1, 3)])
        self.assertEqual(items[1]['suggested_file_path'], 'Show.S01E03.mkv')
        self.assertFalse(items[0]['assigned'])

    def test_fill_gaps_route(self):
        provider = mock.Mock()
        provider.get_torrent_file_list.return_value = (
            [{'id': 1, 'path': '/Pack/Show.S01E02.mkv', 'bytes': 9}, {'id': 2, 'path': '/Pack/info.nfo', 'bytes': 1}],
            'Other Pack', 'T2')
        built = [{'item_key': 'ep_1_s01e02', 'imdb_id': 'tt0000001', 'type': 'episode'}]
        with mock.patch.object(self.mr, 'get_debrid_provider', return_value=provider), \
                mock.patch.object(self.mr, '_build_assignment', return_value=(built, 'default')) as build:
            response = self.client.post('/magnet/fill_gaps', json={
                'magnet_link': 'magnet:?xt=urn:btih:b', 'episodes': ['1:2', 'junk'],
                'context': self.CONTEXT, 'episode_order': 'default',
                'notice': 'Successfully assigned 997 item(s). Episode order saved on the show.'})
        data = response.get_json()
        self.assertTrue(data['success'], data)
        args = build.call_args.args
        self.assertEqual([f['filename'] for f in args[0]], ['Show.S01E02.mkv'])
        self.assertEqual((args[6], args[8], args[9]), ('T2', 'episode_list', ['1:2']))
        stored = self.mr._assignment_store.pop(data['redirect_url'].split('token=')[1])
        self.assertEqual((stored['torrent_id'], stored['torrent_filename']), ('T2', 'Other Pack'))
        self.assertEqual(stored['magnet_link'], 'magnet:?xt=urn:btih:b')
        # The first screen's confirm message is shown on this one
        self.assertIn('Episode order saved', stored['notice'])

    def test_fill_gaps_rejects_bad_requests(self):
        for body in [{'magnet_link': 'http://x/y.torrent', 'episodes': ['1:2'], 'context': self.CONTEXT},
                     {'magnet_link': 'magnet:?x', 'episodes': [], 'context': self.CONTEXT},
                     {'magnet_link': 'magnet:?x', 'episodes': ['1:2'], 'context': dict(self.CONTEXT, media_type='movie')}]:
            self.assertEqual(self.client.post('/magnet/fill_gaps', json=body).status_code, 400, body)

    def test_empty_items_added_to_wanted_through_process_metadata(self):
        episodes = [{'season_number': 1, 'episode_number': e, 'imdb_id': 'tt0000001'} for e in (1, 2, 3)]
        with mock.patch('metadata.metadata.get_imdb_id_if_missing', return_value='tt0000001'), \
                mock.patch('metadata.metadata.get_metadata', return_value={'title': 'Show', 'year': 2020}), \
                mock.patch('metadata.metadata.process_metadata', return_value={'episodes': episodes}) as process, \
                mock.patch('database.wanted_items.add_wanted_items', return_value=2) as add:
            added = self.mr._add_empty_to_wanted(['ep_1_s01e02', 'ep_1_s01e03', 'bogus'], 'Anime*')
        self.assertEqual(added, 2)
        request = process.call_args.args[0][0]
        self.assertEqual((request['requested_seasons'], request['versions']), ([1], {'Anime': True}))
        items, versions = add.call_args.args
        self.assertEqual([i['episode_number'] for i in items], [2, 3])
        self.assertEqual(items[0]['content_source'], 'Magnet_Assigner')

    def test_confirm_sends_empty_items_not_ignored_ones_to_wanted(self):
        provider = mock.Mock()
        provider.add_torrent.return_value = 'T1'
        with mock.patch.object(self.mr, 'get_debrid_provider', return_value=provider), \
                mock.patch.object(self.mr, '_add_empty_to_wanted', return_value=1) as wanted:
            response = self.client.post('/magnet/confirm_manual_assignment', data={
                'magnet_link': 'magnet:?dn=pack', 'torrent_filename': 'Pack', 'torrent_id': 'T0',
                'version': '1080p', 'empty_to_wanted': '1',
                'ep_1_s01e01': '', 'ep_1_s01e02': '--ignore--'})
        data = response.get_json()
        self.assertTrue(data['success'], data)
        self.assertEqual(data['wanted_count'], 1)
        wanted.assert_called_once_with(['ep_1_s01e01'], '1080p')

    def test_nothing_picked_adds_no_torrent(self):
        provider = mock.Mock()
        with mock.patch.object(self.mr, 'get_debrid_provider', return_value=provider), \
                mock.patch.object(self.mr, '_add_empty_to_wanted', return_value=2):
            response = self.client.post('/magnet/confirm_manual_assignment', data={
                'magnet_link': 'magnet:?dn=pack', 'torrent_filename': 'Pack', 'torrent_id': 'T0',
                'version': '1080p', 'empty_to_wanted': '1', 'ep_1_s01e01': '', 'ep_1_s01e02': '--ignore--'})
        self.assertTrue(response.get_json()['success'])
        provider.add_torrent.assert_not_called()  # nothing points at it: no orphan torrent

    def test_torrent_file_upload_confirms_without_re_adding(self):
        # The page used to post the string "None" as the magnet for .torrent uploads,
        # so confirm tried to add a magnet called "None" and failed
        provider = mock.Mock()
        added = []
        with mock.patch.object(self.mr, 'get_debrid_provider', return_value=provider), \
                mock.patch('metadata.metadata.get_metadata',
                           return_value={'imdb_id': 'tt0000001', 'tmdb_id': '1', 'title': 'Movie', 'genres': []}), \
                mock.patch.object(self.mr, 'add_media_item', side_effect=lambda item, **kw: added.append(item) or 7), \
                mock.patch.object(self.mr, 'update_torrent_tracking'):
            response = self.client.post('/magnet/confirm_manual_assignment', data={
                'magnet_link': '', 'torrent_filename': 'Movie.2020', 'torrent_id': 'TFILE', 'version': '1080p',
                'is_torrent_file': '1', 'movie_1': 'Movie.2020.mkv'})
        self.assertTrue(response.get_json()['success'], response.get_json())
        provider.add_torrent.assert_not_called()
        self.assertEqual((added[0]['filled_by_torrent_id'], added[0]['filled_by_magnet']), ('TFILE', None))

    def test_confirm_without_the_option_leaves_empty_items_alone(self):
        provider = mock.Mock()
        provider.add_torrent.return_value = 'T1'
        with mock.patch.object(self.mr, 'get_debrid_provider', return_value=provider), \
                mock.patch.object(self.mr, '_add_empty_to_wanted') as wanted:
            response = self.client.post('/magnet/confirm_manual_assignment', data={
                'magnet_link': 'magnet:?dn=pack', 'torrent_filename': 'Pack', 'torrent_id': 'T0',
                'version': '1080p', 'ep_1_s01e01': ''})
        self.assertEqual(response.status_code, 400)
        wanted.assert_not_called()
