#!/usr/bin/env python3
"""psutil open_files()/memory_maps() must not run on Windows.

Reported 2026-09-30 (MaddGuru): on Windows, open_files() inspects every handle
(~100 ms each once its helper thread starts timing out) while holding the GIL,
freezing the process for ~2 minutes per call; afterwards threads could no
longer start or exit and the web UI stopped answering until restart.
"""

import os
import tempfile
import unittest
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

from queues import performance_monitor as pm


class _Proc:
    def __init__(self):
        self.open_files = mock.Mock(side_effect=AssertionError('open_files() called'))
        self.memory_maps = mock.Mock(side_effect=AssertionError('memory_maps() called'))
        self.num_handles = mock.Mock(return_value=1300)
        self.connections = mock.Mock(return_value=[])
        self.threads = mock.Mock(return_value=[])


class TestWindowsSkipsHandleWalk(unittest.TestCase):
    def setUp(self):
        self.monitor = pm.PerformanceMonitor.__new__(pm.PerformanceMonitor)
        self.monitor.performance_logger = mock.Mock()
        self.monitor._write_entry = mock.Mock()
        self.proc = _Proc()
        for p in (mock.patch.object(pm, '_IS_WINDOWS', True),
                  mock.patch.object(pm.psutil, 'Process', return_value=self.proc)):
            p.start()
            self.addCleanup(p.stop)

    def test_detailed_memory(self):
        self.monitor._log_detailed_memory()
        self.monitor.performance_logger.error.assert_not_called()
        entry = self.monitor._write_entry.call_args.args[0]
        self.assertEqual(entry['memory']['open_files']['count'], 0)
        self.assertEqual(entry['memory']['open_files']['open_handles'], 1300)

    def test_file_descriptors(self):
        self.monitor._log_file_descriptors()
        self.monitor.performance_logger.error.assert_not_called()
        entry = self.monitor._write_entry.call_args.args[0]
        self.assertEqual(entry['metrics']['open_files_count'], 0)
        self.assertEqual(entry['metrics']['open_handles'], 1300)
        text = self.monitor.performance_logger.info.call_args.args[0]
        self.assertIn('not collected on Windows (1300 handles', text)

    def test_non_windows_unchanged(self):
        with mock.patch.object(pm, '_IS_WINDOWS', False):
            self.proc.open_files = mock.Mock(return_value=[])
            self.monitor._log_file_descriptors()
        self.proc.open_files.assert_called_once()
        self.proc.num_handles.assert_not_called()
        entry = self.monitor._write_entry.call_args.args[0]
        self.assertNotIn('open_handles', entry['metrics'])


if __name__ == '__main__':
    unittest.main()
