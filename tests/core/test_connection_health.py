"""Home page Connections tile: the cached connection-health summary."""
import time
import unittest
from unittest import mock

from utilities import connection_health as ch


class ConnectionHealthTests(unittest.TestCase):
    def setUp(self):
        ch._results.clear()
        ch._checked_at.clear()
        ch._running.clear()
        patcher = mock.patch.object(ch, '_ensure_started')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_checked_yet(self):
        s = ch.summary()
        self.assertFalse(s['checked'])
        self.assertEqual(s['total'], 0)

    def test_unconfigured_services_are_left_out(self):
        # The system checks return None for services that aren't set up.
        ch.record('system', {
            'plex_status': {'name': 'Plex', 'connected': True, 'error': None},
            'trakt_status': None,
            'scrob_status': None,
        })
        s = ch.summary()
        self.assertTrue(s['checked'])
        self.assertEqual((s['ok'], s['total']), (1, 1))
        self.assertEqual(s['down'], [])

    def test_down_services_are_listed_across_kinds(self):
        ch.record('system', {'plex_status': {'name': 'Plex', 'connected': False, 'error': 'Status code: 401'}})
        ch.record('scrapers', [{'name': 'Jackett (Jackett_1)', 'connected': True},
                               {'name': 'Newznab (Newznab_1)', 'connected': False, 'error': 'Connection refused'}])
        ch.record('content_sources', [{'name': 'My Plex Watchlist_1', 'connected': True}])
        s = ch.summary()
        self.assertEqual((s['ok'], s['total']), (2, 4))
        self.assertEqual([d['name'] for d in s['down']], ['Plex', 'Newznab (Newznab_1)'])
        self.assertEqual(s['down'][0]['error'], 'Status code: 401')

    def test_tile_waits_for_the_system_check(self):
        # Scraper results alone (e.g. from the Connections page) don't count as a full check.
        ch.record('scrapers', [{'name': 'Jackett (Jackett_1)', 'connected': True}])
        self.assertFalse(ch.summary()['checked'])

    def test_slow_checks_wait_hours_system_checks_minutes(self):
        now = time.time()
        ch._checked_at.update({'system': now - ch.SYSTEM_EVERY - 1, 'scrapers': now - 60})
        self.assertTrue(ch._due('system', ch.SYSTEM_EVERY))
        self.assertFalse(ch._due('scrapers', ch.SLOW_EVERY))
        self.assertTrue(ch._due('content_sources', ch.SLOW_EVERY))  # never checked

    def test_a_running_check_is_not_started_twice(self):
        self.assertTrue(ch._due('system', ch.SYSTEM_EVERY))
        self.assertFalse(ch._due('system', ch.SYSTEM_EVERY))

    def test_a_failing_check_keeps_the_last_result_and_waits(self):
        ch.record('scrapers', [{'name': 'Jackett (Jackett_1)', 'connected': True}])
        ch._running.add('scrapers')
        ch._check('scrapers', mock.Mock(side_effect=RuntimeError('boom')))
        self.assertEqual(ch._results['scrapers'][0]['name'], 'Jackett (Jackett_1)')
        self.assertNotIn('scrapers', ch._running)
        self.assertFalse(ch._due('scrapers', ch.SLOW_EVERY))


if __name__ == '__main__':
    unittest.main()
