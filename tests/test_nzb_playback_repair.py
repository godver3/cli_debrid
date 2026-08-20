import json as _json_module
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


class TestSymlinkMatches(unittest.TestCase):
    """_symlink_matches must work for both Symlinked/Local (symlink) and
    Plex mode (real file path reported by Plex's API, never a symlink)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmpdir, ignore_errors=True))
        self.entry_dir = os.path.join(self.tmpdir, 'Show.S01E01.Entry.Name')
        os.makedirs(self.entry_dir)
        self.real_file = os.path.join(self.entry_dir, 'Show.S01E01.mkv')
        with open(self.real_file, 'w') as f:
            f.write('data')

    def test_symlinked_local_mode(self):
        link = os.path.join(self.tmpdir, 'link.mkv')
        os.symlink(self.real_file, link)
        self.assertTrue(playback._symlink_matches(
            {'location_on_disk': link}, 'Show.S01E01.Entry.Name', 'Show.S01E01.mkv'))

    def test_plex_mode_real_file_no_symlink(self):
        # Plex mode: location_on_disk is the real mounted path directly, as
        # reported by Plex's API — never a symlink.
        self.assertTrue(playback._symlink_matches(
            {'location_on_disk': self.real_file}, 'Show.S01E01.Entry.Name', 'Show.S01E01.mkv'))

    def test_plex_mode_wrong_file_does_not_match(self):
        self.assertFalse(playback._symlink_matches(
            {'location_on_disk': self.real_file}, 'Show.S01E01.Entry.Name', 'Different.File.mkv'))

    def test_missing_path_returns_false(self):
        self.assertFalse(playback._symlink_matches(
            {'location_on_disk': os.path.join(self.tmpdir, 'does-not-exist.mkv')},
            'Show.S01E01.Entry.Name', 'Show.S01E01.mkv'))

    def test_empty_location_returns_false(self):
        self.assertFalse(playback._symlink_matches({}, 'Show.S01E01.Entry.Name', 'Show.S01E01.mkv'))


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
        self.assertTrue(playback.has_active_exact_repair(7, 'old-uuid', 'old.mkv'))
        self.assertFalse(playback.has_active_exact_repair(7, 'old-uuid', 'other.mkv'))
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

    def _install_fake_repair_engine(self, func):
        usenet_module = sys.modules.get('usenet') or types.ModuleType('usenet')
        repair_engine_module = types.ModuleType('usenet.repair_engine')
        repair_engine_module.find_and_submit_playback_candidate = func
        usenet_module.repair_engine = repair_engine_module
        old_usenet = sys.modules.get('usenet')
        old_repair_engine = sys.modules.get('usenet.repair_engine')
        sys.modules['usenet'] = usenet_module
        sys.modules['usenet.repair_engine'] = repair_engine_module
        def _restore():
            if old_usenet is not None:
                sys.modules['usenet'] = old_usenet
            else:
                sys.modules.pop('usenet', None)
            if old_repair_engine is not None:
                sys.modules['usenet.repair_engine'] = old_repair_engine
            else:
                sys.modules.pop('usenet.repair_engine', None)
        self.addCleanup(_restore)

    def test_awaiting_candidate_reuses_search_pipeline_on_success(self):
        """A candidate found broken must go back through the same search
        pipeline automatically — status='awaiting_candidate' is not a
        dead end — and a successful re-submission should be rescheduled
        quickly rather than sitting on the claim's 10-minute lease."""
        playback.begin_playback_repair(self.item, self.target)
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['status'], 'awaiting_candidate')

        calls = []

        def fake_retry(repair_row, item):
            calls.append((repair_row['id'], item['id']))
            return 'submitted'

        self._install_fake_repair_engine(fake_retry)
        old_item = playback._media_item
        playback._media_item = lambda _id: dict(self.item)
        self.addCleanup(lambda: setattr(playback, '_media_item', old_item))

        playback.process_pending_playback_repairs()

        self.assertEqual(calls, [(repair['id'], 7)])
        with self.connect() as conn:
            updated = conn.execute('SELECT * FROM nzb_playback_repairs WHERE id=?', (repair['id'],)).fetchone()
        self.assertEqual(updated['last_error'], 'submitted')
        self.assertIsNotNone(updated['next_attempt_at'])
        self.assertIsNone(updated['lease_until'])

    def test_awaiting_candidate_reschedules_when_no_replacement_found(self):
        playback.begin_playback_repair(self.item, self.target)
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()

        self._install_fake_repair_engine(lambda repair_row, item: 'no_replacement')
        old_item = playback._media_item
        playback._media_item = lambda _id: dict(self.item)
        self.addCleanup(lambda: setattr(playback, '_media_item', old_item))

        playback.process_pending_playback_repairs()

        with self.connect() as conn:
            updated = conn.execute('SELECT * FROM nzb_playback_repairs WHERE id=?', (repair['id'],)).fetchone()
        self.assertEqual(updated['status'], 'awaiting_candidate')
        self.assertEqual(updated['last_error'], 'no_replacement')
        self.assertIsNotNone(updated['next_attempt_at'])

    def test_healthy_candidate_cleans_exact_target_then_finalizes_activity(self):
        playback.begin_playback_repair(self.item, self.target)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO nzb_repair_activity
                   (broken_nzb_id,broken_nzb_title,outcome)
                   VALUES ('old-uuid','Old.Release','not_found'),
                          ('unrelated-uuid','Old.Release','not_found')"""
            )
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
            activity = conn.execute(
                'SELECT * FROM nzb_repair_activity WHERE id=?',
                (repair['activity_id'],),
            ).fetchone()
            duplicate = conn.execute(
                """SELECT COUNT(*) FROM nzb_repair_activity
                   WHERE broken_nzb_id='old-uuid' AND broken_nzb_title='Old.Release'
                     AND outcome='not_found'"""
            ).fetchone()[0]
            unrelated = conn.execute(
                """SELECT COUNT(*) FROM nzb_repair_activity
                   WHERE broken_nzb_id='unrelated-uuid' AND outcome='not_found'"""
            ).fetchone()[0]
        self.assertEqual(repair['status'], 'complete')
        self.assertEqual(activity['outcome'], 'replaced')
        self.assertEqual(activity['replacement_nzb_id'], 'new-uuid')
        self.assertEqual(duplicate, 0)
        self.assertEqual(unrelated, 1)
        self.assertEqual([call[0] for call in calls], [
            '/api/repair/replacements/verify', '/api/repair/replacements/ack'])


    def test_undeletable_failed_job_is_abandoned_after_max_attempts(self):
        playback.begin_playback_repair(self.item, self.target)
        playback.set_playback_candidate(7, {'title': 'Candidate.Release', 'guid': 'GUID-2'}, 'bad-uuid', 'Candidate.Release')
        with self.connect() as conn:
            conn.execute("UPDATE media_items SET state='Adding',filled_by_torrent_id='nzb:bad-uuid'")
        self.assertTrue(playback.reject_active_candidate(7, 'bad-uuid'))

        playback.set_playback_candidate(7, {'title': 'Working.Release', 'guid': 'GUID-3'}, 'new-uuid', 'Working.Release')
        with self.connect() as conn:
            conn.execute("UPDATE media_items SET state='Collected',filled_by_torrent_id='nzb:new-uuid',filled_by_file='new.mkv'")
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertIn('bad-uuid', repair['failed_job_ids_json'])

        class _UndeletableJobClient(_Client):
            def remove_nzb_exact(self, info_hash):
                self.deleted.append(info_hash)
                return info_hash != 'bad-uuid'

        client = _UndeletableJobClient()
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

        def request(path, payload, timeout):
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

        for attempt in range(1, playback.FAILED_JOB_MAX_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            self.assertNotEqual(repair['status'], 'complete', f'should not complete after attempt {attempt}')
            self.assertIn('bad-uuid', repair['failed_job_ids_json'])
            self.assertEqual(_json_module.loads(repair['failed_job_attempts_json'])['bad-uuid'], attempt)
            # Simulate the 30s retry schedule elapsing so the next call re-claims it.
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        # The next attempt hits FAILED_JOB_MAX_ATTEMPTS and abandons the job,
        # letting the repair proceed to its own old-file cleanup and finish.
        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['status'], 'complete')
        self.assertEqual(repair['failed_job_ids_json'], '[]')
        self.assertEqual(repair['failed_job_attempts_json'], '{}')

    def test_persistent_stale_target_defers_to_background_cleanup_retry(self):
        """A persistently-stale exact-cleanup ack must not block the repair
        from finalizing as replaced — it should defer the old-file cleanup to
        the background retry instead of blocking or abandoning it outright."""
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

        ack_should_succeed = {'value': False}

        def request(path, payload, timeout):
            if path.endswith('/verify'):
                return 200, {'status': 'healthy', 'entry_name': 'Working.Release', 'file_name': 'new.mkv'}, ''
            if ack_should_succeed['value']:
                return 200, {'status': 'removed'}, ''
            return 409, {'code': 'stale_target'}, ''

        playback._mount_request = request
        self.addCleanup(lambda: setattr(playback, '_media_item', old_item))
        self.addCleanup(lambda: setattr(playback, '_symlink_matches', old_symlink))
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))
        plex_module = types.ModuleType('utilities.plex_functions')
        plex_module.plex_update_item = lambda item: True
        old_plex = sys.modules.get('utilities.plex_functions')
        sys.modules['utilities.plex_functions'] = plex_module
        self.addCleanup(lambda: sys.modules.__setitem__('utilities.plex_functions', old_plex) if old_plex else sys.modules.pop('utilities.plex_functions', None))

        for attempt in range(1, playback.STALE_TARGET_MAX_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            self.assertNotEqual(repair['status'], 'complete', f'should not complete after attempt {attempt}')
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        # The attempt that hits STALE_TARGET_MAX_ATTEMPTS must defer cleanup
        # and finalize the repair as replaced in the SAME pass — not block on
        # or permanently abandon the old-file cleanup.
        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            activity = conn.execute(
                'SELECT * FROM nzb_repair_activity WHERE id=?', (repair['activity_id'],)
            ).fetchone()
        self.assertEqual(repair['status'], 'complete')
        self.assertEqual(activity['outcome'], 'replaced')
        self.assertEqual(repair['cleanup_status'], 'pending')
        self.assertIsNotNone(repair['cleanup_first_pending_at'])
        targets = _json_module.loads(repair['cleanup_targets_json'])
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0]['deferred'])
        self.assertNotEqual(targets[0]['status'], 'complete')

        # Background retry keeps failing the same way — must NOT mark it
        # complete or abandoned yet; just reschedule further out.
        playback.retry_deferred_playback_cleanups()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['cleanup_status'], 'pending')
        self.assertIsNotNone(repair['next_attempt_at'])

        # Once decypharr stops returning stale_target, the background retry
        # (running independently of the fast completion loop) finishes the
        # old-file cleanup without needing the repair to be reopened.
        ack_should_succeed['value'] = True
        with self.connect() as conn:
            conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
            conn.commit()
        playback.retry_deferred_playback_cleanups()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['cleanup_status'], 'complete')
        targets = _json_module.loads(repair['cleanup_targets_json'])
        self.assertEqual(targets[0]['status'], 'complete')

    def test_persistent_generic_cleanup_failure_also_defers_after_max_attempts(self):
        """A persistent non-stale, non-5xx cleanup failure (e.g. an
        unexpected 4xx) must be treated the same as stale_target — capped
        and deferred to the background retry — instead of retrying inline
        forever, since it sits in the same fallback branch."""
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

        def request(path, payload, timeout):
            if path.endswith('/verify'):
                return 200, {'status': 'healthy', 'entry_name': 'Working.Release', 'file_name': 'new.mkv'}, ''
            return 400, {'code': 'bad_request'}, ''

        playback._mount_request = request
        self.addCleanup(lambda: setattr(playback, '_media_item', old_item))
        self.addCleanup(lambda: setattr(playback, '_symlink_matches', old_symlink))
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))
        plex_module = types.ModuleType('utilities.plex_functions')
        plex_module.plex_update_item = lambda item: True
        old_plex = sys.modules.get('utilities.plex_functions')
        sys.modules['utilities.plex_functions'] = plex_module
        self.addCleanup(lambda: sys.modules.__setitem__('utilities.plex_functions', old_plex) if old_plex else sys.modules.pop('utilities.plex_functions', None))

        for attempt in range(1, playback.STALE_TARGET_MAX_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            self.assertNotEqual(repair['status'], 'complete', f'should not complete after attempt {attempt}')
            targets = _json_module.loads(repair['cleanup_targets_json'])
            self.assertEqual(targets[0]['generic_attempts'], attempt)
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['status'], 'complete')
        self.assertEqual(repair['cleanup_status'], 'pending')
        targets = _json_module.loads(repair['cleanup_targets_json'])
        self.assertTrue(targets[0]['deferred'])
        self.assertEqual(targets[0]['last_error'], 'bad_request')

    def test_expired_deferred_cleanup_is_abandoned_not_retried_forever(self):
        playback.begin_playback_repair(self.item, self.target)
        candidate = {'title': 'Working.Release', 'guid': 'GUID-3'}
        playback.set_playback_candidate(7, candidate, 'new-uuid', 'Working.Release')
        with self.connect() as conn:
            conn.execute("UPDATE media_items SET state='Collected',filled_by_torrent_id='nzb:new-uuid',filled_by_file='new.mkv'")
            conn.execute(
                """UPDATE nzb_playback_repairs SET status='complete',cleanup_status='pending',
                   cleanup_first_pending_at=datetime('now','-49 hours'),
                   cleanup_targets_json=?""",
                (_json_module.dumps([{
                    'entry_name': 'Old.Release', 'file_name': 'old.mkv', 'info_hash': 'old-uuid',
                    'cli_debrid_id': 7, 'reason': 'media_probe_failed', 'status': 'pending',
                    'deferred': True, 'stale_attempts': playback.STALE_TARGET_MAX_ATTEMPTS,
                    'background_attempts': 3,
                }]),),
            )
            conn.commit()

        old_request = playback._mount_request
        playback._mount_request = lambda path, payload, timeout: (409, {'code': 'stale_target'}, '')
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))

        playback.retry_deferred_playback_cleanups()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['cleanup_status'], 'abandoned')
        self.assertIsNone(repair['next_attempt_at'])

    def _prep_verifying_candidate(self):
        playback.begin_playback_repair(self.item, self.target)
        candidate = {'title': 'Working.Release', 'guid': 'GUID-3'}
        playback.set_playback_candidate(7, candidate, 'new-uuid', 'Working.Release')
        with self.connect() as conn:
            conn.execute(
                "UPDATE media_items SET state='Collected',filled_by_torrent_id='nzb:new-uuid',filled_by_file='new.mkv'")

        client = _Client()
        usenet_module = types.ModuleType('usenet')
        usenet_module.get_usenet_client = lambda: client
        old_usenet = sys.modules.get('usenet')
        sys.modules['usenet'] = usenet_module
        self.addCleanup(lambda: sys.modules.__setitem__('usenet', old_usenet) if old_usenet else sys.modules.pop('usenet', None))
        old_item = playback._media_item
        playback._media_item = lambda _id: dict(self.connect().execute('SELECT * FROM media_items WHERE id=7').fetchone())
        self.addCleanup(lambda: setattr(playback, '_media_item', old_item))

    def test_verify_stale_target_rejects_candidate_after_max_attempts(self):
        """A verify-stage stale_target that never clears must not loop
        forever — unlike the ack-stage stale_target case, there's no
        confirmed-healthy replacement to protect yet, so the candidate
        itself is rejected and search resumes."""
        self._prep_verifying_candidate()
        old_request = playback._mount_request
        playback._mount_request = lambda path, payload, timeout: (409, {'code': 'stale_target'}, '')
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))

        for attempt in range(1, playback.VERIFY_STALE_TARGET_MAX_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            self.assertNotEqual(repair['status'], 'awaiting_candidate', f'should not reject before attempt {attempt}')
            self.assertEqual(repair['verify_stale_target_attempts'], attempt)
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['status'], 'awaiting_candidate')
        self.assertIsNone(repair['candidate_info_hash'])
        self.assertEqual(repair['last_error'], 'verify_stale_target_max_attempts')
        self.assertEqual(repair['verify_stale_target_attempts'], 0)

    def test_verify_unknown_rejects_candidate_after_max_attempts(self):
        self._prep_verifying_candidate()
        old_request = playback._mount_request
        playback._mount_request = lambda path, payload, timeout: (
            200, {'status': 'unknown', 'reason': 'replacement_not_ready'}, '')
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))

        for attempt in range(1, playback.VERIFY_UNKNOWN_MAX_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            self.assertNotEqual(repair['status'], 'awaiting_candidate', f'should not reject before attempt {attempt}')
            self.assertEqual(repair['verify_unknown_attempts'], attempt)
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['status'], 'awaiting_candidate')
        self.assertIsNone(repair['candidate_info_hash'])
        self.assertEqual(repair['last_error'], 'replacement_not_ready')
        self.assertEqual(repair['verify_unknown_attempts'], 0)

    def test_verify_busy_does_not_reject_within_short_window(self):
        """repair_busy just means decypharr's own sweep is running — a few
        attempts of that must not burn through the same short fuse as a
        genuinely stale candidate."""
        self._prep_verifying_candidate()
        old_request = playback._mount_request
        playback._mount_request = lambda path, payload, timeout: (409, {'code': 'repair_busy'}, '')
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))

        for _ in range(playback.VERIFY_STALE_TARGET_MAX_ATTEMPTS + 5):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertNotEqual(repair['status'], 'awaiting_candidate')
        self.assertIsNotNone(repair['candidate_info_hash'])
        self.assertEqual(repair['verify_busy_attempts'], playback.VERIFY_STALE_TARGET_MAX_ATTEMPTS + 5)

    def test_candidate_search_flags_visibility_then_gives_up_after_days(self):
        """No healthy candidate ever found: stays silent until the
        visibility threshold, then flags itself in the activity log and
        slows down, then eventually stops scheduling after the hard
        give-up window rather than retrying forever."""
        playback.begin_playback_repair(self.item, self.target)
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()

        self._install_fake_repair_engine(lambda repair_row, item: 'no_replacement')
        old_item = playback._media_item
        playback._media_item = lambda _id: dict(self.item)
        self.addCleanup(lambda: setattr(playback, '_media_item', old_item))

        for attempt in range(1, playback.CANDIDATE_SEARCH_VISIBILITY_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()
        with self.connect() as conn:
            updated = conn.execute('SELECT * FROM nzb_playback_repairs WHERE id=?', (repair['id'],)).fetchone()
            activity = conn.execute('SELECT * FROM nzb_repair_activity WHERE id=?', (repair['activity_id'],)).fetchone()
        self.assertIsNone(updated['candidate_search_stuck_since'])
        self.assertEqual(activity['outcome'], 'replacement_pending')

        # The attempt that reaches the visibility threshold must flag the
        # activity log so this isn't silently invisible, without stopping.
        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            updated = conn.execute('SELECT * FROM nzb_playback_repairs WHERE id=?', (repair['id'],)).fetchone()
            activity = conn.execute('SELECT * FROM nzb_repair_activity WHERE id=?', (repair['activity_id'],)).fetchone()
        self.assertIsNotNone(updated['candidate_search_stuck_since'])
        self.assertEqual(activity['outcome'], 'skipped_max_attempts')
        self.assertEqual(updated['status'], 'awaiting_candidate')
        self.assertIsNotNone(updated['next_attempt_at'])

        # Force the stuck-since timestamp far enough in the past to cross
        # the hard give-up threshold, then confirm it stops scheduling.
        with self.connect() as conn:
            conn.execute(
                "UPDATE nzb_playback_repairs SET candidate_search_stuck_since=datetime('now', ?),"
                "next_attempt_at=NULL WHERE id=?",
                (f'-{playback.CANDIDATE_SEARCH_GIVE_UP_DAYS + 1} days', repair['id']),
            )
            conn.commit()
        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            updated = conn.execute('SELECT * FROM nzb_playback_repairs WHERE id=?', (repair['id'],)).fetchone()
            activity = conn.execute('SELECT * FROM nzb_repair_activity WHERE id=?', (repair['activity_id'],)).fetchone()
        self.assertEqual(updated['status'], 'complete')
        self.assertEqual(activity['outcome'], 'abandoned_after_retries')
        self.assertIsNone(updated['next_attempt_at'])

    def test_source_mismatch_rejects_candidate_after_max_attempts_when_reverted(self):
        """If the item is back on the original broken hash (not replaced by
        anything else), a persistent candidate/source mismatch after the cap
        is treated like a bad candidate: reject and resume searching."""
        self._prep_verifying_candidate()
        with self.connect() as conn:
            conn.execute("UPDATE media_items SET filled_by_torrent_id='nzb:old-uuid'")
            conn.commit()

        for attempt in range(1, playback.CANDIDATE_SOURCE_MISMATCH_MAX_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            self.assertNotEqual(repair['status'], 'awaiting_candidate', f'should not reject before attempt {attempt}')
            self.assertEqual(repair['source_mismatch_attempts'], attempt)
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['status'], 'awaiting_candidate')
        self.assertIsNone(repair['candidate_info_hash'])
        self.assertEqual(repair['last_error'], 'candidate_source_changed_max_attempts')
        self.assertEqual(repair['source_mismatch_attempts'], 0)

    def test_source_mismatch_stops_without_cleanup_when_superseded_externally(self):
        """If the item now points at neither our candidate nor the original
        broken hash, something else already replaced it (e.g. the general
        repair engine winning a race) — stop guessing at the NEW candidate,
        but still hand the ORIGINAL broken file's cleanup (known exactly,
        independent of whatever replaced it) to the background retry rather
        than leaving it referenced nowhere."""
        self._prep_verifying_candidate()
        with self.connect() as conn:
            conn.execute(
                """UPDATE media_items SET filled_by_torrent_id='nzb:someone-elses-uuid',
                   filled_by_file='external.mkv',filled_by_title='External.Release',
                   debrid_folder_name='External.Release'"""
            )
            conn.commit()

        for _ in range(playback.CANDIDATE_SOURCE_MISMATCH_MAX_ATTEMPTS - 1):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()

        playback.process_pending_playback_repairs()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
            activity = conn.execute(
                'SELECT * FROM nzb_repair_activity WHERE id=?', (repair['activity_id'],)
            ).fetchone()
        self.assertEqual(repair['status'], 'complete')
        self.assertEqual(repair['last_error'], 'superseded_externally')
        self.assertIsNone(repair['next_attempt_at'])
        self.assertEqual(activity['outcome'], 'replaced')
        self.assertEqual(activity['replacement_nzb_id'], 'someone-elses-uuid')
        self.assertEqual(activity['replacement_title'], 'External.Release')
        # Handed off to the background cleanup retry, same as a normal
        # successful repair — not left dangling with nothing watching it.
        self.assertEqual(repair['cleanup_status'], 'pending')
        self.assertIsNotNone(repair['cleanup_first_pending_at'])
        targets = _json_module.loads(repair['cleanup_targets_json'])
        self.assertEqual(targets[0]['status'], 'pending')

    def test_superseded_externally_cleanup_is_picked_up_by_background_retry(self):
        """The deferred cleanup queued by the superseded_externally path
        must actually be reachable by retry_deferred_playback_cleanups,
        not just flagged and forgotten."""
        self._prep_verifying_candidate()
        with self.connect() as conn:
            conn.execute("UPDATE media_items SET filled_by_torrent_id='nzb:someone-elses-uuid'")
            conn.commit()
        for _ in range(playback.CANDIDATE_SOURCE_MISMATCH_MAX_ATTEMPTS):
            playback.process_pending_playback_repairs()
            with self.connect() as conn:
                conn.execute('UPDATE nzb_playback_repairs SET next_attempt_at=NULL')
                conn.commit()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['last_error'], 'superseded_externally')

        old_request = playback._mount_request
        playback._mount_request = lambda path, payload, timeout: (200, {'status': 'removed'}, '')
        self.addCleanup(lambda: setattr(playback, '_mount_request', old_request))

        playback.retry_deferred_playback_cleanups()
        with self.connect() as conn:
            repair = conn.execute('SELECT * FROM nzb_playback_repairs').fetchone()
        self.assertEqual(repair['cleanup_status'], 'complete')
        targets = _json_module.loads(repair['cleanup_targets_json'])
        self.assertEqual(targets[0]['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
