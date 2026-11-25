"""Not-wanted hashes are checked again once an indexer link is resolved.

Scrape-time filtering sees only the indexer link. A Jackett download URL has no
hash in it, so the same broken torrent found again through a second indexer
passed the filter and was picked again (Supernatural S13, 2026-08-15).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401
from database import not_wanted_magnets as nw
from queues.torrent_processor import TorrentProcessor

HASH = '9cd9dcb9740722df4a117bf7cb843e0fc8788b1a'


class ResolvedHashNotWantedTests(unittest.TestCase):
    def _check(self, magnet, temp_file=None, item=None, stored=(HASH,)):
        keys = frozenset(nw.get_base_filename(h) for h in stored)
        with mock.patch.object(nw, '_cached_set', return_value=(frozenset(stored), keys)), \
             mock.patch.object(nw, 'get_setting', return_value=False):
            return TorrentProcessor._resolved_hash_not_wanted(magnet, temp_file, item)

    def test_resolved_magnet_with_blacklisted_hash_is_skipped(self):
        self.assertTrue(self._check(f'magnet:?xt=urn:btih:{HASH}&tr=udp://other.tracker'))

    def test_upper_case_stored_hash_still_matches(self):
        self.assertTrue(self._check(f'magnet:?xt=urn:btih:{HASH}', stored=(HASH.upper(),)))

    def test_torrent_file_hash_is_checked(self):
        with mock.patch('queues.torrent_processor.extract_hash_from_file', return_value=HASH):
            self.assertTrue(self._check(None, temp_file='/tmp/x.torrent'))

    def test_other_hash_passes(self):
        self.assertFalse(self._check('magnet:?xt=urn:btih:' + 'b' * 40))

    def test_item_override_skips_check(self):
        self.assertFalse(self._check(f'magnet:?xt=urn:btih:{HASH}', item={'disable_not_wanted_check': True}))


if __name__ == '__main__':
    unittest.main()
