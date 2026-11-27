"""Magnet Assign's file-listing preview never removes a torrent that was
already on the TorBox account (it used to delete the user's own copy)."""

import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from debrid.torbox.client import TorboxProvider

MAGNET = 'magnet:?xt=urn:btih:a055aa5aa9ce5e099f1f80d9bd1ccb1cad164871'
TORRENT_URL = 'https://indexer.example/download/12345.torrent'


class PreviewTest(unittest.TestCase):
    def _list(self, existing=None, *, link=MAGNET, find_side_effect=None,
              all_torrents=None, all_torrents_side_effect=None, added_id='156516547'):
        p = TorboxProvider.__new__(TorboxProvider)
        p._api_key = 'test-key'
        info = {'files': [{'path': 'P/a.mkv'}], 'filename': 'P'}
        find_kwargs = {'side_effect': find_side_effect} if find_side_effect is not None else {'return_value': existing}
        all_kwargs = (
            {'side_effect': all_torrents_side_effect}
            if all_torrents_side_effect is not None
            else {'return_value': all_torrents if all_torrents is not None else []}
        )
        with mock.patch.object(p, '_find_existing_torrent', **find_kwargs) as find_mock, \
                mock.patch('debrid.torbox.client.get_all_torrents', **all_kwargs), \
                mock.patch.object(p, 'add_torrent', return_value=added_id), \
                mock.patch.object(p, 'get_torrent_info', return_value=info), \
                mock.patch.object(p, 'remove_torrent') as rm, \
                mock.patch('debrid.torbox.client.time.sleep'):
            out = p.get_torrent_file_list(link)
        return out, rm, find_mock

    def test_existing_torrent_is_listed_and_kept(self):
        out, rm, _ = self._list({'id': 156516547})
        self.assertEqual(out[2], '156516547')
        rm.assert_not_called()

    def test_a_new_preview_add_is_still_removed(self):
        _, rm, _ = self._list(None)
        rm.assert_called_once()

    def test_lookup_raises_retries_once_and_keeps_when_still_unknown(self):
        out, rm, find_mock = self._list(find_side_effect=RuntimeError("TorBox 502"))
        self.assertEqual(out[2], '156516547')
        self.assertEqual(find_mock.call_count, 2)
        rm.assert_not_called()

    def test_lookup_retry_succeeds_and_removes_known_new(self):
        _, rm, find_mock = self._list(find_side_effect=[RuntimeError("transient"), None])
        self.assertEqual(find_mock.call_count, 2)
        rm.assert_called_once()

    def test_torrent_url_of_existing_torrent_is_kept(self):
        out, rm, _ = self._list(link=TORRENT_URL, all_torrents=[{'id': 156516547}], added_id='156516547')
        self.assertEqual(out[2], '156516547')
        rm.assert_not_called()

    def test_torrent_url_of_new_torrent_is_removed(self):
        _, rm, _ = self._list(link=TORRENT_URL, all_torrents=[{'id': 111111}], added_id='156516547')
        rm.assert_called_once()

    def test_torrent_url_keeps_when_pre_listing_fails(self):
        out, rm, _ = self._list(link=TORRENT_URL, all_torrents_side_effect=RuntimeError("timeout"))
        self.assertEqual(out[2], '156516547')
        rm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
