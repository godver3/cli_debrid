"""database_writing.remove_duplicate_items — shared by Debug → Remove Duplicates and the AI tool.

The AI tool imported it from database.database_writing, where it didn't exist
(the logic lived inside the Debug route), so the tool always reported "started"
and did nothing. Exclude patterns also crashed: they read filled_by_file from
rows whose SELECT didn't include it.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _conn(rows):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE media_items (id INTEGER PRIMARY KEY, title TEXT, type TEXT, state TEXT, "
        "collected_at TEXT, version TEXT, imdb_id TEXT, tmdb_id TEXT, ghostlisted INTEGER DEFAULT 0, "
        "filled_by_torrent_id TEXT, filled_by_file TEXT)")
    conn.executemany(
        "INSERT INTO media_items (id, title, type, state, collected_at, filled_by_file) VALUES (?, ?, 'movie', ?, ?, ?)",
        rows)
    return conn


def _ids(conn):
    return sorted(r[0] for r in conn.execute("SELECT id FROM media_items"))


class RemoveDuplicateItemsTest(unittest.TestCase):
    def setUp(self):
        from database.database_writing import _remove_duplicate_items
        self.run_it = lambda conn, **kw: _remove_duplicate_items(
            conn, kw.get('dry_run', False), 'all', kw.get('exclude', []), [], lambda p, n: False)

    def test_keeps_the_collected_row(self):
        conn = _conn([(1, 'A', 'Wanted', '2024-01-01', 'a.mkv'),
                      (2, 'A', 'Collected', '2024-02-01', 'a.mkv'),
                      (3, 'B', 'Collected', '2024-01-01', 'b.mkv')])
        result = self.run_it(conn)
        self.assertEqual(result['items_deleted'], 1)
        self.assertEqual(_ids(conn), [2, 3])

    def test_dry_run_deletes_nothing(self):
        conn = _conn([(1, 'A', 'Collected', '2024-01-01', 'a.mkv'),
                      (2, 'A', 'Collected', '2024-02-01', 'a.mkv')])
        result = self.run_it(conn, dry_run=True)
        self.assertEqual((result['total_duplicates'], result['items_deleted']), (1, 0))
        self.assertEqual(_ids(conn), [1, 2])

    def test_exclude_pattern_protects_the_group(self):
        conn = _conn([(1, 'A', 'Collected', '2024-01-01', 'Keep.Me.mkv'),
                      (2, 'A', 'Collected', '2024-02-01', 'Keep.Me.mkv'),
                      (3, 'B', 'Collected', '2024-01-01', 'b.mkv'),
                      (4, 'B', 'Collected', '2024-02-01', 'b.mkv')])
        result = self.run_it(conn, exclude=['keep.me'])
        self.assertEqual(result['items_deleted'], 1)
        self.assertEqual(_ids(conn), [1, 2, 3])

    def test_ai_tool_import_path_exists(self):
        from database.database_writing import remove_duplicate_items
        self.assertTrue(callable(remove_duplicate_items))


if __name__ == '__main__':
    unittest.main()
