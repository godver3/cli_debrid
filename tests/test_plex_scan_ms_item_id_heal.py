#!/usr/bin/env python3
"""Regression test: the already-collected update path in
database/collected_items.py must heal an empty-string ms_item_id the same
way it already heals empty-string imdb_id/tmdb_id in the same query.

SQLite's COALESCE treats '' as a non-NULL value, so
`ms_item_id = COALESCE(ms_item_id, ?)` never replaces an existing empty
string even though the should_update check above it (which treats a falsy
ms_item_id, including '', as "needs update") decided an update was needed.
The sibling imdb_id/tmdb_id assignments in the same UPDATE already use
`COALESCE(NULLIF(x, ''), ?)` -- ms_item_id was just missed.
"""

import os
import sqlite3
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UPDATE_QUERY = """
    UPDATE media_items
    SET ms_item_id = COALESCE(NULLIF(ms_item_id, ''), ?)
    WHERE id = ?
"""


def _make_conn():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        """
        CREATE TABLE media_items (
            id INTEGER PRIMARY KEY,
            ms_item_id TEXT
        )
        """
    )
    return conn


class TestMsItemIdHeal(unittest.TestCase):
    def test_empty_string_is_healed(self):
        conn = _make_conn()
        conn.execute("INSERT INTO media_items (id, ms_item_id) VALUES (1, '')")
        conn.execute(UPDATE_QUERY, ('plex-rating-key-123', 1))
        row = conn.execute("SELECT ms_item_id FROM media_items WHERE id = 1").fetchone()
        self.assertEqual(row[0], 'plex-rating-key-123')

    def test_null_is_healed(self):
        conn = _make_conn()
        conn.execute("INSERT INTO media_items (id, ms_item_id) VALUES (1, NULL)")
        conn.execute(UPDATE_QUERY, ('plex-rating-key-123', 1))
        row = conn.execute("SELECT ms_item_id FROM media_items WHERE id = 1").fetchone()
        self.assertEqual(row[0], 'plex-rating-key-123')

    def test_existing_value_is_preserved(self):
        conn = _make_conn()
        conn.execute("INSERT INTO media_items (id, ms_item_id) VALUES (1, 'already-linked')")
        conn.execute(UPDATE_QUERY, ('plex-rating-key-123', 1))
        row = conn.execute("SELECT ms_item_id FROM media_items WHERE id = 1").fetchone()
        self.assertEqual(row[0], 'already-linked')

    def test_source_uses_healing_coalesce(self):
        # Guard against the fix regressing back to the plain COALESCE form.
        path = os.path.join(PROJECT_ROOT, "database", "collected_items.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("ms_item_id = COALESCE(NULLIF(ms_item_id, ''), ?)", source)
        self.assertNotIn("ms_item_id = COALESCE(ms_item_id, ?)", source)


if __name__ == "__main__":
    unittest.main()
