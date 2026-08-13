import os
import sqlite3
import sys
import tempfile
import types
import unittest
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
database_package = types.ModuleType('database')
database_package.__path__ = [os.path.join(ROOT, 'database')]
sys.modules.setdefault('database', database_package)
core_spec = importlib.util.spec_from_file_location('database.core', os.path.join(ROOT, 'database', 'core.py'))
core_module = importlib.util.module_from_spec(core_spec)
sys.modules['database.core'] = core_module
core_spec.loader.exec_module(core_module)
playback_spec = importlib.util.spec_from_file_location(
    'database.nzb_playback_repair', os.path.join(ROOT, 'database', 'nzb_playback_repair.py'))
playback = importlib.util.module_from_spec(playback_spec)
sys.modules['database.nzb_playback_repair'] = playback
playback_spec.loader.exec_module(playback)


class _Client:
    def __init__(self):
        self.registered = []
        self.deleted = []

    def register_cli_ids_for_item(self, info_hash, item_id):
        self.registered.append((info_hash, item_id))
        return True

    def remove_nzb_exact(self, info_hash):
        self.deleted.append(info_hash)
        return True


class TestNZBPlaybackRepair(unittest.TestCase):
    def test_candidate_keys_prefer_original_release_title(self):
        keys = playback.candidate_keys({
            'title': 'cli-mount-renamed-release',
            'original_title': 'Indexer Original Release',
        })
        self.assertIn('t:indexeroriginalrelease', keys)
        self.assertNotIn('t:climountrenamedrelease', keys)

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        self.addCleanup(lambda: os.unlink(self.path) if os.path.exists(self.path) else None)

        def connect():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn

        self.connect = connect
        self.original_connect = playback.get_db_connection
        playback.get_db_connection = connect
        self.addCleanup(lambda: setattr(playback, 'get_db_connection', self.original_connect))
        with connect() as conn:
            conn.executescript("""
                CREATE TABLE media_items (
                    id INTEGER PRIMARY KEY, state TEXT, filled_by_torrent_id TEXT,
                    filled_by_file TEXT, filled_by_title TEXT, debrid_folder_name TEXT,
                    location_on_disk TEXT
                );
                CREATE TABLE nzb_repair_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, title TEXT,
                    media_type TEXT, season_number INTEGER, episode_number INTEGER,
                    broken_nzb_id TEXT, broken_nzb_title TEXT, replacement_nzb_id TEXT,
                    replacement_title TEXT, outcome TEXT, triggered_by TEXT,
                    repair_attempts INTEGER, last_repair_at TIMESTAMP, updated_at TIMESTAMP
                );
                INSERT INTO media_items VALUES
                    (7,'Collected','nzb:old-uuid','old.mkv','Old.Release','Old.Release','/plex/item.mkv');
            """)
        playback.create_nzb_playback_repair_table()
        self.item = {
            'id': 7, 'state': 'Collected', 'filled_by_torrent_id': 'nzb:old-uuid',
            'filled_by_file': 'old.mkv', 'filled_by_title': 'Old.Release',
            'debrid_folder_name': 'Old.Release', 'location_on_disk': '/plex/item.mkv',
            'title': 'Show', 'type': 'episode', 'season_number': 1, 'episode_number': 2,
            'original_scraped_torrent_title': 'Original.Release',
        }
        self.target = {
            'entry_name': 'Old.Release', 'file_name': 'old.mkv', 'info_hash': 'old-uuid',
            'cli_debrid_id': 7, 'reason': 'media_probe_failed', 'segment_id': 'old-segment',
        }

    def test_original_and_failed_candidate_stay_excluded(self):
        self.assertTrue(playback.begin_playback_repair(self.item, self.target))
        original = {'title': 'Original.Release'}
        failed = {'title': 'Another.Release', 'guid': 'GUID-1'}
        self.assertTrue(playback.candidate_is_excluded(7, original))
        playback.record_failed_candidate(7, failed, 'failed-uuid')
        self.assertTrue(playback.candidate_is_excluded(7, failed))

    def test_failed_active_candidate_restores_original(self):
        playback.begin_playback_repair(self.item, self.target)
        candidate = {'title': 'Candidate.Release', 'guid': 'GUID-2'}
        playback.set_playback_candidate(7, candidate, 'new-uuid', 'Candidate.Release')
        with self.connect() as conn:
            conn.execute("UPDATE media_items SET state='Adding',filled_by_torrent_id='nzb:new-uuid'")
        self.assertTrue(playback.reject_active_candidate(7, 'new-uuid'))
        with self.connect() as conn:
            item = conn.execute('SELECT * FROM media_items WHERE id=7').fetchone()
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(item['state'], 'Collected')
        self.assertEqual(item['filled_by_torrent_id'], 'nzb:old-uuid')
        self.assertEqual(repair['status'], 'awaiting_candidate')
        self.assertIn('new-uuid', repair['failed_job_ids_json'])

    def test_healthy_candidate_cleans_exact_target_then_finalizes_activity(self):
        playback.begin_playback_repair(self.item, self.target)
        candidate = {'title': 'Working.Release', 'guid': 'GUID-3'}
        playback.set_playback_candidate(7, candidate, 'new-uuid', 'Working.Release')
        with self.connect() as conn:
            conn.execute("UPDATE media_items SET state='Collected',filled_by_torrent_id='nzb:new-uuid',filled_by_file='new.mkv'")

        client = _Client()
        usenet_module = types.ModuleType('usenet')
        usenet_module.get_usenet_client = lambda: client
        old_usenet = sys.modules.get('usenet')
        sys.modules['usenet'] = usenet_module
        self.addCleanup(lambda: sys.modules.__setitem__('usenet', old_usenet) if old_usenet else sys.modules.pop('usenet', None))
        old_item = playback._media_item
        old_symlink = playback._symlink_matches
        old_request = playback._mount_request
        playback._media_item = lambda _id: dict(self.connect().execute('SELECT * FROM media_items WHERE id=7').fetchone())
        playback._symlink_matches = lambda *args: True
        calls = []

        def request(path, payload, timeout):
            calls.append((path, payload))
            if path.endswith('/verify'):
                return 200, {'status': 'healthy', 'entry_name': 'Working.Release', 'file_name': 'new.mkv'}, ''
            return 200, {'status': 'removed'}, ''

        playback._mount_request = request
        self.addCleanup(lambda: setattr(playback, '_media_item', old_item))
        self.addCleanup(lambda: setattr(playback, '_symlink_matches', old_symlink))
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))
        plex_module = types.ModuleType('utilities.plex_functions')
        plex_module.plex_update_item = lambda item: True
        old_plex = sys.modules.get('utilities.plex_functions')
        sys.modules['utilities.plex_functions'] = plex_module
        self.addCleanup(lambda: sys.modules.__setitem__('utilities.plex_functions', old_plex) if old_plex else sys.modules.pop('utilities.plex_functions', None))

        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            activity = conn.execute('SELECT * FROM nzb_repair_activity').fetchone()
        self.assertEqual(repair['status'], 'complete')
        self.assertEqual(activity['outcome'], 'replaced')
        self.assertEqual(activity['replacement_nzb_id'], 'new-uuid')
        self.assertEqual([call[0] for call in calls], [
            '/api/repair/replacements/verify', '/api/repair/replacements/ack'])


if __name__ == '__main__':
    unittest.main()
