import os
import unittest
from unittest.mock import Mock, patch

from utilities.cloudflare_bypass import (
    _browser_environment,
    _read_page_title,
    _wait_for_challenge_resolution,
)


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


class TestPageTitleNavigation(unittest.TestCase):
    def test_returns_lowercase_title(self):
        page = Mock()
        page.title.return_value = 'Just a moment...'

        self.assertEqual(_read_page_title(page), 'just a moment...')

    def test_navigation_context_error_is_retryable(self):
        page = Mock()
        page.title.side_effect = RuntimeError(
            'Page.title: Execution context was destroyed, most likely because of a navigation.'
        )

        self.assertIsNone(_read_page_title(page))

    def test_unrelated_browser_error_is_not_hidden(self):
        page = Mock()
        page.title.side_effect = RuntimeError('Target page has been closed')

        with self.assertRaisesRegex(RuntimeError, 'Target page has been closed'):
            _read_page_title(page)

    @patch('utilities.cloudflare_bypass.time.sleep')
    @patch('utilities.cloudflare_bypass.time.monotonic', side_effect=[100, 100, 101])
    def test_navigation_during_poll_is_retried(self, _monotonic, sleep):
        page = Mock()
        page.title.side_effect = [
            RuntimeError(
                'Page.title: Execution context was destroyed, most likely because of a navigation.'
            ),
            'FlixPatrol Top 10',
        ]

        self.assertTrue(_wait_for_challenge_resolution(page, timeout_seconds=30))
        self.assertEqual(page.title.call_count, 2)
        sleep.assert_called_once_with(0.25)


if __name__ == '__main__':
    unittest.main()
