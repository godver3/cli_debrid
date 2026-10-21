#!/usr/bin/env python3
"""Regression test: fallback-version creation must not be blocked by the
original item's own just-set 'Blacklisted' state.

queues/blacklisted_queue.py's fallback-version path does, in order:
  1. update_media_item_state(original_item_id, 'Blacklisted')   # e.g. version 'Preferred'
  2. queue_manager.create_and_add_item_to_wanted_queue(new_item_data)  # e.g. version '480'

create_and_add_item_to_wanted_queue() ran a "ghostlisted/blacklisted" guard
query matching only on imdb_id/season/episode (no version), so it always
found the row from step 1 and refused to create the fallback -- logging
"QueueManager reported failure in creating/queuing fallback item ...".
This made the fallback-version feature a no-op for every user who had one
configured, since a fallback's target episode is by definition the same
episode as the item that just got blacklisted at a different version.

The fix scopes the 'Blacklisted' half of the check to the same normalized
version as the item being created; a genuine permanent ghostlist (any
version) still blocks unconditionally.
"""

import os
import sqlite3
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EPISODE_QUERY = """
    SELECT id FROM media_items
    WHERE (imdb_id = ? OR tmdb_id = ?)
    AND type = ?
    AND season_number = ?
    AND episode_number = ?
    AND (ghostlisted = 1 OR (state = 'Blacklisted' AND RTRIM(COALESCE(version, ''), '*') = ?))
    LIMIT 1
"""


def _make_conn():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        """
        CREATE TABLE media_items (
            id INTEGER PRIMARY KEY,
            imdb_id TEXT,
            tmdb_id TEXT,
            type TEXT,
            season_number INTEGER,
            episode_number INTEGER,
            state TEXT,
            version TEXT,
            ghostlisted INTEGER DEFAULT 0
        )
        """
    )
    return conn


def _insert(conn, state, version, ghostlisted=0):
    conn.execute(
        "INSERT INTO media_items (imdb_id, type, season_number, episode_number, state, version, ghostlisted) "
        "VALUES (?,?,?,?,?,?,?)",
        ('tt0124932', 'episode', 49, 27, state, version, ghostlisted),
    )
    conn.commit()


class TestFallbackVersionCreationIsNotSelfBlocked(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()

    def tearDown(self):
        self.conn.close()

    def test_fallback_version_creation_allowed_after_original_just_blacklisted(self):
        # The original item ('Preferred') was just blacklisted moments ago.
        _insert(self.conn, state='Blacklisted', version='Preferred')
        # Creating the fallback at '480' for the same episode must NOT be blocked.
        row = self.conn.execute(
            EPISODE_QUERY, ('tt0124932', 'tt0124932', 'episode', 49, 27, '480')
        ).fetchone()
        self.assertIsNone(row)

    def test_same_version_already_blacklisted_still_blocks(self):
        _insert(self.conn, state='Blacklisted', version='480')
        row = self.conn.execute(
            EPISODE_QUERY, ('tt0124932', 'tt0124932', 'episode', 49, 27, '480')
        ).fetchone()
        self.assertIsNotNone(row)

    def test_decorated_same_version_still_blocks(self):
        _insert(self.conn, state='Blacklisted', version='480*')
        row = self.conn.execute(
            EPISODE_QUERY, ('tt0124932', 'tt0124932', 'episode', 49, 27, '480')
        ).fetchone()
        self.assertIsNotNone(row)

    def test_permanent_ghostlist_blocks_regardless_of_version(self):
        _insert(self.conn, state='Blacklisted', version='Preferred', ghostlisted=1)
        row = self.conn.execute(
            EPISODE_QUERY, ('tt0124932', 'tt0124932', 'episode', 49, 27, '480')
        ).fetchone()
        self.assertIsNotNone(row)

    def test_internal_asterisk_in_a_custom_version_name_is_preserved(self):
        # A user-named version like '4K*HDR' has its '*' in the middle, not
        # as a trailing pending-upgrade marker. REPLACE(...,'*','') would
        # have stripped it too (yielding '4KHDR'), diverging from Python's
        # target_version = (...).rstrip('*') (only strips trailing '*',
        # leaving '4K*HDR' as-is) -- silently reintroducing the self-block
        # bug for any version name containing an internal '*'. RTRIM only
        # strips from the right end, matching rstrip('*') exactly.
        _insert(self.conn, state='Blacklisted', version='4K*HDR')
        row = self.conn.execute(
            EPISODE_QUERY, ('tt0124932', 'tt0124932', 'episode', 49, 27, '4K*HDR')
        ).fetchone()
        self.assertIsNotNone(row)


class TestProductionCodeMatchesFixedQuery(unittest.TestCase):
    def test_queue_manager_uses_version_scoped_blacklist_check(self):
        with open(os.path.join(PROJECT_ROOT, "queues", "queue_manager.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertIn(
            "AND (ghostlisted = 1 OR (state = 'Blacklisted' AND RTRIM(COALESCE(version, ''), '*') = ?))",
            source,
        )
        self.assertIn(
            "target_version = (new_item_data_for_db.get('version') or '').rstrip('*')",
            source,
        )


if __name__ == "__main__":
    unittest.main()
