"""AllDebrid cache checks must not flood the account with links.

Reported: a user's AllDebrid account was suspended because it "generates thousands of
links without any download". AD has no instant-availability lookup, so a cache check
uploads the magnet, reads it, and deletes it. Before this fix one check of a cached
release called /magnet/files (a download link per file) about three times, the cached
copy was left on the account whenever another provider won the pick, and with several
providers configured AD was checked for every release even when a provider ranked
above it already had it cached.
"""

import asyncio
import unittest
from unittest import mock

import database  # noqa: F401 - app import order; debrid alone hits a routes<->debrid cycle

from debrid.alldebrid import client as ad_client
from debrid.alldebrid.client import AllDebridProvider
from queues.torrent_processor import MAX_ACCOUNT_ADDING_CACHE_CHECKS_PER_ITEM, TorrentProcessor
from utilities import acquisition_health

HASH = 'a' * 40
MAGNET = f'magnet:?xt=urn:btih:{HASH}'
EPISODES = [{'n': f'Show.S01E{i:02d}.mkv', 's': 1000, 'l': f'https://alldebrid.com/f/{i}'} for i in range(1, 11)]


class FakeAllDebridApi:
    """Answers make_request like the AD v4.1 magnet endpoints."""

    def __init__(self, ready, existing=False):
        self.ready = ready
        self.magnets = {}
        if existing:
            self.magnets['900'] = {'id': 900, 'hash': HASH, 'filename': 'Show.S01', 'statusCode': 4 if ready else 1}
        self.calls = []

    def endpoint_calls(self, name):
        return [c for c in self.calls if c.endswith(name)]

    def __call__(self, method, endpoint, api_key, data=None, files=None, use_query_auth=False, **kwargs):
        self.calls.append(endpoint)
        params = kwargs.get('params') or {}
        if endpoint.endswith('/magnet/status'):
            if 'id' in params:
                magnet = self.magnets.get(str(params['id']))
                if not magnet:
                    raise Exception('404 not found')
                return {'status': 'success', 'data': {'magnets': dict(magnet)}}
            return {'status': 'success', 'data': {'magnets': list(self.magnets.values())}}
        if endpoint == '/magnet/upload':
            self.magnets['100'] = {'id': 100, 'hash': HASH, 'filename': 'Show.S01',
                                   'statusCode': 4 if self.ready else 0}
            return {'status': 'success', 'data': {'magnets': [{'id': 100, 'hash': HASH, 'ready': self.ready}]}}
        if endpoint == '/magnet/files':
            tid = str(data['id[]'])
            return {'status': 'success', 'data': {'magnets': [
                {'id': tid, 'files': EPISODES if self.magnets[tid]['statusCode'] == 4 else []}]}}
        if endpoint == '/magnet/delete':
            self.magnets.pop(str(data['id']), None)
            return {'status': 'success'}
        raise AssertionError(f'unexpected endpoint {endpoint}')


def _provider():
    p = AllDebridProvider.__new__(AllDebridProvider)
    p._status = {}
    p._cached_torrent_ids, p._cached_torrent_titles, p._all_torrent_ids = {}, {}, {}
    p._upload_ready_status, p._preexisting_torrent_ids, p._files_cache = {}, set(), {}
    p.phalanx_enabled, p.phalanx_cache = False, None
    p._api_key = 'key'
    return p


class AllDebridCacheCheckTest(unittest.TestCase):
    def setUp(self):
        ad_client._cache_check_uploads.clear()
        patches = [
            mock.patch('database.torrent_tracking.mark_torrent_removed'),
            mock.patch('database.torrent_tracking.update_cache_check_removal'),
            mock.patch('database.torrent_tracking.update_torrent_tracking', return_value=True),
            mock.patch('database.torrent_tracking.record_torrent_addition'),
            mock.patch.object(ad_client, 'add_to_not_wanted'),
            mock.patch.object(ad_client.time, 'sleep'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(ad_client._cache_check_uploads.clear)

    def _check(self, api, **kwargs):
        p = kwargs.pop('provider', None) or _provider()
        with mock.patch.object(ad_client, 'make_request', api):
            result = asyncio.run(p.is_cached(MAGNET, **kwargs))
        return p, result

    def test_uncached_check_fetches_no_links_and_does_not_wait(self):
        api = FakeAllDebridApi(ready=False)
        _, result = self._check(api)
        self.assertIs(result, False)
        self.assertEqual(api.endpoint_calls('/magnet/files'), [])
        self.assertEqual(api.endpoint_calls('/magnet/delete'), ['/magnet/delete'])
        self.assertEqual(api.magnets, {})
        # One status read to classify it, one before the delete: no 30-poll wait loop.
        self.assertLessEqual(len(api.endpoint_calls('/magnet/status')), 3)

    def test_cached_check_fetches_links_once_and_removes_by_default(self):
        api = FakeAllDebridApi(ready=True)
        _, result = self._check(api)
        self.assertIs(result, True)
        self.assertEqual(len(api.endpoint_calls('/magnet/files')), 1)
        self.assertEqual(api.magnets, {})

    def test_caller_that_will_add_keeps_the_cached_copy(self):
        api = FakeAllDebridApi(ready=True)
        p, result = self._check(api, remove_cached=False)
        self.assertIs(result, True)
        self.assertIn('100', api.magnets)
        with mock.patch.object(ad_client, 'make_request', api):
            info = p.get_torrent_info(p.get_cached_torrent_id(HASH))
        self.assertEqual(len(info['files']), len(EPISODES))
        self.assertEqual(len(api.endpoint_calls('/magnet/files')), 1)

    def test_torrent_already_on_account_is_never_removed_or_counted(self):
        for ready in (True, False):
            api = FakeAllDebridApi(ready=ready, existing=True)
            self._check(api)
            self.assertIn('900', api.magnets)
            self.assertEqual(api.endpoint_calls('/magnet/upload'), [])
        self.assertEqual(ad_client.cache_check_budget_left(), ad_client.CACHE_CHECK_UPLOADS_PER_HOUR)

    def test_hourly_budget_stops_uploads(self):
        p = _provider()
        with mock.patch.object(ad_client, 'CACHE_CHECK_UPLOADS_PER_HOUR', 2):
            for _ in range(2):
                self._check(FakeAllDebridApi(ready=False), provider=p)
            self.assertTrue(p.cache_check_budget_exhausted())
            api = FakeAllDebridApi(ready=True)
            _, result = self._check(api, provider=p)
            self.assertIsNone(result)
            self.assertEqual(api.calls, [])
            self.assertEqual(acquisition_health.usable_results([{'magnet': MAGNET}], [p]), [])

    def test_budget_frees_after_an_hour(self):
        ad_client._cache_check_uploads.extend([0.0] * ad_client.CACHE_CHECK_UPLOADS_PER_HOUR)
        self.assertEqual(ad_client.cache_check_budget_left(), ad_client.CACHE_CHECK_UPLOADS_PER_HOUR)


class FakeProvider:
    def __init__(self, name, cached, adds_to_account=False):
        self.PROVIDER_NAME = name
        self.cached = cached
        self.CACHE_CHECK_ADDS_TO_ACCOUNT = adds_to_account
        self._all_torrent_ids = {}
        self.checks = 0

    def get_cached_torrent_id(self, hash_value):
        return f'{self.PROVIDER_NAME}-cached' if self.cached else None

    def get_cached_torrent_title(self, hash_value):
        return 'Show.S01E01.1080p'

    def get_torrent_info(self, torrent_id):
        return {'id': torrent_id, 'filename': 'Show.S01E01.1080p', 'status': 'downloaded',
                'files': [{'path': '/Show.S01E01.1080p.mkv', 'bytes': 1}]}

    def get_active_downloads(self):
        return 0, 10


def _process(providers, n_results=1):
    def fake_cache_check(self, magnet_or_url, temp_file, item=None, provider=None, remove_cached=False):
        provider.checks += 1
        return provider.cached, 'direct_check'

    results = [{'title': f'Show.S01E01.1080p.v{i}', 'magnet': f'magnet:?xt=urn:btih:{i:040d}'}
               for i in range(n_results)]
    processor = TorrentProcessor(providers[0])
    with mock.patch.object(TorrentProcessor, '_providers', new_callable=mock.PropertyMock,
                           return_value=list(providers)), \
         mock.patch.object(TorrentProcessor, 'check_cache_status', fake_cache_check), \
         mock.patch.object(TorrentProcessor, '_check_sibling_debrid_pack', return_value=None), \
         mock.patch('queues.torrent_processor.time.sleep'):
        info, _, _ = processor._process_results_inner(results, accept_uncached=False)
    return info or {}


class AllDebridCheckOrderTest(unittest.TestCase):
    def test_not_checked_when_a_higher_provider_is_cached(self):
        rd, ad = FakeProvider('RealDebrid', True), FakeProvider('AllDebrid', True, adds_to_account=True)
        self.assertEqual(_process([rd, ad])['_provider'], 'RealDebrid')
        self.assertEqual(ad.checks, 0)

    def test_checked_when_higher_providers_miss(self):
        rd, ad = FakeProvider('RealDebrid', False), FakeProvider('AllDebrid', True, adds_to_account=True)
        self.assertEqual(_process([rd, ad])['_provider'], 'AllDebrid')
        self.assertEqual(ad.checks, 1)

    def test_ranked_first_still_wins_over_lower_cached_provider(self):
        ad, rd = FakeProvider('AllDebrid', True, adds_to_account=True), FakeProvider('RealDebrid', True)
        self.assertEqual(_process([ad, rd])['_provider'], 'AllDebrid')

    def test_per_item_cap(self):
        ad = FakeProvider('AllDebrid', False, adds_to_account=True)
        self.assertEqual(_process([ad], n_results=MAX_ACCOUNT_ADDING_CACHE_CHECKS_PER_ITEM + 5), {})
        self.assertEqual(ad.checks, MAX_ACCOUNT_ADDING_CACHE_CHECKS_PER_ITEM)

    def test_other_providers_are_not_capped(self):
        rd = FakeProvider('RealDebrid', False)
        _process([rd], n_results=MAX_ACCOUNT_ADDING_CACHE_CHECKS_PER_ITEM + 5)
        self.assertEqual(rd.checks, MAX_ACCOUNT_ADDING_CACHE_CHECKS_PER_ITEM + 5)


if __name__ == '__main__':
    unittest.main()
