#!/usr/bin/env python3
"""
Unit tests for database/statistics.py get_cached_download_stats resilience.

The dashboard stats must NEVER block on a slow/unreachable debrid provider: the
refresh runs on a background thread and the request always returns the current
(possibly stale) cached value immediately; a true cold start returns a
non-blocking placeholder. The blocking refresh worker is stubbed so we test the
wrapper's behaviour, not provider I/O.
"""

import unittest
import sys
import os
import types
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _permissive(name, **attrs):
    m = types.ModuleType(name)
    m.__getattr__ = lambda a: (lambda *x, **k: None)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _load_statistics():
    # database package + stubbed submodules (relative imports resolve against these)
    pkg = types.ModuleType('database'); pkg.__path__ = []
    sys.modules['database'] = pkg
    sys.modules['database.core'] = _permissive('database.core')
    sys.modules['database.poster_management'] = _permissive('database.poster_management')
    sys.modules['routes'] = _permissive('routes')
    sys.modules['routes.poster_cache'] = _permissive('routes.poster_cache')
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    sys.modules['utilities.settings'] = _permissive('utilities.settings',
                                                    get_setting=lambda *a, **k: (a[2] if len(a) > 2 else ''))
    sys.modules['aiohttp'] = _permissive('aiohttp')
    sys.modules['flask'] = _permissive('flask', request=None, url_for=lambda *a, **k: '')
    # debrid: the exception names MUST be real classes (used in except clauses)
    debrid = types.ModuleType('debrid')
    class TooManyDownloadsError(Exception): pass
    class ProviderUnavailableError(Exception): pass
    debrid.TooManyDownloadsError = TooManyDownloadsError
    debrid.ProviderUnavailableError = ProviderUnavailableError
    debrid.get_debrid_provider = lambda *a, **k: None
    sys.modules['debrid'] = debrid

    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'database', 'statistics.py')
    spec = importlib.util.spec_from_file_location('database.statistics', path)
    m = importlib.util.module_from_spec(spec)
    sys.modules['database.statistics'] = m
    spec.loader.exec_module(m)
    return m


st = _load_statistics()

GOOD_ACTIVE = {'count': 3, 'limit': 10, 'percentage': 30, 'status': 'normal', 'error': None}
GOOD_USAGE = {'used': '500 GB', 'limit': '2000 GB', 'percentage': 25, 'error': None}


class TestStatsResilience(unittest.TestCase):
    def setUp(self):
        # reset module cache + coordination state before each test
        st.download_stats_cache.update({
            'active_downloads': None, 'usage_stats': None, 'subscription': None,
            'last_update': 0, 'last_attempt': 0, 'cache_duration': 300,
        })
        st._stats_refresh_in_progress = False
        self._orig = st._refresh_download_stats_blocking
        self.addCleanup(lambda: setattr(st, '_refresh_download_stats_blocking', self._orig))

    def test_fresh_cache_returns_immediately_without_refresh(self):
        st.download_stats_cache.update({'active_downloads': GOOD_ACTIVE, 'usage_stats': GOOD_USAGE,
                                        'last_update': time.time()})
        called = []
        st._refresh_download_stats_blocking = lambda: called.append(1)
        a, u = st.get_cached_download_stats()
        self.assertEqual(a, GOOD_ACTIVE)
        self.assertEqual(called, [])  # no refresh when fresh

    def test_expired_with_stale_data_returns_stale_without_blocking(self):
        # prior good data but cache expired; refresh is SLOW (5s) → must not block
        st.download_stats_cache.update({'active_downloads': GOOD_ACTIVE, 'usage_stats': GOOD_USAGE,
                                        'last_update': time.time() - 10_000})
        def slow():
            time.sleep(5)
        st._refresh_download_stats_blocking = slow
        t0 = time.time()
        a, u = st.get_cached_download_stats()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0, 'must return stale data without waiting for the slow refresh')
        self.assertEqual(a, GOOD_ACTIVE)        # served stale
        self.assertEqual(u, GOOD_USAGE)

    def test_cold_start_returns_placeholder_not_block(self):
        # no data at all; refresh produces nothing → placeholder, no long wait
        st._refresh_download_stats_blocking = lambda: None  # returns fast, sets nothing
        t0 = time.time()
        a, u = st.get_cached_download_stats()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 3.0)
        self.assertEqual(a['status'], 'loading')
        self.assertIsNone(a['error'])

    def test_refresh_started_only_once_under_throttle(self):
        st.download_stats_cache.update({'active_downloads': GOOD_ACTIVE, 'usage_stats': GOOD_USAGE,
                                        'last_update': time.time() - 10_000})
        starts = []
        def slow():
            starts.append(1); time.sleep(0.5)
        st._refresh_download_stats_blocking = slow
        for _ in range(5):
            st.get_cached_download_stats()
        time.sleep(0.8)
        self.assertEqual(len(starts), 1, 'concurrent/expired hits must coalesce into one refresh')


GOOD_SUB = {'days_remaining': 200, 'expiration': '2027-01-01', 'premium': True, 'error': None}


class TestSubscriptionResilience(unittest.TestCase):
    def setUp(self):
        st.download_stats_cache.update({
            'subscription': None, 'subscription_update': 0, 'subscription_attempt': 0,
            'last_update': 0, 'last_attempt': 0,
        })
        st._sub_refresh_in_progress = False
        self._orig = st._refresh_subscription_blocking
        self.addCleanup(lambda: setattr(st, '_refresh_subscription_blocking', self._orig))

    def test_fresh_returns_without_refresh(self):
        st.download_stats_cache.update({'subscription': GOOD_SUB, 'subscription_update': time.time()})
        called = []
        st._refresh_subscription_blocking = lambda: called.append(1)
        self.assertEqual(st.get_cached_subscription_status(), GOOD_SUB)
        self.assertEqual(called, [])

    def test_stale_served_without_blocking(self):
        st.download_stats_cache.update({'subscription': GOOD_SUB, 'subscription_update': time.time() - 100_000})
        st._refresh_subscription_blocking = lambda: time.sleep(5)
        t0 = time.time()
        sub = st.get_cached_subscription_status()
        self.assertLess(time.time() - t0, 2.0)   # must not wait for the slow refresh
        self.assertEqual(sub, GOOD_SUB)           # stale served

    def test_cold_start_placeholder_not_block(self):
        st._refresh_subscription_blocking = lambda: None
        t0 = time.time()
        sub = st.get_cached_subscription_status()
        self.assertLess(time.time() - t0, 4.0)
        self.assertIsNone(sub['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
