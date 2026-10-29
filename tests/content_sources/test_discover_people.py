"""Discover actor/director search, filmography, and batch request."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from flask import Flask
except ImportError:
    Flask = None

PERSON = {
    'id': 525,
    'name': 'Christopher Nolan',
    'profile_path': '/nolan.jpg',
    'known_for_department': 'Directing',
    'combined_credits': {
        'cast': [],
        'crew': [{
            'id': 27205,
            'media_type': 'movie',
            'title': 'Inception',
            'release_date': '2010-07-16',
            'job': 'Director',
            'popularity': 90,
            'vote_average': 8.4,
            'poster_path': '/inception.jpg',
        }],
    },
}


@unittest.skipUnless(Flask, 'flask not installed')
class DiscoverPeopleRoutesTests(unittest.TestCase):
    def setUp(self):
        import routes.models as models
        import routes.discover_routes as dr
        self.dr = dr
        dr._prewarm_timer.cancel()
        app = Flask(__name__)
        app.register_blueprint(dr.discover_bp, url_prefix='/discover')
        self.client = app.test_client()
        self.settings = {
            'TMDB': {'api_key': 'tmdb-key'},
            'File Management': {'file_collection_management': 'Plex'},
        }
        for patch in [
            mock.patch.object(models, 'is_user_system_enabled', return_value=False),
            mock.patch.object(dr, 'get_setting', side_effect=self._setting),
            mock.patch.object(dr, 'add_db_status_and_episode_info',
                              side_effect=lambda results, use_battery=False: None),
        ]:
            patch.start()
            self.addCleanup(patch.stop)

    def _setting(self, section, key=None, default=None):
        block = self.settings.get(section, {})
        if key is None:
            return block
        return block.get(key, default)

    def test_people_search_returns_matches(self):
        with mock.patch.object(self.dr, 'get_tmdb_json', return_value={
            'results': [{'id': 31, 'name': 'Tom Hanks', 'known_for_department': 'Acting',
                         'known_for': [{'title': 'Forrest Gump'}]}],
        }) as tmdb:
            data = self.client.get('/discover/api/people?query=tom+hanks').get_json()
        self.assertEqual(data['results'][0]['name'], 'Tom Hanks')
        self.assertIn('search/person', tmdb.call_args[0][0])

    def test_person_filmography(self):
        with mock.patch.object(self.dr, 'get_tmdb_json', return_value=PERSON):
            data = self.client.get('/discover/api/person/525?role=directing').get_json()
        self.assertEqual(data['person']['name'], 'Christopher Nolan')
        self.assertEqual(data['results'][0]['title'], 'Inception')
        self.assertEqual(data['counts']['directing'], 1)

    def test_request_batch_adds_wanted_items(self):
        with mock.patch.object(self.dr, '_request_filmography_item',
                               return_value={'id': '27205', 'title': 'Inception', 'ok': True, 'items_added': 1}):
            data = self.client.post('/discover/api/request-batch', json={
                'versions': ['1080p'],
                'items': [{'id': 27205, 'media_type': 'movie', 'title': 'Inception'}],
            }).get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['requested'], 1)

    def test_request_batch_requires_versions(self):
        response = self.client.post('/discover/api/request-batch', json={
            'items': [{'id': 1, 'media_type': 'movie', 'title': 'X'}],
        })
        self.assertEqual(response.status_code, 400)

    def test_plex_collection_uses_filmography(self):
        created = {'collections': [{'name': 'Christopher Nolan', 'added': 1, 'missing': 0}]}
        with mock.patch.object(self.dr, 'get_tmdb_json', return_value=PERSON), \
             mock.patch('database.plex_collections.create_named_collections_from_tmdb', return_value=created) as create:
            data = self.client.post('/discover/api/person/525/plex-collection', json={'role': 'directing'}).get_json()
        self.assertTrue(data['success'])
        create.assert_called_once()
        args, _ = create.call_args
        self.assertEqual(args[0], 'Christopher Nolan')
        self.assertEqual(args[1][0]['title'], 'Inception')

    def test_plex_collection_remembers_the_person_for_later_fills(self):
        created = {'collections': [{'name': 'Tom Hanks', 'added': 0, 'missing': 1}]}
        person = dict(PERSON, id=31, name='Tom Hanks')
        with mock.patch.object(self.dr, 'get_tmdb_json', return_value=person), \
             mock.patch('database.plex_collections.create_named_collections_from_tmdb', return_value=created), \
             mock.patch('database.plex_collections.remember_person_collection') as remember:
            data = self.client.post('/discover/api/person/31/plex-collection', json={'role': 'acting'}).get_json()
        self.assertTrue(data['success'])
        remember.assert_called_once()
        self.assertEqual(remember.call_args[0][0], 31)
        self.assertEqual(remember.call_args[0][1], 'Tom Hanks')


@unittest.skipUnless(Flask, 'flask not installed')
class RequestFilmographyItemTests(unittest.TestCase):
    def setUp(self):
        import routes.discover_routes as dr
        self.dr = dr
        dr._prewarm_timer.cancel()

    def test_skips_titles_without_imdb(self):
        with mock.patch('cli_battery.app.direct_api.DirectAPI.tmdb_to_imdb', return_value=(None, None)):
            result = self.dr._request_filmography_item(
                {'id': 1, 'media_type': 'movie', 'title': 'Nope'}, {'1080p': True}, 'CD-Discover')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'No IMDb ID')

    def test_adds_whole_show_when_seasons_exist(self):
        with mock.patch('cli_battery.app.direct_api.DirectAPI.tmdb_to_imdb', return_value=('tt1', 'test')), \
             mock.patch('cli_battery.app.direct_api.DirectAPI.get_show_seasons', return_value=({1: {}, 2: {}}, 'test')), \
             mock.patch('metadata.metadata.process_metadata', return_value={'episodes': [{'imdb_id': 'tt1'}]}) as meta, \
             mock.patch('database.wanted_items.add_wanted_items', return_value=12) as add:
            result = self.dr._request_filmography_item(
                {'id': 1396, 'media_type': 'tv', 'title': 'Breaking Bad'}, {'1080p': True}, 'CD-Discover')
        self.assertTrue(result['ok'])
        self.assertEqual(result['items_added'], 12)
        wanted = meta.call_args[0][0][0]
        self.assertEqual(wanted['requested_seasons'], [1, 2])
        add.assert_called_once()


if __name__ == '__main__':
    unittest.main()
