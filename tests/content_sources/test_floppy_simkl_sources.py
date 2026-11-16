"""Floppy and Simkl content sources.

Floppy row shapes are copied from a real Floppy instance (v26.11.12); Simkl
shapes follow api.simkl.org's /sync/all-items and OAuth2 device-flow docs.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import database  # noqa: F401  (import order: other modules hit a circular import when first)
from content_checkers import floppy, simkl, tracker_common

IMDB = {('603', 'movie'): 'tt0133093', ('1399', 'tv'): 'tt0944947', ('27205', 'movie'): 'tt1375666',
        ('1668', 'tv'): 'tt0108778'}


def fake_tmdb(tmdb_id, media_type):
    return IMDB.get((str(tmdb_id), media_type))


def db(rows=()):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE media_items (imdb_id TEXT, title TEXT, state TEXT, ghostlisted INTEGER)')
    conn.executemany('INSERT INTO media_items VALUES (?,?,?,?)', rows)
    return conn


def floppy_row(media_id, media_type, status=0, season=None, source='tmdb', ids=None):
    return {'id': 1, 'item': {'media_id': media_id, 'source': source, 'media_type': media_type,
                              'title': 'x', 'ids': ids or {}, 'season_number': season},
            'item_id': f'{media_type}/{source}/{media_id}', 'status': status}


def page(results, nxt=None):
    return {'pagination': {'total': len(results), 'next': nxt}, 'results': results}


class GateTests(unittest.TestCase):
    def run_gate(self, entries, rows=(), unblacklist=False):
        with patch('database.core.get_db_connection', return_value=db(rows)):
            return tracker_common.gate_and_merge(entries, 'Test', unblacklist=unblacklist)

    def test_blacklisted_and_ghostlisted_are_dropped(self):
        out = self.run_gate([{'imdb_id': 'tt1', 'media_type': 'movie'},
                             {'imdb_id': 'tt2', 'media_type': 'movie'},
                             {'imdb_id': 'tt3', 'media_type': 'movie'}],
                            rows=[('tt1', 'A', 'Blacklisted', 0), ('tt2', 'B', 'Wanted', 1)])
        self.assertEqual(out, [{'imdb_id': 'tt3', 'media_type': 'movie'}])

    def test_unblacklist_lets_blacklisted_through_but_not_ghostlisted(self):
        out = self.run_gate([{'imdb_id': 'tt1', 'media_type': 'movie'}, {'imdb_id': 'tt2', 'media_type': 'movie'}],
                            rows=[('tt1', 'A', 'Blacklisted', 0), ('tt2', 'B', 'Wanted', 1)], unblacklist=True)
        self.assertEqual([i['imdb_id'] for i in out], ['tt1'])

    def test_season_entries_merge_and_whole_show_wins(self):
        out = self.run_gate([{'imdb_id': 'tt9', 'media_type': 'tv', 'season': 2},
                             {'imdb_id': 'tt9', 'media_type': 'tv', 'season': 1}])
        self.assertEqual(out, [{'imdb_id': 'tt9', 'media_type': 'tv', 'requested_seasons': [1, 2]}])
        out = self.run_gate([{'imdb_id': 'tt9', 'media_type': 'tv', 'season': 2},
                             {'imdb_id': 'tt9', 'media_type': 'tv', 'season': None}])
        self.assertEqual(out, [{'imdb_id': 'tt9', 'media_type': 'tv'}])


class FloppyTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(tracker_common, 'tmdb_to_imdb', side_effect=fake_tmdb)
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(tracker_common, 'tvdb_to_imdb', return_value=None)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = patch('database.core.get_db_connection', side_effect=lambda: db())
        p3.start()
        self.addCleanup(p3.stop)

    def test_entry_shapes(self):
        self.assertEqual(floppy._entry(floppy_row('603', 'movie')),
                         {'imdb_id': 'tt0133093', 'media_type': 'movie', 'season': None})
        self.assertEqual(floppy._entry(floppy_row('1399', 'season', season=3)),
                         {'imdb_id': 'tt0944947', 'media_type': 'tv', 'season': 3})
        self.assertEqual(floppy._entry(floppy_row('1', 'book')), None)
        # MAL anime without external ids can't be resolved
        self.assertIsNone(floppy._entry(floppy_row('5114', 'anime', source='mal')))
        # ...but with an imdb id it can
        self.assertEqual(floppy._entry(floppy_row('5114', 'anime', source='mal', ids={'imdb': 'tt1355642'}))['imdb_id'],
                         'tt1355642')

    def test_watchlist_queries_statuses_and_types(self):
        calls = []

        def fake_get(path, params=None):
            calls.append((path, params))
            if path == '/media/movie/':
                return page([floppy_row('603', 'movie')])
            if path == '/media/tv/':
                return page([floppy_row('1399', 'tv')])
            return page([])
        with patch.object(floppy, '_get', side_effect=fake_get):
            out = floppy.get_wanted_from_floppy_watchlist({'floppy_statuses': ['Planning', 'Paused']}, {'1080p': True})
        items, versions = out[0]
        self.assertEqual(sorted(i['imdb_id'] for i in items), ['tt0133093', 'tt0944947'])
        self.assertEqual(versions, {'1080p': True})
        self.assertEqual([c[0] for c in calls], ['/media/movie/', '/media/tv/', '/media/anime/'])
        self.assertIn(('status', 0), calls[0][1])
        self.assertIn(('status', 2), calls[0][1])

    def test_movies_only_filter(self):
        paths = []
        with patch.object(floppy, '_get', side_effect=lambda p, q=None: paths.append(p) or page([])):
            floppy.get_wanted_from_floppy_watchlist({'media_type': 'Movies'}, {})
        self.assertEqual(paths, ['/media/movie/'])

    def test_pagination_follows_next(self):
        pages = [page([floppy_row('603', 'movie')], nxt='more'), page([floppy_row('27205', 'movie')])]
        with patch.object(floppy, '_get', side_effect=lambda p, q=None: pages.pop(0)):
            rows = floppy._get_all('/media/movie/')
        self.assertEqual(len(rows), 2)

    def test_lists(self):
        with patch.object(floppy, '_get', return_value=page([floppy_row('1399', 'tv'), floppy_row('27205', 'movie')])):
            out = floppy.get_wanted_from_floppy_lists('1, x, ', {'d': True})
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0][0]), 2)

    def test_unreachable_floppy_gives_nothing(self):
        with patch.object(floppy, '_get', return_value=None):
            self.assertEqual(floppy.get_wanted_from_floppy_watchlist({}, {}), [])
            self.assertEqual(floppy.get_wanted_from_floppy_lists('3', {}), [])


def resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.content = b'x'
    r.json.return_value = body
    r.raise_for_status.return_value = None
    return r


class SimklTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = patch.dict(os.environ, {'USER_CONFIG': self.tmp, 'USER_DB_CONTENT': self.tmp})
        env.start()
        self.addCleanup(env.stop)
        settings = {('Simkl', 'client_id'): 'cid', ('Simkl', 'client_secret'): ''}
        p = patch.object(simkl, 'get_setting', side_effect=lambda s, k, d=None: settings.get((s, k), d))
        p.start()
        self.addCleanup(p.stop)
        for target, kw in ((tracker_common, {'side_effect': fake_tmdb}),):
            q = patch.object(target, 'tmdb_to_imdb', **kw)
            q.start()
            self.addCleanup(q.stop)
        q = patch.object(tracker_common, 'tvdb_to_imdb', return_value=None)
        q.start()
        self.addCleanup(q.stop)
        q = patch('database.core.get_db_connection', side_effect=lambda: db())
        q.start()
        self.addCleanup(q.stop)
        simkl._device.clear()

    def test_device_flow_saves_tokens(self):
        with patch.object(simkl.api, 'post', return_value=resp(200, {
                'device_code': 'secret', 'user_code': 'BDWP-HQPK',
                'verification_uri_complete': 'https://simkl.com/pin?user_code=BDWP-HQPK',
                'expires_in': 900, 'interval': 5})):
            start = simkl.start_device_auth()
        self.assertTrue(start['success'])
        self.assertNotIn('device_code', start)
        with patch.object(simkl.api, 'post', return_value=resp(400, {'error': 'authorization_pending'})):
            self.assertEqual(simkl.poll_device_auth()['status'], 'pending')
        simkl._device['next_poll'] = 0
        with patch.object(simkl.api, 'post', return_value=resp(200, {
                'access_token': 'simkl_at_x', 'refresh_token': 'simkl_rt_x', 'expires_in': 604800})):
            self.assertEqual(simkl.poll_device_auth()['status'], 'approved')
        saved = json.load(open(os.path.join(self.tmp, '.simkl_auth.json')))
        self.assertEqual(saved['access_token'], 'simkl_at_x')
        self.assertEqual(saved['refresh_token'], 'simkl_rt_x')
        self.assertTrue(simkl.auth_status()['signed_in'])

    def test_slow_down_backs_off(self):
        simkl._device.update(device_code='d', interval=5, expires_at=9e12, next_poll=0)
        with patch.object(simkl.api, 'post', return_value=resp(400, {'error': 'slow_down'})):
            self.assertEqual(simkl.poll_device_auth()['status'], 'pending')
        self.assertEqual(simkl._device['interval'], 10)

    def test_expiring_token_is_refreshed(self):
        json.dump({'access_token': 'old', 'refresh_token': 'rt', 'expires_at': 0},
                  open(os.path.join(self.tmp, '.simkl_auth.json'), 'w'))
        with patch.object(simkl.api, 'post', return_value=resp(200, {'access_token': 'new', 'expires_in': 604800})) as post:
            self.assertEqual(simkl._access_token(), 'new')
        self.assertEqual(post.call_args.kwargs['data']['grant_type'], 'refresh_token')
        self.assertEqual(json.load(open(os.path.join(self.tmp, '.simkl_auth.json')))['refresh_token'], 'rt')

    def test_entries_from_all_items(self):
        data = {
            'movies': [{'movie': {'title': 'The Matrix', 'ids': {'simkl': 1, 'tmdb': '603'}}, 'status': 'plantowatch'}],
            'shows': [{'show': {'title': 'GoT', 'ids': {'simkl': 2, 'imdb': 'tt0944947'}}}],
            'anime': [{'show': {'title': 'Film', 'ids': {'simkl': 3, 'imdb': 'tt5311514'}}, 'anime_type': 'movie'},
                      {'show': {'title': 'Nope', 'ids': {'simkl': 4, 'mal': 1}}, 'anime_type': 'tv'}],
        }
        self.assertEqual(simkl._entries(data), [
            {'imdb_id': 'tt0133093', 'media_type': 'movie', 'season': None},
            {'imdb_id': 'tt0944947', 'media_type': 'tv', 'season': None},
            {'imdb_id': 'tt5311514', 'media_type': 'movie', 'season': None},
        ])

    def test_watchlist_uses_activities_cache(self):
        json.dump({'access_token': 'tok', 'refresh_token': 'rt', 'expires_at': 9e12},
                  open(os.path.join(self.tmp, '.simkl_auth.json'), 'w'))
        paths = []

        def fake_get(path, token, params=None):
            paths.append(path)
            if path == '/sync/activities':
                return {'all': '2026-11-15T10:00:00Z'}
            if path == '/sync/all-items/movies/plantowatch':
                return {'movies': [{'movie': {'ids': {'tmdb': '603'}}}]}
            if path == '/sync/all-items/shows/plantowatch':
                return {'shows': [{'show': {'ids': {'tmdb': '1668'}}}]}
            return {}
        with patch.object(simkl, '_get', side_effect=fake_get):
            first = simkl.get_wanted_from_simkl_watchlist({'include_anime': False}, {'v': True})
            second = simkl.get_wanted_from_simkl_watchlist({'include_anime': False}, {'v': True})
        self.assertEqual(first, second)
        self.assertEqual(sorted(i['imdb_id'] for i in first[0][0]), ['tt0108778', 'tt0133093'])
        self.assertEqual(paths.count('/sync/all-items/movies/plantowatch'), 1)
        self.assertEqual(paths.count('/sync/activities'), 2)

    def test_movies_skip_show_only_statuses(self):
        json.dump({'access_token': 'tok', 'expires_at': 9e12}, open(os.path.join(self.tmp, '.simkl_auth.json'), 'w'))
        paths = []
        with patch.object(simkl, '_get', side_effect=lambda p, t, q=None: paths.append(p) or {}):
            simkl.get_wanted_from_simkl_watchlist({'simkl_statuses': ['Watching'], 'media_type': 'All'}, {})
        self.assertNotIn('/sync/all-items/movies/watching', paths)
        self.assertIn('/sync/all-items/shows/watching', paths)
        self.assertIn('/sync/all-items/anime/watching', paths)

    def test_not_signed_in_gives_nothing(self):
        self.assertEqual(simkl.get_wanted_from_simkl_watchlist({}, {}), [])


if __name__ == '__main__':
    unittest.main()
