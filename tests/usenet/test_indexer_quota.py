"""Per-indexer daily API-hit and grab limits (usenet/indexer_quota.py)."""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import database  # noqa: F401  (app import order)
from usenet import indexer_quota as iq

SCRAPERS = {
    'Geek_1': {'type': 'Newznab', 'enabled': True, 'url': 'https://api.nzbgeek.info',
               'api_limit_per_day': '3', 'grab_limit_per_day': '1', 'limit_reset': 'midnight_local'},
    'Planet_1': {'type': 'Newznab', 'enabled': True, 'url': 'https://api.nzbplanet.net',
                 'api_limit_per_day': '', 'grab_limit_per_day': None},
    'Torrentio_1': {'type': 'Torrentio', 'enabled': True},
}


def _setting(section, key=None, default=None):
    if section == 'Scrapers' and key is None:
        return SCRAPERS
    return default


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(iq, 'USAGE_FILE', os.path.join(self.tmp.name, 'indexer_usage.json'))
        p.start()
        self.addCleanup(p.stop)
        g = mock.patch.object(iq, 'get_setting', side_effect=_setting)
        g.start()
        self.addCleanup(g.stop)
        iq.reset_for_tests()
        self.addCleanup(iq.reset_for_tests)

    def test_blank_or_missing_limit_means_unlimited(self):
        for _ in range(50):
            iq.record('Planet_1', 'api')
            iq.record('Planet_1', 'grab')
        self.assertTrue(iq.allow('Planet_1', 'api'))
        self.assertTrue(iq.allow('Planet_1', 'grab'))
        self.assertTrue(iq.allow('Unknown_1', 'api'))
        self.assertTrue(iq.allow(None, 'grab'))

    def test_limit_blocks_after_n_calls(self):
        for _ in range(3):
            self.assertTrue(iq.allow('Geek_1', 'api'))
            iq.record('Geek_1', 'api')
        self.assertFalse(iq.allow('Geek_1', 'api'))
        self.assertTrue(iq.allow('Geek_1', 'grab'))   # separate quota
        iq.record('Geek_1', 'grab')
        self.assertFalse(iq.allow('Geek_1', 'grab'))

    def test_count_survives_restart(self):
        iq.record('Geek_1', 'api')
        iq.flush()
        iq._usage = None   # as after a restart
        self.assertEqual(iq.used('Geek_1', 'api'), 1)

    def test_yesterdays_calls_do_not_count_at_midnight_reset(self):
        yesterday = time.time() - 86400 - 60
        iq._load()['Geek_1'] = {'api': [yesterday] * 5, 'grab': []}
        self.assertEqual(iq.used('Geek_1', 'api'), 0)
        self.assertTrue(iq.allow('Geek_1', 'api'))

    def test_window_starts(self):
        now = datetime(2026, 11, 14, 18, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(iq.window_start('rolling_24h', now), now - 86400)
        self.assertEqual(iq.window_start('midnight_utc', now),
                         datetime(2026, 11, 14, tzinfo=timezone.utc).timestamp())
        local_mid = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(iq.window_start('midnight_local', now), local_mid.timestamp())
        self.assertEqual(iq.resets_at('midnight_local', now), (local_mid + timedelta(days=1)).timestamp())

    def test_url_matching_and_kind(self):
        self.assertEqual(iq.instance_for_url('https://api.nzbgeek.info/api?t=get&id=x'), 'Geek_1')
        self.assertEqual(iq.instance_for_url('https://nzbgeek.info/getnzb/abc.nzb'), 'Geek_1')
        self.assertEqual(iq.instance_for_url('https://api.nzbplanet.net/getnzb/f.nzb&i=1'), 'Planet_1')
        self.assertIsNone(iq.instance_for_url('https://api.real-debrid.com/rest/1.0/user'))
        self.assertIsNone(iq.instance_for_url('https://evil-nzbgeek.info/api'))
        self.assertEqual(iq.request_kind('https://api.nzbgeek.info/api', {'t': 'tvsearch'}), 'api')
        self.assertEqual(iq.request_kind('https://api.nzbgeek.info/api', {'t': 'get', 'id': 'x'}), 'grab')
        self.assertEqual(iq.request_kind('https://api.nzbgeek.info/api?t=get&id=x'), 'grab')
        self.assertEqual(iq.request_kind('https://api.nzbplanet.net/getnzb/f.nzb&i=1'), 'grab')

    def test_api_tracker_counts_indexer_requests(self):
        import routes.api_tracker as tracker
        api = tracker.api
        resp = mock.Mock(status_code=200, text='<rss/>')
        resp.raise_for_status = mock.Mock()
        with mock.patch.object(api.session, 'get', return_value=resp), \
             mock.patch.object(tracker, 'api_logger', mock.Mock(), create=True):
            api.get('https://api.nzbgeek.info/api', params={'t': 'search', 'q': 'x'})
            api.get('https://api.nzbgeek.info/api?t=get&id=abc')
            api.get('https://api.real-debrid.com/rest/1.0/user')
        self.assertEqual(iq.used('Geek_1', 'api'), 1)
        self.assertEqual(iq.used('Geek_1', 'grab'), 1)

    def test_exhausted_indexer_is_not_searched(self):
        from scraper import newznab
        newznab._NZB_CACHE.clear()
        newznab.clear_season_cache()
        newznab.reset_search_cooldowns()
        for _ in range(3):
            iq.record('Geek_1', 'api')
        api_get = mock.Mock()
        with mock.patch.object(newznab.api, 'get', api_get):
            out = newznab.scrape_newznab_instance('Geek_1', SCRAPERS['Geek_1'], 'tt0000001', 'Show', 2024,
                                                  'episode', season=1, episode=1)
            newznab.prefetch_season('Geek_1', dict(SCRAPERS['Geek_1'], api_key='k'), 'tt0000001', 'Show', 1)
        self.assertEqual(out, [])
        api_get.assert_not_called()

    def test_usage_line(self):
        iq.record('Geek_1', 'api')
        self.assertEqual(iq.usage_line('Geek_1'), 'API 1/3 · Grabs 0/1 today')
        self.assertEqual(iq.usage_line('Planet_1'), 'API 0 · Grabs 0 today')


if __name__ == '__main__':
    unittest.main()
