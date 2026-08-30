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


class TestPersistIsSerializedAndAtomic(unittest.TestCase):
    """Persisting on every toggle (rather than only an explicit Save click)
    makes concurrent calls -- e.g. a user rapidly flipping several switches
    -- much more likely. Two overlapping unlocked, non-atomic
    open(path, 'w')/json.dump() calls to the same file can interleave and
    corrupt task_toggles.json. Fixed with a lock plus a temp-file +
    os.replace write.
    """

    def setUp(self):
        self.source = _read("routes/program_operation_routes.py")

    def test_module_level_lock_exists(self):
        self.assertIn("_task_toggles_write_lock = threading.Lock()", self.source)

    def _persist_function_body(self):
        start = self.source.index("def _persist_task_toggles_from_runner(runner):")
        end = self.source.index("\n@program_operation_bp.route('/save_task_toggles'", start)
        return self.source[start:end]

    def test_write_is_serialized_under_the_lock(self):
        body = self._persist_function_body()
        self.assertIn("with _task_toggles_write_lock:", body)
        lock_idx = body.index("with _task_toggles_write_lock:")
        write_idx = body.index("json.dump(data_to_save, f, indent=4)")
        self.assertLess(lock_idx, write_idx)

    def test_write_is_atomic_via_temp_file_and_replace(self):
        body = self._persist_function_body()
        self.assertIn("temp_path = toggles_file_path + '.tmp'", body)
        self.assertIn("os.replace(temp_path, toggles_file_path)", body)
        # Never write json.dump directly to the real path -- only the temp one.
        self.assertNotIn("open(toggles_file_path, 'w')", body)


if __name__ == "__main__":
    unittest.main()
