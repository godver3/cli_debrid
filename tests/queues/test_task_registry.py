"""queues/runner/task_registry.py: defaults, overrides, and the old-settings migration."""
import json
import os
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401  (app import order; run_program first is circular)
from queues.runner import task_registry as reg


def _settings(**sections):
    def get(section, key=None, default=None):
        return sections.get(section, {}).get(key, default)
    return get


PLEX = {'File Management': {'file_collection_management': 'Plex'},
        'Plex': {'url': 'http://plex', 'token': 't'}}
SYMLINK = {'File Management': {'file_collection_management': 'Symlinked/Local',
                               'plex_url_for_symlink': 'http://plex', 'plex_token_for_symlink': 't'}}


class DefaultsTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch('utilities.set_supervisor_env.is_limited_environment', return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_queues_and_core_tasks_on(self):
        on = reg.default_enabled(_settings(**PLEX))
        for name in reg.QUEUE_TASKS + ('task_heartbeat', 'task_nzb_health_check', 'task_backup_database'):
            self.assertIn(name, on)

    def test_plex_mode(self):
        on = reg.default_enabled(_settings(**PLEX))
        self.assertIn('task_plex_full_scan', on)
        self.assertIn('task_plex_movie_boxsets', on)
        self.assertNotIn('task_verify_symlinked_files', on)
        self.assertNotIn('task_check_plex_files', on)

    def test_plex_checks_off_without_mount_path(self):
        get = _settings(**{**PLEX, 'Plex': {'disable_plex_library_checks': True}})
        on = reg.default_enabled(get)
        self.assertNotIn('Checking', on)
        self.assertNotIn('task_plex_full_scan', on)
        self.assertIn('task_check_plex_files', on)

    def test_symlink_mode(self):
        on = reg.default_enabled(_settings(**SYMLINK))
        self.assertIn('task_verify_symlinked_files', on)
        self.assertNotIn('task_plex_full_scan', on)
        self.assertNotIn('task_scan_mount_for_external_adds', on)

    def test_jellyfin_symlink_mode_skips_plex_collections(self):
        get = _settings(**{'File Management': {'file_collection_management': 'Symlinked/Local',
                                               'media_server_type': 'jellyfin'}})
        on = reg.default_enabled(get)
        self.assertNotIn('task_plex_smart_collection_posters', on)
        self.assertNotIn('task_plex_movie_boxsets', on)

    def test_overlays_follow_setting(self):
        self.assertNotIn('task_overlay_sync', reg.default_enabled(_settings(**PLEX)))
        get = _settings(**PLEX, **{'Overlay Settings': {'overlays_enabled': True}})
        self.assertIn('task_overlay_sync', reg.default_enabled(get))

    def test_library_maintenance_off_even_in_limited_mode(self):
        with mock.patch('utilities.set_supervisor_env.is_limited_environment', return_value=True):
            on = reg.default_enabled(_settings(**PLEX))
        self.assertNotIn('task_run_library_maintenance', on)
        self.assertIn('task_regulate_system_load', on)

    def test_content_sources_on_by_default(self):
        on = reg.default_enabled(_settings(**PLEX), {'Trakt Watchlist_1': {}})
        self.assertIn('task_Trakt Watchlist_1_wanted', on)

    def test_content_source_intervals(self):
        iv = reg.default_intervals({'Collected_1': {}, 'Overseerr_1': {}})
        self.assertEqual(iv['task_Collected_1_wanted'], 86400)
        self.assertEqual(iv['task_Overseerr_1_wanted'], 900)

    def test_manual_tasks_never_scheduled(self):
        iv = reg.default_intervals()
        for spec in reg.SPECS.values():
            if spec.kind == reg.MANUAL:
                self.assertNotIn(spec.name, iv)


class OverrideTests(unittest.TestCase):
    def test_queue_override_applies(self):
        on = reg.apply_overrides({'Wanted', 'Checking'}, {'Checking': False}, {'Wanted', 'Checking'})
        self.assertEqual(on, {'Wanted'})

    def test_setting_backed_ignores_override(self):
        known = {'task_process_plex_reported_issues'}
        on = reg.apply_overrides(set(), {'task_process_plex_reported_issues': True}, known)
        self.assertEqual(on, set())

    def test_unknown_override_ignored(self):
        self.assertEqual(reg.apply_overrides({'Wanted'}, {'task_gone': True}, {'Wanted'}), {'Wanted'})


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {'USER_DB_CONTENT': self.tmp})
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch('utilities.set_supervisor_env.is_limited_environment', return_value=False)
        p2.start()
        self.addCleanup(p2.stop)

    def _write(self, name, data):
        with open(os.path.join(self.tmp, name), 'w') as f:
            json.dump(data, f)

    def _read(self, name):
        with open(os.path.join(self.tmp, name)) as f:
            return json.load(f)

    def test_save_writes_only_differences(self):
        known = {'Wanted', 'task_backup_debrid', 'task_heartbeat', 'task_process_plex_reported_issues'}
        defaults = {'Wanted', 'task_heartbeat'}
        enabled = {'task_heartbeat', 'task_backup_debrid', 'task_process_plex_reported_issues'}
        reg.save_overrides(enabled, defaults, known)
        data = self._read('task_toggles.json')
        self.assertEqual(data.pop(reg.TOGGLES_VERSION_KEY), reg.TOGGLES_VERSION)
        self.assertEqual(data, {'Wanted': False, 'task_backup_debrid': True})

    def test_legacy_conversion_drops_queues_and_defaults(self):
        get = _settings(**PLEX)
        self._write('task_toggles.json', {
            '_migration_version': '0.6.34',
            'Checking': False,                 # never applied before: dropped
            'task_heartbeat': True,            # matches default: dropped
            'task_backup_debrid': True,        # user turned on: kept
            'task_refresh_plex_tokens': False,  # user turned off: kept
            'task_gone': True,                 # unknown: dropped
        })
        self.assertTrue(reg.convert_legacy_toggles(get, {}))
        self.assertEqual(reg.load_overrides(), {'task_backup_debrid': True, 'task_refresh_plex_tokens': False})

    def test_migrate_moves_old_debug_settings(self):
        self._write('task_toggles.json', {'_migration_version': '0.6.34'})
        config = {'Debug': {'enable_library_maintenance_task': True,
                            'content_source_check_period': {'Trakt Lists_1': 30, 'Collected_1': 15}},
                  'Content Sources': {'Trakt Lists_1': {}, 'Collected_1': {}}}
        dropped = []
        with mock.patch.object(reg, '_drop_setting', side_effect=lambda s, k: dropped.append((s, k))):
            reg.migrate(_settings(**config), lambda: config)
        self.assertEqual(reg.load_overrides(), {'task_run_library_maintenance': True})
        # 15 was the hidden form's value, not a user choice
        self.assertEqual(self._read('task_intervals.json'), {'task_Trakt Lists_1_wanted': 1800})
        self.assertIn(('Debug', 'enable_library_maintenance_task'), dropped)
        self.assertIn(('Debug', 'content_source_check_period'), dropped)

    def test_migrate_keeps_existing_custom_interval(self):
        self._write('task_toggles.json', {'_migration_version': reg.TOGGLES_VERSION})
        self._write('task_intervals.json', {'task_Trakt Lists_1_wanted': 600})
        config = {'Debug': {'content_source_check_period': {'Trakt Lists_1': 30}}}
        with mock.patch.object(reg, '_drop_setting'):
            reg.migrate(_settings(**config), lambda: config)
        self.assertEqual(self._read('task_intervals.json'), {'task_Trakt Lists_1_wanted': 600})

    def test_unknown_version_resets(self):
        self._write('task_toggles.json', {'_migration_version': '0.1', 'task_backup_debrid': True})
        reg.migrate(_settings(), lambda: {})
        self.assertEqual(reg.load_overrides(), {})


class RunnerResolutionTests(unittest.TestCase):
    def test_every_task_has_a_method(self):
        import queues.run_program as rp
        from queues.queue_manager import QueueManager
        queue_methods = {
            'Wanted': 'process_wanted', 'Scraping': 'process_scraping', 'Adding': 'process_adding',
            'Checking': 'process_checking', 'Sleeping': 'process_sleeping', 'Unreleased': 'process_unreleased',
            'Blacklisted': 'process_blacklisted', 'Pending Uncached': 'process_pending_uncached',
            'Upgrading': 'process_upgrading', 'final_check_queue': 'process_final_check',
            'Pre_release': 'process_pre_release',
        }
        for spec in reg.SPECS.values():
            with self.subTest(task=spec.name):
                if spec.kind == reg.QUEUE:
                    self.assertTrue(hasattr(QueueManager, queue_methods[spec.name]))
                else:
                    self.assertTrue(hasattr(rp.ProgramRunner, spec.name))

    def test_interval_update_reuses_schedule_task(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, 'queues', 'run_program.py')) as f:
            src = f.read()
        body = src[src.index('def update_task_interval'):src.index('def task_trim_memory')]
        self.assertIn('self._schedule_task(normalized_name, target_interval_seconds)', body)
        self.assertNotIn('IntervalTrigger(', body)


if __name__ == '__main__':
    unittest.main()
