#!/usr/bin/env python3
"""Tests for live content-source version resolution."""

import os
import unittest
from unittest.mock import patch

from content_checkers.content_cache_management import (
    load_live_content_source_config,
    normalize_enabled_versions,
)
from tests._source import runner_source, runner_method, runner_tree  # noqa: F401


class TestNormalizeEnabledVersions(unittest.TestCase):
    def test_normalizes_list_versions(self):
        self.assertEqual(
            normalize_enabled_versions(["1080p", "4k"]),
            {"1080p": True, "4k": True},
        )

    def test_keeps_only_enabled_dictionary_versions(self):
        self.assertEqual(
            normalize_enabled_versions({"1080p": True, "4k": False, "UFC": "true"}),
            {"1080p": True, "UFC": True},
        )

    def test_invalid_or_empty_versions_are_empty(self):
        for versions in (None, "1080p", [], {}, {"1080p": False}):
            with self.subTest(versions=versions):
                self.assertEqual(normalize_enabled_versions(versions), {})


class TestLoadLiveContentSourceConfig(unittest.TestCase):
    @patch("utilities.settings.get_all_settings", create=True)
    def test_returns_current_saved_source_instead_of_scheduled_snapshot(self, get_settings):
        saved_source = {
            "type": "My Plex Watchlist",
            "versions": ["1080p"],
        }
        get_settings.return_value = {
            "Content Sources": {
                "My Plex Watchlist_1": saved_source,
            }
        }

        source = load_live_content_source_config("My Plex Watchlist_1")

        self.assertEqual(source["versions"], ["1080p"])
        self.assertIsNot(source, saved_source)

    @patch("utilities.settings.get_all_settings", create=True)
    def test_missing_source_returns_none(self, get_settings):
        get_settings.return_value = {"Content Sources": {}}
        self.assertIsNone(load_live_content_source_config("Removed Source_1"))


class TestContentSourceExecutionUsesLiveVersions(unittest.TestCase):
    def test_live_config_is_loaded_before_versions_are_resolved(self):
        method = runner_method('process_content_source')

        live_load = method.index("live_source_data = load_live_content_source_config(source)")
        version_read = method.index("versions_from_config = data.get('versions', [])")
        self.assertLess(live_load, version_read)
        self.assertIn("versions_dict = normalize_enabled_versions(versions_from_config)", method)
        self.assertIn("has no enabled versions in current settings", method)


if __name__ == "__main__":
    unittest.main()
