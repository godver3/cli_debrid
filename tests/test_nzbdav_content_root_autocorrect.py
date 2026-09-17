#!/usr/bin/env python3
"""
Regression test for godver3/cli_debrid#504: the NzbDAV Mount Path help text
told users to point mounted_file_location at the raw rclone-mount root (e.g.
/mnt/nzbdav), but _content_root() used that path unchanged as if it already
contained category subdirectories directly. Following the (now-fixed) help
text meant every folder lookup scanned NzbDAV's own internal top-level
entries (content, nzbs, .ids, completed-symlinks) as if they were release
categories, so nothing ever resolved.

_content_root() now detects that specific misconfiguration - the configured
root contains NzbDAV's own known siblings ('content' + 'completed-symlinks',
per the module's docstring) rather than category folders - and transparently
resolves into the `content` subfolder instead.
"""

import unittest
import sys
import os
import types
import tempfile
import shutil
import importlib.util


def _load_client_module():
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: {}
    sys.modules['utilities.settings'] = uss
    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')
    rta = types.ModuleType('routes.api_tracker')
    rta.api = types.SimpleNamespace(get=None, post=None)
    sys.modules['routes.api_tracker'] = rta
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'usenet', 'nzbdav_client.py')
    spec = importlib.util.spec_from_file_location('nzbdav_client_content_root_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


nc = _load_client_module()


class TestNzbdavContentRootAutocorrect(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _client(self, flat_layout=False):
        c = nc.NzbdavClient.__new__(nc.NzbdavClient)
        c.flat_layout = flat_layout
        c.mount_path = self.root
        return c

    def test_raw_mount_root_redirects_into_content(self):
        # Mimics NzbDAV's own top-level layout: content/, completed-symlinks/,
        # nzbs/, .ids/ - none of these are real release categories.
        for name in ('content', 'completed-symlinks', 'nzbs', '.ids'):
            os.makedirs(os.path.join(self.root, name))

        c = self._client()
        self.assertEqual(c._content_root(), os.path.join(self.root, 'content'))

    def test_correctly_configured_content_dir_is_unchanged(self):
        # Already pointed at the content dir directly - real category folders,
        # no NzbDAV-internal siblings alongside them.
        for name in ('movies', 'shows'):
            os.makedirs(os.path.join(self.root, name))

        c = self._client()
        self.assertEqual(c._content_root(), self.root)

    def test_missing_completed_symlinks_sibling_does_not_redirect(self):
        # Only 'content' present (no 'completed-symlinks') - not enough signal
        # to assume this is the raw mount root; leave it alone.
        os.makedirs(os.path.join(self.root, 'content'))
        os.makedirs(os.path.join(self.root, 'movies'))

        c = self._client()
        self.assertEqual(c._content_root(), self.root)

    def test_zurg_flat_layout_never_redirects(self):
        # Zurg has no content/completed-symlinks split at all - this logic is
        # NzbDAV-specific and must never touch flat-layout mounts.
        for name in ('content', 'completed-symlinks'):
            os.makedirs(os.path.join(self.root, name))

        c = self._client(flat_layout=True)
        self.assertEqual(c._content_root(), self.root)


if __name__ == '__main__':
    unittest.main()
