#!/usr/bin/env python3
"""Tests for the settings snapshot prepended to shared log bundles.

Debugging user reports (e.g. a watchlist unblacklist/granular-versions issue)
kept requiring a separate round of screenshots to learn what was actually
configured. get_diagnostic_settings_snapshot() bundles app version + redacted
settings into the log share so that information travels with the logs.
"""

import unittest
from unittest.mock import patch

from utilities.ai_context import get_diagnostic_settings_snapshot


FAKE_CONFIG = {
    'Content Sources': {
        'Other Plex Watchlist_1': {
            'type': 'Other Plex Watchlist',
            'enabled': True,
            'username': 'Bela_I',
            'token': 'super-secret-plex-token',
            'unblacklist_on_source_run': True,
            'versions': ['1080p'],
        }
    },
    'Debug': {
        'enable_granular_version_additions': False,
        'disable_content_source_caching': False,
    },
    'Real-Debrid': {
        'api_key': 'rd-secret-key-value',
    },
}


class TestDiagnosticSettingsSnapshot(unittest.TestCase):
    @patch('utilities.settings.load_config', return_value=FAKE_CONFIG)
    @patch('utilities.version.get_app_version', return_value='9.9.9-test')
    def test_includes_app_version(self, mock_version, mock_config):
        snapshot = get_diagnostic_settings_snapshot()
        self.assertIn('App version: 9.9.9-test', snapshot)

    @patch('utilities.settings.load_config', return_value=FAKE_CONFIG)
    def test_redacts_sensitive_values(self, mock_config):
        snapshot = get_diagnostic_settings_snapshot()
        self.assertNotIn('super-secret-plex-token', snapshot)
        self.assertNotIn('rd-secret-key-value', snapshot)
        self.assertNotIn('Bela_I', snapshot)

    @patch('utilities.settings.load_config', return_value=FAKE_CONFIG)
    def test_keeps_non_sensitive_debugging_relevant_fields(self, mock_config):
        snapshot = get_diagnostic_settings_snapshot()
        self.assertIn('unblacklist_on_source_run', snapshot)
        self.assertIn('"unblacklist_on_source_run": true', snapshot)
        self.assertIn('enable_granular_version_additions', snapshot)

    @patch('utilities.settings.load_config', side_effect=Exception('boom'))
    def test_survives_config_load_failure(self, mock_config):
        # Should not raise even if config loading blows up.
        snapshot = get_diagnostic_settings_snapshot()
        self.assertIsInstance(snapshot, str)

    @patch('utilities.settings.load_config', return_value=FAKE_CONFIG)
    def test_lists_content_sources_with_enabled_state(self, mock_config):
        snapshot = get_diagnostic_settings_snapshot()
        self.assertIn('--- Content Sources ---', snapshot)
        self.assertIn(
            "Other Plex Watchlist_1 (type=Other Plex Watchlist, enabled=True, "
            "versions=['1080p'], unblacklist_on_source_run=True)",
            snapshot,
        )

    @patch('utilities.settings.load_config', return_value=FAKE_CONFIG)
    def test_debug_advanced_settings_included_in_flat_summary(self, mock_config):
        snapshot = get_diagnostic_settings_snapshot()
        self.assertIn('Debug.enable_granular_version_additions', snapshot)


if __name__ == "__main__":
    unittest.main()
