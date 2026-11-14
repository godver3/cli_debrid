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
        ts, res = self.nz._SEASON_CACHE[key]
        self.nz._SEASON_CACHE[key] = (ts - self.nz._SEASON_CACHE_TTL - 1, res)
        self.assertIsNone(self.nz._season_cached_episode('http://idx/api', '0000001', 1, 1))


if __name__ == '__main__':
    unittest.main()
