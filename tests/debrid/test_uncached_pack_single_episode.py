"""A torrent pack kept for one episode (filter_results' pack_for_single_episode)
is only added when cached: uncached it would download the whole pack, e.g. a
Survivor S01-S22, for one file (Full/Hybrid uncached handling)."""

import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from queues.torrent_processor import TorrentProcessor
from tests.debrid.test_cache_check_failover import FakeProvider, MAGNET


def _process(provider, result):
    def fake_cache_check(self, magnet_or_url, temp_file, item=None, provider=None, remove_cached=False):
        return provider.cached, 'direct_check'

    processor = TorrentProcessor(provider)
    with mock.patch.object(TorrentProcessor, '_providers', new_callable=mock.PropertyMock,
                           return_value=[provider]), \
         mock.patch.object(TorrentProcessor, 'check_cache_status', fake_cache_check), \
         mock.patch.object(TorrentProcessor, '_check_sibling_debrid_pack', return_value=None), \
         mock.patch('queues.torrent_processor.time.sleep'):
        info, _, _ = processor._process_results_inner([result], accept_uncached=True)
    return info or {}


class UncachedPackForSingleEpisodeTest(unittest.TestCase):
    def test_uncached_pack_for_one_episode_is_skipped(self):
        p = FakeProvider('RealDebrid', False)
        result = {'title': 'Show.S01-S22.1080p', 'magnet': MAGNET, 'pack_for_single_episode': True}
        self.assertEqual(_process(p, result), {})
        self.assertEqual(p.adds, 0)

    def test_cached_pack_for_one_episode_is_used(self):
        p = FakeProvider('RealDebrid', True)
        result = {'title': 'Show.S01-S22.1080p', 'magnet': MAGNET, 'pack_for_single_episode': True}
        self.assertEqual(_process(p, result).get('_provider'), 'RealDebrid')

    def test_uncached_single_still_added_when_uncached_accepted(self):
        p = FakeProvider('RealDebrid', False)
        info = _process(p, {'title': 'Show.S01E01.1080p', 'magnet': MAGNET})
        self.assertEqual(p.adds, 1)
        self.assertEqual(info.get('_provider'), 'RealDebrid')
