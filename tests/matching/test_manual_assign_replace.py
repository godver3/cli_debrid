"""Manual assign "Replace episodes already in the library".

With the box ticked, confirming an episode that's already Collected in the
same version reuses that row (back to Checking on the new file, old files
saved in replaced_files for removal after collection) instead of inserting a
second copy. Unticked keeps the old add-a-copy behavior.

Runs in a fresh interpreter against a temp database with the real schema:
other tests leave stubs for database modules in sys.modules.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import pytest

pytestmark = pytest.mark.slow  # starts a fresh Python interpreter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, %(root)r)
    import database  # noqa
    from database.schema_management import verify_database
    verify_database()
    from database.core import get_db_connection
    from database.database_writing import replace_collected_item, stash_replaced_files

    conn = get_db_connection()
    def add(**row):
        cols = ', '.join(row)
        cur = conn.execute(f"INSERT INTO media_items ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.commit()
        return cur.lastrowid
    base = dict(imdb_id='tt0988818', tmdb_id='57041', title='Gintama', type='episode', season_number=1)
    old = add(**base, episode_number=1, state='Collected', version='Anime*', filled_by_file='old.mkv',
              filled_by_title='Old Pack', filled_by_torrent_id='OLD', location_on_disk='/sym/Gintama S01E01 old.mkv',
              original_path_for_symlink='/mnt/Old Pack/old.mkv', original_filename='old.mkv',
              collected_at='2026-01-01 00:00:00', original_collected_at='2026-01-01 00:00:00')
    other_version = add(**base, episode_number=2, state='Collected', version='1080p', filled_by_file='x.mkv',
                        filled_by_torrent_id='X', location_on_disk='/sym/x.mkv')
    wanted = add(**base, episode_number=3, state='Wanted', version='Anime')
    new = dict(base, version='Anime', state='Checking', filled_by_file='[CBT] Gintama 001-002.mkv',
               filled_by_title='[CBT] Gintama S01', filled_by_magnet='magnet:?xt=urn:btih:abc',
               filled_by_torrent_id='NEW')
    out = {
        'replaced': replace_collected_item(dict(new, episode_number=1)),
        'other_version': replace_collected_item(dict(new, episode_number=2)),
        'wanted': replace_collected_item(dict(new, episode_number=3)),
        'old_id': old,
        'stash_skips_fileless': stash_replaced_files([{'id': 1}]),
    }
    row = dict(conn.execute("SELECT * FROM media_items WHERE id = ?", (old,)).fetchone())
    out['row'] = {k: row[k] for k in ('state', 'version', 'filled_by_file', 'filled_by_torrent_id',
                                      'location_on_disk', 'original_path_for_symlink', 'original_filename',
                                      'collected_at', 'original_collected_at', 'replaced_files')}
    out['rows'] = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
    print('RESULT ' + json.dumps(out, default=str))
''')


class ReplaceCollectedItemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, USER_DB_CONTENT=tmp, USER_CONFIG=tmp, USER_LOGS=tmp, PYTHONDONTWRITEBYTECODE='1')
        proc = subprocess.run([sys.executable, '-c', SCRIPT % {'root': ROOT}], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=300)
        lines = [l for l in proc.stdout.splitlines() if l.startswith('RESULT ')]
        if not lines:
            raise AssertionError(f"probe failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        cls.out = json.loads(lines[-1][len('RESULT '):])

    def test_collected_row_in_same_version_is_reused(self):
        self.assertEqual(self.out['replaced'], self.out['old_id'])
        self.assertEqual(self.out['rows'], 3)  # nothing inserted

    def test_row_points_at_the_new_file(self):
        row = self.out['row']
        self.assertEqual(row['state'], 'Checking')
        self.assertEqual(row['version'], 'Anime')
        self.assertEqual(row['filled_by_file'], '[CBT] Gintama 001-002.mkv')
        self.assertEqual(row['filled_by_torrent_id'], 'NEW')
        for cleared in ('location_on_disk', 'original_path_for_symlink', 'original_filename', 'collected_at'):
            self.assertIsNone(row[cleared], cleared)
        self.assertIsNotNone(row['original_collected_at'])

    def test_old_files_saved_for_removal_after_collection(self):
        saved = json.loads(self.out['row']['replaced_files'])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['location_on_disk'], '/sym/Gintama S01E01 old.mkv')
        self.assertEqual(saved[0]['original_path_for_symlink'], '/mnt/Old Pack/old.mkv')
        self.assertEqual(saved[0]['filled_by_torrent_id'], 'OLD')

    def test_other_versions_and_uncollected_rows_are_left_alone(self):
        self.assertIsNone(self.out['other_version'])
        self.assertIsNone(self.out['wanted'])

    def test_rows_without_files_stash_nothing(self):
        self.assertEqual(self.out['stash_skips_fileless'], [])


class ConfirmFormTests(unittest.TestCase):
    def test_checkbox_isnt_read_as_an_item(self):
        # Every other form key is an item key; the checkbox has to be popped first
        src = open(os.path.join(ROOT, 'routes', 'magnet_routes.py')).read()
        confirm = src[src.index('def confirm_manual_assignment'):]
        self.assertLess(confirm.index("assignments.pop('replace_existing'"),
                        confirm.index('for item_key, selected_file_list in assignments.items()'))


if __name__ == '__main__':
    unittest.main()
