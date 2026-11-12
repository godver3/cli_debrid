"""HighWay through the real queue add path (TorrentProcessor), not just the provider."""

import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from debrid.highway import api as hw_api
from queues.torrent_processor import TorrentProcessor
from tests.debrid.test_highway_provider import CACHED, FakeHighwayApi, _provider


class HighwayQueueAddTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeHighwayApi()
        del self.api.transfers['11']  # not on the account yet: a fresh grab
        for p in [mock.patch.object(hw_api, 'make_request', self.api),
                  mock.patch('database.torrent_tracking.mark_torrent_removed'),
                  mock.patch('database.torrent_tracking.update_cache_check_removal'),
                  mock.patch('database.torrent_tracking.update_torrent_tracking', return_value=True),
                  mock.patch('database.torrent_tracking.record_torrent_addition'),
                  mock.patch('queues.torrent_processor.time.sleep')]:
            p.start()
            self.addCleanup(p.stop)
        self.provider = _provider()

    def test_cached_result_is_added_through_the_queue(self):
        processor = TorrentProcessor(self.provider)
        results = [{'title': 'Show.S01.1080p', 'magnet': f'magnet:?xt=urn:btih:{CACHED}'}]

        def cache_check(_self, magnet_or_url, temp_file, item=None, provider=None, remove_cached=False):
            return provider.is_cached_sync(magnet_or_url), 'direct_check'

        with mock.patch.object(TorrentProcessor, '_providers', new_callable=mock.PropertyMock,
                               return_value=[self.provider]), \
             mock.patch.object(TorrentProcessor, 'check_cache_status', cache_check), \
             mock.patch.object(TorrentProcessor, '_check_sibling_debrid_pack', return_value=None):
            info, _, _ = processor._process_results_inner(results, accept_uncached=False)

        self.assertIsNotNone(info, 'HighWay result was skipped by the queue add path')
        self.assertEqual(info['id'], '11')
        self.assertEqual(info['status'], 'downloaded')
        self.assertEqual(len(info['files']), 2)


if __name__ == '__main__':
    unittest.main()
