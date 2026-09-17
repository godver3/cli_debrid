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
                manual_replace INTEGER DEFAULT 0
            )
        ''')

        fake_db_module = types.ModuleType('database')
        fake_db_module.get_db_connection = lambda: self.conn
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

    def tearDown(self):
        self.conn.close()
        for name in ('database', 'debrid', 'utilities.deletion_manager'):
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


if __name__ == '__main__':
    unittest.main()
