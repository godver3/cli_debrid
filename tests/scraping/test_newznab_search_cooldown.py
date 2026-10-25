#!/usr/bin/env python3
"""Newznab search cooldown (scraper/newznab_cooldown.py).

Users with large MDBLists got their indexer accounts banned: once an indexer
started answering "Request limit reached", every other title kept searching it,
because only the one refused query was cached as empty. A refusal must now
pause every search to that indexer until the cooldown lifts.
"""

import os
import tempfile
import unittest
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import requests

from scraper import newznab_cooldown as cd

_RSS = ('<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        '<channel>{items}</channel></rss>')
_ITEM = ('<item><title>{title}</title><guid>{title}</guid>'
         '<enclosure url="http://x/{title}.nzb" length="1073741824"/></item>')


def _rss(*titles):
    return _RSS.format(items=''.join(_ITEM.format(title=t) for t in titles))


def _err(code, desc):
    return f'<?xml version="1.0" encoding="UTF-8"?><error code="{code}" description="{desc}"/>'


class TestClassify(unittest.TestCase):
    def c(self, status, body='', headers=None):
        hit = cd.classify_search_response(status, headers or {}, body)
        return hit[0] if hit else None

    def test_request_limit_codes(self):
        # Spec / NNTmux v1: 500 with the running count in the description.
        self.assertEqual(self.c(200, _err(500, 'Request limit reached (1000/1000)')), cd.LIMIT_SECONDS)
        self.assertEqual(self.c(200, _err(501, 'Download limit reached')), cd.LIMIT_SECONDS)
        self.assertEqual(self.c(200, _err(429, 'Slow down')), cd.LIMIT_SECONDS)
        # Limit wording under some other code.
        self.assertEqual(self.c(200, _err(900, 'Daily API quota exceeded')), cd.LIMIT_SECONDS)

    def test_http_429_uses_retry_after(self):
        self.assertEqual(self.c(429), cd.LIMIT_SECONDS)
        self.assertEqual(self.c(429, headers={'Retry-After': '120'}), 120)

    def test_short_waits(self):
        # NNTmux v2 reuses code 500 for a concurrency throttle: not an hour.
        self.assertEqual(self.c(200, _err(500, 'Too many concurrent requests, retry shortly')),
                         cd.SHORT_WAIT_SECONDS)
        self.assertEqual(self.c(503, 'Too many requests! You must wait 3 seconds before trying again.'), 5)
        self.assertEqual(self.c(503, 'Please retry in 2 minutes'), 120)

    def test_account_errors(self):
        for code in (100, 101, 102, 910):
            self.assertEqual(self.c(200, _err(code, 'nope')), cd.ACCOUNT_SECONDS, code)

    def test_503_without_wording_is_short(self):
        self.assertEqual(self.c(503, '<html>Service Unavailable</html>'), cd.UNAVAILABLE_SECONDS)
        self.assertEqual(self.c(503, 'API limit reached'), cd.LIMIT_SECONDS)

    def test_not_refusals(self):
        self.assertIsNone(self.c(200, _err(201, 'Incorrect parameter (q must not be empty)')))
        self.assertIsNone(self.c(200, _err(300, 'No such item')))
        self.assertIsNone(self.c(500, '<html>Internal Server Error</html>'))
        self.assertIsNone(self.c(502, 'Bad Gateway'))
        # Release titles with limit words in a normal result list.
        self.assertIsNone(self.c(200, _rss('Limitless.S01E01.1080p', 'Show.Limited.Series.2024.1080p')))

    def test_key_separates_api_keys(self):
        self.assertNotEqual(cd.indexer_key('http://i/api', 'a'), cd.indexer_key('http://i/api', 'b'))
        self.assertEqual(cd.indexer_key('http://I/api/', 'a'), cd.indexer_key('http://i/api', 'a'))


class TestRecord(unittest.TestCase):
    def setUp(self):
        cd.reset_search_cooldowns()
        self.addCleanup(cd.reset_search_cooldowns)

    def test_longer_cooldown_is_not_shortened(self):
        self.assertTrue(cd.record('k', 3600, 'limit'))
        self.assertFalse(cd.record('k', 60, 'short'))
        left, reason = cd.remaining('k')
        self.assertGreater(left, 3000)
        self.assertEqual(reason, 'limit')

    def test_expiry(self):
        cd.record('k', 10, 'x')
        with mock.patch.object(cd.time, 'monotonic', return_value=cd.time.monotonic() + 11):
            self.assertIsNone(cd.remaining('k'))


class TestNewznabUsesCooldown(unittest.TestCase):
    def setUp(self):
        from scraper import newznab
        self.nz = newznab
        self.nz._NZB_CACHE.clear()
        cd.reset_search_cooldowns()
        # Cooldowns are module state; never leak one into another test's indexer.
        self.addCleanup(cd.reset_search_cooldowns)
        self.addCleanup(self.nz._NZB_CACHE.clear)
        self.calls = []

    def scrape(self, title, url='http://idx'):
        with mock.patch.object(self.nz, 'get_setting', return_value=30), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            return self.nz.scrape_newznab_instance(
                'Idx', {'url': url, 'api_key': 'k'}, 'tt123', title, 2024, 'movie')

    def test_limit_in_200_body_pauses_every_later_search(self):
        def fake_get(url, params=None, timeout=None):
            self.calls.append(params)
            return mock.Mock(status_code=200, headers={},
                             text=_err(500, 'Request limit reached (1000/1000)'), url=url)

        with mock.patch.object(self.nz.api, 'get', side_effect=fake_get):
            self.assertEqual(self.scrape('Movie One'), [])
            first = len(self.calls)
            self.assertGreater(first, 0)
            self.assertEqual(self.scrape('Movie Two'), [])
            self.assertEqual(self.scrape('Movie Three'), [])
        self.assertEqual(len(self.calls), first)

    def test_raised_429_pauses_searches(self):
        def fake_get(url, params=None, timeout=None):
            self.calls.append(params)
            resp = requests.Response()
            resp.status_code = 429
            resp.headers['Retry-After'] = '600'
            raise requests.exceptions.HTTPError('429 Client Error', response=resp)

        with mock.patch.object(self.nz.api, 'get', side_effect=fake_get):
            self.scrape('Movie One')
            first = len(self.calls)
            self.scrape('Movie Two')
        self.assertEqual(len(self.calls), first)
        left, _ = cd.remaining(cd.indexer_key('http://idx/api', 'k'))
        self.assertLessEqual(left, 600)

    def test_other_indexers_keep_searching(self):
        def fake_get(url, params=None, timeout=None):
            self.calls.append(url)
            if url.startswith('http://bad'):
                return mock.Mock(status_code=200, headers={}, url=url,
                                 text=_err(500, 'Request limit reached'))
            return mock.Mock(status_code=200, headers={}, url=url, text=_rss('Movie.Two.2024.1080p.WEB-GRP'))

        with mock.patch.object(self.nz.api, 'get', side_effect=fake_get):
            self.scrape('Movie One', url='http://bad')
            self.calls.clear()
            self.scrape('Movie Two', url='http://bad')
            self.assertEqual(self.calls, [])
            self.assertTrue(self.scrape('Movie Two', url='http://good'))

    def test_season_aggregate_stops_after_refusal(self):
        def fake_get(url, params=None, timeout=None):
            self.calls.append(params)
            return mock.Mock(status_code=200, headers={}, text=_err(500, 'Request limit reached'))

        with mock.patch.object(self.nz.api, 'get', side_effect=fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            self.nz.scrape_newznab_season_aggregate(
                [('A', {'url': 'http://a', 'api_key': 'k'})],
                imdb_id='tt1', title='Show', year=2024, season=1, episode_numbers=list(range(1, 11)),
            )
        # 10 episodes x 2 queries would be 20; the parallel first wave may land a
        # few before the cooldown is recorded, but not the whole season.
        self.assertLess(len(self.calls), 20)
        self.assertIsNotNone(cd.remaining(cd.indexer_key('http://a/api', 'k')))


if __name__ == '__main__':
    unittest.main()
