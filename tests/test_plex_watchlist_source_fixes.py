#!/usr/bin/env python3
"""Plex watchlist content-source fixes.

- RSS: every <guid> on an item is read (tmdb-only entries are converted, not dropped),
  malformed feeds fall back to feedparser instead of being discarded, and a feed at
  Plex's 25-item cap logs a warning.
- Other Plex Watchlist: TMDB->IMDB fallback, like My Plex Watchlist.
- Detail fetch: non-200 responses report their HTTP status instead of crashing the log line.
- Plex Friends Watchlist: community API friends + paginated watchlists via the main token.
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
import content_checkers.plex_watchlist as pw
import content_checkers.plex_rss_watchlist as rss


def _rss(items_xml):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Watchlist</title>{items_xml}</channel></rss>""".encode()


def _item(title, guids, category='movie'):
    g = ''.join(f'<guid isPermaLink="false">{x}</guid>' for x in guids)
    return f'<item><title>{title}</title>{g}<category>{category}</category></item>'


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class TestPlexRss(unittest.TestCase):
    def _run(self, content, tmdb_map=None):
        api = mock.Mock()
        api.tmdb_to_imdb.side_effect = lambda tmdb_id, media_type: ((tmdb_map or {}).get(tmdb_id), 'test')
        with mock.patch.object(rss.requests, 'get', return_value=_Resp(content)), \
             mock.patch('cli_battery.app.direct_api.DirectAPI', return_value=api), \
             mock.patch.object(rss, 'get_setting', return_value=False), \
             mock.patch.object(rss, 'get_media_item_presence_overall', return_value='Missing'), \
             mock.patch.object(rss.logging, 'warning') as warn:
            result = rss.get_wanted_from_plex_rss('https://rss.plex.tv/x', {'Default': True})
        items = [i for batch, _ in result for i in batch]
        return items, [c.args[0] for c in warn.call_args_list]

    def test_tmdb_only_item_is_converted(self):
        items, _ = self._run(_rss(_item('New Movie', ['tmdb://123'])), {'123': 'tt0000123'})
        self.assertEqual([(i['imdb_id'], i['media_type']) for i in items], [('tt0000123', 'movie')])

    def test_imdb_preferred_when_item_has_several_guids(self):
        items, _ = self._run(_rss(_item('Show', ['tvdb://9', 'imdb://tt0000009', 'tmdb://5'], 'show')))
        self.assertEqual([(i['imdb_id'], i['media_type']) for i in items], [('tt0000009', 'tv')])

    def test_malformed_xml_falls_back_to_feedparser(self):
        bad = _rss(_item('Tom & Jerry', ['imdb://tt0000001']))  # unescaped '&' breaks strict XML
        items, _ = self._run(bad)
        self.assertEqual([i['imdb_id'] for i in items], ['tt0000001'])

    def test_feed_at_plex_cap_warns(self):
        body = ''.join(_item(f'M{n}', [f'imdb://tt{n:07d}']) for n in range(rss.PLEX_RSS_ITEM_CAP))
        items, warnings = self._run(_rss(body))
        self.assertEqual(len(items), rss.PLEX_RSS_ITEM_CAP)
        self.assertTrue(any('caps watchlist RSS feeds' in w for w in warnings))


def _async_result(results):
    async def _fake(items, token):
        return results(items)
    return _fake


class TestOtherPlexTmdbFallback(unittest.TestCase):
    def test_tmdb_only_item_is_converted(self):
        plex_item = SimpleNamespace(title='Upcoming', type='movie', key='/library/metadata/abc',
                                    _server=SimpleNamespace(url=lambda key: 'https://x' + key))
        account = SimpleNamespace(username='friend', title='Friend', email='', watchlist=lambda: [plex_item])
        fetched = lambda items: [{'imdb_id': None, 'tmdb_id': '77', 'media_type': 'movie',
                                  'original_plex_item': items[0]['original_plex_item']}]
        api = mock.Mock()
        api.tmdb_to_imdb.return_value = ('tt0000077', 'test')
        with mock.patch.object(pw, 'MyPlexAccount', return_value=account), \
             mock.patch.object(pw, 'run_async_fetches', _async_result(fetched)), \
             mock.patch.object(pw, 'DirectAPI', return_value=api):
            result = pw.get_wanted_from_other_plex_watchlist('friend', 'tok', {'Default': True})
        self.assertEqual(result[0][0], [{'imdb_id': 'tt0000077', 'media_type': 'movie', 'content_source_detail': 'friend', 'tmdb_id': '77'}])
        api.tmdb_to_imdb.assert_called_once_with('77', media_type='movie')


class _FakeResponse:
    status = 429

    async def text(self):
        return 'Too Many Requests'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestDetailFetchErrorStatus(unittest.TestCase):
    def test_non_200_reports_http_status(self):
        session = SimpleNamespace(get=lambda *a, **k: _FakeResponse())
        item = {'title': 'X', 'url': 'https://x', 'original_plex_item': None}
        result = asyncio.run(pw.fetch_item_details_and_extract_ids(session, item, 'tok'))
        self.assertEqual(result['error'], 'HTTP429')


class TestPlexFriendsWatchlist(unittest.TestCase):
    FRIENDS = {'allFriendsV2': [
        {'user': {'id': 'u1', 'username': 'Alice', 'displayName': 'Alice A'}},
        {'user': {'id': 'u2', 'username': 'bob', 'displayName': 'Bobby'}},
    ]}

    def _community(self, token, query, variables=None):
        if 'allFriendsV2' in query:
            return self.FRIENDS
        pages = {
            ('u1', None): ([{'id': 'm1', 'title': 'Movie', 'type': 'MOVIE'}], 'c1'),
            ('u1', 'c1'): ([{'id': 's1', 'title': 'Show', 'type': 'SHOW'}], None),
            ('u2', None): ([{'id': 'm1', 'title': 'Movie', 'type': 'MOVIE'}], None),
        }
        nodes, cursor = pages[(variables['uuid'], variables['after'])]
        return {'user': {'watchlist': {'nodes': nodes, 'pageInfo': {'hasNextPage': bool(cursor), 'endCursor': cursor}}}}

    def _run(self, friends_setting):
        seen_urls = []

        def fetched(items):
            seen_urls.extend(i['url'] for i in items)
            ids = {'m1': ('tt0000001', 'movie'), 's1': ('tt0000002', 'show')}
            return [{'imdb_id': ids[i['url'].rsplit('/', 1)[-1]][0], 'tmdb_id': None,
                     'media_type': ids[i['url'].rsplit('/', 1)[-1]][1], 'original_plex_item': i['original_plex_item']}
                    for i in items]

        with mock.patch.object(pw, '_plex_community_query', side_effect=self._community), \
             mock.patch.object(pw, '_main_plex_token', return_value='tok'), \
             mock.patch.object(pw, 'run_async_fetches', _async_result(fetched)):
            result = pw.get_wanted_from_plex_friends_watchlist({'friends': friends_setting}, {'Default': True})
        return result[0][0], seen_urls

    def test_all_friends_paginated_and_deduped(self):
        items, urls = self._run('')
        self.assertEqual(sorted(i['imdb_id'] for i in items), ['tt0000001', 'tt0000002'])
        self.assertEqual(len(urls), 2)  # m1 is on both lists but fetched once
        self.assertTrue(all(u.startswith(pw.PLEX_DISCOVER_METADATA_URL) for u in urls))
        self.assertIn({'imdb_id': 'tt0000002', 'media_type': 'tv', 'content_source_detail': 'Alice'}, items)

    def test_friend_filter_matches_username_or_display_name_case_insensitively(self):
        items, _ = self._run('bobby')
        self.assertEqual(items, [{'imdb_id': 'tt0000001', 'media_type': 'movie', 'content_source_detail': 'bob'}])


if __name__ == '__main__':
    unittest.main()
