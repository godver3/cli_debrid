"""Health check for the built-in mount: a running engine whose server can't
log in is reported, not shown as fine."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401


class BuiltinHealthTests(unittest.TestCase):
    def _check(self, providers):
        from routes.settings_validation_routes import validate_usenet_provider
        from usenet.builtin_client import BuiltinClient
        with mock.patch('utilities.builtin_engine.problems', return_value=[]), \
                mock.patch.object(BuiltinClient, 'check_connectivity', return_value=(True, None)), \
                mock.patch.object(BuiltinClient, '_call', return_value=(200, {'providers': providers})):
            return validate_usenet_provider('builtin', '', wait=0)

    def test_bad_login_is_reported(self):
        ok, msg = self._check([{'name': 'news.x', 'open': 0, 'errors': 0,
                                'last_error': 'nntp: authentication failed: 502 Authentication Failed'}])
        self.assertFalse(ok)
        self.assertIn('news.x: nntp: authentication failed', msg)

    def test_connected_server_is_fine(self):
        self.assertEqual(self._check([{'name': 'news.x', 'open': 2, 'errors': 1, 'last_error': 'old'}]),
                         (True, 'Built-in mount engine is running'))


if __name__ == '__main__':
    unittest.main()
