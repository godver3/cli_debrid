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
                rs._run_rclone_to_symlink_task(d, d, False, 'guard-task')
                progress = rs.rclone_scan_progress.pop('guard-task')
                self.assertTrue(progress['complete'])
                self.assertIn('already being processed', progress['message'])
                # The guard must not release a path it never acquired.
                self.assertIn(key, rs._IN_FLIGHT_SCAN_PATHS)
            finally:
                with rs._IN_FLIGHT_LOCK:
                    rs._IN_FLIGHT_SCAN_PATHS.discard(key)

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
