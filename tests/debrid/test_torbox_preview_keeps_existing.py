"""Magnet Assign's file-listing preview never removes a torrent that was
already on the TorBox account (it used to delete the user's own copy)."""

import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from debrid.torbox.client import TorboxProvider

MAGNET = 'magnet:?xt=urn:btih:a055aa5aa9ce5e099f1f80d9bd1ccb1cad164871'


class PreviewTest(unittest.TestCase):
    def _list(self, existing):
        p = TorboxProvider.__new__(TorboxProvider)
        info = {'files': [{'path': 'P/a.mkv'}], 'filename': 'P'}
        with mock.patch.object(p, '_find_existing_torrent', return_value=existing), \
                mock.patch.object(p, 'add_torrent', return_value='156516547'), \
                mock.patch.object(p, 'get_torrent_info', return_value=info), \
                mock.patch.object(p, 'remove_torrent') as rm, \
                mock.patch('debrid.torbox.client.time.sleep'):
            out = p.get_torrent_file_list(MAGNET)
        return out, rm

    def test_existing_torrent_is_listed_and_kept(self):
        out, rm = self._list({'id': 156516547})
        self.assertEqual(out[2], '156516547')
        rm.assert_not_called()

    def test_a_new_preview_add_is_still_removed(self):
        _, rm = self._list(None)
        rm.assert_called_once()


if __name__ == '__main__':
    unittest.main()
