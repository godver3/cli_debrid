"""Database page Source column: where an item's file came from."""
import sqlite3
import unittest

import database  # noqa: F401  (app import order)
from routes.database_routes import item_source_label, source_filter_clause, source_sql

COLS = ['id', 'filled_by_torrent_id', 'debrid_provider']
ROWS = [(1, 'nzb:a', None), (2, 'RD1', 'Real-Debrid'), (3, 'TB1', 'Torbox'), (4, 'X', None), (5, None, None), (6, '', None)]


class SourceLabelTests(unittest.TestCase):
    def test_usenet(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': 'nzb:abc'}), 'Usenet')

    def test_debrid_with_provider(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': 'X1', 'debrid_provider': 'Torbox'}), 'Debrid (Torbox)')

    def test_debrid_without_provider(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': 'X1'}), 'Debrid')

    def test_no_file(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': None}), '')


class SourceFilterTests(unittest.TestCase):
    """The SQL filter agrees with item_source_label for every operator."""
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE media_items (id INTEGER, filled_by_torrent_id TEXT, debrid_provider TEXT)')
        self.conn.executemany('INSERT INTO media_items VALUES (?,?,?)', ROWS)

    def ids(self, operator, value):
        clause, params = source_filter_clause(COLS, operator, value)
        return [r[0] for r in self.conn.execute(f'SELECT id FROM media_items WHERE {clause} ORDER BY id', params)]

    def test_sql_matches_python_label(self):
        labels = [r[0] for r in self.conn.execute(f'SELECT {source_sql(COLS)} FROM media_items ORDER BY id')]
        expected = [item_source_label({'filled_by_torrent_id': t, 'debrid_provider': p}) for _, t, p in ROWS]
        self.assertEqual(labels, expected)

    def test_equals(self):
        self.assertEqual(self.ids('equals', 'Usenet'), [1])
        self.assertEqual(self.ids('equals', 'Debrid'), [2, 3, 4])  # any provider
        self.assertEqual(self.ids('equals', 'Debrid (Torbox)'), [3])
        self.assertEqual(self.ids('equals', 'None'), [5, 6])

    def test_not_equals_and_contains(self):
        self.assertEqual(self.ids('not_equals', 'Debrid'), [1, 5, 6])
        self.assertEqual(self.ids('contains', 'real'), [2])
        self.assertEqual(self.ids('not_contains', 'Debrid'), [1, 5, 6])

    def test_empty(self):
        self.assertEqual(self.ids('is_null', ''), [5, 6])
        self.assertEqual(self.ids('is_not_null', ''), [1, 2, 3, 4])

    def test_without_provider_column(self):
        clause, params = source_filter_clause(['id', 'filled_by_torrent_id'], 'equals', 'Debrid')
        self.conn.execute('CREATE TABLE t2 AS SELECT id, filled_by_torrent_id FROM media_items')
        rows = [r[0] for r in self.conn.execute(f'SELECT id FROM t2 WHERE {clause} ORDER BY id', params)]
        self.assertEqual(rows, [2, 3, 4])


if __name__ == '__main__':
    unittest.main()
