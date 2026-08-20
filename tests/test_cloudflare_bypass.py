import os
import unittest
from unittest.mock import patch

from utilities.cloudflare_bypass import _browser_environment


class TestBrowserEnvironment(unittest.TestCase):
    def test_uses_writable_profile_for_home_and_xdg_paths(self):
        profile = '/tmp/cf-profile'

        with patch.dict(os.environ, {'DISPLAY': ':7', 'HOME': '/root'}, clear=True):
            browser_env = _browser_environment(profile)

        self.assertEqual(browser_env['HOME'], profile)
        self.assertEqual(browser_env['XDG_CONFIG_HOME'], profile + '/.config')
        self.assertEqual(browser_env['XDG_CACHE_HOME'], profile + '/.cache')
        self.assertEqual(browser_env['DISPLAY'], ':7')

    def test_does_not_mutate_process_environment(self):
        with patch.dict(os.environ, {'HOME': '/root'}, clear=True):
            _browser_environment('/tmp/cf-profile')
            self.assertEqual(os.environ['HOME'], '/root')
            self.assertNotIn('XDG_CONFIG_HOME', os.environ)


if __name__ == '__main__':
    unittest.main()
