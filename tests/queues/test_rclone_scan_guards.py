"""In-flight path guard and per-mount video extensions of _run_rclone_to_symlink_task."""
import os
import tempfile
import unittest
from unittest.mock import patch

from utilities import builtin_engine


def _rs():
    import importlib
    import database  # noqa: F401  (app import order, avoids a circular import)
    return importlib.import_module('routes.debug_routes.rclone_scan')


class TestInFlightGuard(unittest.TestCase):
    def setUp(self):
        global rs
        rs = _rs()

    def test_second_scan_of_same_path_returns_early(self):
        with tempfile.TemporaryDirectory() as d:
            key = os.path.realpath(d)
            with rs._IN_FLIGHT_LOCK:
                rs._IN_FLIGHT_SCAN_PATHS.add(key)
            try:
                with patch.object(rs.threading, 'Timer') as timer:
                    rs._run_rclone_to_symlink_task(d, d, False, 'guard-task')
                progress = rs.rclone_scan_progress.pop('guard-task')
                self.assertTrue(progress['complete'])
                # Marked busy so the external scan doesn't read zero counts as 'no media'.
                self.assertTrue(progress['busy'])
                self.assertIn('scanned again', progress['message'])
                # The guard must not release a path it never acquired.
                self.assertIn(key, rs._IN_FLIGHT_SCAN_PATHS)
                # The skipped call's progress entry still gets its cleanup timer.
                timer.assert_called_once()
                # ...and its arguments wait for a re-run.
                self.assertEqual(rs._RERUN_SCAN_ARGS[key]['rclone_mount_path_str'], d)
            finally:
                with rs._IN_FLIGHT_LOCK:
                    rs._IN_FLIGHT_SCAN_PATHS.discard(key)
                    rs._RERUN_SCAN_ARGS.pop(key, None)

    def test_skipped_dry_run_is_not_queued(self):
        with tempfile.TemporaryDirectory() as d:
            key = os.path.realpath(d)
            with rs._IN_FLIGHT_LOCK:
                rs._IN_FLIGHT_SCAN_PATHS.add(key)
            try:
                with patch.object(rs.threading, 'Timer'):
                    rs._run_rclone_to_symlink_task(d, d, True, 'dry-guard')
                rs.rclone_scan_progress.pop('dry-guard', None)
                self.assertNotIn(key, rs._RERUN_SCAN_ARGS)
            finally:
                with rs._IN_FLIGHT_LOCK:
                    rs._IN_FLIGHT_SCAN_PATHS.discard(key)

    def test_queued_call_runs_again_when_the_scan_ends(self):
        with tempfile.TemporaryDirectory() as d:
            key = os.path.realpath(d)
            with rs._IN_FLIGHT_LOCK:
                rs._RERUN_SCAN_ARGS[key] = dict(
                    rclone_mount_path_str=d, symlink_base_path_str='x', dry_run=False,
                    trigger_plex_update_on_success=True, assumed_item_title_from_path=None,
                    user_initiated=True, skip_known_paths=False)
            with patch.object(rs.threading, 'Timer'), patch.object(rs.threading, 'Thread') as thread:
                rs._run_rclone_to_symlink_task(d, '', False, 'first-run')  # fails fast on empty symlink base
            rs.rclone_scan_progress.pop('first-run', None)
            thread.assert_called_once()
            kwargs = thread.call_args.kwargs['kwargs']
            self.assertEqual(kwargs['symlink_base_path_str'], 'x')
            self.assertTrue(kwargs['user_initiated'])
            self.assertNotEqual(kwargs['task_id'], 'first-run')
            thread.return_value.start.assert_called_once()
            self.assertNotIn(key, rs._RERUN_SCAN_ARGS)
            self.assertNotIn(key, rs._IN_FLIGHT_SCAN_PATHS)

    def test_batched_scans_are_sent_even_when_the_run_fails(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(rs.threading, 'Timer'), patch.object(rs, '_flush_imported') as flush:
                rs._run_rclone_to_symlink_task(d, '', False, 'fail-run')
            rs.rclone_scan_progress.pop('fail-run', None)
            flush.assert_called()

    def test_path_is_released_after_a_run(self):
        with tempfile.TemporaryDirectory() as d:
            # The task starts a non-daemon 300 s cleanup Timer; keep it from holding pytest open.
            with patch.object(rs.threading, 'Timer'):
                rs._run_rclone_to_symlink_task(d, '', False, 'release-task')  # fails fast on empty symlink base
            rs.rclone_scan_progress.pop('release-task', None)
            self.assertNotIn(os.path.realpath(d), rs._IN_FLIGHT_SCAN_PATHS)


class TestVideoExtensions(unittest.TestCase):
    def setUp(self):
        global rs
        rs = _rs()

    def test_default_set_adds_m4v_only(self):
        with patch.object(builtin_engine, '_load_settings', return_value={}):
            exts = rs._video_extensions_for('/somewhere/else')
        self.assertIn('.m4v', exts)
        self.assertIn('.mkv', exts)
        self.assertNotIn('.ts', exts)
        self.assertNotIn('.m2ts', exts)

    def test_builtin_mount_uses_its_allowed_extensions(self):
        with tempfile.TemporaryDirectory() as d:
            scan = os.path.join(d, '__all__', 'Job')
            os.makedirs(scan)
            settings = {'Built-in Mount': {'enabled': True, 'mount_path': d,
                                           'allowed_extensions': 'mkv, ts'}}
            with patch.object(builtin_engine, '_load_settings', return_value=settings):
                self.assertEqual(rs._video_extensions_for(scan), {'.mkv', '.ts'})
                # A scan outside the built-in mount keeps the default list.
                self.assertIn('.mp4', rs._video_extensions_for('/elsewhere'))


if __name__ == '__main__':
    unittest.main()
