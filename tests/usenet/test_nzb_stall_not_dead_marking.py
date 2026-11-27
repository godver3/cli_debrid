"""A stalled NZB download is not proof the release is bad.

Live 2026-11-26: the built-in engine hung its import workers, jobs stayed
'queued', and the >2h stall timeout in task_nzb_health_check not-wanted 140
releases that were never evaluated. A timeout must never add to not-wanted;
the built-in engine's jobs are left to the engine.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  (app import order; run_program first is circular)
from queues.run_program import ProgramRunner
from usenet.builtin_client import BuiltinClient


class _External:
    """Any non-built-in provider (cli_mount / NzbDAV): same slot shape."""
    def __init__(self, status='queued'):
        self.deleted = []
        self._s = _Engine.__new__(_Engine)
        self._s.status, self._s.error = status, None

    def get_job_status(self, job_id):
        return _Engine.get_job_status(self._s, job_id)

    def find_job_hash_by_name(self, name):
        return 'job0'

    def delete_queue_job(self, h):
        self.deleted.append(h)
        return 200

    def get_nzb_file_info(self, *a, **k):
        return None


class _Engine(BuiltinClient):
    """Built-in engine slot: 'queued'/'importing' read as downloading at 0%."""
    def __init__(self, status='queued', error=None, retryable=False):  # no super(): no engine to reach
        self.status, self.error, self.retryable = status, error, retryable
        self.deleted = []

    def get_job_status(self, job_id):
        failed = self.status == 'failed'
        raw = {'id': job_id, 'info_hash': job_id, 'status': self.status}
        if failed:
            raw['error'] = self.error
            raw['retryable'] = self.retryable
        return {'state': 'failed' if failed else 'downloading', 'progress': 0, 'raw': raw}

    def find_job_hash_by_name(self, name):
        return 'job0'

    def delete_queue_job(self, h):
        self.deleted.append(h)
        return 200

    def get_nzb_file_info(self, *a, **k):
        return None


def _runner():
    runner = ProgramRunner.__new__(ProgramRunner)
    adding = mock.Mock()
    item = {'id': 1, 'type': 'movie', 'title': 'Movie', 'filled_by_torrent_id': 'nzb:job0',
            'filled_by_file': 'Movie.1', 'filled_by_magnet': 'http://idx/nzb/AAA',
            'last_updated': (datetime.now() - timedelta(minutes=5)).isoformat(),
            'scrape_results': json.dumps([{'nzb_url': 'http://idx/nzb/AAA'}, {'nzb_url': 'http://idx/nzb/BBB'}])}
    adding.items = [item]
    adding._nzb_downloading_job_ids = set()
    adding._nzb_submitted_ids = set()
    runner.queue_manager = mock.Mock()
    runner.queue_manager.queues = {'Adding': adding}
    return runner, adding, item


class StallNotDeadMarkingTests(unittest.TestCase):
    def setUp(self):
        self._saved = (ProgramRunner._nzb_health_last_duration, getattr(ProgramRunner, '_nzb_health_job_cursor', 0),
                       ProgramRunner._nzb_provider_last_progress)
        ProgramRunner._nzb_health_last_duration = 0.0
        ProgramRunner._nzb_provider_last_progress = None
        ProgramRunner._nzb_health_job_cursor = 0
        ProgramRunner._nzb_folder_wait_counts.clear()
        ProgramRunner._nzb_folder_wait_since.clear()
        ProgramRunner._nzb_job_started_at.clear()
        ProgramRunner._nzb_ghost_repeat_counts.clear()

    def tearDown(self):
        (ProgramRunner._nzb_health_last_duration, ProgramRunner._nzb_health_job_cursor,
         ProgramRunner._nzb_provider_last_progress) = self._saved
        ProgramRunner._nzb_job_started_at.clear()
        ProgramRunner._nzb_folder_wait_counts.clear()
        ProgramRunner._nzb_folder_wait_since.clear()
        ProgramRunner._nzb_ghost_repeat_counts.clear()

    def _tick(self, runner, client, provider):
        runner._nzb_health_last_duration = 0.0
        settings = {'provider': provider}

        def gs(s, k, d=None):
            return d

        with mock.patch('usenet.get_usenet_client', return_value=client), \
             mock.patch('usenet.get_setting', side_effect=lambda s: settings), \
             mock.patch('utilities.settings.get_setting', side_effect=gs), \
             mock.patch('database.nzb_playback_repair.reject_active_candidate', return_value=False), \
             mock.patch('database.not_wanted_magnets.add_to_not_wanted_nzb_guid') as nw, \
             mock.patch('database.database_writing.update_media_item') as umi, \
             mock.patch('database.get_db_connection') as gdb:
            gdb.return_value.execute.return_value.fetchone.return_value = (0,)
            self.logs = None
            with self.assertLogs(level='DEBUG') as logs:
                runner.task_nzb_health_check()
            self.logs = logs.output
        return nw, umi

    def test_builtin_job_still_queued_past_timeout_is_left_alone(self):
        runner, adding, item = _runner()
        runner._nzb_job_started_at['job0'] = datetime.now() - timedelta(hours=3)
        engine = _Engine('queued')
        ProgramRunner._nzb_provider_last_progress = datetime.now() - timedelta(hours=3)  # idle: would time out elsewhere
        nw, umi = self._tick(runner, engine, 'builtin')
        nw.assert_not_called()
        umi.assert_not_called()
        self.assertEqual(engine.deleted, [])
        adding.remove_item.assert_not_called()
        runner.queue_manager.move_to_wanted.assert_not_called()
        self.assertEqual(item['filled_by_torrent_id'], 'nzb:job0')
        self.assertFalse([l for l in self.logs if 'stalled' in l], self.logs)

    def test_builtin_job_importing_past_timeout_is_left_alone(self):
        runner, _, item = _runner()
        runner._nzb_job_started_at['job0'] = datetime.now() - timedelta(hours=30)
        engine = _Engine('importing')
        nw, _ = self._tick(runner, engine, 'builtin')
        nw.assert_not_called()
        self.assertEqual(engine.deleted, [])
        self.assertEqual(item['filled_by_torrent_id'], 'nzb:job0')

    def test_builtin_failed_job_keeps_existing_failure_handling(self):
        runner, _, _ = _runner()
        runner._nzb_job_started_at['job0'] = datetime.now() - timedelta(hours=3)
        engine = _Engine('failed', error='missing articles', retryable=False)
        with mock.patch('utilities.nzb_failure_cleanup.blacklist_and_cleanup_nzb_failure') as bl:
            self._tick(runner, engine, 'builtin')
        bl.assert_called_once()
        self.assertEqual(bl.call_args[0][1], 'failed in cli_mount')

    def test_builtin_retryable_failed_moves_on_without_not_wanting(self):
        runner, adding, item = _runner()
        sib = {'id': 2, 'filled_by_torrent_id': 'nzb:job0', 'filled_by_magnet': 'http://idx/nzb/AAA'}
        adding.items.append(sib)
        engine = _Engine('failed', error='import timed out', retryable=True)
        with mock.patch('utilities.nzb_failure_cleanup.blacklist_and_cleanup_nzb_failure') as bl:
            nw, umi = self._tick(runner, engine, 'builtin')
        bl.assert_not_called()
        nw.assert_not_called()
        runner.queue_manager.move_to_blacklisted.assert_not_called()
        self.assertNotIn(item['id'], ProgramRunner._nzb_ghost_repeat_counts)
        self.assertNotIn(sib['id'], ProgramRunner._nzb_ghost_repeat_counts)
        self.assertEqual(engine.deleted, ['job0'])
        runner.queue_manager.move_to_wanted.assert_any_call(sib, 'Adding')
        kwargs = umi.call_args.kwargs
        self.assertIsNone(kwargs['filled_by_torrent_id'])
        self.assertEqual([r['nzb_url'] for r in json.loads(kwargs['scrape_results'])], ['http://idx/nzb/BBB'])
        self.assertTrue(any('Stalled job — 1 results remain, retrying in Adding' in l
                            for l in self.logs), self.logs)

    def test_builtin_retryable_failed_no_more_results_moves_to_wanted_without_fail_counter(self):
        runner, _, item = _runner()
        item['scrape_results'] = json.dumps([{'nzb_url': 'http://idx/nzb/AAA'}])
        engine = _Engine('failed', error='import timed out', retryable=True)
        with mock.patch('utilities.nzb_failure_cleanup.blacklist_and_cleanup_nzb_failure') as bl:
            nw, umi = self._tick(runner, engine, 'builtin')
        bl.assert_not_called()
        nw.assert_not_called()
        runner.queue_manager.move_to_blacklisted.assert_not_called()
        self.assertEqual(engine.deleted, ['job0'])
        self.assertNotIn(item['id'], ProgramRunner._nzb_ghost_repeat_counts)
        runner.queue_manager.move_to_wanted.assert_called_once_with(item, 'Adding')

    def test_external_stall_moves_on_without_not_wanting(self):
        runner, adding, item = _runner()
        runner._nzb_job_started_at['job0'] = datetime.now() - timedelta(hours=3)
        engine = _External('queued')
        ProgramRunner._nzb_provider_last_progress = datetime.now() - timedelta(hours=3)  # provider idle too
        nw, umi = self._tick(runner, engine, 'climount')
        nw.assert_not_called()
        self.assertEqual(engine.deleted, ['job0'])  # stalled job still removed
        # next result retried, stalled URL dropped from the candidates
        kwargs = umi.call_args.kwargs
        self.assertIsNone(kwargs['filled_by_torrent_id'])
        self.assertEqual([r['nzb_url'] for r in json.loads(kwargs['scrape_results'])], ['http://idx/nzb/BBB'])
        self.assertTrue(any('timed out (no verdict) — trying next result, not marking it not-wanted' in l
                            for l in self.logs), self.logs)

    def test_external_stall_siblings_not_wanted_either(self):
        runner, adding, item = _runner()
        sib = {'id': 2, 'filled_by_torrent_id': 'nzb:job0', 'filled_by_magnet': 'http://idx/nzb/AAA'}
        adding.items.append(sib)
        runner._nzb_job_started_at['job0'] = datetime.now() - timedelta(hours=3)
        ProgramRunner._nzb_provider_last_progress = datetime.now() - timedelta(hours=3)
        nw, _ = self._tick(runner, _External('queued'), 'climount')
        nw.assert_not_called()
        runner.queue_manager.move_to_wanted.assert_any_call(sib, 'Adding')


if __name__ == '__main__':
    unittest.main()
