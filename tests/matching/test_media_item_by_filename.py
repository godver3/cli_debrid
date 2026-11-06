"""get_media_item_by_filename: indexed exact match first, suffix LIKE fallback.

NZB rows never get location_basename (the triggers skip them), so the
location_on_disk fallback must keep finding them.
"""
import sqlite3
import unittest
from unittest import mock

from database import database_reading


class _Conn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *a):
        return self._conn.execute(*a)

    def close(self):
        pass


class GetMediaItemByFilenameTest(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, type TEXT, title TEXT, "
                     "filled_by_file TEXT, location_on_disk TEXT, location_basename TEXT)")
        conn.executemany("INSERT INTO media_items VALUES (?,?,?,?,?,?)", [
            (1, "movie", "A", "a.mkv", "/lib/a.mkv", "a.mkv"),
            (2, "movie", "B", None, "/lib/B/b.mkv", "b.mkv"),
            (3, "movie", "NZB", None, "/mnt/nzb/c.mkv", None),
        ])
        p = mock.patch.object(database_reading, "get_db_connection", return_value=_Conn(conn))
        p.start()
        self.addCleanup(p.stop)

    def test_exact_filled_by_file(self):
        self.assertEqual(database_reading.get_media_item_by_filename("a.mkv")["id"], 1)

    def test_exact_location_basename(self):
        self.assertEqual(database_reading.get_media_item_by_filename("b.mkv")["id"], 2)

    def test_fallback_location_on_disk_suffix(self):
        self.assertEqual(database_reading.get_media_item_by_filename("c.mkv")["id"], 3)

    def test_no_match(self):
        self.assertIsNone(database_reading.get_media_item_by_filename("zzz.mkv"))


if __name__ == "__main__":
    unittest.main()
