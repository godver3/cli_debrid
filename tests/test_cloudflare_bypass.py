import os
import unittest
from unittest.mock import Mock, mock_open, patch

from utilities.cloudflare_bypass import (
    _browser_environment,
    _clean_stale_x11_sockets,
    _lock_file_pid_alive,
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


class TestLockFilePidAlive(unittest.TestCase):
    """A running Xvfb's /tmp/.X<N>-lock file holds its PID. Xvfb dying hard
    (OOM-killed, kill -9, crash) commonly leaves the lock file behind without
    the PID it names still running - the pre-existing bug this fix closes."""

    def test_own_pid_is_alive(self):
        # Our own process is guaranteed to exist for the duration of the test.
        with patch('builtins.open', mock_open(read_data=f'  {os.getpid()}\n')):
            self.assertTrue(_lock_file_pid_alive('/tmp/.X99-lock'))

    def test_nonexistent_pid_is_not_alive(self):
        # PID 2**22 is far beyond any real process table on Linux (max ~4M,
        # and never anywhere close to that in a container).
        with patch('builtins.open', mock_open(read_data='4194304\n')):
            self.assertFalse(_lock_file_pid_alive('/tmp/.X99-lock'))

    def test_malformed_content_defaults_to_alive(self):
        """Regression check: can't confirm dead -> don't touch it, same
        conservative default the pre-existing code already used for the
        "lock file exists" case in general."""
        with patch('builtins.open', mock_open(read_data='not-a-pid\n')):
            self.assertTrue(_lock_file_pid_alive('/tmp/.X99-lock'))

    def test_unreadable_lock_file_defaults_to_alive(self):
        with patch('builtins.open', side_effect=OSError('permission denied')):
            self.assertTrue(_lock_file_pid_alive('/tmp/.X99-lock'))


class TestCleanStaleX11Sockets(unittest.TestCase):
    """Full sweep behavior, filesystem calls mocked so nothing here touches
    the real /tmp/.X11-unix (which may have a genuinely live X server on the
    machine running these tests)."""

    def _run(self, listdir, lock_exists, pid_alive=None):
        with patch('utilities.cloudflare_bypass.os.path.isdir', return_value=True), \
             patch('utilities.cloudflare_bypass.os.listdir', return_value=listdir), \
             patch('utilities.cloudflare_bypass.os.path.exists', return_value=lock_exists), \
             patch('utilities.cloudflare_bypass.os.remove') as mock_remove, \
             patch('utilities.cloudflare_bypass._lock_file_pid_alive', return_value=pid_alive):
            _clean_stale_x11_sockets()
            return mock_remove

    def test_orphan_socket_no_lock_file_is_removed(self):
        """Regression check: the original (pre-this-fix) behavior - a socket
        with no lock file at all must still be removed exactly as before."""
        mock_remove = self._run(listdir=['X99'], lock_exists=False)
        mock_remove.assert_called_once_with('/tmp/.X11-unix/X99')

    def test_lock_file_with_live_pid_is_left_alone(self):
        """A genuinely running Xvfb must never be touched."""
        mock_remove = self._run(listdir=['X99'], lock_exists=True, pid_alive=True)
        mock_remove.assert_not_called()

    def test_lock_file_with_dead_pid_removes_both_lock_and_socket(self):
        """The actual bug fix: a lock file whose recorded PID is gone must
        no longer block cleanup - both the lock file and the socket get
        removed instead of colliding with every future launch forever."""
        mock_remove = self._run(listdir=['X99'], lock_exists=True, pid_alive=False)
        removed_paths = {call.args[0] for call in mock_remove.call_args_list}
        self.assertEqual(removed_paths, {'/tmp/.X99-lock', '/tmp/.X11-unix/X99'})

    def test_non_display_entries_are_ignored(self):
        mock_remove = self._run(listdir=['not-a-display', 'X'], lock_exists=False)
        mock_remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()
