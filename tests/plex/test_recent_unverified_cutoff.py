"""database/symlink_verification.py: the "recent" verification scan only sees recent files.

added_at is stored as a datetime string. The cutoff used to be an epoch number,
and SQLite sorts every text value above every number, so "recent" matched all
unverified files ever recorded. A big relink (775 Pokémon episodes) was then
checked against only Plex's 100 newest items, failed, and set off a Plex scan
per file on every run.
"""
import sqlite3
import tempfile
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import database  # noqa: F401  (app import order)
from database import symlink_verification as sv


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE media_items (id INTEGER PRIMARY KEY, title TEXT, episode_title TEXT,
                                  season_number INTEGER, episode_number INTEGER, type TEXT);
        CREATE TABLE symlinked_files_verification (
            id INTEGER PRIMARY KEY, media_item_id INTEGER, filename TEXT, full_path TEXT,
            added_at TIMESTAMP, verified BOOLEAN DEFAULT FALSE, verified_at TIMESTAMP,
            verification_attempts INTEGER DEFAULT 0, last_attempt TIMESTAMP,
            permanently_failed BOOLEAN DEFAULT FALSE, failure_reason TEXT);
    """)
    return conn


class RecentCutoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'm.db')
        self.dir = tmp.name
        conn = _db(self.path)
        now = datetime.now()
        for i, added in enumerate((now - timedelta(days=1), now - timedelta(hours=1)), start=1):
            conn.execute("INSERT INTO media_items (id, title, type) VALUES (?, 'Show', 'episode')", (i,))
            conn.execute("INSERT INTO symlinked_files_verification (media_item_id, filename, full_path, added_at)"
                         " VALUES (?, ?, ?, ?)", (i, f'f{i}.mkv', f'/x/f{i}.mkv', added))
        conn.commit()
        conn.close()
        p = mock.patch.object(sv, 'get_db_connection', side_effect=self._conn)
        p.start()
        self.addCleanup(p.stop)

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_only_files_inside_the_window(self):
        files = sv.get_recent_unverified_files(hours=6, limit=100)
        self.assertEqual([f['filename'] for f in files], ['f2.mkv'])

    def test_wider_window_includes_older_file(self):
        files = sv.get_recent_unverified_files(hours=48, limit=100)
        self.assertEqual(sorted(f['filename'] for f in files), ['f1.mkv', 'f2.mkv'])

    def test_relinking_an_old_file_makes_it_recent_again(self):
        real = os.path.join(self.dir, 'f1.mkv')
        open(real, 'w').close()
        conn = self._conn()
        conn.execute("UPDATE symlinked_files_verification SET full_path = ?, verified = TRUE WHERE id = 1", (real,))
        conn.commit()
        conn.close()
        self.assertTrue(sv.add_symlinked_file_for_verification(1, real))
        names = [f['filename'] for f in sv.get_recent_unverified_files(hours=6, limit=100)]
        self.assertIn('f1.mkv', names)


if __name__ == '__main__':
    unittest.main()
