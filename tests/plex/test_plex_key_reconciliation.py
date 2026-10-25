#!/usr/bin/env python3
"""Tests for overlays.scheduled_tasks._sync_library_keys_for_new_items
(godver3/cli_debrid#496 item 5: Plex key reconciliation should fail closed).

Covers the three hardening changes:
  1. Ambiguous identity keys (two Plex items sharing an imdb_id/tmdb_id/
     title+year, or a duplicate file path) are excluded rather than resolved
     last-write-wins.
  2. A partial Plex inventory fetch (get_all_items_with_guids.
     last_fetch_incomplete) skips split-apart detection (Pass 2, which
     REPLACES an existing ms_item_id) while blank-key assignment (Pass 1,
     which only fills an empty one) still applies.
  3. The final UPDATE is pinned to the ms_item_id value read earlier, so a
     concurrent change to that row is a no-op instead of being clobbered.

overlays.scheduled_tasks imports cleanly in this environment (unlike most of
the app), so this uses a real temp sqlite DB and a fake PlexClient rather
than the source-inspection pattern used elsewhere in this suite.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import overlays.scheduled_tasks as scheduled_tasks


class _FakePlexClient:
    """Stands in for overlays.plex_client.PlexClient."""

    def __init__(self, items, incomplete=False):
        self._items = items
        self.last_fetch_incomplete = incomplete

    def get_all_items_with_guids(self, plex_type):
        return self._items


class MsKeySyncTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE media_items (
                id INTEGER PRIMARY KEY,
                type TEXT,
                imdb_id TEXT,
                tmdb_id TEXT,
                title TEXT,
                year INTEGER,
                location_on_disk TEXT,
                ms_item_id TEXT,
                state TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE media_overlay_state (
                media_item_id INTEGER,
                status TEXT,
                reason TEXT,
                updated_at TEXT
            )
        ''')
        conn.commit()
        conn.close()

        def _connect():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self._connect = _connect
        self.patcher = patch.object(scheduled_tasks, '_get_db_connection', _connect)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def insert_row(self, **kwargs):
        defaults = {'type': 'movie', 'imdb_id': None, 'tmdb_id': None, 'title': None,
                    'year': None, 'location_on_disk': None, 'ms_item_id': '', 'state': 'Collected'}
        defaults.update(kwargs)
        conn = self._connect()
        cur = conn.execute(
            'INSERT INTO media_items (type, imdb_id, tmdb_id, title, year, location_on_disk, ms_item_id, state) '
            'VALUES (:type, :imdb_id, :tmdb_id, :title, :year, :location_on_disk, :ms_item_id, :state)',
            defaults)
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id

    def get_ms_item_id(self, row_id):
        conn = self._connect()
        val = conn.execute('SELECT ms_item_id FROM media_items WHERE id=?', (row_id,)).fetchone()[0]
        conn.close()
        return val

    def run_sync(self, items, incomplete=False):
        fake_client = _FakePlexClient(items, incomplete=incomplete)
        with patch('overlays.plex_client.PlexClient', return_value=fake_client):
            return scheduled_tasks._sync_library_keys_for_new_items('http://plex', 'token')


class TestBlankKeyAssignment(MsKeySyncTestBase):
    def test_unique_match_assigns_blank_key(self):
        row_id = self.insert_row(imdb_id='tt123', title='Movie', year=2020, location_on_disk='/x/movie.mkv')
        self.run_sync([{'ratingKey': '100', 'title': 'Movie', 'year': 2020, 'imdb_id': 'tt123',
                         'tmdb_id': None, 'file_paths': ['/x/movie.mkv']}])
        self.assertEqual(self.get_ms_item_id(row_id), '100')


class TestAmbiguousKeysExcluded(MsKeySyncTestBase):
    def test_two_plex_items_sharing_imdb_id_never_assign_via_that_key(self):
        # A real split-apart in progress: two Plex items both carry the same
        # IMDb ID. Neither ratingKey is a trustworthy match via imdb_id alone.
        row_id = self.insert_row(imdb_id='tt999', title='Movie', year=2020, location_on_disk='/no/match.mkv')
        self.run_sync([
            {'ratingKey': '100', 'title': 'Movie', 'year': 2020, 'imdb_id': 'tt999',
             'tmdb_id': None, 'file_paths': ['/a/movie.mkv']},
            {'ratingKey': '200', 'title': 'Movie', 'year': 2020, 'imdb_id': 'tt999',
             'tmdb_id': None, 'file_paths': ['/b/movie.mkv']},
        ])
        # location_on_disk doesn't match either file path, so this row can
        # only resolve via imdb_id/title -- both ambiguous. Must stay blank.
        self.assertEqual(self.get_ms_item_id(row_id), '')

    def test_unambiguous_file_path_still_resolves_despite_ambiguous_imdb(self):
        row_id = self.insert_row(imdb_id='tt999', title='Movie', year=2020, location_on_disk='/a/movie.mkv')
        self.run_sync([
            {'ratingKey': '100', 'title': 'Movie', 'year': 2020, 'imdb_id': 'tt999',
             'tmdb_id': None, 'file_paths': ['/a/movie.mkv']},
            {'ratingKey': '200', 'title': 'Movie', 'year': 2020, 'imdb_id': 'tt999',
             'tmdb_id': None, 'file_paths': ['/b/movie.mkv']},
        ])
        # file_path is exact and unique -- resolves even though imdb_id is ambiguous.
        self.assertEqual(self.get_ms_item_id(row_id), '100')


class TestPartialInventorySkipsSplitDetection(MsKeySyncTestBase):
    def test_split_detection_skipped_on_partial_fetch(self):
        # Row already has an ms_item_id; Plex now reports its file path under a
        # DIFFERENT ratingKey -- normally a legitimate split-apart update, but
        # the fetch was flagged incomplete, so this must NOT be applied.
        row_id = self.insert_row(location_on_disk='/x/movie.mkv', ms_item_id='100')
        self.run_sync(
            [{'ratingKey': '200', 'title': 'Movie', 'year': 2020, 'imdb_id': None,
              'tmdb_id': None, 'file_paths': ['/x/movie.mkv']}],
            incomplete=True,
        )
        self.assertEqual(self.get_ms_item_id(row_id), '100')

    def test_split_detection_applies_on_complete_fetch(self):
        row_id = self.insert_row(location_on_disk='/x/movie.mkv', ms_item_id='100')
        self.run_sync(
            [{'ratingKey': '200', 'title': 'Movie', 'year': 2020, 'imdb_id': None,
              'tmdb_id': None, 'file_paths': ['/x/movie.mkv']}],
            incomplete=False,
        )
        self.assertEqual(self.get_ms_item_id(row_id), '200')

    def test_blank_key_assignment_still_applies_on_partial_fetch(self):
        # Pass 1 only fills an empty value -- safe even on a partial inventory.
        row_id = self.insert_row(location_on_disk='/x/movie.mkv', ms_item_id='')
        self.run_sync(
            [{'ratingKey': '100', 'title': 'Movie', 'year': 2020, 'imdb_id': None,
              'tmdb_id': None, 'file_paths': ['/x/movie.mkv']}],
            incomplete=True,
        )
        self.assertEqual(self.get_ms_item_id(row_id), '100')


class TestRaceSafeUpdate(MsKeySyncTestBase):
    def test_concurrent_change_is_not_clobbered(self):
        row_id = self.insert_row(location_on_disk='/x/movie.mkv', ms_item_id='100')

        # Simulate a concurrent writer changing the row between this sync's
        # read and its write, by intercepting the connection factory to mutate
        # the row right before the sync's own write transaction opens.
        real_connect = self._connect
        call_count = {'n': 0}

        def _connect_with_race():
            call_count['n'] += 1
            if call_count['n'] == 2:
                # Second connection open in the function is the write phase
                # (first was the initial SELECT) -- mutate concurrently now.
                side = real_connect()
                side.execute("UPDATE media_items SET ms_item_id='999' WHERE id=?", (row_id,))
                side.commit()
                side.close()
            return real_connect()

        with patch.object(scheduled_tasks, '_get_db_connection', _connect_with_race):
            fake_client = _FakePlexClient(
                [{'ratingKey': '200', 'title': 'Movie', 'year': 2020, 'imdb_id': None,
                  'tmdb_id': None, 'file_paths': ['/x/movie.mkv']}],
                incomplete=False,
            )
            with patch('overlays.plex_client.PlexClient', return_value=fake_client):
                scheduled_tasks._sync_library_keys_for_new_items('http://plex', 'token')

        # The write's WHERE ms_item_id='100' predicate no longer matches (it's
        # now '999'), so the sync's update must be a no-op, not overwrite '999'.
        self.assertEqual(self.get_ms_item_id(row_id), '999')


if __name__ == '__main__':
    unittest.main()
