"""Checking-tick hot paths: indexed sibling lookup and the cached mount-root index."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from database import database_reading
from utilities import local_library_scan as lls


class _Conn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *a):
        return self._conn.execute(*a)

    def close(self):
        pass


class MediaItemsByTorrentIdTest(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, state TEXT, "
                     "filled_by_torrent_id TEXT, filled_by_file TEXT)")
        conn.executemany("INSERT INTO media_items VALUES (?,?,?,?)", [
            (1, "Checking", "nzb:abc", "e1.mkv"),
            (2, "Collected", "nzb:abc", "e2.mkv"),
            (3, "Checking", "nzb:other", "x.mkv"),
        ])
        p = mock.patch.object(database_reading, "get_db_connection", return_value=_Conn(conn))
        p.start()
        self.addCleanup(p.stop)

    def test_returns_siblings_in_all_states(self):
        rows = database_reading.get_media_items_by_torrent_id("nzb:abc", columns=["id", "filled_by_file"])
        self.assertEqual(sorted(r["id"] for r in rows), [1, 2])
        self.assertEqual(set(rows[0]), {"id", "filled_by_file"})

    def test_empty_id(self):
        self.assertEqual(database_reading.get_media_items_by_torrent_id(""), [])


class RootFolderIndexTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for name in ("Show (Season 1 & 2)", "Other.Release"):
            os.mkdir(os.path.join(self.root, name))
        lls._root_index_cache.clear()

    def test_maps_match_key_to_real_names(self):
        index = lls._root_folder_index(self.root)
        self.assertEqual(index[lls._folder_match_key("Show (Season 1  2)")], ["Show (Season 1 & 2)"])

    def test_listing_reused_within_ttl_and_refreshed_after(self):
        lls._root_folder_index(self.root)
        with mock.patch.object(lls.os, "listdir", side_effect=AssertionError("relisted")):
            lls._root_folder_index(self.root)
        with mock.patch.object(lls.time, "monotonic", return_value=lls.time.monotonic() + lls._ROOT_INDEX_TTL + 1), \
             mock.patch.object(lls.os, "listdir", return_value=["New"]) as listdir:
            self.assertIn("new", lls._root_folder_index(self.root))
            listdir.assert_called_once()

    def test_missing_root_raises_oserror(self):
        with self.assertRaises(OSError):
            lls._root_folder_index(os.path.join(self.root, "nope"))


if __name__ == "__main__":
    unittest.main()
