"""Season prefetch: one whole-season Newznab search serves every episode of it."""
import unittest

import database  # noqa: F401  (app import order: avoids a debrid<->database import cycle)
from unittest import mock

_ITEM = ('<item><title>{title}</title><guid>{guid}</guid>'
         '<enclosure url="http://x/{guid}.nzb" length="1073741824"/></item>')
_RSS = ('<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        '<channel>{items}</channel></rss>')


def _rss(*titles):
    return _RSS.format(items=''.join(_ITEM.format(title=t, guid=t) for t in titles))


class TestSeasonPrefetch(unittest.TestCase):
    def setUp(self):
        from scraper import newznab
        self.nz = newznab
        self.nz._NZB_CACHE.clear()
        self.nz.clear_season_cache()
        self.nz.reset_search_cooldowns()
        self.addCleanup(self.nz.reset_search_cooldowns)
        self.calls = []
        self.settings = {'url': 'http://idx', 'api_key': 'k'}

    def fake_get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append(dict(params))
        if params.get('t') == 'tvsearch' and 'ep' not in params:
            # whole-season ID search: E01, E02, a double episode and a season pack
            return mock.Mock(status_code=200, text=_rss(
                'Show.S01E01.1080p.WEB-GRP', 'Show.S01E02.1080p.WEB-GRP',
                'Show.S01E03E04.1080p.WEB-GRP', 'Show.S01.1080p.WEB-GRP'))
        if params.get('t') == 'search' and params.get('q', '').endswith('S01'):
            return mock.Mock(status_code=200, text=_rss('Show S01E02 720p HDTV-OTH'))
        # live per-episode query
        return mock.Mock(status_code=200, text=_rss('Show.S01E09.1080p.WEB-LIVE'))

    def scrape(self, ep):
        return self.nz.scrape_newznab_instance(
            'Idx', self.settings, 'tt0000001', 'Show', 2024, 'episode', season=1, episode=ep)

    def test_episodes_served_from_one_season_search(self):
        with mock.patch.object(self.nz.api, 'get', side_effect=self.fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            n = self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1)
            self.assertEqual(n, 5)
            prefetch_calls = len(self.calls)
            self.assertEqual(prefetch_calls, 2)  # ID search (one page) + "Show S01" text search

            e1 = self.scrape(1)
            e2 = self.scrape(2)
            e4 = self.scrape(4)
            self.assertEqual(len(self.calls), prefetch_calls)  # no API calls for E01/E02/E04
            self.assertEqual([r['title'] for r in e1], ['Show.S01E01.1080p.WEB-GRP'])
            self.assertEqual(sorted(r['title'] for r in e2),
                             ['Show S01E02 720p HDTV-OTH', 'Show.S01E02.1080p.WEB-GRP'])
            self.assertEqual([r['title'] for r in e4], ['Show.S01E03E04.1080p.WEB-GRP'])
            # The season pack is never served as an episode.
            self.assertFalse(any('Show.S01.1080p' in r['title'] for r in e1 + e2 + e4))

            # Not in the season list -> live per-episode query as before.
            e9 = self.scrape(9)
            self.assertGreater(len(self.calls), prefetch_calls)
            self.assertEqual([r['title'] for r in e9], ['Show.S01E09.1080p.WEB-LIVE'])

    def test_season_search_pages_through_results(self):
        pages = {0: [f'Show.S01E{e:02d}.1080p.WEB-G{e}' for e in range(1, 101)],
                 100: ['Show.S01E01.720p.WEB-LATE']}

        def paged(url, params=None, timeout=None):
            params = params or {}
            self.calls.append(dict(params))
            if params.get('t') == 'tvsearch':
                return mock.Mock(status_code=200, text=_rss(*pages.get(params.get('offset', 0), [])))
            return mock.Mock(status_code=200, text=_rss())

        with mock.patch.object(self.nz.api, 'get', side_effect=paged), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.assertEqual(self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1), 101)
        offsets = [c.get('offset') for c in self.calls if c.get('t') == 'tvsearch']
        self.assertEqual(offsets, [0, 100])

    def test_failed_prefetch_leaves_live_queries(self):
        with mock.patch.object(self.nz.api, 'get', return_value=mock.Mock(status_code=503, text='')):
            self.assertEqual(self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1), 0)
        self.assertIsNone(self.nz._season_cached_episode('http://idx/api', '0000001', 1, 1))

    def test_prefetch_expires(self):
        with mock.patch.object(self.nz.api, 'get', side_effect=self.fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1)
        key = self.nz._season_cache_key('http://idx/api', '0000001', 1)
        ts, res, done = self.nz._SEASON_CACHE[key]
        self.nz._SEASON_CACHE[key] = (ts - self.nz._SEASON_CACHE_TTL - 1, res, done)
        self.assertIsNone(self.nz._season_cached_episode('http://idx/api', '0000001', 1, 1))


    def test_fresh_prefetch_is_reused_not_refetched(self):
        with mock.patch.object(self.nz.api, 'get', side_effect=self.fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.assertEqual(self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1), 5)
            first = len(self.calls)
            # NZBBatch calls this again on the next Scraping tick: no new requests.
            self.assertEqual(self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1), 5)
            self.assertEqual(len(self.calls), first)

    def test_pack_search_becomes_episode_search_when_nzb_packs_disabled(self):
        def setting(section, key, default=None):
            if key == 'disable_nzb_season_packs':
                return True
            return default
        with mock.patch.object(self.nz.api, 'get', side_effect=self.fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0), \
             mock.patch.object(self.nz, 'get_setting', side_effect=setting):
            out = self.nz.scrape_newznab_instance(
                'Idx', self.settings, 'tt0000001', 'Show', 2024, 'episode', season=1, episode=3, multi=True)
            # No episode number: nothing usable to search for.
            none = self.nz.scrape_newznab_instance(
                'Idx', self.settings, 'tt0000001', 'Show', 2024, 'episode', season=1, episode=None, multi=True)
        self.assertEqual(none, [])
        self.assertTrue(out)
        # Only single-episode queries went out: no season-only (pack) search.
        self.assertTrue(self.calls)
        for c in self.calls:
            if c.get('t') == 'tvsearch':
                self.assertEqual(c.get('ep'), 3)
            else:
                self.assertTrue(c.get('q', '').endswith('S01E03'))

    def test_season_search_follows_newznab_total_when_pages_are_capped(self):
        # An indexer capping pages at 2 items, reporting total=5.
        titles = [f'Show.S01E{e:02d}.1080p.WEB-G' for e in range(1, 6)]

        def capped(url, params=None, timeout=None):
            params = params or {}
            self.calls.append(dict(params))
            if params.get('t') == 'tvsearch':
                off = params.get('offset', 0)
                items = ''.join(_ITEM.format(title=t, guid=t) for t in titles[off:off + 2])
                return mock.Mock(status_code=200, text=_RSS.format(
                    items=f'<newznab:response offset="{off}" total="5"/>' + items))
            return mock.Mock(status_code=200, text=_rss())

        with mock.patch.object(self.nz.api, 'get', side_effect=capped), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.assertEqual(self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1), 5)
        offsets = [c.get('offset') for c in self.calls if c.get('t') == 'tvsearch']
        self.assertEqual(offsets, [0, 2, 4])
        self.assertIsNotNone(self.nz._season_cached_episode('http://idx/api', '0000001', 1, 5))

    def test_incomplete_prefetch_is_not_served(self):
        def failing_page2(url, params=None, timeout=None):
            params = params or {}
            self.calls.append(dict(params))
            if params.get('t') == 'tvsearch':
                if params.get('offset', 0):
                    return mock.Mock(status_code=500, text='')
                return mock.Mock(status_code=200, text=_rss(
                    *[f'Show.S01E01.720p.WEB-G{i}' for i in range(100)]))
            return mock.Mock(status_code=200, text=_rss())

        with mock.patch.object(self.nz.api, 'get', side_effect=failing_page2), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1)
        # Page 2 may hold E01's better releases: E01 goes to a live query.
        self.assertIsNone(self.nz._season_cached_episode('http://idx/api', '0000001', 1, 1))

    def test_full_text_search_page_is_not_served(self):
        # The "Show S01" text search is one page: when the indexer has more
        # matches than that page holds, episodes go to their own live search.
        def big_text_search(url, params=None, timeout=None):
            params = params or {}
            self.calls.append(dict(params))
            if params.get('t') == 'tvsearch':
                return mock.Mock(status_code=200, text=_rss('Show.S01E01.1080p.WEB-GRP'))
            items = ''.join(_ITEM.format(title=f'Show S01E02 720p-G{i}', guid=f'g{i}') for i in range(5))
            return mock.Mock(status_code=200, text=_RSS.format(
                items='<newznab:response offset="0" total="400"/>' + items))

        with mock.patch.object(self.nz.api, 'get', side_effect=big_text_search), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 1)
        self.assertIsNone(self.nz._season_cached_episode('http://idx/api', '0000001', 1, 1))

    def test_shows_without_imdb_do_not_share_a_prefetch(self):
        with mock.patch.object(self.nz.api, 'get', side_effect=self.fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.nz.prefetch_season('Idx', self.settings, None, 'Show', 1)
            first = len(self.calls)
            self.nz.prefetch_season('Idx', self.settings, None, 'Other Show', 1)
            self.assertGreater(len(self.calls), first)   # fetched, not "reusing" Show's
        self.assertNotEqual(self.nz._season_cache_key('http://idx/api', '', 1, 'Show'),
                            self.nz._season_cache_key('http://idx/api', '', 1, 'Other Show'))
        self.assertEqual(len(self.nz._SEASON_CACHE), 2)

    def test_pack_search_runs_when_nzb_packs_allowed(self):
        def setting(section, key, default=None):
            if key == 'disable_nzb_season_packs':
                return False
            return default
        with mock.patch.object(self.nz.api, 'get', side_effect=self.fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0), \
             mock.patch.object(self.nz, 'get_setting', side_effect=setting):
            self.nz.scrape_newznab_instance(
                'Idx', self.settings, 'tt0000001', 'Show', 2024, 'episode', season=1, episode=3, multi=True)
        self.assertTrue(self.calls)


class TestSearchCooldown(unittest.TestCase):
    def setUp(self):
        from scraper import newznab
        self.nz = newznab
        self.nz._NZB_CACHE.clear()
        self.nz.clear_season_cache()
        self.nz.reset_search_cooldowns()
        self.addCleanup(self.nz.reset_search_cooldowns)
        self.settings = {'url': 'http://idx', 'api_key': 'k'}

    def _scrape(self, ep):
        return self.nz.scrape_newznab_instance(
            'Idx', self.settings, 'tt0000001', 'Show', 2024, 'episode', season=1, episode=ep)

    def test_raised_429_pauses_every_query_to_that_indexer(self):
        import requests
        resp = mock.Mock(status_code=429, text='Too Many Requests', headers={})
        api_get = mock.Mock(side_effect=requests.exceptions.HTTPError('429', response=resp))
        with mock.patch.object(self.nz.api, 'get', api_get):
            self._scrape(1)
            hits = api_get.call_count
            self.assertGreaterEqual(hits, 1)
            self._scrape(2)   # a different query: still not sent
            self.nz.prefetch_season('Idx', self.settings, 'tt0000001', 'Show', 2)
        self.assertEqual(api_get.call_count, hits)

    def test_newznab_limit_error_with_http_200(self):
        body = '<?xml version="1.0"?><error code="500" description="Request limit reached"/>'
        api_get = mock.Mock(return_value=mock.Mock(status_code=200, text=body, headers={}, url='u'))
        with mock.patch.object(self.nz.api, 'get', api_get):
            self._scrape(1)
            hits = api_get.call_count
            self._scrape(2)
        self.assertEqual(api_get.call_count, hits)
        self.assertGreater(self.nz._search_cooldown_left('http://idx/api'), 0)

    def test_result_feed_mentioning_limits_is_not_a_refusal(self):
        feed = _rss('Show.S01E01.Download.Limit.Reached.1080p.WEB-GRP')
        api_get = mock.Mock(return_value=mock.Mock(status_code=200, text=feed, headers={}, url='u'))
        with mock.patch.object(self.nz.api, 'get', api_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self._scrape(1)
        self.assertEqual(self.nz._search_cooldown_left('http://idx/api'), 0)

    def test_pause_lengths(self):
        f = self.nz._search_refusal_seconds
        self.assertEqual(f(429, {}, 'Too Many Requests'), 120)          # burst rate limit
        self.assertEqual(f(503, {}, 'Service Unavailable'), 60)
        self.assertEqual(f(200, {}, '<error code="500" description="Request limit reached"/>'), 600)
        self.assertEqual(f(429, {'Retry-After': '90'}, ''), 90)
        self.assertEqual(f(429, {'Retry-After': '3600'}, ''), 600)      # capped
        self.assertEqual(f(404, {}, 'not found'), 0)
        self.assertEqual(f(500, {}, 'boom'), 0)
        self.assertEqual(f(200, {}, '<error code="429" description="Slow down"/>'), 120)
        self.assertEqual(f(403, {}, '{"message": "Daily API quota exceeded"}'), 600)

    def test_other_indexers_unaffected(self):
        self.nz._record_search_refusal('http://idx/api', 'Idx', 600, 429)
        self.assertEqual(self.nz._search_cooldown_left('http://other/api'), 0)


if __name__ == '__main__':
    unittest.main()
