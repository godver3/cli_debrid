#!/usr/bin/env python3
"""
Regression test for utilities/post_processing.py::replace_cleanup_after_collect().

Reported: using "Replace" on the library page (mark_movie_replace, sets
manual_replace=1 on the old file's DB row) left the old file behind both on
the mount and as a dangling symlink after the replacement was collected.

Root cause: the old implementation only ever called the debrid provider's
remove_torrent() (a no-op 404 for an NZB job id - that has to go through the
usenet provider instead) and asked Plex to remove the entry, with no direct
filesystem unlink at all. In Symlinked/Local mode, whenever Plex removal
didn't apply (no Plex configured, Jellyfin-only setup) or silently failed,
neither the symlink nor the real mount-side file were ever removed.

Fix: delegate per-row cleanup to DeletionManager.delete_single_item(), the
same fully-audited deletion path used by the library page's own Delete
button - it already handles NZB-vs-debrid removal via the provider
factories and directly unlinks the symlink + original file in symlink mode.

This test stubs the heavy `database`/`debrid`/`utilities.deletion_manager`
imports (all lazy, inside the function body) and verifies:
  - the correct stale manual_replace=1 row(s) are identified and passed to
    DeletionManager.delete_single_item with delete_files/delete_symlinks
    both True (not just a Plex-only removal),
  - a movie item with no manual_replace siblings is a no-op,
  - the current item is never deleted.
"""

import unittest
import sys
import os
import sqlite3
import types
import importlib.util
from unittest.mock import MagicMock


def _load_post_processing():
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    us = types.ModuleType('utilities.settings')
    us.get_setting = lambda *a, **k: None
    sys.modules['utilities.settings'] = us

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'utilities', 'post_processing.py')
    spec = importlib.util.spec_from_file_location('post_processing_replace_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pp = _load_post_processing()


class TestReplaceCleanupAfterCollect(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('''
            CREATE TABLE media_items (
                id INTEGER PRIMARY KEY,
                imdb_id TEXT, type TEXT, state TEXT, version TEXT,
                season_number INTEGER, episode_number INTEGER,
                manual_replace INTEGER DEFAULT 0,
                title TEXT, episode_title TEXT,
                location_on_disk TEXT, original_path_for_symlink TEXT,
                filled_by_torrent_id TEXT, replaced_files TEXT
            )
        ''')

        class _UnclosableConn:
            # The code under test closes its connection; keep the in-memory DB readable.
            def __init__(_self, conn):
                _self._conn = conn

            def close(_self):
                pass

            def __getattr__(_self, name):
                return getattr(_self._conn, name)

        fake_db_module = types.ModuleType('database')
        fake_db_module.get_db_connection = lambda: _UnclosableConn(self.conn)
        sys.modules['database'] = fake_db_module

        fake_debrid_module = types.ModuleType('debrid')
        fake_debrid_module.get_debrid_provider = lambda: MagicMock()
        sys.modules['debrid'] = fake_debrid_module

        self.delete_calls = []

        class _FakeDeletionManager:
            def __init__(self, debrid_provider=None):
                pass

            def delete_single_item(_self, item_id, **kwargs):
                self.delete_calls.append((item_id, kwargs))
                return {'success': True, 'item_id': item_id, 'errors': []}

        fake_dm_module = types.ModuleType('utilities.deletion_manager')
        fake_dm_module.DeletionManager = _FakeDeletionManager
        sys.modules['utilities.deletion_manager'] = fake_dm_module

        self.deferred_calls = []
        fake_plex_module = types.ModuleType('utilities.plex_functions')
        fake_plex_module.remove_replaced_plex_media_after_scan = lambda *a: self.deferred_calls.append(a)
        sys.modules['utilities.plex_functions'] = fake_plex_module
        self._orig_get_setting = pp.get_setting

    def tearDown(self):
        self.conn.close()
        pp.get_setting = self._orig_get_setting
        for name in ('database', 'debrid', 'utilities.deletion_manager', 'utilities.plex_functions'):
            sys.modules.pop(name, None)

    def test_stale_movie_replaced_deleted_via_deletion_manager_not_plex_only(self):
        self.conn.execute(
            "INSERT INTO media_items (id, imdb_id, type, state, version, manual_replace) VALUES "
            "(1, 'tt123', 'movie', 'Collected', '1080p', 1), "
            "(2, 'tt123', 'movie', 'Collected', '1080p', 0)"
        )
        self.conn.commit()

        pp.replace_cleanup_after_collect({
            'id': 2, 'imdb_id': 'tt123', 'type': 'movie', 'version': '1080p',
        })

        self.assertEqual(len(self.delete_calls), 1)
        old_id, kwargs = self.delete_calls[0]
        self.assertEqual(old_id, 1)
        # Must actually unlink files/symlinks, not rely solely on media server removal.
        self.assertTrue(kwargs.get('delete_files'))
        self.assertTrue(kwargs.get('delete_symlinks'))
        self.assertTrue(kwargs.get('delete_from_debrid'))
        self.assertFalse(kwargs.get('skip_database'))

    def test_no_manual_replace_rows_is_noop(self):
        self.conn.execute(
            "INSERT INTO media_items (id, imdb_id, type, state, version, manual_replace) VALUES "
            "(1, 'tt999', 'movie', 'Collected', '1080p', 0)"
        )
        self.conn.commit()

        pp.replace_cleanup_after_collect({
            'id': 1, 'imdb_id': 'tt999', 'type': 'movie', 'version': '1080p',
        })

        self.assertEqual(self.delete_calls, [])

    def test_current_item_never_deleted(self):
        # Only the current item exists (marked manual_replace itself, e.g. re-processed) - must not self-delete.
        self.conn.execute(
            "INSERT INTO media_items (id, imdb_id, type, state, version, manual_replace) VALUES "
            "(5, 'tt555', 'movie', 'Collected', '1080p', 1)"
        )
        self.conn.commit()

        pp.replace_cleanup_after_collect({
            'id': 5, 'imdb_id': 'tt555', 'type': 'movie', 'version': '1080p',
        })

        self.assertEqual(self.delete_calls, [])

    def test_episode_replace_matches_same_season_episode_version(self):
        self.conn.execute(
            "INSERT INTO media_items (id, imdb_id, type, state, version, season_number, episode_number, manual_replace) VALUES "
            "(10, 'tt777', 'episode', 'Collected', '1080p', 1, 1, 1), "
            "(11, 'tt777', 'episode', 'Collected', '1080p', 1, 1, 0), "
            "(12, 'tt777', 'episode', 'Collected', '1080p', 1, 2, 1)"  # different episode - unrelated
        )
        self.conn.commit()

        pp.replace_cleanup_after_collect({
            'id': 11, 'imdb_id': 'tt777', 'type': 'episode', 'version': '1080p',
            'season_number': 1, 'episode_number': 1,
        })

        self.assertEqual(len(self.delete_calls), 1)
        self.assertEqual(self.delete_calls[0][0], 10)


    # --- Symlinked/Local: old Plex version removed only after the replacement is scanned in ---
    # Reported: Spider-Man: Brand New Day replaced from the library showed up as the newest
    # "recently added" movie, because the old (only) Plex version was deleted before Plex had
    # scanned in the replacement, which deleted the whole Plex item.

    OLD = '/sym/Movies/Spider-Man (2026)/Spider-Man (2026) - tt1 - 1080p - (TURG).mkv'
    NEW = '/sym/Movies/Spider-Man (2026)/Spider-Man (2026) - tt1 - 1080p - (GL0P).mkv'

    def _settings(self, mode='Symlinked/Local', jellyfin=''):
        values = {('File Management', 'file_collection_management'): mode,
                  ('Debug', 'emby_jellyfin_url'): jellyfin}
        pp.get_setting = lambda section, key, default=None: values.get((section, key), default)

    def _replace_movie(self, old_path, new_path, old_orig='/mnt/TURG/a.mkv', new_orig='/mnt/GL0P/b.mkv'):
        self.conn.execute(
            "INSERT INTO media_items (id, imdb_id, type, state, version, manual_replace, title, "
            "location_on_disk, original_path_for_symlink) VALUES "
            "(1, 'tt1', 'movie', 'Collected', '1080p', 1, 'Spider-Man', ?, ?)", (old_path, old_orig))
        self.conn.commit()
        pp.replace_cleanup_after_collect({
            'id': 2, 'imdb_id': 'tt1', 'type': 'movie', 'version': '1080p', 'title': 'Spider-Man',
            'location_on_disk': new_path, 'original_path_for_symlink': new_orig,
        })
        import threading
        for t in threading.enumerate():
            if t.name.startswith('replace-plex-'):
                t.join(timeout=5)

    def test_symlink_mode_defers_plex_removal_until_replacement_scanned(self):
        self._settings()
        self._replace_movie(self.OLD, self.NEW)
        _, kwargs = self.delete_calls[0]
        self.assertFalse(kwargs['delete_from_media_server'])
        self.assertTrue(kwargs['delete_symlinks'])
        self.assertTrue(kwargs['delete_files'])
        self.assertEqual(self.deferred_calls, [('Spider-Man', self.NEW, [self.OLD], None)])

    def test_same_symlink_path_never_deletes_the_replacement(self):
        # Template without {original_filename}: the replacement sits at the old path.
        self._settings()
        self._replace_movie(self.NEW, self.NEW)
        _, kwargs = self.delete_calls[0]
        self.assertFalse(kwargs['delete_symlinks'])
        self.assertFalse(kwargs['delete_from_media_server'])
        self.assertEqual(self.deferred_calls, [])

    def test_same_original_file_never_deleted(self):
        self._settings()
        self._replace_movie(self.OLD, self.NEW, old_orig='/mnt/X/a.mkv', new_orig='/mnt/X/a.mkv')
        _, kwargs = self.delete_calls[0]
        self.assertFalse(kwargs['delete_files'])
        self.assertFalse(kwargs['delete_from_debrid'])

    def test_jellyfin_keeps_immediate_removal(self):
        self._settings(jellyfin='http://jellyfin:8096')
        self._replace_movie(self.OLD, self.NEW)
        _, kwargs = self.delete_calls[0]
        self.assertTrue(kwargs['delete_from_media_server'])
        self.assertEqual(self.deferred_calls, [])

    def test_plex_mode_keeps_immediate_removal(self):
        self._settings(mode='Plex')
        self._replace_movie(self.OLD, self.NEW)
        _, kwargs = self.delete_calls[0]
        self.assertTrue(kwargs['delete_from_media_server'])
        self.assertEqual(self.deferred_calls, [])

    # --- Library "Move back to Wanted": the old files are removed once the replacement lands ---
    # Reported: sending an episode back to Wanted left the old symlink, the old torrent/NZB on
    # the mount and the old Plex version behind after the new episode was collected, because
    # the route cleared every file column and nothing remembered what to delete.

    def _move_to_wanted_episode(self, old_files, new_path, new_orig='/mnt/GL0P/b.mkv', new_job='nzb:new'):
        import json
        raw = json.dumps(old_files)
        self.conn.execute(
            "INSERT INTO media_items (id, imdb_id, type, state, version, season_number, episode_number, "
            "title, location_on_disk, original_path_for_symlink, filled_by_torrent_id, replaced_files) VALUES "
            "(20, 'tt9', 'episode', 'Collected', '1080p', 1, 5, 'Show', ?, ?, ?, ?)",
            (new_path, new_orig, new_job, raw))
        self.conn.commit()
        item = dict(self.conn.execute("SELECT * FROM media_items WHERE id = 20").fetchone())
        pp.cleanup_files_replaced_by_move_to_wanted(item)
        import threading
        for t in threading.enumerate():
            if t.name.startswith('replace-plex-'):
                t.join(timeout=5)
        return item

    OLD_EP = {'id': 20, 'title': 'Show', 'type': 'episode', 'state': 'Collected',
              'location_on_disk': '/sym/Show/Season 01/Show - S01E05 - (TURG).mkv',
              'original_path_for_symlink': '/mnt/TURG/a.mkv', 'filled_by_torrent_id': 'nzb:old'}
    NEW_EP = '/sym/Show/Season 01/Show - S01E05 - (GL0P).mkv'

    def test_move_to_wanted_old_files_deleted_from_snapshot_not_db_row(self):
        self._settings()
        item = self._move_to_wanted_episode([self.OLD_EP], self.NEW_EP)
        self.assertEqual(len(self.delete_calls), 1)
        item_id, kwargs = self.delete_calls[0]
        self.assertEqual(item_id, 20)
        self.assertEqual(kwargs['item'], self.OLD_EP)
        # The row now holds the replacement: never delete or blacklist it.
        self.assertTrue(kwargs['skip_database'])
        self.assertTrue(kwargs['delete_symlinks'])
        self.assertTrue(kwargs['delete_files'])
        self.assertTrue(kwargs['delete_from_debrid'])
        self.assertFalse(kwargs['delete_from_media_server'])
        self.assertEqual(self.deferred_calls, [('Show', self.NEW_EP, [self.OLD_EP['location_on_disk']], None)])
        self.assertIsNone(self.conn.execute("SELECT replaced_files FROM media_items WHERE id = 20").fetchone()[0])

    def test_move_to_wanted_cleanup_runs_once(self):
        self._settings()
        item = self._move_to_wanted_episode([self.OLD_EP], self.NEW_EP)
        pp.cleanup_files_replaced_by_move_to_wanted(item)  # stale dict, list already claimed
        self.assertEqual(len(self.delete_calls), 1)

    def test_move_to_wanted_same_paths_and_job_kept(self):
        self._settings()
        old = dict(self.OLD_EP, location_on_disk=self.NEW_EP)
        self._move_to_wanted_episode([old], self.NEW_EP, new_orig='/mnt/X/c.mkv', new_job='nzb:old')
        _, kwargs = self.delete_calls[0]
        self.assertFalse(kwargs['delete_symlinks'])
        self.assertFalse(kwargs['delete_from_media_server'])
        self.assertFalse(kwargs['delete_from_debrid'])

    def test_no_replaced_files_is_noop(self):
        self._settings()
        pp.cleanup_files_replaced_by_move_to_wanted({'id': 1, 'replaced_files': None})
        self.assertEqual(self.delete_calls, [])


if __name__ == '__main__':
    unittest.main()
