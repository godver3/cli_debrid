import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from utilities import library_recommendations as lr
from utilities import recommendation_provider


def _rec(tmdb_id, votes=100, poster='/p.jpg'):
    return {'id': tmdb_id, 'title': f't{tmdb_id}', 'poster_path': poster, 'vote_count': votes}


class LibraryRecommendationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (
            ('_db_dir', self.tmp.name),
            ('_CACHE_FILE', os.path.join(self.tmp.name, 'cache.json')),
            ('_file_cache', None),
        ):
            p = patch.object(lr, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.library = []
        self.blacklist = {}
        self.tmdb = {}
        self.tmdb_calls = []
        for target, fake in (
            ('_library_rows', lambda: self.library),
            ('get_tmdb_json', self._fake_tmdb),
        ):
            p = patch.object(lr, target, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)
        p = patch('database.manual_blacklist.get_manual_blacklist', side_effect=lambda: self.blacklist)
        p.start()
        self.addCleanup(p.stop)

    def _fake_tmdb(self, url, timeout=10):
        self.tmdb_calls.append(url)
        for fragment, data in self.tmdb.items():
            if fragment in url:
                return data
        return {'results': []}

    def _watch(self, rows):
        conn = sqlite3.connect(os.path.join(self.tmp.name, 'watch_history.db'))
        conn.execute(
            'CREATE TABLE watch_history (title TEXT, type TEXT, watched_at TIMESTAMP, '
            'imdb_id TEXT, tmdb_id TEXT, show_title TEXT, season INTEGER, episode INTEGER)'
        )
        conn.executemany(
            'INSERT INTO watch_history (title, type, watched_at, imdb_id, tmdb_id, show_title) '
            'VALUES (?, ?, ?, ?, ?, ?)', rows)
        conn.commit()
        conn.close()

    def test_titles_suggested_by_more_seeds_rank_higher_and_known_titles_drop(self):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._watch([
            ('Heat', 'movie', now, None, '949', None),
            ('Ronin', 'movie', now, None, '8195', None),
        ])
        # 500 is already wanted, 8195 is watched, 1 has no poster, 2 has too few votes
        self.library = [('movie', '500', 0, 'Reservoir Dogs', None)]
        self.tmdb = {
            'movie/949/recommendations': {'results': [
                _rec(10), _rec(20), _rec(500), _rec(8195), _rec(1, poster=None), _rec(2, votes=3)]},
            'movie/8195/recommendations': {'results': [_rec(20)]},
        }

        results = lr.build_recommendations('key')

        self.assertEqual([r['id'] for r in results], [20, 10])
        self.assertEqual(results[0]['rec_reason'], 'Because you watched Ronin and Heat')
        self.assertTrue(all(r['media_type'] == 'movie' for r in results))

    def test_episodes_seed_the_show_and_collection_fills_in(self):
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d %H:%M:%S')
        self._watch([('Pilot', 'episode', old, None, '1399', 'Game of Thrones')])
        self.library = [('movie', '27205', 1, 'Inception', datetime.now().isoformat())]
        self.tmdb = {
            'tv/1399/recommendations': {'results': [_rec(1396)]},
            'movie/27205/recommendations': {'results': [_rec(157336)]},
        }

        results = {(r['media_type'], r['id']): r for r in lr.build_recommendations('key')}

        self.assertEqual(results[('tv', 1396)]['rec_reason'], 'Because you watched Game of Thrones')
        self.assertEqual(results[('movie', 157336)]['rec_reason'], 'Because you collected Inception')

    def test_seed_results_are_cached_and_blacklist_excludes(self):
        self.library = [('movie', '949', 1, 'Heat', datetime.now().isoformat())]
        self.blacklist = {'tt0000001': {'media_type': 'movie'}}
        self.tmdb = {
            'find/tt0000001': {'movie_results': [{'id': 10}]},
            'movie/949/recommendations': {'results': [_rec(10), _rec(20)]},
        }

        self.assertEqual([r['id'] for r in lr.build_recommendations('key')], [20])
        calls = len(self.tmdb_calls)
        lr._file_cache = None  # forces a reload from disk, like a restart
        self.assertEqual([r['id'] for r in lr.build_recommendations('key')], [20])
        self.assertEqual(len(self.tmdb_calls), calls)

    def test_rewatches_count_most_and_shows_get_depth(self):
        self.assertEqual(lr._taste_weight('movie', 1, 1), 1)
        self.assertEqual(lr._taste_weight('movie', 2, 1), 2)
        self.assertEqual(lr._taste_weight('movie', 9, 1), 4)  # capped at 4 watches
        self.assertEqual(lr._taste_weight('tv', 20, 20), 2)       # 20 distinct episodes
        self.assertEqual(lr._taste_weight('tv', 40, 20), 4)       # watched through twice

    def test_old_rewatched_favourite_outranks_one_recent_watch(self):
        now = datetime.now()
        old = (now - timedelta(days=500)).strftime('%Y-%m-%d %H:%M:%S')
        self._watch([
            ('Heat', 'movie', old, None, '949', None),
            ('Heat', 'movie', (now - timedelta(days=520)).strftime('%Y-%m-%d %H:%M:%S'), None, '949', None),
            ('Heat', 'movie', (now - timedelta(days=540)).strftime('%Y-%m-%d %H:%M:%S'), None, '949', None),
            ('Ronin', 'movie', now.strftime('%Y-%m-%d %H:%M:%S'), None, '8195', None),
        ])
        seeds, _ = lr._collect_seeds('key')
        weights = {s['title']: s['weight'] for s in seeds}
        self.assertGreater(weights['Heat'], weights['Ronin'])

    def test_movies_and_shows_have_separate_seed_budgets(self):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rows = [(f'm{i}', 'movie', now, None, str(1000 + i), None) for i in range(30)]
        rows += [(f'e{i}', 'episode', now, None, '1399', 'Game of Thrones') for i in range(3)]
        self._watch(rows)
        seeds, _ = lr._collect_seeds('key')
        kinds = [s['key'][0] for s in seeds]
        self.assertEqual(kinds.count('movie'), lr.SEEDS_PER_TYPE)
        self.assertEqual(kinds.count('tv'), 1)

    def test_nothing_to_seed_from_returns_empty(self):
        self.assertEqual(lr.build_recommendations('key'), [])
        self.assertEqual(self.tmdb_calls, [])


class LibraryProviderSelectionTest(unittest.TestCase):
    @patch('utilities.recommendation_provider.get_setting')
    def test_tmdb_key_alone_selects_library(self, get_setting):
        get_setting.side_effect = lambda section, key, default='': (
            'tmdb-key' if (section, key) == ('TMDB', 'api_key') else default)
        with patch('content_checkers.scrob.get_scrob_config', return_value=None):
            self.assertEqual(recommendation_provider.get_recommendation_provider(), 'library')


if __name__ == '__main__':
    unittest.main()
