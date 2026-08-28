#!/usr/bin/env python3
"""Tests for NZB supersede / ffprobe-reject orphan prevention."""

import importlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCancelSupersededNzbJob(unittest.TestCase):
    def test_cancels_prior_job_when_binding_new_one(self):
        aq = importlib.import_module('queues.adding_queue')
        item = {
            'id': 123,
            'filled_by_torrent_id': 'nzb:old-job',
            'filled_by_magnet': 'https://indexer/getnzb/old.nzb',
        }
        downloading = {'old-job'}
        with patch.object(aq, 'torrent_has_other_active_owner', return_value=False), \
             patch.object(aq, 'remove_unwanted_torrent') as mock_remove, \
             patch('utilities.nzb_failure_cleanup.blacklist_and_cleanup_nzb_failure') as mock_cleanup:
            aq.cancel_superseded_nzb_job(
                item,
                'nzb:new-job',
                downloading_job_ids=downloading,
            )
            mock_cleanup.assert_called_once()
            self.assertEqual(mock_cleanup.call_args[0][0], item)
            mock_remove.assert_called_once()
            self.assertEqual(mock_remove.call_args[0][0], 'nzb:old-job')
            self.assertNotIn('old-job', downloading)

    def test_noop_when_same_job(self):
        aq = importlib.import_module('queues.adding_queue')
        item = {'id': 1, 'filled_by_torrent_id': 'nzb:same'}
        with patch.object(aq, 'remove_unwanted_torrent') as mock_remove, \
             patch('utilities.nzb_failure_cleanup.blacklist_and_cleanup_nzb_failure') as mock_cleanup:
            aq.cancel_superseded_nzb_job(item, 'nzb:same')
            mock_cleanup.assert_not_called()
            mock_remove.assert_not_called()


class TestRejectUnplayableSourceNzbHash(unittest.TestCase):
    def test_ffprobe_reject_runs_shared_cleanup(self):
        with patch('utilities.nzb_failure_cleanup.blacklist_and_cleanup_nzb_failure') as mock_cleanup, \
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
            mock_cleanup.assert_called_once()
            self.assertEqual(mock_cleanup.call_args[0][0], item)
            self.assertEqual(mock_cleanup.call_args[0][1], 'ffprobe playability check failed')


if __name__ == '__main__':
    unittest.main()
