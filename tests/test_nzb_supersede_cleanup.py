#!/usr/bin/env python3
"""Tests for NZB supersede / ffprobe-reject orphan prevention."""

import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCancelSupersededNzbJob(unittest.TestCase):
    def test_cancels_prior_job_when_binding_new_one(self):
        aq = importlib.import_module('queues.adding_queue')
        downloading = {'old-job'}
        with patch.object(aq, 'torrent_has_other_active_owner', return_value=False), \
             patch.object(aq, 'remove_unwanted_torrent') as mock_remove, \
             patch('database.not_wanted_magnets.add_to_not_wanted') as mock_nw, \
             patch('queues.run_program.clear_nzb_job_health_cache') as mock_clear:
            aq.cancel_superseded_nzb_job(
                'nzb:old-job',
                'nzb:new-job',
                123,
                downloading_job_ids=downloading,
            )
            mock_nw.assert_called_once_with('old-job')
            mock_remove.assert_called_once()
            self.assertEqual(mock_remove.call_args[0][0], 'nzb:old-job')
            self.assertNotIn('old-job', downloading)
            mock_clear.assert_called_once_with('old-job')

    def test_noop_when_same_job(self):
        aq = importlib.import_module('queues.adding_queue')
        with patch.object(aq, 'remove_unwanted_torrent') as mock_remove, \
             patch('database.not_wanted_magnets.add_to_not_wanted') as mock_nw:
            aq.cancel_superseded_nzb_job('nzb:same', 'nzb:same', 1)
            mock_nw.assert_not_called()
            mock_remove.assert_not_called()


class TestRejectUnplayableSourceNzbHash(unittest.TestCase):
    def test_ffprobe_reject_blacklists_job_hash(self):
        with patch('database.not_wanted_magnets.add_to_not_wanted_nzb_guid'), \
             patch('database.not_wanted_magnets.add_to_not_wanted_nzb_segment') as mock_seg, \
             patch('database.not_wanted_magnets.add_to_not_wanted') as mock_hash, \
             patch('utilities.session_bad_torrents.mark_torrent_unplayable'), \
             patch('queues.adding_queue.remove_unwanted_torrent'), \
             patch('database.database_writing.enable_fallback_to_single_scraper'), \
             patch('database.database_writing.update_media_item_state'):
            from utilities.local_library_scan import _reject_unplayable_source
            item = {
                'id': 99,
                'filled_by_torrent_id': 'nzb:dead-beef',
                'filled_by_magnet': 'https://indexer/getnzb/dead.nzb',
                'nzb_segment_id': 'seg@ngpost',
            }
            _reject_unplayable_source(item, is_nzb=True)
            mock_seg.assert_called_once_with('seg@ngpost')
            mock_hash.assert_called_once_with('dead-beef')


if __name__ == '__main__':
    unittest.main()
