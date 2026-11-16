import json
import os
import tempfile
import unittest
from unittest import mock

from utilities import trakt_oauth
from utilities.trakt_auth_cleanup import trakt_is_configured


def _validation_routes():
    import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
    from routes import settings_validation_routes
    return settings_validation_routes


def _response(status_code, payload=None):
    response = mock.Mock(status_code=status_code, text=json.dumps(payload or {}))
    response.json.return_value = payload or {}
    return response


class TestTraktOAuthBodies(unittest.TestCase):
    """Trakt PKCE apps have no client secret, and Trakt's schema accepts
    client_secret only as an optional string, so it must be left out."""

    def _sent_body(self, call):
        with mock.patch.object(trakt_oauth.requests, 'post', return_value=_response(200)) as post:
            call()
        self.assertTrue(post.call_args.args[0].startswith('https://auth.trakt.tv/oauth/'))
        return post.call_args.kwargs['json']

    def test_device_token_omits_missing_secret(self):
        for secret in (None, '', '   '):
            body = self._sent_body(lambda: trakt_oauth.poll_device_token('dev', 'cid', secret))
            self.assertEqual(body, {'client_id': 'cid', 'code': 'dev'})

    def test_device_token_sends_secret_when_set(self):
        body = self._sent_body(lambda: trakt_oauth.poll_device_token('dev', 'cid', 'sec'))
        self.assertEqual(body['client_secret'], 'sec')

    def test_refresh_omits_missing_secret(self):
        body = self._sent_body(lambda: trakt_oauth.refresh_token('ref', 'cid', None))
        self.assertEqual(body, {'client_id': 'cid', 'refresh_token': 'ref',
                                'grant_type': 'refresh_token'})

    def test_device_code_sends_only_client_id(self):
        body = self._sent_body(lambda: trakt_oauth.request_device_code('cid'))
        self.assertEqual(body, {'client_id': 'cid'})


class TestSecretOptional(unittest.TestCase):
    def test_client_id_alone_counts_as_configured(self):
        self.assertTrue(trakt_is_configured({'Trakt': {'client_id': 'cid', 'client_secret': ''}}))

    def test_validation_accepts_missing_secret(self):
        routes = _validation_routes()
        payload = {'device_code': 'd', 'user_code': 'u', 'verification_url': 'v'}
        with mock.patch.object(routes, 'request_device_code', return_value=_response(200, payload)):
            ok, message = routes.validate_trakt_credentials('c' * 64, '')
        self.assertTrue(ok, message)

    def test_validation_reports_unknown_client_id(self):
        routes = _validation_routes()
        with mock.patch.object(routes, 'request_device_code', return_value=_response(401)):
            ok, message = routes.validate_trakt_credentials('c' * 64, '')
        self.assertFalse(ok)
        self.assertIn('Client ID', message)


class TestMainTokenRefresh(unittest.TestCase):
    def test_refresh_updates_both_token_stores(self):
        from content_checkers import trakt as trakt_module

        token = {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 86400}
        settings = {}
        with tempfile.TemporaryDirectory() as config_dir:
            config_file = os.path.join(config_dir, '.pytrakt.json')
            with mock.patch.object(trakt_module, 'TRAKT_CONFIG_FILE', config_file), \
                 mock.patch.object(trakt_oauth, 'refresh_token',
                                   return_value=_response(200, token)) as refresh, \
                 mock.patch('utilities.settings.set_setting',
                            side_effect=lambda section, key, value: settings.__setitem__(key, value)):
                trakt_module._refresh_main_token({'OAUTH_REFRESH': 'old-refresh'}, 'cid', '')

            refresh.assert_called_once_with('old-refresh', 'cid', '')
            with open(config_file) as f:
                saved = json.load(f)
        self.assertEqual(saved['OAUTH_TOKEN'], 'new-access')
        self.assertEqual(saved['OAUTH_REFRESH'], 'new-refresh')
        # The Metadata Battery's copy must get the new single-use refresh token too.
        self.assertEqual(settings['refresh_token'], 'new-refresh')
        self.assertEqual(settings['access_token'], 'new-access')

    def test_failed_refresh_raises_with_trakt_error(self):
        from content_checkers import trakt as trakt_module

        error = {'error': 'invalid_grant', 'error_description': 'refresh_token is invalid'}
        with mock.patch.object(trakt_oauth, 'refresh_token', return_value=_response(400, error)):
            with self.assertRaisesRegex(Exception, 'invalid_grant'):
                trakt_module._refresh_main_token({'OAUTH_REFRESH': 'old'}, 'cid', None)


if __name__ == '__main__':
    unittest.main()
