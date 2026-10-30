"""Discover person search / filmography helpers."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities.tmdb_people import filmography, person_payload, summarize_person


NOLAN = {
    'id': 525,
    'name': 'Christopher Nolan',
    'profile_path': '/nolan.jpg',
    'known_for_department': 'Directing',
    'biography': 'Director.',
    'combined_credits': {
        'cast': [
            {
                'id': 27205,
                'media_type': 'movie',
                'title': 'Inception',
                'release_date': '2010-07-16',
                'character': 'Passenger',
                'popularity': 80,
                'poster_path': '/inception.jpg',
                'vote_average': 8.4,
            },
            {
                'id': 99,
                'media_type': 'tv',
                'name': '',
                'adult': False,
            },
        ],
        'crew': [
            {
                'id': 27205,
                'media_type': 'movie',
                'title': 'Inception',
                'release_date': '2010-07-16',
                'job': 'Director',
                'popularity': 90,
                'poster_path': '/inception.jpg',
                'vote_average': 8.4,
            },
            {
                'id': 155,
                'media_type': 'movie',
                'title': 'The Dark Knight',
                'release_date': '2008-07-18',
                'job': 'Director',
                'popularity': 70,
                'vote_average': 9.0,
            },
            {
                'id': 155,
                'media_type': 'movie',
                'title': 'The Dark Knight',
                'job': 'Producer',
            },
            {
                'id': 1,
                'media_type': 'movie',
                'title': 'Adult Film',
                'job': 'Director',
                'adult': True,
            },
        ],
    },
}


class FilmographyTests(unittest.TestCase):
    def test_merges_acting_and_directing_on_the_same_title(self):
        works = filmography(NOLAN, 'all')
        inception = next(w for w in works if w['id'] == 27205)
        self.assertEqual(inception['roles'], ['acting', 'directing'])
        self.assertEqual(inception['character'], 'Passenger')
        self.assertEqual(inception['job'], 'Director')

    def test_directing_excludes_producer_and_cameo_only_titles(self):
        works = filmography(NOLAN, 'directing')
        ids = {w['id'] for w in works}
        self.assertEqual(ids, {27205, 155})

    def test_acting_is_cast_only(self):
        works = filmography(NOLAN, 'acting')
        self.assertEqual([w['id'] for w in works], [27205])

    def test_skips_adult_and_untitled_credits(self):
        ids = {w['id'] for w in filmography(NOLAN, 'all')}
        self.assertNotIn(1, ids)
        self.assertNotIn(99, ids)

    def test_newest_first(self):
        works = filmography(NOLAN, 'directing')
        self.assertEqual([w['title'] for w in works], ['Inception', 'The Dark Knight'])

    def test_person_payload_counts(self):
        payload = person_payload(NOLAN, 'directing')
        self.assertEqual(payload['person']['name'], 'Christopher Nolan')
        self.assertEqual(payload['counts'], {'all': 2, 'acting': 1, 'directing': 2})
        self.assertEqual(len(payload['results']), 2)

    def test_search_summary_uses_known_for_titles(self):
        card = summarize_person({
            'id': 31,
            'name': 'Tom Hanks',
            'known_for_department': 'Acting',
            'known_for': [
                {'title': 'Forrest Gump'},
                {'name': 'Band of Brothers'},
                {'title': 'Cast Away'},
                {'title': 'Extra'},
            ],
        })
        self.assertEqual(card['known_for'], ['Forrest Gump', 'Band of Brothers', 'Cast Away'])


class PersonCollectionMemoryTests(unittest.TestCase):
    def test_stores_any_person_under_their_tmdb_id(self):
        import json
        import tempfile
        import threading
        from unittest import mock
        try:
            from database import plex_collections as pc
        except Exception:
            self.skipTest('database.plex_collections unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'plex_collection_state.json')
            with mock.patch.object(pc, '_STATE_FILE', path), mock.patch.object(pc, '_state_lock', threading.Lock()):
                pc.remember_person_collection(31, 'Tom Hanks', 'acting', 'all', [
                    {'id': 13, 'media_type': 'movie'},
                ])
                pc.remember_person_collection(525, 'Christopher Nolan', 'directing', 'movie', [
                    {'id': 27205, 'media_type': 'movie'},
                ])
            stored = json.loads(open(path).read())['_person_collections']
        self.assertEqual(set(stored), {'31', '525'})
        self.assertEqual(stored['31']['name'], 'Tom Hanks')
        self.assertEqual(stored['525']['name'], 'Christopher Nolan')


class PersonPlexCollectionMatchingTests(unittest.TestCase):
    def test_matches_tmdb_credit_to_plex_item_by_exact_title_and_year(self):
        try:
            from database import plex_collections as pc
        except Exception:
            self.skipTest('database.plex_collections unavailable')

        work = {
            'id': 2059,
            'media_type': 'movie',
            'title': 'National Treasure',
            'release_date': '2004-11-19',
        }
        title_key = pc._title_year_key('National Treasure', 2004)
        with mock.patch.object(pc, '_get_plex_credentials', return_value=('http://plex', 'token')), \
             mock.patch.object(pc, '_get_library_sections', return_value={
                 '1': {'type': 'movie', 'title': 'Movies'},
             }), \
             mock.patch.object(pc, '_get_machine_id', return_value='machine'), \
             mock.patch.object(pc, '_get_section_id_map', return_value={title_key: '101'}), \
             mock.patch.object(pc, '_get_or_create_collection', return_value='500'), \
             mock.patch.object(pc, '_add_items_to_collection') as add:
            result = pc.create_named_collections_from_tmdb('Nicolas Cage', [work])

        add.assert_called_once_with('http://plex', 'token', 'machine', '500', ['101'])
        self.assertEqual(result['collections'][0]['added'], 1)
        self.assertEqual(result['collections'][0]['missing'], 0)

    def test_title_year_fallback_is_not_used_when_plex_match_is_ambiguous(self):
        try:
            from database import plex_collections as pc
        except Exception:
            self.skipTest('database.plex_collections unavailable')

        response = mock.Mock()
        response.json.return_value = {'MediaContainer': {'Metadata': [
            {'ratingKey': '101', 'title': 'The Movie', 'year': 2004, 'Guid': []},
            {'ratingKey': '102', 'title': 'The Movie', 'year': 2004, 'Guid': []},
        ]}}
        response.raise_for_status.return_value = None
        with mock.patch.object(pc.requests, 'get', return_value=response):
            id_map = pc._get_section_id_map('http://plex', 'token', '1', 'movie')

        self.assertNotIn(pc._title_year_key('The Movie', 2004), id_map)


if __name__ == '__main__':
    unittest.main()
