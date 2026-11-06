"""run_async_fetches only requests details for titles it has not resolved before.

Every watchlist run fetched every title's Plex details just to read its IMDb/TMDB
GUIDs, though a Plex metadata key always points at the same title. A 63-title
friends watchlist on a 30s interval made 63 requests per run.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import content_checkers.plex_watchlist as pw


def _items(*keys):
    return [{'title': k, 'url': f'https://plex.test/library/metadata/{k}',
             'original_plex_item': SimpleNamespace(title=k)} for k in keys]


class PlexWatchlistIdCacheTests(unittest.TestCase):
    def setUp(self):
        self.fetched = []
        self.responses = {
            'a': {'imdb_id': 'tt1', 'tmdb_id': '1', 'media_type': 'movie'},
            'b': {'imdb_id': None, 'tmdb_id': '2', 'media_type': 'show'},
            'noids': {'imdb_id': None, 'tmdb_id': None, 'media_type': 'show'},
            'err': {'imdb_id': None, 'tmdb_id': None, 'media_type': None, 'error': 'Timeout'},
        }

        async def fake_fetch(session, item_data, token):
            self.fetched.append(item_data['title'])
            return {**self.responses[item_data['title']], 'original_plex_item': item_data['original_plex_item']}

        patch = mock.patch.object(pw, 'fetch_item_details_and_extract_ids', side_effect=fake_fetch)
        patch.start()
        self.addCleanup(patch.stop)
        cache = mock.patch.object(pw, '_ITEM_IDS_CACHE', {})
        cache.start()
        self.addCleanup(cache.stop)

    def _run(self, items):
        return asyncio.run(pw.run_async_fetches(items, 'token'))

    def test_second_run_uses_cache(self):
        self._run(_items('a', 'b'))
        self.fetched.clear()
        items = _items('a', 'b')
        results = self._run(items)
        self.assertEqual(self.fetched, [])
        self.assertEqual([(r['imdb_id'], r['tmdb_id'], r['media_type']) for r in results],
                         [('tt1', '1', 'movie'), (None, '2', 'show')])
        # Each result carries this run's item, not the one cached from the first run
        self.assertIs(results[0]['original_plex_item'], items[0]['original_plex_item'])

    def test_only_new_titles_fetched_and_order_kept(self):
        self._run(_items('a'))
        self.fetched.clear()
        results = self._run(_items('b', 'a'))
        self.assertEqual(self.fetched, ['b'])
        self.assertEqual([r['original_plex_item'].title for r in results], ['b', 'a'])

    def test_errors_and_missing_ids_not_cached(self):
        self._run(_items('noids', 'err'))
        self.fetched.clear()
        results = self._run(_items('noids', 'err'))
        self.assertEqual(sorted(self.fetched), ['err', 'noids'])
        self.assertEqual(results[1]['error'], 'Timeout')

    def test_cache_cleared_when_over_cap(self):
        with mock.patch.object(pw, '_ITEM_IDS_CACHE_MAX', 0):
            pw._ITEM_IDS_CACHE['https://plex.test/old'] = ('tt9', None, 'movie')
            self._run(_items('a'))
        self.assertNotIn('https://plex.test/old', pw._ITEM_IDS_CACHE)
        self.assertIn('https://plex.test/library/metadata/a', pw._ITEM_IDS_CACHE)


if __name__ == '__main__':
    unittest.main()
