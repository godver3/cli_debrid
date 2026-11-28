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

_REAL_IS_OURS = ems._is_ours
_REAL_BUILD_OURS = ems._build_ours_sets  # the base class patches the module attribute


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

        # Nothing previously rejected, by default. _build_rejected_sets/_is_rejected
        # lazily import database.* for the same reason _already_tracked does, so
        # they are patched here too; TestRejectedContentIsSkipped drives the real
        # filter by overriding these.
        self._rejected_sets_patch = patch.object(ems, '_build_rejected_sets', return_value=set())
        self._rejected_sets_patch.start()
        self._is_rejected_patch = patch.object(ems, '_is_rejected', return_value=False)
        self._is_rejected_patch.start()

        # Nothing is cli_debrid's own, by default (same lazy database.* imports).
        self._ours_patches = [
            patch.object(ems, '_load_torrent_additions', return_value=[]),
            patch.object(ems, '_build_ours_sets', return_value=set()),
            patch.object(ems, '_is_ours', return_value=False),
        ]
        for p_ in self._ours_patches:
            p_.start()

        # Stub out routes.debug_routes so the lazy import inside
        # scan_mount_for_external_adds() never touches the real (flask-
        # dependent) module. import_result records every call for assertions.
        self.import_calls = []
        self.import_kwargs = []
        self.import_hook = lambda path: None
        self.import_user_initiated = []
        self.import_result = {'items_added_to_db': 0, 'symlinks_created': 0, 'media_files_found': 1}
        self._install_debug_routes_stub()

    def tearDown(self):
        self._restore_debug_routes_stub()
        for p_ in self._ours_patches:
            p_.stop()
        self._is_rejected_patch.stop()
        self._rejected_sets_patch.stop()
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

        def fake_run_task(scan_path, symlink_base_path, dry_run, task_id, trigger_plex_update_on_success=False, assumed_item_title_from_path=None, user_initiated=False, **kwargs):
            self.import_calls.append(scan_path)
            self.import_kwargs.append(kwargs)
            self.import_hook(scan_path)
            self.import_user_initiated.append(user_initiated)
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

    def _write_state(self, state):
        with open(os.path.join(self.db_content_dir.name, ems.STATE_FILENAME), 'w') as f:
            json.dump(state, f)

    def _age(self, *keys, seconds=3600):
        """Make folders look first-seen `seconds` ago (settle time)."""
        state = self._state()
        for k in keys:
            state[k]['first_seen'] -= seconds
        self._write_state(state)

    def _run_settled(self, *keys):
        """First sight (records pending), age past settle time, then the run
        that is allowed to import."""
        self._run_scan()
        self._age(*keys)
        return self._run_scan()

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
        self.import_result = {'items_added_to_db': 1, 'symlinks_created': 1, 'media_files_found': 1}
        summary = self._run_settled('New.Show.S01')

        self.assertEqual(summary['imported'], 1)
        self.assertEqual(len(self.import_calls), 1)
        self.assertTrue(self.import_kwargs[0]['skip_known_paths'])
        self.assertTrue(self.import_calls[0].endswith('New.Show.S01'))
        state = self._state()
        self.assertTrue(state['New.Show.S01']['imported'])
        self.assertFalse(state['Old.Show.S01'].get('imported', False))

    def test_already_imported_folder_is_not_reimported(self):
        self._mkfolder('Old.Show.S01')
        self._run_scan()
        self._mkfolder('New.Show.S01')
        self.import_result = {'items_added_to_db': 1, 'symlinks_created': 1, 'media_files_found': 1}
        self._run_settled('New.Show.S01')

        self.import_calls.clear()
        self._run_scan()  # third run: nothing new
        self.assertEqual(self.import_calls, [])

    def test_failed_import_is_retried_up_to_max_attempts(self):
        self._mkfolder('Old.Show.S01')
        self._run_scan()
        self._mkfolder('Failing.Show.S01')
        self.import_result = {'items_added_to_db': 0, 'symlinks_created': 0, 'media_files_found': 1}  # 0 added == failure

        state_path = os.path.join(self.db_content_dir.name, ems.STATE_FILENAME)
        self._run_scan()
        self._age('Failing.Show.S01')
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
        # One missed listing only marks it gone; it isn't forgotten yet.
        state = self._state()
        self.assertIn('gone_since', state['Gone.Show.S01'])

        # Past the grace period it is dropped.
        real_time = time.time
        with patch.object(ems.time, 'time', lambda: real_time() + ems.GONE_GRACE_SECONDS + 1):
            self._run_scan()
        state = self._state()
        self.assertNotIn('Gone.Show.S01', state)
        self.assertIn('Staying.Show.S01', state)

    def test_folder_missing_from_one_listing_stays_baselined(self):
        # A flaky mount lists only part of its folders once. When the rest come
        # back they must still be baseline -- not imported as "new" content.
        self._mkfolder('Staying.Show.S01')
        self._mkfolder('Flaky.Show.S01')
        self._run_scan()

        flaky = os.path.join(self.mount_dir.name, 'Flaky.Show.S01')
        shutil.rmtree(flaky)
        self._run_scan()
        os.makedirs(flaky)
        self._run_scan()

        self.assertEqual(self.import_calls, [])
        entry = self._state()['Flaky.Show.S01']
        self.assertTrue(entry['baseline'])
        self.assertNotIn('gone_since', entry)


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


class TestRejectedContentIsSkipped(ExternalMountScanTestBase):
    """The mount is not only external content.

    cli_debrid's own cache-check probes and failed grabs leave folders behind
    that the DB no longer points at, so they look exactly like an external add.
    Importing them reinstates the releases the queues just blacklisted -- live,
    this produced 10 library rows for one episode and 11 for one season, each
    with its own symlink and Plex scan.
    """

    JUNK = 'Ted Lasso S04E05 Riches of Embarrassment 1080p ATVP WEB-DL DDP5 1 Atmos H 264-FLUX'
    GENUINE = 'Some.Show.S01E01.1080p.WEB-DL'

    def _baseline(self):
        """First run always baselines, so anything under test has to appear after
        one. Uses a throwaway folder to get that out of the way."""
        self._mkfolder('Pre.Existing.Show.S01')
        self._run_scan()

    def _reject(self, *names):
        """Drive the real filter with a known rejected set."""
        normalized = {n.lower() for n in names}
        self._is_rejected_patch.stop()
        self._rejected_sets_patch.stop()
        self._rejected_sets_patch = patch.object(
            ems, '_build_rejected_sets', return_value=normalized)
        self._rejected_sets_patch.start()
        self._is_rejected_patch = patch.object(
            ems, '_is_rejected',
            side_effect=lambda name, rejected: name.lower() in rejected)
        self._is_rejected_patch.start()

    def test_blacklisted_release_is_not_imported(self):
        self._baseline()
        self._mkfolder(self.JUNK)
        self._reject(self.JUNK)
        summary = self._run_scan()
        self.assertEqual(self.import_calls, [],
                         'a release the queues rejected must not come back as an external add')
        self.assertEqual(summary['imported'], 0)
        self.assertEqual(summary['rejected'], 1)

    def test_genuine_external_add_still_imports_alongside_junk(self):
        self._baseline()
        self._mkfolder(self.JUNK)
        self._mkfolder(self.GENUINE)
        self._reject(self.JUNK)
        self.import_result = {'items_added_to_db': 1, 'symlinks_created': 1, 'media_files_found': 1}
        summary = self._run_settled(self.GENUINE)
        self.assertEqual(len(self.import_calls), 1)
        self.assertTrue(self.import_calls[0].endswith(self.GENUINE))
        self.assertEqual(summary['imported'], 1)
        self.assertTrue(self._state()[self.JUNK]['rejected'])

    def test_rejection_is_recorded_without_consuming_an_attempt(self):
        """No import was tried, so the 3-attempt budget must be untouched --
        otherwise a folder that is later un-blacklisted could never be picked up."""
        self._baseline()
        self._mkfolder(self.JUNK)
        self._reject(self.JUNK)
        self._run_scan()
        entry = self._state()[self.JUNK]
        self.assertTrue(entry['rejected'])
        self.assertNotIn('attempts', entry)
        self.assertFalse(entry.get('imported'))

    def test_rejected_folder_is_not_rechecked_on_the_next_run(self):
        self._baseline()
        self._mkfolder(self.JUNK)
        self._reject(self.JUNK)
        self._run_scan()
        summary = self._run_scan()
        self.assertEqual(self.import_calls, [])
        self.assertEqual(summary['rejected'], 0, 'should not be re-evaluated within the window')

    def test_rejected_folder_is_reconsidered_after_the_retry_window(self):
        """The user may since have un-blacklisted it and fetched it by hand --
        precisely the case this task exists to catch."""
        self._baseline()
        self._mkfolder(self.JUNK)
        self._reject(self.JUNK)
        self._run_scan()

        state = self._state()
        state[self.JUNK]['last_attempt'] = time.time() - (ems.RETRY_AFTER_SECONDS + 60)
        state[self.JUNK]['first_seen'] -= 3600
        with open(os.path.join(self.db_content_dir.name, ems.STATE_FILENAME), 'w') as f:
            json.dump(state, f)

        # No longer rejected, and now genuinely present.
        self._reject()
        self.import_result = {'items_added_to_db': 1, 'symlinks_created': 1}
        summary = self._run_scan()
        self.assertEqual(len(self.import_calls), 1)
        self.assertEqual(summary['imported'], 1)

    def test_baseline_content_is_still_never_imported(self):
        """The rejected path must not disturb the baseline guarantee: content
        already in the mount when the feature was switched on stays untouched
        whether or not it is rejected."""
        self._mkfolder(self.GENUINE)
        self._mkfolder(self.JUNK)
        self._run_scan()  # first run baselines both
        self._reject(self.JUNK)
        summary = self._run_scan()
        self.assertEqual(self.import_calls, [])
        self.assertEqual(summary['imported'], 0)
        self.assertEqual(summary['rejected'], 0, 'baseline is checked before rejection')


class TestHardening(ExternalMountScanTestBase):
    """Fixes A-H: ours, settle, no-media, partial import, baseline robustness,
    concurrency and mount layouts."""

    def setUp(self):
        super().setUp()
        self.import_result = {'items_added_to_db': 1, 'symlinks_created': 1, 'media_files_found': 1}

    def _baselined(self):
        self._mkfolder('Pre.Existing')
        self._run_scan()

    def _file(self, *parts):
        path = os.path.join(self.mount_dir.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()

    # B
    def test_new_folder_is_pending_then_imports_only_after_settle(self):
        self._baselined()
        self._mkfolder('New.Show.S01')
        summary = self._run_scan()
        self.assertEqual(summary['pending'], 1)
        self.assertEqual(self.import_calls, [])
        # A second run inside the settle window still does not import.
        self._run_scan()
        self.assertEqual(self.import_calls, [])
        self._age('New.Show.S01')
        self._run_scan()
        self.assertEqual(len(self.import_calls), 1)

    # A
    def test_ours_folder_is_skipped_and_not_attempted(self):
        self._baselined()
        self._mkfolder('Our.Torrent (1)')
        with patch.object(ems, '_is_ours', return_value=True):
            summary = self._run_scan()
        self.assertEqual(summary['ours'], 1)
        entry = self._state()['Our.Torrent (1)']
        self.assertTrue(entry['rejected'] and entry['ours'])
        self.assertEqual(self.import_calls, [])

    def test_ours_set_matches_any_torrent_additions_key_and_dup_suffix(self):
        class FakeDR:
            @staticmethod
            def normalize_string_for_comparison(v):
                return v.lower()
        fake_db = types.ModuleType('database')
        fake_dr = types.ModuleType('database.database_reading')
        fake_dr.normalize_string_for_comparison = FakeDR.normalize_string_for_comparison
        additions = [('abc', {'original_scraped_torrent_title': 'Some.Release-GRP'}),
                     ('def', {'debrid_folder_name': 'Other (2)'})]
        with patch.dict(sys.modules, {'database': fake_db, 'database.database_reading': fake_dr}):
            ours = _REAL_BUILD_OURS(additions)
            self.assertTrue(_REAL_IS_OURS('Some.Release-GRP (1)', ours))
            self.assertTrue(_REAL_IS_OURS('Other', ours))
            self.assertFalse(_REAL_IS_OURS('Unrelated', ours))

    # D
    def test_zero_media_does_not_consume_attempt_and_gives_up_after_24h(self):
        self._baselined()
        self._mkfolder('Empty.Show')
        self.import_result = {'items_added_to_db': 0, 'symlinks_created': 0, 'media_files_found': 0}
        self._run_scan()
        self._age('Empty.Show')
        for _ in range(ems.MAX_ATTEMPTS + 2):
            self._run_scan()
        self.assertEqual(len(self.import_calls), ems.MAX_ATTEMPTS + 2)
        entry = self._state()['Empty.Show']
        self.assertEqual(entry.get('attempts', 0), 0)
        self.assertFalse(entry.get('given_up'))
        self._age('Empty.Show', seconds=ems.PENDING_GIVE_UP_SECONDS)
        with self.assertLogs(level='WARNING') as cm:
            summary = self._run_scan()
        self.assertEqual(summary['given_up'], 1)
        self.assertTrue(any("giving up on 'Empty.Show' after" in m for m in cm.output))
        n = len(self.import_calls)
        self._run_scan()
        self.assertEqual(len(self.import_calls), n)

    def test_max_attempts_logs_give_up_once(self):
        self._baselined()
        self._mkfolder('Bad.Show')
        self.import_result = {'items_added_to_db': 0, 'media_files_found': 1}
        self._run_scan()
        self._age('Bad.Show')
        for _ in range(ems.MAX_ATTEMPTS):
            with self.assertLogs(level='WARNING') as cm:
                self._run_scan()
            state = self._state()
            state['Bad.Show']['last_attempt'] = 0
            self._write_state(state)
        self.assertTrue(self._state()['Bad.Show']['given_up'])
        self.assertEqual(sum("giving up on 'Bad.Show'" in m for m in cm.output), 1)

    # E
    def test_partial_import_stays_retryable(self):
        self._baselined()
        self._mkfolder('Partial.Show')
        self.import_result = {'items_added_to_db': 2, 'media_files_found': 3, 'metadata_errors': 1}
        summary = self._run_settled('Partial.Show')
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['imported'], 0)
        self.assertFalse(self._state()['Partial.Show'].get('imported'))

    def test_retry_with_only_already_imported_files_counts_as_imported(self):
        self._baselined()
        self._mkfolder('Retry.Show')
        self.import_result = {'items_added_to_db': 0, 'media_files_found': 0, 'skipped_previously_processed': 3}
        summary = self._run_settled('Retry.Show')
        self.assertEqual(summary['imported'], 1)

    # F
    def test_state_has_meta_and_old_state_without_it_still_works(self):
        self._baselined()
        meta = self._state()['__meta__']
        self.assertIn('baselined_at', meta)
        self.assertIn('last_run', meta)
        self.assertEqual(meta['last_entry_count'], 1)
        state = self._state()
        del state['__meta__']
        self._write_state(state)
        summary = self._run_scan()
        self.assertIsNone(summary['skipped_reason'])
        self.assertEqual(summary['baselined'], 0)
        self.assertIn('__meta__', self._state())

    def test_corrupt_state_is_moved_aside_and_run_skipped(self):
        self._baselined()
        path = os.path.join(self.db_content_dir.name, ems.STATE_FILENAME)
        with open(path, 'w') as f:
            f.write('{not json')
        self._mkfolder('Arrived.Meanwhile')
        with self.assertLogs(level='ERROR'):
            summary = self._run_scan()
        self.assertTrue(summary['skipped_reason'])
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any('.corrupt-' in n for n in os.listdir(self.db_content_dir.name)))
        self.assertEqual(self.import_calls, [])
        summary = self._run_scan()  # fresh baseline, no import
        self.assertEqual(summary['baselined'], 2)
        self.assertEqual(self.import_calls, [])

    def test_flaky_listing_skips_run_without_pruning(self):
        for i in range(25):
            self._mkfolder(f'Show.{i}')
        self._run_scan()
        for i in range(5, 25):
            shutil.rmtree(os.path.join(self.mount_dir.name, f'Show.{i}'))
        with self.assertLogs(level='WARNING') as cm:
            self._run_scan()
        self.assertTrue(any('flaky' in m for m in cm.output))
        state = self._state()
        self.assertEqual(len(state) - 1, 25)
        self.assertFalse(any('gone_since' in v for k, v in state.items() if k != '__meta__'))

    def test_long_gap_baselines_new_folders(self):
        self._baselined()
        state = self._state()
        state['__meta__']['last_run'] -= 3 * 24 * 3600
        self._write_state(state)
        self._mkfolder('Arrived.While.Off')
        summary = self._run_scan()
        self.assertEqual(summary['baselined'], 1)
        self.assertTrue(self._state()['Arrived.While.Off']['baseline'])
        self.assertEqual(self.import_calls, [])

    # G
    def test_overlapping_scan_is_skipped(self):
        self._baselined()
        self.assertTrue(ems._SCAN_LOCK.acquire(blocking=False))
        try:
            summary = self._run_scan()
        finally:
            ems._SCAN_LOCK.release()
        self.assertIn('still running', summary['skipped_reason'])

    # H
    def test_all_dir_is_scanned_instead_of_mount_root(self):
        self._mkfolder('__all__/Torrent.A')
        ems._warned_all_dir = False
        with self.assertLogs(level='WARNING') as cm:
            self._run_scan()
        self.assertTrue(any('__all__' in m for m in cm.output))
        state = self._state()
        self.assertIn('Torrent.A', state)
        self.assertNotIn('__all__', state)

    def _run_with_usenet(self, usenet_path):
        base = _settings_for(self.mount_dir.name, self.symlink_dir.name)

        def fake(section, key, default=None):
            if (section, key) == ('Usenet Provider', 'mounted_file_location'):
                return usenet_path
            return base(section, key, default)
        with patch.object(ems, 'get_setting', side_effect=fake):
            return ems.scan_mount_for_external_adds()

    def test_usenet_root_nested_layout_with_prefixed_keys_and_own_baseline(self):
        self._mkfolder('Main.One')
        self._run_scan()  # main baselined, usenet not configured yet
        usenet = tempfile.TemporaryDirectory()
        self.addCleanup(usenet.cleanup)
        for job in ('Job.A', 'Job.B'):
            d = os.path.join(usenet.name, 'tv', job)
            os.makedirs(d)
            open(os.path.join(d, 'x.mkv'), 'w').close()
        summary = self._run_with_usenet(usenet.name + '/__all__')  # suffix stripped
        self.assertEqual(summary['baselined'], 2)  # new root baselined, not imported
        state = self._state()
        self.assertIn('usenet:tv/Job.A', state)
        self.assertNotIn('usenet:tv', state)
        self.assertIn('last_entry_count', state['__meta__']['usenet'])
        self.assertEqual(self.import_calls, [])
        d = os.path.join(usenet.name, 'tv', 'Job.C')
        os.makedirs(d)
        open(os.path.join(d, 'x.mkv'), 'w').close()
        self._run_with_usenet(usenet.name)
        self._age('usenet:tv/Job.C')
        self._run_with_usenet(usenet.name)
        self.assertEqual(len(self.import_calls), 1)
        self.assertTrue(self.import_calls[0].endswith(os.path.join('tv', 'Job.C')))

    def test_usenet_cli_mount_layout_scans_all_and_skips_reserved(self):
        # A separate cli_mount/built-in mount used for usenet exposes each job in
        # __all__ AND nzbs/; listing the root would see every job twice.
        usenet = tempfile.TemporaryDirectory()
        self.addCleanup(usenet.cleanup)
        for view in ('__all__', 'nzbs'):
            d = os.path.join(usenet.name, view, 'Job.A')
            os.makedirs(d)
            open(os.path.join(d, 'x.mkv'), 'w').close()
        os.makedirs(os.path.join(usenet.name, 'torrents', 'Torrent.X'))
        base = _settings_for(self.mount_dir.name, self.symlink_dir.name)
        with patch.object(ems, 'get_setting', side_effect=lambda s, k, d=None: usenet.name if k == 'mounted_file_location' else base(s, k, d)):
            roots = ems._resolve_roots(self.mount_dir.name)
        usenet_root = [r for r in roots if r['id'] == 'usenet'][0]
        self.assertEqual(usenet_root['path'], os.path.join(usenet.name, '__all__'))
        self.assertEqual([k for k, _, _ in ems._list_root(usenet_root)], ['usenet:Job.A'])

    def test_known_jobs_skip_category_probe(self):
        from usenet import mount_layout
        mount = tempfile.TemporaryDirectory()
        self.addCleanup(mount.cleanup)
        os.makedirs(os.path.join(mount.name, 'Job.A'))
        with patch.object(mount_layout, 'is_category_dir', side_effect=AssertionError('probed')):
            self.assertEqual(mount_layout.list_job_dirs(mount.name, known_jobs={'Job.A'}),
                             [('Job.A', os.path.join(mount.name, 'Job.A'))])

    def test_flaky_listing_accepted_after_repeated_low_counts(self):
        # A real mass deletion must not leave the root skipped forever.
        for i in range(25):
            self._mkfolder(f'Show.{i}')
        self._run_scan()
        for i in range(5, 25):
            shutil.rmtree(os.path.join(self.mount_dir.name, f'Show.{i}'))
        with self.assertLogs(level='WARNING'):
            for _ in range(ems.FLAKY_ACCEPT_AFTER):
                self._run_scan()
        meta = self._state()['__meta__']
        self.assertEqual(meta['last_entry_count'], 5)
        self.assertNotIn('low_listing_runs', meta)
        self._mkfolder('New.Arrival')
        self._run_scan()  # back to normal: no flaky skip at 6 vs 5
        self.assertIn('New.Arrival', self._state())

    def test_usenet_root_inside_main_root_is_ignored(self):
        self._mkfolder('Main.One')
        self._run_scan()
        self._mkfolder('Main.One/sub')
        roots = None
        base = _settings_for(self.mount_dir.name, self.symlink_dir.name)
        with patch.object(ems, 'get_setting', side_effect=lambda s, k, d=None: os.path.join(self.mount_dir.name, 'Main.One') if k == 'mounted_file_location' else base(s, k, d)):
            roots = ems._resolve_roots(self.mount_dir.name)
        self.assertEqual([r['id'] for r in roots], ['main'])


class TestRejectedSetConstruction(unittest.TestCase):
    """_build_rejected_sets joins the hash-keyed not-wanted list to mount folder
    names through torrent_additions.item_data, which carries debrid_folder_name.
    Neither database.* module is importable here (see module docstring), so the
    join is exercised as a simulation against a real in-memory schema."""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            'CREATE TABLE torrent_additions (torrent_hash TEXT, item_data TEXT)')
        self.conn.execute(
            'CREATE TABLE media_items (filled_by_title TEXT, real_debrid_original_title TEXT,'
            ' original_scraped_torrent_title TEXT, state TEXT, ghostlisted INTEGER DEFAULT 0)')

    def tearDown(self):
        self.conn.close()

    def _add_torrent(self, torrent_hash, folder_name):
        self.conn.execute(
            'INSERT INTO torrent_additions (torrent_hash, item_data) VALUES (?,?)',
            (torrent_hash, json.dumps({'debrid_folder_name': folder_name,
                                       'filled_by_title': folder_name})))
        self.conn.commit()

    def _build(self, not_wanted):
        """Mirrors _build_rejected_sets, minus the lazy imports."""
        rejected = set()
        not_wanted = {h.lower() for h in not_wanted}
        for row in self.conn.execute('SELECT torrent_hash, item_data FROM torrent_additions'):
            if (row['torrent_hash'] or '').lower() not in not_wanted:
                continue
            data = json.loads(row['item_data'])
            for key in ('debrid_folder_name', 'filled_by_title'):
                if data.get(key):
                    rejected.add(data[key].lower())
        for row in self.conn.execute(
                "SELECT filled_by_title, real_debrid_original_title,"
                " original_scraped_torrent_title FROM media_items"
                " WHERE state = 'Blacklisted' OR ghostlisted = 1"):
            for value in tuple(row):
                if value:
                    rejected.add(value.lower())
        rejected.discard('')
        return rejected

    def test_not_wanted_hash_resolves_to_its_mount_folder_name(self):
        self._add_torrent('9AB6157B5B68300855478884E53EA41D3612DD51', 'Ted.Lasso.S04E05-FLUX')
        self.assertIn('ted.lasso.s04e05-flux',
                      self._build({'9ab6157b5b68300855478884e53ea41d3612dd51'}))

    def test_hash_comparison_is_case_insensitive(self):
        """Providers report hashes in either case; the not-wanted file stores
        whatever it was given."""
        self._add_torrent('abcdef0123456789abcdef0123456789abcdef01', 'Lowercase.Release')
        self.assertIn('lowercase.release',
                      self._build({'ABCDEF0123456789ABCDEF0123456789ABCDEF01'}))

    def test_a_torrent_still_wanted_is_not_rejected(self):
        self._add_torrent('1111111111111111111111111111111111111111', 'Still.Wanted.Release')
        self.assertEqual(self._build({'2222222222222222222222222222222222222222'}), set())

    def test_blacklisted_media_item_titles_are_included(self):
        self.conn.execute(
            'INSERT INTO media_items (filled_by_title, state) VALUES (?,?)',
            ('Blacklisted.Release.1080p', 'Blacklisted'))
        self.conn.commit()
        self.assertIn('blacklisted.release.1080p', self._build(set()))

    def test_ghostlisted_media_item_titles_are_included(self):
        self.conn.execute(
            'INSERT INTO media_items (real_debrid_original_title, state, ghostlisted)'
            ' VALUES (?,?,?)', ('Ghosted.Release.2160p', 'Collected', 1))
        self.conn.commit()
        self.assertIn('ghosted.release.2160p', self._build(set()))

    def test_collected_titles_are_not_rejected(self):
        """Only rejected content -- a Collected row is handled by _already_tracked."""
        self.conn.execute(
            'INSERT INTO media_items (filled_by_title, state) VALUES (?,?)',
            ('Perfectly.Fine.Release', 'Collected'))
        self.conn.commit()
        self.assertEqual(self._build(set()), set())


if __name__ == "__main__":
    unittest.main()
