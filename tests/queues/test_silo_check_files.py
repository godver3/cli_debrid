"""PlexTasks._silo_check_files: Plex mode run with Silo (Checking -> Collected)."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401  (app import order)
from queues.runner import plex_tasks as pt


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class SiloCheckFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mount = os.path.join(self.tmp, 'mount')
        os.makedirs(os.path.join(self.mount, 'Some.Movie.2020'))
        self.file = os.path.join(self.mount, 'Some.Movie.2020', 'Some.Movie.2020.mkv')
        with open(self.file, 'wb') as f:
            f.write(b'x')
        self.dbpath = os.path.join(self.tmp, 'media.db')
        conn = _db(self.dbpath)
        conn.execute('CREATE TABLE media_items (id INTEGER PRIMARY KEY, state TEXT, collected_at TEXT, '
                     'location_on_disk TEXT, ghostlisted INTEGER)')
        conn.execute('INSERT INTO media_items (id, state) VALUES (1, "Checking")')
        conn.commit()
        conn.close()
        self.item = {'id': 1, 'type': 'movie', 'filled_by_title': 'Some.Movie.2020',
                     'filled_by_file': 'Some.Movie.2020.mkv', 'debrid_folder_name': 'Some.Movie.2020',
                     'original_scraped_torrent_title': '', 'real_debrid_original_title': ''}
        self.runner = pt.PlexTasks.__new__(pt.PlexTasks)
        self.runner.plex_scan_tick_counts = {}
        self.key = 'Some.Movie.2020:Some.Movie.2020.mkv'
        patches = [
            mock.patch.object(pt, 'get_db_connection', lambda: _db(self.dbpath)),
            mock.patch.object(pt, 'get_setting', lambda s, k, default=None: default),
            mock.patch.object(pt, 'plex_mode_scan_due', lambda folder: True),
            mock.patch.object(pt, 'claim_plex_mode_scan', lambda folder: True),
            mock.patch.object(pt, 'get_media_item_by_id', lambda i: {'id': i, 'state': 'Collected'}),
            mock.patch.object(pt, 'handle_state_change', lambda item: None),
            mock.patch('database.database_writing.add_to_collected_notifications', lambda item: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.scans = []
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))

    def _state(self):
        conn = _db(self.dbpath)
        try:
            return conn.execute('SELECT state, location_on_disk FROM media_items WHERE id = 1').fetchone()
        finally:
            conn.close()

    def _run(self, collect):
        with mock.patch('utilities.silo_functions.scan_path', lambda p, t='': self.scans.append(p) or True), \
             mock.patch('utilities.silo_functions.collect_from_silo', collect):
            self.runner._silo_check_files([dict(self.item)], self.mount, lambda item, path, key: True)

    def test_found_file_scans_its_folder(self):
        self._run(lambda items: (0, 0))
        self.assertEqual(self.scans, [os.path.dirname(self.file)])
        self.assertEqual(self.runner.plex_scan_tick_counts[self.key], 1)
        self.assertEqual(self._state()['state'], 'Checking')

    def test_collected_after_ticks_with_mount_path(self):
        for _ in range(pt.PlexTasks.SILO_COLLECT_AFTER_TICKS):
            self._run(lambda items: (0, 0))
            self.assertEqual(self._state()['state'], 'Checking')
        self._run(lambda items: (0, 0))
        row = self._state()
        self.assertEqual(row['state'], 'Collected')
        self.assertEqual(row['location_on_disk'], self.file)
        self.assertNotIn(self.key, self.runner.plex_scan_tick_counts)

    def test_collected_by_silo_drops_tick_count(self):
        self._run(lambda items: (0, 0))
        self.assertEqual(self.runner.plex_scan_tick_counts[self.key], 1)

        def silo_collects(items):
            conn = _db(self.dbpath)
            conn.execute('UPDATE media_items SET state = "Collected" WHERE id = 1')
            conn.commit()
            conn.close()
            return 1, 1
        self._run(silo_collects)
        self.assertNotIn(self.key, self.runner.plex_scan_tick_counts)

    def test_ffprobe_reject_skips(self):
        with mock.patch('utilities.silo_functions.scan_path', lambda p, t='': self.scans.append(p) or True), \
             mock.patch('utilities.silo_functions.collect_from_silo', lambda items: (0, 0)):
            self.runner._silo_check_files([dict(self.item)], self.mount, lambda item, path, key: False)
        self.assertEqual(self.scans, [])
        self.assertEqual(self.runner.plex_scan_tick_counts, {})

    def test_missing_file_does_nothing(self):
        os.remove(self.file)
        self._run(lambda items: (0, 0))
        self.assertEqual(self.scans, [])
        self.assertEqual(self._state()['state'], 'Checking')


if __name__ == '__main__':
    unittest.main()
