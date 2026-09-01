#!/usr/bin/env python3
"""Regression test: add_media_item()'s user-initiated adopt branch must apply the
incoming item's file identity, not just unghost the row.

When add_media_item(item, user_initiated=True) finds a ghostlisted/blacklisted
row of the same version it UPDATEs that row and returns its id instead of
inserting. That UPDATE originally wrote only ghostlisted/state/magnet_link/
scrape_results/torrent_name/last_updated, so every file pointer the caller
supplied -- filled_by_file, location_on_disk, original_path_for_symlink -- was
silently discarded and the row kept referencing whatever it pointed at before
being ghostlisted.

Two callers were affected:

  * routes/magnet_routes.py assigns the filename the user manually picked
    ('filled_by_file' = selected_filename) and then adds with
    user_initiated=True. On the adopt path that filename was dropped and the
    item went to Checking hunting for the previous row's file.

  * routes/debug_routes.py's rclone/external-add import adds with state
    'Collected' and a known location_on_disk, so an adopted row advertised a
    file the import had just replaced -- breaking symlink verification,
    deletion and the reconcile audit, which all read location_on_disk.

routes/scraper_routes.py had already worked around this with its own manual
"UPDATE ... filled_by_file = ?" before calling add_media_item; that workaround
is now redundant but harmless.

Follows the in-memory-sqlite approach of test_fallback_version_ghostlist_check.py
and test_external_add_ghostlist_adopt.py: importing database.database_writing
pulls in the Flask app, so the branch's field-selection logic is exercised
directly against the real column set.
"""

import sqlite3
import unittest


# The fields the adopt branch writes unconditionally (pre-existing behaviour).
BASE_ADOPT_FIELDS = ('state', 'magnet_link', 'scrape_results', 'torrent_name')

# The identity fields the fix adds. Must stay in sync with the tuple in
# database_writing.add_media_item's adopt branch.
IDENTITY_FIELDS = (
    'filled_by_file', 'filled_by_title', 'filled_by_magnet',
    'filled_by_torrent_id', 'location_on_disk',
    'original_path_for_symlink', 'collected_at',
    'content_source',
)

# Written via COALESCE rather than plain assignment: filled in when NULL,
# never overwritten once set. Matches collected_items.py's convention.
COALESCED_FIELDS = ('original_collected_at',)


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
            filled_by_file TEXT, filled_by_title TEXT, filled_by_magnet TEXT,
            filled_by_torrent_id TEXT, location_on_disk TEXT,
            original_path_for_symlink TEXT, collected_at TIMESTAMP,
            original_collected_at TIMESTAMP, content_source TEXT,
            magnet_link TEXT, scrape_results TEXT, torrent_name TEXT,
            last_updated TIMESTAMP
        )
        """
    )
    return conn


def _seed_ghostlisted(conn, version='2160p'):
    cur = conn.execute(
        "INSERT INTO media_items (imdb_id, tmdb_id, title, type, season_number,"
        " episode_number, state, version, ghostlisted, filled_by_file,"
        " filled_by_title, location_on_disk, original_path_for_symlink)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ('tt42178219', '308014', 'Some Show', 'episode', 1, 2, 'Collected',
         version, 1, 'OLD.mkv', 'OLD.RELEASE', '/mnt/media/OLD.mkv',
         '/mnt/dfs/__all__/OLD.RELEASE/OLD.mkv'))
    conn.commit()
    return cur.lastrowid


def _adopt(conn, existing_id, item, include_identity=True):
    """Reproduce the adopt branch's UPDATE construction.

    include_identity=False reproduces the pre-fix behaviour, so the tests can
    demonstrate the bug rather than just asserting the fix.
    """
    update_fields = ['ghostlisted = ?']
    update_values = [0]
    for field in BASE_ADOPT_FIELDS:
        if field in item:
            update_fields.append(f'{field} = ?')
            update_values.append(item[field])
    if include_identity:
        for field in IDENTITY_FIELDS:
            if field in item:
                update_fields.append(f'{field} = ?')
                update_values.append(item[field])
        for field in COALESCED_FIELDS:
            if field in item:
                update_fields.append(f'{field} = COALESCE({field}, ?)')
                update_values.append(item[field])
    update_values.append(existing_id)
    conn.execute(
        f"UPDATE media_items SET {', '.join(update_fields)} WHERE id = ?",
        update_values)
    conn.commit()
    return len(update_fields)


class TestMagnetAssignShape(unittest.TestCase):
    """magnet_routes: user manually picks a filename for a ghostlisted item."""

    ITEM = {
        'state': 'Checking',
        'filled_by_file': 'NEW.S01E02.mkv',      # the file the user picked
        'filled_by_title': 'NEW.TORRENT.NAME',
        'version': '2160p',
    }

    def test_pre_fix_drops_the_selected_filename(self):
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        _adopt(conn, existing, self.ITEM, include_identity=False)
        row = conn.execute("SELECT filled_by_file, state FROM media_items"
                           " WHERE id = ?", (existing,)).fetchone()
        self.assertEqual(row['filled_by_file'], 'OLD.mkv',
                         'demonstrates the bug: user selection discarded')
        self.assertEqual(row['state'], 'Checking',
                         'item advances to Checking pointing at the wrong file')

    def test_fix_applies_the_selected_filename(self):
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        _adopt(conn, existing, self.ITEM)
        row = conn.execute("SELECT filled_by_file, filled_by_title, ghostlisted"
                           " FROM media_items WHERE id = ?", (existing,)).fetchone()
        self.assertEqual(row['filled_by_file'], 'NEW.S01E02.mkv')
        self.assertEqual(row['filled_by_title'], 'NEW.TORRENT.NAME')
        self.assertEqual(row['ghostlisted'], 0)


class TestExternalImportShape(unittest.TestCase):
    """debug_routes: external mount import adds a Collected item with a path."""

    ITEM = {
        'state': 'Collected',
        'filled_by_file': 'NEW.mkv',
        'filled_by_title': 'NEW.RELEASE',
        'location_on_disk': '/mnt/media/NEW.mkv',
        'original_path_for_symlink': '/mnt/dfs/__all__/NEW.RELEASE/NEW.mkv',
        'content_source': 'external_webhook',
        'version': '2160p',
    }

    def test_pre_fix_leaves_stale_location(self):
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        _adopt(conn, existing, self.ITEM, include_identity=False)
        row = conn.execute("SELECT location_on_disk, original_path_for_symlink"
                           " FROM media_items WHERE id = ?", (existing,)).fetchone()
        self.assertEqual(row['location_on_disk'], '/mnt/media/OLD.mkv')
        self.assertEqual(row['original_path_for_symlink'],
                         '/mnt/dfs/__all__/OLD.RELEASE/OLD.mkv')

    def test_fix_repoints_every_pointer(self):
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        _adopt(conn, existing, self.ITEM)
        row = conn.execute(
            "SELECT location_on_disk, original_path_for_symlink, filled_by_file,"
            " content_source FROM media_items WHERE id = ?", (existing,)).fetchone()
        self.assertEqual(row['location_on_disk'], '/mnt/media/NEW.mkv')
        self.assertEqual(row['original_path_for_symlink'],
                         '/mnt/dfs/__all__/NEW.RELEASE/NEW.mkv')
        self.assertEqual(row['filled_by_file'], 'NEW.mkv')
        self.assertEqual(row['content_source'], 'external_webhook')


class TestNoOpForQueueDestinedItems(unittest.TestCase):
    """A caller adding a Wanted item has no file yet — nothing to overwrite."""

    def test_absent_fields_are_not_written(self):
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        field_count = _adopt(conn, existing, {'state': 'Wanted', 'version': '2160p'})
        # ghostlisted + state only.
        self.assertEqual(field_count, 2)
        row = conn.execute("SELECT filled_by_file, location_on_disk, state"
                           " FROM media_items WHERE id = ?", (existing,)).fetchone()
        self.assertEqual(row['state'], 'Wanted')
        self.assertEqual(row['filled_by_file'], 'OLD.mkv',
                         'must not blank a field the caller did not supply')
        self.assertEqual(row['location_on_disk'], '/mnt/media/OLD.mkv')

    def test_explicit_none_is_still_written(self):
        """A caller that deliberately passes None means 'clear this'."""
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        _adopt(conn, existing, {'filled_by_file': None, 'version': '2160p'})
        row = conn.execute("SELECT filled_by_file FROM media_items WHERE id = ?",
                           (existing,)).fetchone()
        self.assertIsNone(row['filled_by_file'])


class TestOriginalCollectedAtPreserved(unittest.TestCase):
    """original_collected_at records when the item was FIRST collected, so the
    adopt branch fills it in but must never overwrite it."""

    def test_existing_value_is_preserved(self):
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        conn.execute("UPDATE media_items SET original_collected_at = ? WHERE id = ?",
                     ('2024-01-01 00:00:00', existing))
        conn.commit()
        _adopt(conn, existing, {'original_collected_at': '2026-09-01 12:00:00',
                                'version': '2160p'})
        row = conn.execute("SELECT original_collected_at FROM media_items"
                           " WHERE id = ?", (existing,)).fetchone()
        self.assertEqual(row['original_collected_at'], '2024-01-01 00:00:00',
                         'a re-acquired item must not look newly added')

    def test_null_value_is_filled_in(self):
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)  # leaves original_collected_at NULL
        _adopt(conn, existing, {'original_collected_at': '2026-09-01 12:00:00',
                                'version': '2160p'})
        row = conn.execute("SELECT original_collected_at FROM media_items"
                           " WHERE id = ?", (existing,)).fetchone()
        self.assertEqual(row['original_collected_at'], '2026-09-01 12:00:00')

    def test_collected_at_is_overwritten(self):
        """collected_at is the CURRENT collection, so it does get replaced —
        the distinction between the two fields is the point."""
        conn = _make_conn()
        existing = _seed_ghostlisted(conn)
        conn.execute("UPDATE media_items SET collected_at = ? WHERE id = ?",
                     ('2024-01-01 00:00:00', existing))
        conn.commit()
        _adopt(conn, existing, {'collected_at': '2026-09-01 12:00:00',
                                'version': '2160p'})
        row = conn.execute("SELECT collected_at FROM media_items WHERE id = ?",
                           (existing,)).fetchone()
        self.assertEqual(row['collected_at'], '2026-09-01 12:00:00')

    def test_coalesced_and_plain_lists_are_disjoint(self):
        self.assertTrue(set(COALESCED_FIELDS).isdisjoint(IDENTITY_FIELDS))
        self.assertTrue(set(COALESCED_FIELDS).isdisjoint(BASE_ADOPT_FIELDS))

    def test_source_uses_coalesce_for_original_collected_at(self):
        """Fails if the source switches to a plain assignment, which would
        silently start resetting collection history."""
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'database', 'database_writing.py')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        self.assertIn(
            "'original_collected_at = COALESCE(original_collected_at, ?)'", source)


class TestIdentityFieldListIntegrity(unittest.TestCase):
    """Guards against the field tuple drifting from reality."""

    def test_version_is_excluded(self):
        """The adopt branch only runs when versions already match; writing
        version there would imply it can differ."""
        self.assertNotIn('version', IDENTITY_FIELDS)

    def test_no_overlap_with_base_fields(self):
        """A field written twice would appear twice in the SET clause."""
        self.assertTrue(set(IDENTITY_FIELDS).isdisjoint(BASE_ADOPT_FIELDS))

    def test_every_field_is_a_real_column(self):
        conn = _make_conn()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(media_items)")}
        for field in IDENTITY_FIELDS:
            self.assertIn(field, columns, f'{field} is not a media_items column')

    def test_matches_source_tuple(self):
        """Fails if database_writing's tuple and this list diverge."""
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'database', 'database_writing.py')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        match = re.search(r'for _identity_field in \((.*?)\):', source, re.DOTALL)
        self.assertIsNotNone(match, 'adopt branch identity loop not found')
        found = tuple(re.findall(r"'([a-z_]+)'", match.group(1)))
        self.assertEqual(found, IDENTITY_FIELDS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
