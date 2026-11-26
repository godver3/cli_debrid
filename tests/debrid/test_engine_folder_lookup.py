"""Checking finds a debrid file in the folder the built-in mount actually
shows its torrent in ("Name (2)"), not only under names worked out from
the release."""

import os
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from utilities import mount_debrid


class EngineFolderTest(unittest.TestCase):
    def test_asks_the_engine_per_provider(self):
        rd, tb = mock.Mock(), mock.Mock()
        client = mock.Mock()
        client._call.side_effect = lambda m, path, timeout: (
            (200, {'folder': 'Pack (2)'}) if path == '/jobs/rd-ABC' else (404, None))
        with mock.patch.object(mount_debrid, 'engine_holds_debrid', return_value=True), \
                mock.patch.object(mount_debrid, 'providers', return_value=[tb, rd]), \
                mock.patch.object(mount_debrid, 'provider_key', side_effect=lambda p: 'rd' if p is rd else 'tb'), \
                mock.patch('usenet.builtin_client.get_builtin_client', return_value=client):
            self.assertEqual(mount_debrid.engine_folder('ABC', None), 'Pack (2)')

    def test_nothing_without_the_built_in_mount(self):
        with mock.patch.object(mount_debrid, 'engine_holds_debrid', return_value=False):
            self.assertIsNone(mount_debrid.engine_folder('ABC', None))


class CheckLocalFileTest(unittest.TestCase):
    def test_file_in_the_suffixed_folder_is_found_and_remembered(self):
        from utilities import local_library_scan as lls
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'Pack (2)'))
            open(os.path.join(root, 'Pack (2)', 'Show.S01E01.mkv'), 'w').close()
            item = {'id': 7, 'filled_by_file': 'Show.S01E01.mkv', 'debrid_folder_name': 'Pack',
                    'filled_by_torrent_id': 'ABC', 'type': 'episode'}
            seen = {}

            def stop_after_find(*a, **k):
                seen['path'] = item.get('debrid_folder_name')
                raise RuntimeError('stop')  # the rest (ffprobe, symlinks) isn't under test

            with mock.patch.object(lls, 'get_setting', side_effect=lambda s, k=None, d=None: root
                                   if k == 'original_files_path' else d), \
                    mock.patch('utilities.mount_debrid.engine_folder', return_value='Pack (2)'), \
                    mock.patch('database.database_writing.update_media_item') as upd, \
                    mock.patch.object(lls.os.path, 'exists', wraps=os.path.exists) as ex:
                try:
                    lls.check_local_file_for_item(item)
                except Exception:
                    pass
            upd.assert_called_with(7, debrid_folder_name='Pack (2)')
            self.assertIn(os.path.join(root, 'Pack (2)', 'Show.S01E01.mkv'), [c.args[0] for c in ex.call_args_list])


if __name__ == '__main__':
    unittest.main()
