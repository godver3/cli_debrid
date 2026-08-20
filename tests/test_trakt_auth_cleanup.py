import json
import os
import tempfile
import unittest

from utilities.trakt_auth_cleanup import clear_stale_trakt_auth, trakt_is_configured


class TestTraktAuthCleanup(unittest.TestCase):
    def test_configured_trakt_is_left_untouched(self):
        config = {
            'Trakt': {
                'client_id': 'current-id',
                'client_secret': 'current-secret',
                'access_token': 'current-token',
            }
        }

        with tempfile.TemporaryDirectory() as config_dir:
            legacy_path = os.path.join(config_dir, '.pytrakt.json')
            with open(legacy_path, 'w') as legacy_file:
                json.dump({'OAUTH_TOKEN': 'legacy-token'}, legacy_file)

            self.assertTrue(trakt_is_configured(config))
            self.assertEqual(clear_stale_trakt_auth(config, config_dir), (False, False))

            with open(legacy_path, 'r') as legacy_file:
                self.assertEqual(json.load(legacy_file)['OAUTH_TOKEN'], 'legacy-token')
        self.assertEqual(config['Trakt']['access_token'], 'current-token')

    def test_incomplete_credentials_clear_both_token_stores(self):
        config = {
            'Trakt': {
                'client_id': '',
                'client_secret': '',
                'access_token': 'access',
                'refresh_token': 'refresh',
                'expires_at': 1234,
                'last_refresh': 'yesterday',
            }
        }

        with tempfile.TemporaryDirectory() as config_dir:
            legacy_path = os.path.join(config_dir, '.pytrakt.json')
            with open(legacy_path, 'w') as legacy_file:
                json.dump({
                    'CLIENT_ID': 'old-id',
                    'CLIENT_SECRET': 'old-secret',
                    'OAUTH_TOKEN': 'old-access',
                    'OAUTH_REFRESH': 'old-refresh',
                    'OAUTH_EXPIRES_AT': 5678,
                    'LAST_REFRESH': 'last-week',
                    'unrelated': 'preserved',
                }, legacy_file)

            self.assertEqual(clear_stale_trakt_auth(config, config_dir), (True, True))

            for key in ('access_token', 'refresh_token', 'expires_at', 'last_refresh'):
                self.assertEqual(config['Trakt'][key], '')

            with open(legacy_path, 'r') as legacy_file:
                legacy_config = json.load(legacy_file)
            for key in (
                'CLIENT_ID', 'CLIENT_SECRET', 'OAUTH_TOKEN', 'OAUTH_REFRESH',
                'OAUTH_EXPIRES_AT', 'LAST_REFRESH',
            ):
                self.assertEqual(legacy_config[key], '')
            self.assertEqual(legacy_config['unrelated'], 'preserved')

    def test_missing_legacy_file_still_clears_main_config(self):
        config = {'Trakt': {'refresh_token': 'stale'}}

        with tempfile.TemporaryDirectory() as config_dir:
            self.assertEqual(clear_stale_trakt_auth(config, config_dir), (True, False))

        self.assertFalse(trakt_is_configured(config))
        self.assertEqual(config['Trakt']['refresh_token'], '')

    def test_already_clean_state_does_not_report_changes(self):
        config = {'Trakt': {'client_id': ' ', 'client_secret': ''}}
        original_config = json.loads(json.dumps(config))

        with tempfile.TemporaryDirectory() as config_dir:
            self.assertEqual(clear_stale_trakt_auth(config, config_dir), (False, False))
        self.assertEqual(config, original_config)

    def test_missing_trakt_section_only_cleans_legacy_file(self):
        config = {'Metadata Battery': {'TMDB API Key': 'preserved'}}

        with tempfile.TemporaryDirectory() as config_dir:
            legacy_path = os.path.join(config_dir, '.pytrakt.json')
            with open(legacy_path, 'w') as legacy_file:
                json.dump({'OAUTH_TOKEN': 'stale', 'other': 'preserved'}, legacy_file)

            self.assertEqual(clear_stale_trakt_auth(config, config_dir), (False, True))

            with open(legacy_path, 'r') as legacy_file:
                legacy_config = json.load(legacy_file)

        self.assertNotIn('Trakt', config)
        self.assertEqual(legacy_config, {'OAUTH_TOKEN': '', 'other': 'preserved'})


if __name__ == '__main__':
    unittest.main()
