"""Regressions from the PR #585 Windows test run (2026-11-20)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from utilities import builtin_engine as be
from utilities import paths


class SafeRealpathTests(unittest.TestCase):
    def test_follows_links_by_hand_when_realpath_raises(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, 'file.mkv')
            open(target, 'w').close()
            link = os.path.join(d, 'link.mkv')
            os.symlink(target, link)
            err = OSError(1005, 'The volume does not contain a recognized file system')
            with mock.patch('os.path.realpath', side_effect=err):
                self.assertEqual(paths.safe_realpath(link), os.path.normpath(target))

    def test_plain_realpath_when_it_works(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(paths.safe_realpath(d), os.path.realpath(d))


class MediaFileAtTests(unittest.TestCase):
    def test_folder_resolves_to_largest_video_never_itself(self):
        with tempfile.TemporaryDirectory() as d:
            job = os.path.join(d, 'Show.S01E01.1080p-GRP')
            os.makedirs(job)
            for name, size in (('show.s01e01.mkv', 300), ('sample.mkv', 900), ('info.nfo', 5000)):
                with open(os.path.join(job, name), 'wb') as f:
                    f.write(b'x' * size)
            self.assertEqual(paths.media_file_at(job), os.path.join(job, 'show.s01e01.mkv'))

    def test_folder_without_video_and_missing_path(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(paths.media_file_at(d))
            self.assertIsNone(paths.media_file_at(os.path.join(d, 'nope')))

    def test_probe_does_not_call_a_folder_empty(self):
        from usenet import repair_engine
        with tempfile.TemporaryDirectory() as d:
            # A folder with no video: inconclusive, so it passes instead of
            # "0 bytes on the mount" rejecting the release.
            self.assertTrue(repair_engine.probe_file_playable(d))


class WindowsPathTests(unittest.TestCase):
    def test_all_path_and_cache_dir_on_windows(self):
        with mock.patch.object(be, 'IS_WINDOWS', True), \
                mock.patch.dict(os.environ, {'USER_DB_CONTENT': os.path.join('data', 'cli_debrid', 'db_content')}):
            self.assertEqual(be.all_path({}), 'M:\\__all__')
            self.assertEqual(be.cache_dir({}), os.path.join('data', 'cli_debrid', 'cache', 'engine'))
            self.assertEqual(be.cache_dir({'cache_dir': '/user/cache/engine'}),
                             os.path.join('data', 'cli_debrid', 'cache', 'engine'))
            self.assertEqual(be.cache_dir({'cache_dir': r'D:\cache'}), r'D:\cache')

    def test_linux_paths_unchanged(self):
        with mock.patch.object(be, 'IS_WINDOWS', False):
            self.assertEqual(be.all_path({'mount_path': '/mnt/clid/'}), '/mnt/clid/__all__')
            self.assertEqual(be.cache_dir({}), '/user/cache/engine')

    def test_write_config_without_fchmod(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'engine.json')
            saved = getattr(os, 'fchmod', None)
            if saved is not None:
                del os.fchmod  # as on Windows
            try:
                self.assertTrue(be.write_config({'a': 'é'}, path))
            finally:
                if saved is not None:
                    os.fchmod = saved
            with open(path, encoding='utf-8') as f:
                self.assertEqual(json.load(f), {'a': 'é'})


class UnreadableConfigTests(unittest.TestCase):
    def test_unreadable_config_is_kept_aside_not_deleted(self):
        from utilities import settings
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {'USER_CONFIG': d}):
            cfg = os.path.join(d, 'config.json')
            with open(cfg, 'w') as f:
                f.write('{not json')
            with mock.patch.object(settings, 'load_config', side_effect=[{}, {'x': 1}, {'x': 1}]), \
                    mock.patch.object(settings, 'save_config'):
                try:
                    settings.ensure_settings_file()
                except Exception:
                    pass
            kept = [n for n in os.listdir(d) if n.startswith('config.json.unreadable-')]
            self.assertEqual(len(kept), 1)
            with open(os.path.join(d, kept[0])) as f:
                self.assertEqual(f.read(), '{not json')


if __name__ == '__main__':
    unittest.main()
