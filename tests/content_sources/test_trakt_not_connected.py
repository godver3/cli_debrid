"""With no Trakt account connected, the battery's Trakt fallbacks return None
quietly: no refresh attempt, no ERROR lines, no HTTP request.

TVDB fallbacks (IMDb mismatch, failed fetch, air dates, seasons) call
trakt_client directly; each call used to log "No refresh token available" and
"Failed to refresh Trakt token" even though Trakt was never set up.
"""
import unittest
from unittest.mock import patch

from cli_battery.app import trakt_auth, trakt_client


def _settings(values):
    return lambda section, key, default='': values.get(key, default)


class TraktNotConnectedTests(unittest.TestCase):
    def setUp(self):
        trakt_client._not_connected_logged = False

    def test_no_tokens_returns_none_without_refresh_or_request(self):
        with patch('utilities.settings.get_setting', side_effect=_settings({})), \
                patch.object(trakt_auth, 'refresh_access_token') as refresh, \
                patch.object(trakt_client._http, 'request') as http, \
                patch.object(trakt_client.logger, 'error') as log_error:
            self.assertIsNone(trakt_client._make_request(f'{trakt_client.TRAKT_BASE_URL}/shows/tt1'))
            self.assertIsNone(trakt_client._make_request(f'{trakt_client.TRAKT_BASE_URL}/shows/tt2'))
        refresh.assert_not_called()
        http.assert_not_called()
        log_error.assert_not_called()

    def test_not_connected_is_logged_once(self):
        with patch('utilities.settings.get_setting', side_effect=_settings({'client_id': 'abc'})), \
                patch.object(trakt_client.logger, 'info') as log_info:
            for _ in range(3):
                trakt_client._make_request(f'{trakt_client.TRAKT_BASE_URL}/shows/tt1')
        self.assertEqual(log_info.call_count, 1)

    def test_refresh_token_only_still_refreshes(self):
        # A connected account whose access token lapsed keeps the old path.
        with patch('utilities.settings.get_setting', side_effect=_settings({'refresh_token': 'r'})), \
                patch.object(trakt_auth, 'refresh_access_token', return_value=False) as refresh, \
                patch.object(trakt_client._http, 'request') as http:
            self.assertIsNone(trakt_client._make_request(f'{trakt_client.TRAKT_BASE_URL}/shows/tt1'))
        refresh.assert_called()
        http.assert_not_called()


if __name__ == '__main__':
    unittest.main()
