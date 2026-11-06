"""The startup sync runs in the background while the program reports Running.

run_initialization (every content source, then the release-date refresh) ran
inline in ProgramRunner.run() and held the program at "Starting" for minutes:
the Queues page hid the queues and Stop was refused, although the scheduler
was already processing them.
"""
import threading
import unittest
from unittest import mock

import database  # noqa: F401  (app import order; run_program first is circular)
import queues.run_program as rp


def _runner():
    runner = object.__new__(rp.ProgramRunner)
    runner._running = True
    runner._initializing = False
    runner._stopping = False
    return runner


class InitialSyncBackgroundTests(unittest.TestCase):
    def setUp(self):
        rp._initial_sync_active.clear()
        self.addCleanup(rp._initial_sync_active.clear)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = []

        def slow_sync(runner_self, is_restart=False):
            self.calls.append(is_restart)
            self.entered.set()
            self.release.wait(5)

        patch = mock.patch.object(rp.ProgramRunner, 'run_initialization', slow_sync)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.release.set)

    def _wait_done(self):
        for _ in range(100):
            if not rp.is_initial_sync_running():
                return
            threading.Event().wait(0.02)
        self.fail('initial sync never finished')

    def test_returns_at_once_and_reports_running(self):
        runner = _runner()
        runner._start_initial_sync()
        self.assertTrue(self.entered.wait(2))
        self.assertTrue(rp.is_initial_sync_running())
        self.assertTrue(runner.is_initial_sync_running())
        self.assertEqual(runner.get_status(), 'Running')
        self.release.set()
        self._wait_done()

    def test_not_started_twice(self):
        runner = _runner()
        runner._start_initial_sync()
        self.assertTrue(self.entered.wait(2))
        runner._start_initial_sync()  # e.g. Stop then Start while the first sync runs
        self.release.set()
        self._wait_done()
        self.assertEqual(self.calls, [False])

    def test_restart_flag_passed_through(self):
        _runner()._start_initial_sync(is_restart=True)
        self.assertTrue(self.entered.wait(2))
        self.release.set()
        self._wait_done()
        self.assertEqual(self.calls, [True])

    def test_flag_cleared_when_sync_fails(self):
        with mock.patch.object(rp.ProgramRunner, 'run_initialization', side_effect=RuntimeError('boom')):
            _runner()._start_initial_sync()
            self._wait_done()
        self.assertFalse(rp.is_initial_sync_running())


if __name__ == '__main__':
    unittest.main()
