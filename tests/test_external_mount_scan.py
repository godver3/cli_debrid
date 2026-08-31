#!/usr/bin/env python3
"""Tests for utilities/external_mount_scan.py (from PR #486, merged into
this branch). No test file was included in the PR's diff despite its
description claiming "12/12 pass" against a fake mount, so this covers the
same scenarios independently: baseline-on-first-run, no-reimport, import of
only new folders, retry-cap enforcement, pruning of removed folders, no
state-file write on a no-op run, and a clean skip outside Symlinked/Local
mode.

scan_mount_for_external_adds() lazily imports routes.debug_routes for
_run_rclone_to_symlink_task/rclone_scan_progress (the same functions the
rclone webhook uses). routes/__init__.py imports flask, which isn't
installed in this test environment, so real imports of that package fail
-- worked around by injecting fake 'routes' / 'routes.debug_routes' module
objects into sys.modules before exercising any import path.

_already_tracked() similarly lazily imports database.database_reading,
whose package __init__ pulls in cli_battery -> sqlalchemy, also unavailable
here -- so it's patched directly at the module level (its own dedicated
tests cover the fast-prefilter/fallthrough behavior it implements) rather
than exercised through a real DB.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from utilities import external_mount_scan as ems


def _settings_for(mount_path, symlink_path, mode='Symlinked/Local'):
    values = {
        ('File Management', 'file_collection_management'): mode,
        ('File Management', 'original_files_path'): mount_path,
        ('File Management', 'symlinked_files_path'): symlink_path,
    }

    def fake_get_setting(section, key, default=None):
        return values.get((section, key), default)

    return fake_get_setting


class ExternalMountScanTestBase(unittest.TestCase):
    def setUp(self):
        self.mount_dir = tempfile.TemporaryDirectory()
        self.db_content_dir = tempfile.TemporaryDirectory()
        self.symlink_dir = tempfile.TemporaryDirectory()

        self._env_patch = patch.dict(os.environ, {'USER_DB_CONTENT': self.db_content_dir.name})
        self._env_patch.start()

        # No items already tracked in the DB, by default. _already_tracked()
        # itself lazily imports database.database_reading (unavailable in
        # this sandbox -- see module docstring), so it's patched directly
        # rather than through its real dedup logic.
        self._known_sets_patch = patch.object(ems, '_build_known_sets', return_value=(set(), set()))
        self._known_sets_patch.start()
        self._already_tracked_patch = patch.object(ems, '_already_tracked', return_value=False)
        self._already_tracked_patch.start()

        # Stub out routes.debug_routes so the lazy import inside
        # scan_mount_for_external_adds() never touches the real (flask-
        # dependent) module. import_result records every call for assertions.
        self.import_calls = []
        self.import_result = {'items_added_to_db': 0, 'symlinks_created': 0}
        self._install_debug_routes_stub()

    def tearDown(self):
        self._restore_debug_routes_stub()
        self._already_tracked_patch.stop()
        self._known_sets_patch.stop()
        self._env_patch.stop()
        self.mount_dir.cleanup()
        self.db_content_dir.cleanup()
        self.symlink_dir.cleanup()

    def _install_debug_routes_stub(self):
        self._saved_routes = sys.modules.get('routes')
        self._saved_debug_routes = sys.modules.get('routes.debug_routes')

        fake_routes_pkg = types.ModuleType('routes')
        fake_debug_routes = types.ModuleType('routes.debug_routes')
        fake_debug_routes.rclone_scan_progress = {}

        def fake_run_task(scan_path, symlink_base_path, dry_run, task_id, trigger_plex_update_on_success=False, assumed_item_title_from_path=None):
            self.import_calls.append(scan_path)
            fake_debug_routes.rclone_scan_progress[task_id] = dict(self.import_result)

        fake_debug_routes._run_rclone_to_symlink_task = fake_run_task

        sys.modules['routes'] = fake_routes_pkg
        sys.modules['routes.debug_routes'] = fake_debug_routes
        self._fake_debug_routes = fake_debug_routes

    def _restore_debug_routes_stub(self):
        if self._saved_routes is not None:
            sys.modules['routes'] = self._saved_routes
        else:
            sys.modules.pop('routes', None)
        if self._saved_debug_routes is not None:
            sys.modules['routes.debug_routes'] = self._saved_debug_routes
        else:
            sys.modules.pop('routes.debug_routes', None)

    def _mkfolder(self, name):
        os.makedirs(os.path.join(self.mount_dir.name, name), exist_ok=True)

    def _run_scan(self, mode='Symlinked/Local'):
        with patch.object(
            ems, 'get_setting',
            side_effect=_settings_for(self.mount_dir.name, self.symlink_dir.name, mode),
        ):
            return ems.scan_mount_for_external_adds()

    def _state(self):
        path = os.path.join(self.db_content_dir.name, ems.STATE_FILENAME)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)


class TestBaselineAndSteadyState(ExternalMountScanTestBase):
    def test_first_run_baselines_without_importing(self):
        self._mkfolder('Existing.Show.S01')
        summary = self._run_scan()
        self.assertEqual(summary['baselined'], 1)
        self.assertEqual(summary['imported'], 0)
        self.assertEqual(self.import_calls, [])
        state = self._state()
        self.assertIn('Existing.Show.S01', state)
        self.assertTrue(state['Existing.Show.S01']['baseline'])

    def test_second_run_does_not_reimport_baseline(self):
        self._mkfolder('Existing.Show.S01')
        self._run_scan()  # baseline
        summary = self._run_scan()
        self.assertEqual(summary['imported'], 0)
        self.assertEqual(summary['new_candidates'], 0)
        self.assertEqual(self.import_calls, [])

    def test_no_state_file_rewrite_on_pure_noop_run(self):
        self._mkfolder('Existing.Show.S01')
        self._run_scan()  # baseline write
        state_path = os.path.join(self.db_content_dir.name, ems.STATE_FILENAME)
        mtime_after_baseline = os.path.getmtime(state_path)
        time.sleep(0.01)
        self._run_scan()  # nothing changed
        self.assertEqual(mtime_after_baseline, os.path.getmtime(state_path))


class TestImportingNewFolders(ExternalMountScanTestBase):
    def test_new_folder_after_baseline_is_imported(self):
        self._mkfolder('Old.Show.S01')
        self._run_scan()  # baseline

        self._mkfolder('New.Show.S01')
        self.import_result = {'items_added_to_db': 1, 'symlinks_created': 1}
        summary = self._run_scan()

        self.assertEqual(summary['imported'], 1)
        self.assertEqual(len(self.import_calls), 1)
        self.assertTrue(self.import_calls[0].endswith('New.Show.S01'))
        state = self._state()
        self.assertTrue(state['New.Show.S01']['imported'])
        self.assertFalse(state['Old.Show.S01'].get('imported', False))

    def test_already_imported_folder_is_not_reimported(self):
        self._mkfolder('Old.Show.S01')
        self._run_scan()
        self._mkfolder('New.Show.S01')
        self.import_result = {'items_added_to_db': 1, 'symlinks_created': 1}
        self._run_scan()

        self.import_calls.clear()
        self._run_scan()  # third run: nothing new
        self.assertEqual(self.import_calls, [])

    def test_failed_import_is_retried_up_to_max_attempts(self):
        self._mkfolder('Old.Show.S01')
        self._run_scan()
        self._mkfolder('Failing.Show.S01')
        self.import_result = {'items_added_to_db': 0, 'symlinks_created': 0}  # 0 added == failure

        state_path = os.path.join(self.db_content_dir.name, ems.STATE_FILENAME)
        for _ in range(ems.MAX_ATTEMPTS):
            summary = self._run_scan()
            self.assertEqual(summary['failed'], 1)
            # Force the retry backoff to have elapsed before the next attempt.
            state = self._state()
            state['Failing.Show.S01']['last_attempt'] = 0
            with open(state_path, 'w') as f:
                json.dump(state, f)

        self.assertEqual(len(self.import_calls), ems.MAX_ATTEMPTS)

        # Attempts exhausted -- must not be retried again.
        self._run_scan()
        self.assertEqual(len(self.import_calls), ems.MAX_ATTEMPTS)

    def test_already_tracked_folder_is_recorded_without_reimport_call(self):
        self._mkfolder('Old.Show.S01')
        self._run_scan()
        self._mkfolder('Tracked.Show.S01')

        self._already_tracked_patch.stop()
        try:
            with patch.object(ems, '_already_tracked', return_value=True):
                summary = self._run_scan()
        finally:
            self._already_tracked_patch = patch.object(ems, '_already_tracked', return_value=False)
            self._already_tracked_patch.start()

        self.assertEqual(self.import_calls, [])
        self.assertEqual(summary['new_candidates'], 0)
        state = self._state()
        self.assertTrue(state['Tracked.Show.S01']['tracked_existing'])


class TestPruning(ExternalMountScanTestBase):
    def test_removed_folder_is_pruned_from_state(self):
        # A second, persistent folder keeps the mount non-empty after removing
        # the target -- scan_mount_for_external_adds() early-returns before
        # reaching the pruning logic when the mount lists as entirely empty.
        self._mkfolder('Staying.Show.S01')
        self._mkfolder('Gone.Show.S01')
        self._run_scan()
        self.assertIn('Gone.Show.S01', self._state())

        shutil.rmtree(os.path.join(self.mount_dir.name, 'Gone.Show.S01'))
        self._run_scan()
        state = self._state()
        self.assertNotIn('Gone.Show.S01', state)
        self.assertIn('Staying.Show.S01', state)


class TestModeAndPathGuards(ExternalMountScanTestBase):
    def test_skips_cleanly_outside_symlinked_mode(self):
        self._mkfolder('Some.Show.S01')
        summary = self._run_scan(mode='Plex')
        self.assertIsNotNone(summary['skipped_reason'])
        self.assertIsNone(self._state())
        self.assertEqual(self.import_calls, [])

    def test_skips_when_mount_path_missing(self):
        with patch.object(
            ems, 'get_setting',
            side_effect=_settings_for('/definitely/does/not/exist', self.symlink_dir.name),
        ):
            summary = ems.scan_mount_for_external_adds()
        self.assertIsNotNone(summary['skipped_reason'])


if __name__ == "__main__":
    unittest.main()
