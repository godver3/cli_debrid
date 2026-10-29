"""Discover person search / filmography helpers."""
import os
import sys
import unittest

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


if __name__ == '__main__':
    unittest.main()
