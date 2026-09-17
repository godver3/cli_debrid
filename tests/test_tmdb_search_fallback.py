#!/usr/bin/env python3
"""
Regression test for the "M. Assign never loads" report: search_trakt() (used
by the manual magnet-assign screen) and search_trakt_fast() (used by the main
search page) both hard-required a Trakt Client ID, with no fallback, even
when TMDB was configured. For any user who only set up TMDB (a very common
setup - TMDB is required for metadata generally, Trakt is optional), any
title search from either screen silently returned zero results forever.

Confirmed live: this exact user had TMDB configured but not Trakt, and their
manual-assign search for "shawshank redemption" logged
"Trakt Client ID not set. Please configure in settings." and returned [].

Both functions now fall back to a TMDB-only search when Trakt isn't
configured but TMDB is, in each function's own existing output shape so
their callers (routes/magnet_routes.py, routes/scraper_routes.py) don't need
to change at all.

utilities/web_scraper.py pulls in flask/aiohttp/scraper.scraper/
queues.adding_queue/debrid/database.poster_management at module scope -
none available in this sandbox - so those are stubbed before loading the
real module from source. The test still runs the real function bodies.
"""

import unittest
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock


def _load_web_scraper():
    def _mod(name):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
        return sys.modules[name]

    _mod('flask').request = MagicMock()
    sys.modules['flask'].url_for = lambda *a, **k: ''
    _mod('aiohttp').ClientSession = MagicMock()

    _mod('routes')
    rat = _mod('routes.api_tracker')
    rat.api = MagicMock()
    rat.api.utils = types.SimpleNamespace(quote=lambda s: s.replace(' ', '%20'))
    rpc = _mod('routes.poster_cache')
    rpc.get_cached_poster_url = lambda *a, **k: None
    rpc.cache_poster_url = lambda *a, **k: None
    rpc.get_cached_media_meta = lambda *a, **k: None
    rpc.cache_media_meta = lambda *a, **k: None

    _mod('scraper')
    sc = _mod('scraper.scraper')
    sc.scrape = lambda *a, **k: []

    _mod('queues')
    aq = _mod('queues.adding_queue')
    aq.AddingQueue = MagicMock

    dbg = _mod('debrid')
    dbg.extract_hash_from_magnet = lambda *a, **k: ''
    dbg.get_debrid_provider = lambda *a, **k: None
    dbase = _mod('debrid.base')
    dbase.DebridProvider = object

    _mod('database')
    dpm = _mod('database.poster_management')
    dpm.get_poster_url = lambda *a, **k: None

    _mod('utilities')
    uss = _mod('utilities.settings')
    _settings = {}
    uss.get_setting = lambda section, key=None, default=None: (
        _settings.get(section, {}).get(key, default) if key is not None else _settings.get(section, {})
    )

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'utilities', 'web_scraper.py')
    spec = importlib.util.spec_from_file_location('web_scraper_tmdb_fallback_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, rat.api, _settings


ws, mock_api, settings = _load_web_scraper()


def _tmdb_response(results):
    resp = MagicMock()
    resp.json.return_value = {'results': results}
    resp.raise_for_status.return_value = None
    return resp


class TestTmdbSearchFallback(unittest.TestCase):
    def setUp(self):
        settings.clear()
        settings['Trakt'] = {'client_id': ''}
        settings['TMDB'] = {'api_key': 'fake-tmdb-key'}
        mock_api.get.reset_mock(side_effect=True)
        ws._search_cache.cache.clear() if hasattr(ws._search_cache, 'cache') else None

    def test_search_trakt_falls_back_to_tmdb_when_no_trakt_client_id(self):
        movie_resp = _tmdb_response([
            {'id': 278, 'title': 'The Shawshank Redemption', 'release_date': '1994-09-23',
             'poster_path': '/poster.jpg', 'backdrop_path': '/bg.jpg', 'vote_count': 25000},
        ])
        tv_resp = _tmdb_response([])
        mock_api.get.side_effect = [movie_resp, tv_resp]

        results = ws.search_trakt('shawshank redemption')

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'The Shawshank Redemption')
        self.assertEqual(results[0]['year'], 1994)
        self.assertEqual(results[0]['id'], '278')
        self.assertEqual(results[0]['media_type'], 'movie')
        self.assertEqual(results[0]['posterPath'], '/poster.jpg')
        self.assertEqual(results[0]['backdropPath'], '/bg.jpg')

    def test_search_trakt_returns_empty_with_neither_provider_configured(self):
        settings['TMDB'] = {'api_key': ''}
        results = ws.search_trakt('anything')
        self.assertEqual(results, [])
        mock_api.get.assert_not_called()

    def test_search_trakt_fast_falls_back_to_tmdb(self):
        movie_resp = _tmdb_response([
            {'id': 278, 'title': 'The Shawshank Redemption', 'release_date': '1994-09-23',
             'poster_path': '/poster.jpg', 'backdrop_path': '/bg.jpg', 'overview': 'Two imprisoned men...',
             'vote_average': 8.7, 'vote_count': 25000, 'genre_ids': [18, 80]},
        ])
        tv_resp = _tmdb_response([])
        mock_api.get.side_effect = [movie_resp, tv_resp]

        results = ws.search_trakt_fast('shawshank redemption')

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r['title'], 'The Shawshank Redemption')
        self.assertEqual(r['year'], 1994)
        self.assertEqual(r['id'], '278')
        self.assertEqual(r['media_type'], 'movie')
        self.assertEqual(r['poster_path'], '/poster.jpg')
        self.assertEqual(r['vote_average'], 8.7)

    def test_search_trakt_fast_skips_results_without_poster(self):
        movie_resp = _tmdb_response([
            {'id': 1, 'title': 'No Poster Movie', 'release_date': '2020-01-01', 'poster_path': None},
        ])
        tv_resp = _tmdb_response([])
        mock_api.get.side_effect = [movie_resp, tv_resp]

        results = ws.search_trakt_fast('no poster movie')
        self.assertEqual(results, [])

    def test_search_trakt_uses_real_trakt_when_client_id_present(self):
        settings['Trakt'] = {'client_id': 'real-trakt-id'}
        trakt_resp = MagicMock()
        trakt_resp.json.return_value = []
        trakt_resp.raise_for_status.return_value = None
        trakt_resp.headers = {}
        mock_api.get.return_value = trakt_resp

        ws.search_trakt('anything')

        called_url = mock_api.get.call_args[0][0]
        self.assertIn('api.trakt.tv', called_url)


if __name__ == '__main__':
    unittest.main()
