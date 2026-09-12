#!/usr/bin/env python3
"""Regression test: an external mount add must not be silently refused by the
ghostlist, and adopting an existing row must repoint it at the new file.

Reported symptom -- every file in an externally added folder failing with::

    DB add error for This.Is.England.90.S01E02...mkv: add_media_item no ID returned

add_media_item() returns None (it does not raise) when its ghostlist/blacklist
guard matches and user_initiated is False, which is how the rclone/external-add
path called it. Content fetched outside cli_debrid -- via DMM, the provider's
own UI, a manual magnet -- is disproportionately likely to be ghostlisted or
blacklisted already, because that is *why* the user went and got it elsewhere.
So the import refused precisely the content the user had just asked for, and
reported it as an opaque "no ID returned".

The fix passes user_initiated=True on the external-add path only (the manual
bulk-scan tool must not unghost everything still present in the mount), which
reaches add_media_item's version-aware branch:

  * same version      -> unghost and UPDATE the existing row, return its id
  * different version -> INSERT a new row, leave the old one ghostlisted

That same-version branch is also the "adopt instead of duplicate" repair for a
dead debrid entry replaced by a fresh one. But it only updates
ghostlisted/state/magnet/scrape/torrent_name -- NOT location_on_disk,
original_path_for_symlink or filled_by_file -- so without an explicit sync
afterwards the adopted row still points at the file the import just replaced.
Both halves are pinned below.

Follows the in-memory-sqlite approach of test_fallback_version_ghostlist_check.py:
importing database.database_writing pulls in the Flask app, so the decisive SQL
and branch semantics are exercised directly instead.
"""

import sqlite3
import unittest


# Mirrors add_media_item()'s guard. The 'Blacklisted' half is version-scoped
# (PR #491); a permanent ghostlist still blocks at any version.
GUARD = ("(ghostlisted = 1 OR (state = 'Blacklisted' "
         "AND RTRIM(COALESCE(version, ''), '*') = ?))")

EPISODE_GUARD_QUERY = f"""
    SELECT id, version FROM media_items
    WHERE (imdb_id = ? OR tmdb_id = ?)
    AND type = ?
    AND season_number = ?
    AND episode_number = ?
    AND {GUARD}
    LIMIT 1
"""

# Fields add_media_item's same-version UPDATE branch actually writes.
ADOPT_UPDATE_FIELDS = ('ghostlisted', 'state', 'magnet_link', 'scrape_results',
                       'torrent_name', 'last_updated')

# Fields the import task syncs after create_symlink succeeds.
SYNC_FIELDS = ('location_on_disk', 'original_path_for_symlink', 'filled_by_file',
               'filled_by_title', 'state', 'collected_at', 'version')


def _make_conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE media_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT, tmdb_id TEXT, title TEXT, type TEXT,
            season_number INTEGER, episode_number INTEGER,
            state TEXT, version TEXT, ghostlisted INTEGER DEFAULT 0,
            location_on_disk TEXT, original_path_for_symlink TEXT,
            filled_by_file TEXT, filled_by_title TEXT,
            collected_at TIMESTAMP, last_updated TIMESTAMP,
            magnet_link TEXT, scrape_results TEXT, torrent_name TEXT
        )
        """
    )
    return conn


def _insert_existing(conn, state='Collected', version='2160p', ghostlisted=0):
    cur = conn.execute(
        "INSERT INTO media_items (imdb_id, tmdb_id, title, type, season_number,"
        " episode_number, state, version, ghostlisted, location_on_disk,"
        " original_path_for_symlink, filled_by_file)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ('tt42178219', '308014', "This Is England '90", 'episode', 1, 2,
         state, version, ghostlisted, '/mnt/media/OLD.mkv',
         '/mnt/dfs/__all__/OLD.RELEASE/OLD.mkv', 'OLD.mkv'))
    conn.commit()
    return cur.lastrowid


def _guard_match(conn, version='2160p'):
    """Run add_media_item's episode guard for the incoming item."""
    return conn.execute(
        EPISODE_GUARD_QUERY,
        ('tt42178219', '308014', 'episode', 1, 2, (version or '').rstrip('*'))
    ).fetchone()


def _simulate_add(conn, version='2160p', user_initiated=False):
    """Reproduce add_media_item's control flow. Returns (item_id, action)."""
    match = _guard_match(conn, version)
    if match:
        if not user_initiated:
            return None, 'blocked'          # <- the reported bug
        if match['version'] == version:
            conn.execute(
                "UPDATE media_items SET ghostlisted = 0, state = ?,"
                " last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                ('Collected', match['id']))
            conn.commit()
            return match['id'], 'adopted'
        # different version falls through to a fresh insert
    cur = conn.execute(
        "INSERT INTO media_items (imdb_id, tmdb_id, title, type, season_number,"
        " episode_number, state, version, location_on_disk,"
        " original_path_for_symlink, filled_by_file)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ('tt42178219', '308014', "This Is England '90", 'episode', 1, 2,
         'Collected', version, '/mnt/media/NEW.mkv',
         '/mnt/dfs/__all__/NEW.RELEASE/NEW.mkv', 'NEW.mkv'))
    conn.commit()
    return cur.lastrowid, 'inserted'


class TestGhostlistBlocksExternalAdd(unittest.TestCase):
    """The bug: the guard matches and the add is refused with no usable reason."""

    def test_ghostlisted_blocks_when_not_user_initiated(self):
        conn = _make_conn()
        _insert_existing(conn, ghostlisted=1)
        item_id, action = _simulate_add(conn, user_initiated=False)
        self.assertIsNone(item_id)
        self.assertEqual(action, 'blocked')

    def test_ghostlist_blocks_at_any_version(self):
        """Unlike 'Blacklisted', a permanent ghostlist is not version-scoped."""
        conn = _make_conn()
        _insert_existing(conn, version='1080p', ghostlisted=1)
        self.assertIsNotNone(_guard_match(conn, version='2160p'))

    def test_blacklisted_is_version_scoped(self):
        """PR #491: blacklist only blocks the same normalized version."""
        conn = _make_conn()
        _insert_existing(conn, state='Blacklisted', version='1080p')
        self.assertIsNone(_guard_match(conn, version='2160p'))
        self.assertIsNotNone(_guard_match(conn, version='1080p'))

    def test_blacklisted_version_asterisk_is_normalized(self):
        conn = _make_conn()
        _insert_existing(conn, state='Blacklisted', version='2160p*')
        self.assertIsNotNone(_guard_match(conn, version='2160p'))


class TestUserInitiatedAdopt(unittest.TestCase):
    """The fix: external adds reach the version-aware branch."""

    def test_same_version_adopts_existing_row(self):
        conn = _make_conn()
        existing = _insert_existing(conn, version='2160p', ghostlisted=1)
        item_id, action = _simulate_add(conn, version='2160p', user_initiated=True)
        self.assertEqual(action, 'adopted')
        self.assertEqual(item_id, existing, 'must reuse the row, not create one')
        count = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        self.assertEqual(count, 1, 'adopt must not duplicate the item')

    def test_adopt_clears_ghostlisted(self):
        conn = _make_conn()
        existing = _insert_existing(conn, ghostlisted=1)
        _simulate_add(conn, user_initiated=True)
        row = conn.execute("SELECT ghostlisted, state FROM media_items WHERE id = ?",
                           (existing,)).fetchone()
        self.assertEqual(row['ghostlisted'], 0)
        self.assertEqual(row['state'], 'Collected')

    def test_different_version_inserts_and_preserves_old(self):
        conn = _make_conn()
        existing = _insert_existing(conn, version='1080p', ghostlisted=1)
        item_id, action = _simulate_add(conn, version='2160p', user_initiated=True)
        self.assertEqual(action, 'inserted')
        self.assertNotEqual(item_id, existing)
        count = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        self.assertEqual(count, 2)
        old = conn.execute("SELECT ghostlisted FROM media_items WHERE id = ?",
                           (existing,)).fetchone()
        self.assertEqual(old['ghostlisted'], 1, 'old version must stay ghostlisted')

    def test_clean_add_unaffected(self):
        """Nothing ghostlisted: user_initiated must not change normal inserts."""
        conn = _make_conn()
        item_id, action = _simulate_add(conn, user_initiated=True)
        self.assertEqual(action, 'inserted')
        self.assertIsNotNone(item_id)


class TestAdoptRequiresFilePointerSync(unittest.TestCase):
    """The second half of the fix: adopt alone leaves the row pointing at the
    file the import just replaced."""

    def test_adopt_alone_leaves_stale_file_pointers(self):
        conn = _make_conn()
        _insert_existing(conn, ghostlisted=1)
        item_id, action = _simulate_add(conn, user_initiated=True)
        self.assertEqual(action, 'adopted')
        row = conn.execute(
            "SELECT location_on_disk, original_path_for_symlink, filled_by_file"
            " FROM media_items WHERE id = ?", (item_id,)).fetchone()
        # Still the old file -- this is why the task syncs explicitly.
        self.assertEqual(row['location_on_disk'], '/mnt/media/OLD.mkv')
        self.assertEqual(row['filled_by_file'], 'OLD.mkv')

    def test_sync_repoints_row_at_new_file(self):
        conn = _make_conn()
        _insert_existing(conn, ghostlisted=1)
        item_id, _ = _simulate_add(conn, user_initiated=True)

        # What the import task does after create_symlink succeeds.
        conn.execute(
            "UPDATE media_items SET location_on_disk = ?,"
            " original_path_for_symlink = ?, filled_by_file = ?,"
            " filled_by_title = ?, state = ?, version = ?"
            " WHERE id = ? AND (ghostlisted IS NULL OR ghostlisted = 0)",
            ('/mnt/media/NEW.mkv', '/mnt/dfs/__all__/NEW.RELEASE/NEW.mkv',
             'NEW.mkv', 'NEW.RELEASE', 'Collected', '2160p', item_id))
        conn.commit()

        row = conn.execute(
            "SELECT location_on_disk, original_path_for_symlink, filled_by_file,"
            " filled_by_title FROM media_items WHERE id = ?", (item_id,)).fetchone()
        self.assertEqual(row['location_on_disk'], '/mnt/media/NEW.mkv')
        self.assertEqual(row['original_path_for_symlink'],
                         '/mnt/dfs/__all__/NEW.RELEASE/NEW.mkv')
        self.assertEqual(row['filled_by_file'], 'NEW.mkv')
        self.assertEqual(row['filled_by_title'], 'NEW.RELEASE')

    def test_sync_is_blocked_while_still_ghostlisted(self):
        """update_media_item()'s WHERE clause skips ghostlisted rows, so the
        unghost in add_media_item must happen first -- ordering matters."""
        conn = _make_conn()
        existing = _insert_existing(conn, ghostlisted=1)
        conn.execute(
            "UPDATE media_items SET location_on_disk = ?"
            " WHERE id = ? AND (ghostlisted IS NULL OR ghostlisted = 0)",
            ('/mnt/media/NEW.mkv', existing))
        conn.commit()
        row = conn.execute("SELECT location_on_disk FROM media_items WHERE id = ?",
                           (existing,)).fetchone()
        self.assertEqual(row['location_on_disk'], '/mnt/media/OLD.mkv',
                         'ghostlisted row must not be updatable')

    def test_sync_field_list_covers_what_adopt_misses(self):
        """Guards against the sync list drifting away from the gap it exists to
        close: every file pointer adopt does not write must be synced."""
        missed_by_adopt = {'location_on_disk', 'original_path_for_symlink',
                           'filled_by_file', 'filled_by_title'}
        self.assertTrue(missed_by_adopt.isdisjoint(ADOPT_UPDATE_FIELDS))
        self.assertTrue(missed_by_adopt.issubset(set(SYNC_FIELDS)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
