"""Jellyfin API calls use the standard Authorization header.

Newer Jellyfin servers turn the legacy X-Emby-Token header off by default and
answer 401; X-Emby-Token is kept alongside for Emby.
"""
import unittest

from utilities.jellyfin_auth import jellyfin_auth_headers


class JellyfinAuthHeaderTests(unittest.TestCase):
    def test_authorization_header(self):
        headers = jellyfin_auth_headers('abc123')
        self.assertEqual(headers['Authorization'], 'MediaBrowser Token="abc123"')
        self.assertEqual(headers['X-Emby-Token'], 'abc123')

    def test_token_is_trimmed_and_cannot_break_quoting(self):
        headers = jellyfin_auth_headers(' ab"c ')
        self.assertEqual(headers['Authorization'], 'MediaBrowser Token="abc"')

    def test_empty_token(self):
        self.assertEqual(jellyfin_auth_headers(None)['Authorization'], 'MediaBrowser Token=""')

    def test_overlay_client_session_sends_authorization(self):
        from overlays.jellyfin_client import JellyfinClient
        client = JellyfinClient('http://jellyfin:8096/', 'abc123')
        self.assertEqual(client.session.headers['Authorization'], 'MediaBrowser Token="abc123"')


if __name__ == '__main__':
    unittest.main()
