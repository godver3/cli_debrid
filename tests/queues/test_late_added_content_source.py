"""A content source added after the runner was built is not switched off.

The runner builds its task list once, at boot. A source added in onboarding
had no task, and when Start ran initialization the "sync config to runtime
state" step wrote enabled=False for it, so the watchlist never ran.
"""
import os
import unittest
from unittest import mock

import database  # noqa: F401  (app import order; run_program first is circular)
import queues.run_program as rp

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _runner(task_intervals, enabled_tasks):
    runner = object.__new__(rp.ProgramRunner)
    runner.task_intervals = dict(task_intervals)
    runner.enabled_tasks = set(enabled_tasks)
    return runner


class RunInitializationSyncTests(unittest.TestCase):
    def _run(self, runner, sources):
        config = {'Content Sources': sources}
        with mock.patch.object(rp, 'get_setting', return_value=True), \
                mock.patch('utilities.settings.get_all_settings', return_value=config), \
                mock.patch('utilities.settings.save_config') as save:
            runner.run_initialization()
        return config, save

    def test_source_unknown_to_runner_left_enabled(self):
        runner = _runner({}, set())
        config, save = self._run(runner, {'My Plex Watchlist_1': {'enabled': True}})
        self.assertTrue(config['Content Sources']['My Plex Watchlist_1']['enabled'])
        save.assert_not_called()

    def test_known_disabled_source_still_synced(self):
        runner = _runner({'task_My Plex Watchlist_1_wanted': 900}, set())
        config, save = self._run(runner, {'My Plex Watchlist_1': {'enabled': True}})
        self.assertFalse(config['Content Sources']['My Plex Watchlist_1']['enabled'])
        save.assert_called_once()


class AddRoutesReloadRunnerTests(unittest.TestCase):
    def _route_source(self, path, func):
        with open(os.path.join(ROOT, path), encoding='utf-8') as f:
            src = f.read()
        start = src.index(f'def {func}(')
        end = src.find('\n@', start)
        return src[start:end]

    def test_onboarding_add_reloads(self):
        body = self._route_source('routes/onboarding_routes.py', 'add_onboarding_content_source')
        self.assertIn("_reload_components_after_settings_change('content_source')", body)

    def test_settings_add_reloads(self):
        body = self._route_source('routes/settings_routes.py', 'add_content_source_route')
        self.assertIn("_reload_components_after_settings_change('content_source')", body)


if __name__ == '__main__':
    unittest.main()
