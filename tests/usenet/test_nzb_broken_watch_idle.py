"""task_nzb_health_check looks for broken library files even when nothing is
being added.

The broken-list poll and the cli_mount log watcher sat after the "no NZB in
Adding" early return, so they only ran while a download was in progress.
Found live 2026-11-21: the built-in mount had marked two files broken during
playback (missing articles, proven) and they sat in the engine's broken list,
never repaired, because Adding was empty.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  (app import order; run_program first is circular)
from queues.run_program import ProgramRunner


def _runner(adding_items):
    runner = ProgramRunner.__new__(ProgramRunner)
    adding = mock.Mock()
    adding.items = adding_items
    adding._nzb_downloading_job_ids = set()
    runner.queue_manager = mock.Mock()
    runner.queue_manager.queues = {'Adding': adding}
    runner._nzb_health_last_duration = 0.0
    return runner


BROKEN = [{'name': 'Blue.Lights.S01E02', 'info_hash': 'b6347a4d', 'status': 'broken'}]


class BrokenWatchWhenIdleTests(unittest.TestCase):
    def setUp(self):
        self._saved_duration = ProgramRunner._nzb_health_last_duration
        ProgramRunner._nzb_health_last_duration = 0.0
        # ProgramRunner is a singleton: the 5-minute poll stamp carries over.
        self._clear_stamp()
        self.addCleanup(self._clear_stamp)

    @staticmethod
    def _clear_stamp():
        inst = ProgramRunner._instance
        if inst is not None and '_last_broken_check_ts' in vars(inst):
            del inst._last_broken_check_ts

    def tearDown(self):
        ProgramRunner._nzb_health_last_duration = self._saved_duration

    def _tick(self, runner, broken, inflight_rows=()):
        conn = mock.MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchall.return_value = list(inflight_rows)
        with mock.patch('usenet.repair_engine.fetch_broken_items', return_value=broken) as fetch, \
                mock.patch('usenet.repair_engine.run_repair') as repair, \
                mock.patch('database.core.get_db_connection', return_value=conn), \
                mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: d):
            runner.task_nzb_health_check()
        return fetch, repair

    def test_empty_adding_still_triggers_repair(self):
        runner = _runner([])
        fetch, repair = self._tick(runner, BROKEN)
        fetch.assert_called_once()
        repair.assert_called_once_with(triggered_by='auto_detected')

    def test_poll_is_rate_limited(self):
        runner = _runner([])
        self._tick(runner, BROKEN)
        fetch, repair = self._tick(runner, BROKEN)  # 9 s later in real life
        fetch.assert_not_called()
        repair.assert_not_called()

    def test_in_flight_items_are_not_repaired(self):
        runner = _runner([])
        fetch, repair = self._tick(runner, BROKEN, inflight_rows=[('nzb:b6347a4d',)])
        fetch.assert_called_once()
        repair.assert_not_called()

    def test_nothing_broken_no_repair(self):
        runner = _runner([])
        fetch, repair = self._tick(runner, [])
        fetch.assert_called_once()
        repair.assert_not_called()


if __name__ == '__main__':
    unittest.main()
