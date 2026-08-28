#!/usr/bin/env python3
"""Tests for shared NZB failure blacklist/cleanup helper."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBlacklistAndCleanupNzbFailure(unittest.TestCase):
    def test_blacklists_and_clears_filled_by_fields(self):
        item = {
            'id': 42,
            'filled_by_torrent_id': 'nzb:abc123',
            'filled_by_magnet': 'https://indexer/getnzb/test.nzb',
            'nzb_segment_id': 'seg@test',
            'original_scraped_torrent_title': 'Release.Title.2020.1080p-GROUP',
            'filled_by_file': 'Release.Title.2020.1080p-GROUP.mkv',
        }
        with patch('database.not_wanted_magnets.add_to_not_wanted_nzb_guid') as mock_guid, \
             patch('database.not_wanted_magnets.add_to_not_wanted_nzb_segment') as mock_seg, \
             patch('database.not_wanted_magnets.add_to_not_wanted') as mock_hash, \
             patch('queues.run_program.clear_nzb_job_health_cache') as mock_cache, \
             patch('database.database_writing.update_media_item') as mock_update:
            from utilities.nzb_failure_cleanup import blacklist_and_cleanup_nzb_failure
            blacklist_and_cleanup_nzb_failure(item, 'test failure')

            mock_guid.assert_called_once_with('https://indexer/getnzb/test.nzb')
            mock_seg.assert_called_once_with('seg@test')
            mock_hash.assert_called_once_with('abc123')
            mock_cache.assert_called_once_with('abc123')
            mock_update.assert_called_once()
            self.assertEqual(mock_update.call_args[0][0], 42)
            update_kwargs = mock_update.call_args[1]
            self.assertIsNone(update_kwargs['filled_by_torrent_id'])
            self.assertEqual(
                update_kwargs['rescrape_original_torrent_title'],
                'Release.Title.2020.1080p-GROUP',
            )
            self.assertIsNone(item['filled_by_torrent_id'])


if __name__ == '__main__':
    unittest.main()
