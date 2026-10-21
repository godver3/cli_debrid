"""Debrid provider failover through the cache check and the add.

The cache check asks every provider in the chain in parallel. The result must be added
on a provider that reported it cached:
- a 451 or "not cached" from the primary must not stop a cached fallback being used;
- when the fallback reported cached but holds no torrent id of its own (a PhalanxDB
  'db_cached' answer), add_to_account used to start at the primary, which then added
  an uncached download instead of the fallback's cached copy;
- when several providers report cached, the configured order wins, not whichever
  answered first.
"""

import time
import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from debrid.base import ProviderUnavailableError
from queues.torrent_processor import TorrentProcessor

HASH = 'a' * 40
_real_sleep = time.sleep  # time.sleep is patched below to skip add retries
MAGNET = f'magnet:?xt=urn:btih:{HASH}&dn=Show.S01E01.1080p'


class FakeProvider:
    def __init__(self, name, cached, keeps_id=True, add_error=None, delay=0.0):
        self.PROVIDER_NAME = name
        self.cached = cached
        self.add_error = add_error
        self.delay = delay
        self._all_torrent_ids = {}
        self._cached_ids = {HASH: f'{name}-cached'} if cached and keeps_id else {}
        self.adds = 0

    def get_cached_torrent_id(self, hash_value):
        return self._cached_ids.get(hash_value)

    def get_cached_torrent_title(self, hash_value):
        return 'Show.S01E01.1080p'

    def get_torrent_info(self, torrent_id):
        return {'id': torrent_id, 'filename': 'Show.S01E01.1080p', 'status': 'downloaded',
                'files': [{'path': '/Show.S01E01.1080p.mkv', 'bytes': 1}]}

    def add_torrent(self, magnet, temp_file):
        self.adds += 1
        if self.add_error:
            raise self.add_error
        return f'{self.PROVIDER_NAME}-new'

    def get_active_downloads(self):
        return 0, 10


def _process(*providers):
    def fake_cache_check(self, magnet_or_url, temp_file, item=None, provider=None, remove_cached=False):
        _real_sleep(provider.delay)
        return provider.cached, 'direct_check'

    processor = TorrentProcessor(providers[0])
    with mock.patch.object(TorrentProcessor, '_providers', new_callable=mock.PropertyMock,
                           return_value=list(providers)), \
         mock.patch.object(TorrentProcessor, 'check_cache_status', fake_cache_check), \
         mock.patch.object(TorrentProcessor, '_check_sibling_debrid_pack', return_value=None), \
         mock.patch('queues.torrent_processor.time.sleep'):
        info, _, _ = processor._process_results_inner(
            [{'title': 'Show.S01E01.1080p', 'magnet': MAGNET}], accept_uncached=False)
    return info or {}


class CacheCheckFailoverTest(unittest.TestCase):
    def test_primary_451_uses_cached_fallback(self):
        rd, ad = FakeProvider('RealDebrid', None), FakeProvider('AllDebrid', True)
        info = _process(rd, ad)
        self.assertEqual((info['id'], info['_provider']), ('AllDebrid-cached', 'AllDebrid'))
        self.assertEqual(rd.adds, 0)

    def test_primary_uncached_uses_cached_fallback(self):
        rd, ad = FakeProvider('RealDebrid', False), FakeProvider('AllDebrid', True)
        self.assertEqual(_process(rd, ad)['_provider'], 'AllDebrid')

    def test_fallback_cached_without_id_is_added_on_the_fallback(self):
        rd = FakeProvider('RealDebrid', False)
        ad = FakeProvider('AllDebrid', True, keeps_id=False)
        info = _process(rd, ad)
        self.assertEqual((info['id'], info['_provider']), ('AllDebrid-new', 'AllDebrid'))
        self.assertEqual(rd.adds, 0)

    def test_add_still_fails_over_when_preferred_provider_refuses(self):
        rd = FakeProvider('RealDebrid', False)
        ad = FakeProvider('AllDebrid', True, keeps_id=False,
                          add_error=ProviderUnavailableError('451 Unavailable For Legal Reasons'))
        info = _process(rd, ad)
        self.assertEqual(info['_provider'], 'RealDebrid')
        self.assertEqual((ad.adds, rd.adds), (1, 1))

    def test_both_cached_prefers_configured_order(self):
        # The primary answers last, but it's first in the chain.
        rd = FakeProvider('RealDebrid', True, delay=0.2)
        ad = FakeProvider('AllDebrid', True)
        self.assertEqual(_process(rd, ad)['_provider'], 'RealDebrid')

    def test_nothing_cached_adds_nothing(self):
        rd, ad = FakeProvider('RealDebrid', False), FakeProvider('AllDebrid', False)
        self.assertEqual(_process(rd, ad), {})
        self.assertEqual((rd.adds, ad.adds), (0, 0))


if __name__ == '__main__':
    unittest.main()
