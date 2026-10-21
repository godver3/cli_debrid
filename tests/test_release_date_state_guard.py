"""update_release_date_and_state(expected_state=...) must not clobber a newer state.

The release-date refresh snapshots its items at the start of a long run. An
item that moved Scraping -> Checking meanwhile was written back to Wanted from
the stale snapshot and went through Scraping/Adding again.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from database import database_writing


class ExpectedStateGuardTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.remove, self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE media_items (id INTEGER PRIMARY KEY, release_date TEXT, state TEXT, '
                     'last_updated TEXT, airtime TEXT, runtime INTEGER)')
        conn.execute("INSERT INTO media_items VALUES (1, '2018-09-12', 'Checking', NULL, '22:00', 60)")
        conn.commit()
        conn.close()

        def _connect():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        patcher = mock.patch.object(database_writing, 'get_db_connection', _connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _row(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT release_date, state FROM media_items WHERE id = 1').fetchone()
        finally:
            conn.close()

    def test_state_kept_when_item_moved_on(self):
        database_writing.update_release_date_and_state(1, '2018-09-19', 'Wanted', expected_state='Scraping')
        self.assertEqual(self._row(), ('2018-09-19', 'Checking'))

    def test_state_written_when_unchanged(self):
        database_writing.update_release_date_and_state(1, '2018-09-19', 'Wanted', expected_state='Checking')
        self.assertEqual(self._row(), ('2018-09-19', 'Wanted'))

    def test_no_guard_by_default(self):
        database_writing.update_release_date_and_state(1, '2018-09-19', 'Wanted')
        self.assertEqual(self._row(), ('2018-09-19', 'Wanted'))


if __name__ == '__main__':
    unittest.main()
