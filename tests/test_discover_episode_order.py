"""Requesting a show from Discover in another episode order.

Dragon Ball Kai has 2 seasons in TVDB's aired order and more in its DVD
order. The request dialogs can now pick the order: /content/show_seasons
lists the seasons in it, and /content/request saves it on the show before
its seasons and episodes are read, unless the show already has library
episodes (that goes through Show options, which moves what is there).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DVD_SEASONS = {0: {'episode_count': 2}, 1: {'episode_count': 26}, 2: {'episode_count': 26}, 3: {'episode_count': 26}}

# Other tests leave stand-ins for flask and project modules in sys.modules
# (test_stats_resilience stubs flask itself). Inside mock.patch.dict, drop
# them so the real modules load; everything is put back after each test.
_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT')


def _fresh_app_modules():
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401  (app import order; routes first is circular)
    return patcher


class DiscoverEpisodeOrderTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.content_requestor_routes as cr
        import routes.magnet_routes as mr
        import routes.models as models
        self.cr, self.mr = cr, mr

        app = Flask(__name__)
        app.register_blueprint(cr.content_requestor_bp, url_prefix='/content')
        self.client = app.test_client()

        self.saved = mock.Mock()
        self.calls = []
        self.saved.side_effect = lambda *a: self.calls.append('save')
        for target, kwargs in [
            (mock.patch.object(cr, 'is_user_system_enabled', return_value=False), None),
            (mock.patch.object(models, 'is_user_system_enabled', return_value=False), None),
            (mock.patch.object(cr.DirectAPI, 'tmdb_to_imdb', return_value=('tt1207808', 'test')), None),
            (mock.patch.object(cr.DirectAPI, 'get_show_seasons', side_effect=self._show_seasons), None),
            (mock.patch.object(cr, 'add_wanted_items', return_value=78), None),
            (mock.patch('metadata.metadata.process_metadata', return_value={'episodes': [{'imdb_id': 'tt1207808'}]}), None),
            (mock.patch.object(mr, '_saved_episode_order', return_value='default'), None),
            (mock.patch.object(mr, '_seasons_for_order', return_value=DVD_SEASONS), None),
            (mock.patch.object(mr, '_save_episode_order', self.saved), None),
            (mock.patch.object(mr, '_sync_plex_order_for_new_show',
                               return_value='Once the show appears in Plex, set its episode ordering there to match.'), None),
        ]:
            target.start()
            self.addCleanup(target.stop)
        self.rows = mock.patch.object(mr, '_library_episode_rows', return_value=0)
        self.rows.start()
        self.addCleanup(self.rows.stop)

    def _show_seasons(self, imdb_id):
        self.calls.append('seasons')
        return ({1: {}, 2: {}, 3: {}}, 'test')

    def _request(self, **extra):
        body = {'id': 61709, 'mediaType': 'tv', 'title': 'Dragon Ball Z Kai', 'versions': ['1080p'], **extra}
        return self.client.post('/content/request', json=body)

    def test_order_saved_before_the_seasons_are_read(self):
        response = self._request(episode_order='tvdb:dvd')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.saved.assert_called_once_with('tt1207808', '61709', 'tvdb:dvd')
        self.assertEqual(self.calls, ['save', 'seasons'])
        self.assertIn('set its episode ordering there', response.get_json()['message'])

    def test_no_order_leaves_the_show_alone(self):
        self.assertEqual(self._request().status_code, 200)
        self.saved.assert_not_called()

    def test_current_order_is_not_saved_again(self):
        with mock.patch.object(self.mr, '_saved_episode_order', return_value='tvdb:dvd'):
            self.assertEqual(self._request(episode_order='tvdb:dvd').status_code, 200)
        self.saved.assert_not_called()

    def test_show_in_library_must_use_show_options(self):
        with mock.patch.object(self.mr, '_library_episode_rows', return_value=61):
            response = self._request(episode_order='tvdb:dvd')
        self.assertEqual(response.status_code, 409)
        self.assertIn('Show options', response.get_json()['error'])
        self.saved.assert_not_called()

    def test_order_that_cannot_load_is_not_saved(self):
        with mock.patch.object(self.mr, '_seasons_for_order', return_value=None):
            self.assertEqual(self._request(episode_order='tvdb:dvd').status_code, 502)
        self.saved.assert_not_called()

    def test_unknown_order_rejected(self):
        self.assertEqual(self._request(episode_order='bogus').status_code, 400)
        self.saved.assert_not_called()

    def test_only_admins_change_the_order(self):
        user = mock.Mock(is_authenticated=True, role='user')
        with mock.patch.object(self.cr, 'is_user_system_enabled', return_value=True), \
                mock.patch.object(self.cr, 'current_user', user):
            self.assertFalse(self.cr._can_set_episode_order())
            user.role = 'admin'
            self.assertTrue(self.cr._can_set_episode_order())

    def test_show_seasons_in_a_picked_order(self):
        data = self.client.get('/content/show_seasons?tmdb_id=61709&order=tvdb:dvd').get_json()
        self.assertEqual(data['seasons'], [1, 2, 3])
        self.assertEqual(data['episode_counts'], {'1': 26, '2': 26, '3': 26})
        self.saved.assert_not_called()

    def test_show_seasons_default_order_unchanged(self):
        data = self.client.get('/content/show_seasons?tmdb_id=61709').get_json()
        self.assertEqual(data['seasons'], [1, 2, 3])
        self.assertNotIn('episode_counts', data)


class DiscoverShowsSavedOrderTests(unittest.TestCase):
    """A show set to DVD order on its page is shown in that order on Discover."""

    ORDERED = {
        0: {'episode_count': 1, 'episodes': {1: {'title': 'Special'}}},
        1: {'episode_count': 2, 'episodes': {1: {'title': 'Prologue', 'first_aired': '2009-04-05T00:00:00Z', 'runtime': 24},
                                             2: {'title': 'Second', 'first_aired': '2009-04-12T00:00:00Z', 'runtime': 24}}},
        5: {'episode_count': 1, 'episodes': {1: {'title': 'Fifth DVD season opener', 'first_aired': '2010-01-01T00:00:00Z'}}},
    }

    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.discover_routes as dr
        import routes.models as models
        self.dr = dr
        app = Flask(__name__)
        app.register_blueprint(dr.discover_bp, url_prefix='/discover')
        self.client = app.test_client()
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = []
        tmdb_season = mock.Mock(status_code=200)
        tmdb_season.json.return_value = {'season_number': 1, 'name': 'Season 1', 'episodes': [
            {'episode_number': 1, 'name': 'TMDB numbering', 'still_path': '/wrong.jpg', 'overview': 'other episode'}]}
        for patch in [
            mock.patch.object(models, 'is_user_system_enabled', return_value=False),
            mock.patch.object(dr, 'get_setting', side_effect=lambda section, key=None, default=None: 'key'),
            mock.patch.object(dr.requests, 'get', return_value=tmdb_season),
            mock.patch('cli_battery.app.direct_api.DirectAPI.tmdb_to_imdb', return_value=('tt1409055', 'test')),
            mock.patch('database.core.get_db_connection', return_value=conn),
        ]:
            patch.start()
            self.addCleanup(patch.stop)

    def test_season_list_from_ordered_seasons(self):
        self.assertEqual(self.dr._season_list(self.ORDERED), [
            {'season_number': 0, 'episode_count': 1, 'name': 'Season 0'},
            {'season_number': 1, 'episode_count': 2, 'name': 'Season 1'},
            {'season_number': 5, 'episode_count': 1, 'name': 'Season 5'},
        ])

    def test_no_saved_order_means_none(self):
        with mock.patch('cli_battery.app.episode_order.show_episode_order', return_value=None):
            self.assertIsNone(self.dr._show_order_seasons('tt1409055'))

    def test_season_tab_lists_the_saved_order(self):
        with mock.patch.object(self.dr, '_show_order_seasons', return_value=self.ORDERED):
            data = self.client.get('/discover/details/61709/tv/season/1').get_json()
        self.assertEqual([e['name'] for e in data['episodes']], ['Prologue', 'Second'])
        self.assertEqual(data['episodes'][0]['air_date'], '2009-04-05')
        # TMDB's still/overview belong to its own episode 1, not this one
        self.assertIsNone(data['episodes'][0]['still_path'])

    def test_season_missing_from_the_saved_order(self):
        with mock.patch.object(self.dr, '_show_order_seasons', return_value=self.ORDERED):
            self.assertEqual(self.client.get('/discover/details/61709/tv/season/3').status_code, 404)

    def test_default_order_unchanged(self):
        with mock.patch.object(self.dr, '_show_order_seasons', return_value=None), \
                mock.patch('cli_battery.app.tvdb_client.get_show_data', return_value=None):
            data = self.client.get('/discover/details/61709/tv/season/1').get_json()
        self.assertEqual([e['name'] for e in data['episodes']], ['TMDB numbering'])


if __name__ == '__main__':
    unittest.main()
