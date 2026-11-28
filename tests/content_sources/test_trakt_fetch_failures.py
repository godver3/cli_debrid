"""Trakt content-source robustness (no Flask / app imports needed).

Functions are lifted out of content_checkers/trakt.py with ast and exec'd against
stubbed globals, so these run without the app's dependencies.

Covers: HTTP-error retry (requests.Response is falsy on 4xx/5xx), failed-vs-empty fetches and
the watchlist cache fallback, check_for_updates on malformed activity + per-key TTL, the
',asc' list-URL artifact, and TMDB/TVDB ids never being emitted as imdb_id.
"""
import ast
import json
import logging
import os
import pickle
import random
import re
import sys
import tempfile
import time
import typing
import unittest
from unittest import mock

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRAKT_PATH = os.path.join(ROOT, 'content_checkers', 'trakt.py')

WANTED = {
    'fetch_items_from_trakt', 'fetch_all_pages_from_trakt', '_get_item_ids', 'get_imdb_id',
    'process_trakt_items', 'get_last_activity', 'check_for_updates', 'load_trakt_cache',
    'save_trakt_cache', '_clean_trakt_list_url', 'get_wanted_from_trakt_watchlist',
    'get_wanted_from_trakt_lists', 'get_wanted_from_friend_trakt_watchlist'
}


def load_trakt(**overrides):
    tree = ast.parse(open(TRAKT_PATH, encoding='utf-8').read())
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    assert {n.name for n in body} == WANTED
    ns = {name: getattr(typing, name) for name in ('List', 'Dict', 'Any', 'Tuple')}
    ns.update(
        __name__='trakt_under_test', logging=logging, json=json, os=os, pickle=pickle, re=re,
        random=random, time=time, requests=requests, sleep=mock.Mock(),
        TRAKT_API_URL='https://api.trakt.tv', REQUEST_TIMEOUT=10, DEFAULT_INITIAL_RETRY_DELAY=3,
        PAGINATION_LIMIT=100, PAGINATION_DELAY=0, LAST_ACTIVITY_CACHE_FILE='/nonexistent/la.pkl',
        TRAKT_WATCHLIST_CACHE_FILE='/nonexistent/wl.pkl', TRAKT_FRIENDS_DIR='/nonexistent',
        get_trakt_headers=lambda: {'h': 1}, _trakt_rate_limiter=mock.MagicMock(),
        GlobalTraktCoordinator=mock.MagicMock(), _set_task_priority=lambda p='normal': None,
        datetime=__import__('datetime').datetime,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), TRAKT_PATH, 'exec'), ns)
    ns.update(overrides)  # after exec, so stubs replace the lifted functions of the same name
    return ns


def http_error(status, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp._content = b'[]'
    assert not resp  # the trap: 4xx/5xx responses are falsy
    return requests.exceptions.HTTPError(response=resp)


def ok_response(payload):
    resp = mock.Mock()
    resp.headers = {'content-type': 'application/json'}
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def error_response(status, headers=None):
    resp = mock.Mock()
    resp.headers = {'content-type': 'application/json', **(headers or {})}
    resp.status_code = status
    resp.raise_for_status.side_effect = http_error(status, headers)
    return resp


class FetchItemsErrorHandlingTests(unittest.TestCase):
    def test_429_is_retried_even_though_response_is_falsy(self):
        ns = load_trakt()
        responses = [error_response(429, {'Retry-After': '1'}), ok_response([{'a': 1}])]
        with mock.patch.object(requests, 'get', side_effect=responses) as get:
            self.assertEqual(ns['fetch_items_from_trakt']('/x'), [{'a': 1}])
        self.assertEqual(get.call_count, 2)

    def test_502_is_retried(self):
        ns = load_trakt()
        responses = [error_response(502), ok_response([1])]
        with mock.patch.object(requests, 'get', side_effect=responses) as get:
            self.assertEqual(ns['fetch_items_from_trakt']('/x'), [1])
        self.assertEqual(get.call_count, 2)

    def test_failure_value_depends_on_flag(self):
        ns = load_trakt()
        for status in (500, 404, 423, 426):
            with mock.patch.object(requests, 'get', return_value=error_response(status)):
                self.assertEqual(ns['fetch_items_from_trakt']('/x'), [], status)
                self.assertIsNone(ns['fetch_items_from_trakt']('/x', return_none_on_error=True), status)

    def test_no_headers_and_exhausted_retries(self):
        ns = load_trakt(get_trakt_headers=lambda: None)
        self.assertEqual(ns['fetch_items_from_trakt']('/x'), [])
        self.assertIsNone(ns['fetch_items_from_trakt']('/x', return_none_on_error=True))
        ns = load_trakt()
        with mock.patch.object(requests, 'get', side_effect=requests.exceptions.ConnectionError()):
            self.assertIsNone(ns['fetch_items_from_trakt']('/x', max_retries=2, return_none_on_error=True))

    def test_success_and_genuinely_empty(self):
        ns = load_trakt()
        with mock.patch.object(requests, 'get', return_value=ok_response([])):
            self.assertEqual(ns['fetch_items_from_trakt']('/x', return_none_on_error=True), [])


class FetchAllPagesTests(unittest.TestCase):
    def test_returns_none_if_any_page_fails(self):
        pages = {1: [{'n': i} for i in range(2)], 2: None}
        ns = load_trakt(fetch_items_from_trakt=lambda ep, h=None, **kw: pages[int(re.search(r'page=(\d+)', ep).group(1))])
        self.assertIsNone(ns['fetch_all_pages_from_trakt']('/sync/watchlist', limit=2))

    def test_combines_pages_and_empty_is_empty_list(self):
        pages = {1: [1, 2], 2: [3]}
        ns = load_trakt(fetch_items_from_trakt=lambda ep, h=None, **kw: pages[int(re.search(r'page=(\d+)', ep).group(1))])
        self.assertEqual(ns['fetch_all_pages_from_trakt']('/w', limit=2), [1, 2, 3])
        ns = load_trakt(fetch_items_from_trakt=lambda ep, h=None, **kw: [])
        self.assertEqual(ns['fetch_all_pages_from_trakt']('/w'), [])


class WatchlistFallbackTests(unittest.TestCase):
    def _ns(self, fetched, cache, **extra):
        saved = mock.Mock()
        stubs = dict(
            ensure_trakt_auth=lambda: 'tok',
            get_trakt_sources=lambda: {'watchlist': [{'enabled': True}]},
            load_trakt_cache=lambda f: cache,
            save_trakt_cache=saved,
            fetch_all_pages_from_trakt=lambda ep, h=None: fetched,
            check_for_updates=lambda **kw: True,
            process_trakt_items=lambda items, unblacklist=False: [{'imdb_id': x, 'media_type': 'movie'} for x in items],
            get_media_item_presence_overall=lambda **kw: None,
        )
        stubs.update(extra)
        ns = load_trakt(**stubs)
        return ns, saved

    def test_failed_fetch_uses_cached_items_keeps_them_and_flags_refetch(self):
        cached = [{'imdb_id': 'tt1', 'media_type': 'movie'}]
        cache = {'processed_items': cached, 'paginated': True}
        ns, saved = self._ns(None, cache)
        result = ns['get_wanted_from_trakt_watchlist']({'1080p': True})
        self.assertEqual(result, [(cached, {'1080p': True})])
        self.assertIs(cache['processed_items'], cached)  # partial/empty result never replaces the cache
        self.assertTrue(cache['refetch_pending'])
        saved.assert_called_once()

    def test_repeated_failure_still_falls_back_to_cache(self):
        cached = [{'imdb_id': 'tt1', 'media_type': 'movie'}]
        cache = {'processed_items': cached, 'paginated': True, 'refetch_pending': True}
        ns, _ = self._ns(None, cache)
        self.assertEqual(ns['get_wanted_from_trakt_watchlist']({'v': True}), [(cached, {'v': True})])

    def test_refetch_pending_bypasses_unchanged_shortcut(self):
        cache = {'processed_items': [{'imdb_id': 'old'}], 'paginated': True, 'refetch_pending': True}
        ns, _ = self._ns(['tt9'], cache, check_for_updates=lambda **kw: False)
        result = ns['get_wanted_from_trakt_watchlist']({'v': True})
        self.assertEqual(result[0][0], [{'imdb_id': 'tt9', 'media_type': 'movie'}])
        self.assertFalse(cache['refetch_pending'])

    def test_failed_fetch_without_cache_returns_empty(self):
        cache = {}
        ns, _ = self._ns(None, cache)
        self.assertEqual(ns['get_wanted_from_trakt_watchlist']({'v': True}), [([], {'v': True})])
        self.assertNotIn('processed_items', cache)

    def test_complete_fetch_is_cached(self):
        cache = {}
        ns, saved = self._ns(['tt1'], cache)
        result = ns['get_wanted_from_trakt_watchlist']({'v': True})
        self.assertEqual(result[0][0], [{'imdb_id': 'tt1', 'media_type': 'movie'}])
        saved.assert_called_once()
        self.assertTrue(cache['paginated'])

    def test_genuinely_empty_watchlist_is_cached_as_empty(self):
        cache = {'processed_items': [{'imdb_id': 'old'}], 'paginated': True}
        ns, saved = self._ns([], cache)
        self.assertEqual(ns['get_wanted_from_trakt_watchlist']({'v': True}), [([], {'v': True})])
        saved.assert_called_once()


class FriendWatchlistFallbackTests(unittest.TestCase):
    def test_none_triggers_user_id_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a1.json'), 'w') as f:
                json.dump({'username': 'bob', 'user_id': 'bob-id'}, f)
            calls = []

            def fetch(endpoint, headers=None):
                calls.append(endpoint)
                return None if endpoint == '/users/bob/watchlist' else ['tt9']

            ns = load_trakt(
                TRAKT_FRIENDS_DIR=d, get_trakt_friend_headers=lambda a: {'h': 1},
                clean_username_for_api=lambda u, a=None: u, fetch_all_pages_from_trakt=fetch,
                process_trakt_items=lambda items, unblacklist=False: [{'imdb_id': i} for i in items],
            )
            result = ns['get_wanted_from_friend_trakt_watchlist']({'auth_id': 'a1'}, {'v': True})
        self.assertEqual(calls, ['/users/bob/watchlist', '/users/bob-id/watchlist'])
        self.assertEqual(result, [([{'imdb_id': 'tt9'}], {'v': True})])


class CheckForUpdatesTests(unittest.TestCase):
    def _ns(self, activity, cache=None):
        store = {'cache': cache}
        saved = []

        def save(c, f):
            saved.append(c)
            store['cache'] = c

        ns = load_trakt(
            load_trakt_cache=lambda f: store['cache'] if store['cache'] is not None else {},
            save_trakt_cache=save,
            fetch_items_from_trakt=lambda ep, *a, **kw: activity,
        )
        ns['_store'], ns['_saved'] = store, saved
        return ns

    def test_unknown_activity_forces_refetch_without_raising(self):
        for bad in ([], None, 'x', 5, {}, {'lists': None}, {'watchlist': 'x'}, {'lists': {}}):
            ns = self._ns(bad, {'lists': {}, 'watchlist': 'a', 'last_updated': time.time()})
            self.assertTrue(ns['check_for_updates'](), bad)
            self.assertTrue(ns['check_for_updates']('https://trakt.tv/users/u/lists/l'), bad)

    def test_corrupt_cache_does_not_raise(self):
        ns = self._ns({'watchlist': {'updated_at': 'a'}, 'lists': {'updated_at': 'b'}}, cache=['junk'])
        self.assertTrue(ns['check_for_updates']())

    def test_unchanged_watchlist_is_false_changed_is_true(self):
        activity = {'watchlist': {'updated_at': 'w1'}, 'lists': {'updated_at': 'l1'}}
        ns = self._ns(activity, {'lists': {}, 'watchlist': 'w1', 'last_updated': time.time()})
        self.assertFalse(ns['check_for_updates'](cache_ttl_minutes=0))
        activity['watchlist']['updated_at'] = 'w2'
        self.assertTrue(ns['check_for_updates'](cache_ttl_minutes=0))

    def test_list_detection_does_not_suppress_watchlist_check(self):
        activity = {'watchlist': {'updated_at': 'w2'}, 'lists': {'updated_at': 'l2'}}
        cache = {'lists': {}, 'watchlist': 'w1', 'last_updated': time.time()}
        ns = self._ns(activity, cache)
        self.assertTrue(ns['check_for_updates']('https://trakt.tv/users/u/lists/mine'))
        # Same list again inside the TTL: suppressed. Watchlist: still checked and detected.
        self.assertFalse(ns['check_for_updates']('https://trakt.tv/users/u/lists/mine'))
        self.assertTrue(ns['check_for_updates']())
        # ...and now the watchlist's own TTL applies.
        self.assertFalse(ns['check_for_updates']())

    def test_each_list_has_its_own_ttl(self):
        activity = {'watchlist': {'updated_at': 'w'}, 'lists': {'updated_at': 'l2'}}
        ns = self._ns(activity, {'lists': {}, 'watchlist': 'w', 'last_updated': time.time()})
        self.assertTrue(ns['check_for_updates']('https://trakt.tv/users/u/lists/one'))
        self.assertTrue(ns['check_for_updates']('https://trakt.tv/users/u/lists/two'))

    def test_old_pickle_without_per_key_timestamps_loads(self):
        old = {'lists': {'one': 'l1'}, 'watchlist': 'w1', 'last_updated': time.time()}  # pre-change layout
        activity = {'watchlist': {'updated_at': 'w1'}, 'lists': {'updated_at': 'l1'}}
        ns = self._ns(activity, old)
        self.assertFalse(ns['check_for_updates']())
        self.assertFalse(ns['check_for_updates']('https://trakt.tv/users/u/lists/one'))

    def test_24h_reset_still_forces_refetch(self):
        activity = {'watchlist': {'updated_at': 'w1'}, 'lists': {'updated_at': 'l1'}}
        old = {'lists': {}, 'watchlist': 'w1', 'last_updated': time.time() - 90000}
        ns = self._ns(activity, old)
        self.assertTrue(ns['check_for_updates']())
        self.assertFalse(ns['check_for_updates']())  # reset recorded the key; not re-triggered


class ListUrlTests(unittest.TestCase):
    def test_clean_url(self):
        ns = load_trakt()
        clean = ns['_clean_trakt_list_url']
        base = 'https://trakt.tv/users/u/lists/nascar'
        self.assertEqual(clean(base), base)
        self.assertEqual(clean(base + ','), base)
        self.assertEqual(clean(base + ',asc'), base)
        self.assertEqual(clean(base + ' , ASC ,'), base)
        self.assertEqual(clean('https://trakt.tv/users/u/lists/l?sort=added'), 'https://trakt.tv/users/u/lists/l?sort=added')
        self.assertEqual(clean('asc'), '')
        self.assertEqual(clean(' , '), '')
        self.assertEqual(clean(''), '')

    def test_list_with_asc_in_slug_is_processed_and_bare_asc_is_skipped(self):
        resp = mock.Mock(status_code=200, headers={'X-Pagination-Page-Count': '1'})
        resp.json.return_value = [{'movie': {}}]
        ns = load_trakt(
            ensure_trakt_auth=lambda: 'tok', parse_trakt_list_url=lambda u: {'username': 'u', 'list_id': u.rsplit('/', 1)[-1]},
            clean_username_for_api=lambda u: u, process_trakt_items=lambda items, unblacklist=False: [{'imdb_id': 'tt1'}],
        )
        with mock.patch.object(requests, 'get', return_value=resp) as get:
            result = ns['get_wanted_from_trakt_lists']('https://trakt.tv/users/u/lists/nascar,asc', {'v': True})
            self.assertEqual(result, [([{'imdb_id': 'tt1'}], {'v': True})])
            self.assertIn('/lists/nascar/items', get.call_args.args[0])
            get.reset_mock()
            self.assertEqual(ns['get_wanted_from_trakt_lists']('asc', {'v': True}), [([], {'v': True})])
            get.assert_not_called()


class IdHandlingTests(unittest.TestCase):
    def _process(self, items, db_rows=None):
        conn = mock.MagicMock()
        conn.cursor.return_value.fetchone.return_value = None
        core = mock.MagicMock(get_db_connection=lambda: conn)
        ns = load_trakt(assign_media_type=lambda item: 'movie' if 'movie' in item else 'tv')
        with mock.patch.dict(sys.modules, {'database.core': core}):
            return ns['process_trakt_items'](items), conn, ns

    def test_imdb_preferred_tmdb_goes_to_tmdb_field_tvdb_only_skipped(self):
        items = [
            {'movie': {'title': 'A', 'ids': {'imdb': 'tt1', 'tmdb': 10}}},
            {'movie': {'title': 'B', 'ids': {'tmdb': 20}}},
            {'show': {'title': 'C', 'ids': {'tvdb': 30}}},
            {'show': {'title': 'D', 'ids': {}}},
            {'movie': {'title': 'A again', 'ids': {'imdb': 'tt1'}}},
        ]
        with self.assertLogs(level='WARNING') as cm:
            result, _, _ = self._process(items)
        self.assertEqual(result, [
            {'imdb_id': 'tt1', 'media_type': 'movie'},
            {'tmdb_id': '20', 'media_type': 'movie'},
        ])
        self.assertTrue(any('TVDB' in m and 'C' in m for m in cm.output), cm.output)

    def test_get_imdb_id_stays_compatible_for_removal_matching(self):
        ns = load_trakt()
        self.assertEqual(ns['get_imdb_id']({'movie': {'ids': {'imdb': 'tt1', 'tmdb': 2}}}, 'movie'), 'tt1')
        self.assertEqual(ns['get_imdb_id']({'movie': {'ids': {'tmdb': 2}}}, 'movie'), '2')
        self.assertEqual(ns['get_imdb_id']({'show': {'ids': {'tvdb': 3}}}, 'tv'), '3')
        self.assertEqual(ns['get_imdb_id']({'show': {'ids': {}}}, 'tv'), '')
        self.assertEqual(ns['get_imdb_id']({'episode': {}}, 'tv'), '')


if __name__ == '__main__':
    unittest.main()
