"""task_nzb_health_check against a SAB-style provider (NzbDAV / Zurg).

The SAB clients return status['progress'] as a percentage and a raw slot with
'percentage' but no 'progress'. The poll used to scale that percentage by 100
again, so a job at 45% read as 4500, took the "complete" path and waited for a
folder forever, never marked as downloading (MaddGuru 2026-10-21). The poll
also always took the same first 30 jobs, so jobs past the cap were never polled.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  (app import order; run_program first is circular)
from queues.run_program import ProgramRunner


class _FakeSabClient:
    def __init__(self, percentage=45):
        self.percentage = percentage
        self.polled = []
        self.file_info_calls = 0

    def get_job_status(self, job_id):
        self.polled.append(job_id)
        return {'state': 'downloading', 'progress': self.percentage,
                'raw': {'nzo_id': job_id, 'percentage': str(self.percentage), 'status': 'Downloading'}}

    def get_nzb_file_info(self, *a, **k):
        self.file_info_calls += 1
        return None


def _runner(n_jobs):
    runner = ProgramRunner.__new__(ProgramRunner)
    adding = mock.Mock()
    submitted = (datetime.now() - timedelta(minutes=5)).isoformat()
    adding.items = [{'id': i, 'type': 'movie', 'title': f'Movie {i}', 'filled_by_torrent_id': f'nzb:job{i}',
                     'filled_by_file': f'Movie.{i}', 'last_updated': submitted}
                    for i in range(n_jobs)]
    adding._nzb_downloading_job_ids = set()
    runner.queue_manager = mock.Mock()
    runner.queue_manager.queues = {'Adding': adding}
    return runner, adding


class NzbHealthPollProgressTests(unittest.TestCase):
    def setUp(self):
        self._saved = (ProgramRunner._nzb_health_last_duration, getattr(ProgramRunner, '_nzb_health_job_cursor', 0),
                       dict(ProgramRunner._nzb_folder_wait_counts),
                       dict(getattr(ProgramRunner, '_nzb_job_started_at', {})))
        ProgramRunner._nzb_health_last_duration = 0.0
        ProgramRunner._nzb_health_job_cursor = 0
        ProgramRunner._nzb_folder_wait_counts.clear()
        if hasattr(ProgramRunner, '_nzb_job_started_at'):
            ProgramRunner._nzb_job_started_at.clear()

    def tearDown(self):
        (ProgramRunner._nzb_health_last_duration, ProgramRunner._nzb_health_job_cursor, waits, started) = self._saved
        ProgramRunner._nzb_folder_wait_counts.clear()
        ProgramRunner._nzb_folder_wait_counts.update(waits)
        if hasattr(ProgramRunner, '_nzb_job_started_at'):
            ProgramRunner._nzb_job_started_at.clear()
            ProgramRunner._nzb_job_started_at.update(started)

    def _tick(self, runner, client):
        runner._nzb_health_last_duration = 0.0
        with mock.patch('usenet.get_usenet_client', return_value=client), \
             mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: d):
            runner.task_nzb_health_check()

    def test_sab_percentage_in_progress_is_downloading_not_complete(self):
        runner, adding = _runner(1)
        client = _FakeSabClient(percentage=45)
        self._tick(runner, client)
        self.assertIn('job0', adding._nzb_downloading_job_ids)
        self.assertEqual(client.file_info_calls, 0)

    def test_capped_polling_rotates_through_every_job(self):
        n = ProgramRunner._NZB_HEALTH_MAX_JOBS_PER_TICK + 13
        runner, _ = _runner(n)
        client = _FakeSabClient()
        self._tick(runner, client)
        self.assertEqual(len(client.polled), ProgramRunner._NZB_HEALTH_MAX_JOBS_PER_TICK)
        self._tick(runner, client)
        self.assertEqual(set(client.polled), {f'job{i}' for i in range(n)})


    def test_row_write_does_not_reset_download_clock(self):
        runner, adding = _runner(1)
        client = _FakeSabClient()
        self._tick(runner, client)
        started = runner._nzb_job_started_at['job0']
        adding.items[0]['last_updated'] = datetime.now().isoformat()  # e.g. a sibling pull rewrote the row
        self._tick(runner, client)
        self.assertEqual(runner._nzb_job_started_at['job0'], started)

    def test_job_past_timeout_stalls_even_after_row_write(self):
        runner, adding = _runner(1)
        client = _FakeSabClient()
        self._tick(runner, client)  # clock seeded 5 minutes ago
        runner._nzb_job_started_at['job0'] = datetime.now() - timedelta(hours=3)  # time passes
        adding.items[0]['last_updated'] = datetime.now().isoformat()
        with mock.patch('database.nzb_playback_repair.reject_active_candidate', return_value=False), \
             self.assertLogs(level='WARNING') as logs:
            self._tick(runner, client)
        self.assertTrue(any('job0 stalled at 45%' in line for line in logs.output), logs.output)

    def test_clock_dropped_when_job_leaves_adding(self):
        runner, adding = _runner(2)
        self._tick(runner, _FakeSabClient())
        adding.items = adding.items[:1]
        self._tick(runner, _FakeSabClient())
        self.assertEqual(set(runner._nzb_job_started_at), {'job0'})


if __name__ == '__main__':
    unittest.main()
