#!/usr/bin/env python3
"""Regression test: flipping a Task Manager toggle must persist immediately.

Root-cause finding behind widespread "I disabled Trakt/a content source and
it's still running" reports: enable_task()/disable_task() only paused or
resumed the in-memory APScheduler job and updated ProgramRunner.enabled_tasks
-- neither wrote task_toggles.json to disk. Persistence only happened via a
separate 'Save' button on the Task Manager page (save_task_toggles route).
Any later settings save restarts ProgramRunner, which rebuilds enabled_tasks
from the stale on-disk file -- silently re-enabling whatever the user had
"disabled" if they never clicked that separate Save button.

The fix factors the persistence logic into _persist_task_toggles_from_runner()
and calls it from both /enable_task and /disable_task, not just /save_task_toggles.

routes/program_operation_routes.py imports flask (unavailable in this test
environment), so this test inspects the source directly rather than importing
the module -- consistent with the other flask-route regression tests in this
suite (see test_manual_run_bypasses_cache.py).
"""

import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8") as f:
        return f.read()


class TestToggleEndpointsPersistImmediately(unittest.TestCase):
    def setUp(self):
        self.source = _read("routes/program_operation_routes.py")

    def test_persist_helper_exists(self):
        self.assertIn("def _persist_task_toggles_from_runner(runner):", self.source)

    def test_enable_task_persists_after_toggling(self):
        start = self.source.index("def enable_task():")
        end = self.source.index("\n@program_operation_bp.route", start)
        body = self.source[start:end]
        self.assertIn("program_runner.enable_task(task_name)", body)
        self.assertIn("_persist_task_toggles_from_runner(program_runner)", body)
        # The persistence call must come after the live enable, not before.
        self.assertLess(
            body.index("program_runner.enable_task(task_name)"),
            body.index("_persist_task_toggles_from_runner(program_runner)"),
        )

    def test_disable_task_persists_after_toggling(self):
        start = self.source.index("def disable_task():")
        end = self.source.index("\ndef _persist_task_toggles_from_runner", start)
        body = self.source[start:end]
        self.assertIn("program_runner.disable_task(task_name)", body)
        self.assertIn("_persist_task_toggles_from_runner(program_runner)", body)
        self.assertLess(
            body.index("program_runner.disable_task(task_name)"),
            body.index("_persist_task_toggles_from_runner(program_runner)"),
        )

    def test_save_task_toggles_route_reuses_shared_helper(self):
        start = self.source.index("def save_task_toggles():")
        end = self.source.index("\n@program_operation_bp.route('/load_task_toggles'", start)
        body = self.source[start:end]
        self.assertIn("_persist_task_toggles_from_runner(runner)", body)


if __name__ == "__main__":
    unittest.main()
