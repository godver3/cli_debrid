#!/usr/bin/env python3
"""Tests for manual/debug content-source runs bypassing the source cache.

Regression coverage for Ed's recurring watchlist issue: a manual re-run after
flipping unblacklist_on_source_run was still being cache-skipped before ever
reaching add_wanted_items, so the unblacklist flag never had anything to act on.
"""

import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8") as f:
        return f.read()


class TestTriggerTaskBypassesCacheForContentSources(unittest.TestCase):
    def setUp(self):
        self.source = _read("queues/run_program.py")

    def test_get_task_target_accepts_manual_flag(self):
        self.assertIn("def _get_task_target(self, task_name: str, manual: bool = False):", self.source)

    def test_manual_content_source_tasks_set_bypass_cache_kwarg(self):
        method_start = self.source.index("def _get_task_target(self, task_name: str, manual: bool = False):")
        method_end = self.source.index("\n    # *** END EDIT ***", method_start)
        method = self.source[method_start:method_end]
        self.assertIn("if manual:", method)
        self.assertIn("kwargs = {'bypass_cache': True}", method)

    def test_trigger_task_requests_manual_resolution(self):
        trigger_start = self.source.index("def trigger_task(self, task_name):")
        trigger_end = self.source.index("\n    def ", trigger_start + 1)
        trigger_method = self.source[trigger_start:trigger_end]
        self.assertIn("self._get_task_target(job_id_base, manual=True)", trigger_method)

    def test_process_content_source_honors_bypass_cache(self):
        method_start = self.source.index("def process_content_source(self, source, data, bypass_cache=False):")
        method_end = self.source.index("\n    def task_purge_not_wanted_magnets_file", method_start)
        method = self.source[method_start:method_end]
        self.assertIn("bypass_cache or should_process_item(item, source, source_cache)", method)
        self.assertIn("source={source}, unblacklist={unblacklist_on_source_run}", method)


class TestDebugManualIngestionRoutePassesUnblacklist(unittest.TestCase):
    def setUp(self):
        self.source = _read("routes/debug_routes.py")

    def test_reads_unblacklist_setting_from_source_data(self):
        self.assertIn(
            "unblacklist_on_source_run = bool(source_data.get('unblacklist_on_source_run', False))",
            self.source,
        )

    def test_trakt_watchlist_fetch_receives_unblacklist_flag(self):
        self.assertIn(
            "get_wanted_from_trakt_watchlist(versions_from_config, unblacklist=unblacklist_on_source_run)",
            self.source,
        )

    def test_add_wanted_items_calls_receive_unblacklist_flag(self):
        self.assertIn(
            "add_wanted_items(final_items_for_db_batch, versions_to_inject or versions_from_config, unblacklist=unblacklist_on_source_run)",
            self.source,
        )
        self.assertIn(
            "add_wanted_items(final_items_for_db_non_batch, versions_from_config, unblacklist=unblacklist_on_source_run)",
            self.source,
        )

    def test_debug_route_no_longer_filters_by_cache(self):
        self.assertNotIn("if should_process_item(item, source_id, source_cache)", self.source)


if __name__ == "__main__":
    unittest.main()
