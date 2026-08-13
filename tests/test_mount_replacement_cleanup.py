import importlib.util
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def _load_cleanup_module():
    database_pkg = types.ModuleType('database')
    database_pkg.__path__ = []
    core = types.ModuleType('database.core')
    core.get_db_connection = lambda: None
    sys.modules['database'] = database_pkg
    sys.modules['database.core'] = core
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'database', 'mount_replacement_cleanup.py')
    spec = importlib.util.spec_from_file_location('mount_cleanup_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_activity_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'database', 'nzb_repair_activity.py')
    spec = importlib.util.spec_from_file_location('nzb_activity_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sync_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'usenet', 'climount_sync.py')
    spec = importlib.util.spec_from_file_location('climount_sync_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup = _load_cleanup_module()
activity = _load_activity_module()
climount_sync = _load_sync_module()


class MountReplacementCleanupTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

        def connect():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        self.connect = connect
        self.db_patch = patch.object(cleanup, 'get_db_connection', side_effect=connect)
        self.db_patch.start()
        self.registration_patch = patch.object(
            cleanup, '_ensure_candidate_registration', return_value=True
        )
        self.registration_patch.start()
        self.cleanup_registration_patch = patch.object(
            cleanup, '_ensure_cleanup_registration', return_value=('ready', '')
        )
        self.cleanup_registration_patch.start()
        cleanup.create_mount_replacement_cleanup_table()
        conn = connect()
        conn.execute("""CREATE TABLE media_items (
            id INTEGER PRIMARY KEY, state TEXT, filled_by_torrent_id TEXT,
            filled_by_magnet TEXT, title TEXT, type TEXT,
            season_number INTEGER, episode_number INTEGER
        )""")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.cleanup_registration_patch.stop()
        self.registration_patch.stop()
        self.db_patch.stop()
        os.unlink(self.db_path)

    @staticmethod
    def target(**overrides):
        value = {
            'entry_name': 'The Sopranos S03',
            'file_name': 'S03E01.mkv',
            'info_hash': 'old-id',
            'cli_debrid_id': 75299,
            'failure_reason': 'media_probe_failed',
            'protocol': 'nzb',
            '_playback_cleanup': True,
        }
        value.update(overrides)
        return value

    def create_stale_cleanup_fixture_schema(self):
        conn = self.connect()
        conn.execute('ALTER TABLE media_items ADD COLUMN filled_by_file TEXT')
        conn.execute('ALTER TABLE media_items ADD COLUMN filled_by_title TEXT')
        conn.execute('ALTER TABLE media_items ADD COLUMN location_on_disk TEXT')
        conn.execute('ALTER TABLE media_items ADD COLUMN nzb_segment_id TEXT')
        conn.execute("""CREATE TABLE nzb_repair_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER, title TEXT, media_type TEXT,
            season_number INTEGER, episode_number INTEGER,
            broken_nzb_id TEXT, broken_nzb_title TEXT,
            replacement_nzb_id TEXT, replacement_title TEXT,
            outcome TEXT NOT NULL, triggered_by TEXT,
            repair_attempts INTEGER DEFAULT 0,
            last_repair_at TIMESTAMP, next_repair_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        conn.close()

    @staticmethod
    def pitt_health_entry(**overrides):
        broken = {
            'entry_name': 'The.Pitt.2025.S01E07.1.00.P.M.DUSKLiGHT',
            'file_name': 'The.Pitt.2025.S01E07.1.00.P.M.DUSKLiGHT.mkv',
            'info_hash': 'old-pitt-id',
            'protocol': 'nzb',
            'cli_debrid_id': 9278,
            'reason': 'usenet_segment_missing',
        }
        broken.update(overrides)
        return {
            'entry_name': broken['entry_name'], 'protocol': 'nzb',
            'status': 'broken', 'failure_reason': broken['reason'],
            'broken_files': [broken],
        }

    def seed_deleted_pitt_item_and_replacement(self, duplicate_activity=True):
        conn = self.connect()
        conn.execute(
            """INSERT INTO media_items
               (id, state, filled_by_torrent_id, filled_by_magnet, title, type,
                season_number, episode_number, filled_by_file, filled_by_title,
                location_on_disk)
               VALUES (19702, 'Collected', 'nzb:new-pitt-id', '', 'The Pitt',
                       'episode', 1, 7, 'The.Pitt.S01E07.FLUX.mkv',
                       'The.Pitt.S01E07.FLUX', '/library/The Pitt/S01E07.mkv')"""
        )
        conn.execute(
            """INSERT INTO nzb_repair_activity
               (item_id, title, media_type, season_number, episode_number,
                broken_nzb_id, broken_nzb_title, outcome)
               VALUES (9278, 'The Pitt', 'episode', 1, 7, 'old-pitt-id',
                       'The.Pitt.2025.S01E07.1.00.P.M.DUSKLiGHT', 'not_found')"""
        )
        if duplicate_activity:
            conn.execute(
                """INSERT INTO nzb_repair_activity
                   (item_id, title, media_type, season_number, episode_number,
                    broken_nzb_id, broken_nzb_title, outcome)
                   VALUES (NULL, NULL, NULL, NULL, NULL,
                           'The.Pitt.2025.S01E07.1.00.P.M.DUSKLiGHT',
                           'The.Pitt.2025.S01E07.1.00.P.M.DUSKLiGHT', 'not_found')"""
            )
        conn.commit()
        conn.close()

    def test_adopts_deleted_old_item_and_collapses_exact_not_found_duplicates(self):
        self.create_stale_cleanup_fixture_schema()
        self.seed_deleted_pitt_item_and_replacement()

        remaining = cleanup.adopt_stale_replaced_nzbs([self.pitt_health_entry()])

        self.assertEqual([], remaining)
        conn = self.connect()
        saga = conn.execute('SELECT * FROM mount_replacement_sagas').fetchone()
        target = conn.execute('SELECT * FROM mount_replacement_cleanups').fetchone()
        rows = conn.execute('SELECT * FROM nzb_repair_activity ORDER BY id').fetchall()
        conn.close()
        self.assertEqual(19702, saga['cli_debrid_id'])
        self.assertEqual('stale_cleanup', saga['saga_kind'])
        self.assertEqual('new-pitt-id', saga['candidate_info_hash'])
        self.assertEqual(9278, target['cli_debrid_id'])
        self.assertEqual('old-pitt-id', target['old_info_hash'])
        self.assertEqual(1, len(rows))
        self.assertEqual('stale_cleanup_pending', rows[0]['outcome'])
        self.assertEqual(19702, rows[0]['item_id'])

    def test_stale_cleanup_verifies_new_nzb_acks_old_identity_and_refreshes(self):
        self.create_stale_cleanup_fixture_schema()
        self.seed_deleted_pitt_item_and_replacement()
        cleanup.adopt_stale_replaced_nzbs([self.pitt_health_entry()])

        with patch.object(activity, 'get_db_connection', side_effect=self.connect), \
                patch.dict(sys.modules, {'database.nzb_repair_activity': activity}), \
                patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})) as verify, \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')) as acknowledge, \
                patch.object(cleanup, '_refresh_verified_plex_item', return_value=True) as refresh:
            result = cleanup.process_pending_mount_cleanups(item_id=19702)

        verify.assert_called_once_with(19702, 'new-pitt-id')
        acknowledged = acknowledge.call_args.args[0]
        self.assertEqual(9278, acknowledged['cli_debrid_id'])
        self.assertEqual('old-pitt-id', acknowledged['old_info_hash'])
        refresh.assert_called_once()
        self.assertEqual(1, result['completed'])
        conn = self.connect()
        saga = conn.execute('SELECT * FROM mount_replacement_sagas').fetchone()
        target = conn.execute('SELECT * FROM mount_replacement_cleanups').fetchone()
        row = conn.execute('SELECT * FROM nzb_repair_activity').fetchone()
        conn.close()
        self.assertEqual('complete', saga['status'])
        self.assertEqual('complete', saga['plex_refresh_status'])
        self.assertEqual('complete', target['status'])
        self.assertEqual('stale_entry_cleaned', row['outcome'])
        self.assertEqual('new-pitt-id', row['replacement_nzb_id'])

    def test_stale_cleanup_retries_only_plex_after_mount_removal(self):
        self.create_stale_cleanup_fixture_schema()
        self.seed_deleted_pitt_item_and_replacement(duplicate_activity=False)
        cleanup.adopt_stale_replaced_nzbs([self.pitt_health_entry()])

        with patch.object(activity, 'get_db_connection', side_effect=self.connect), \
                patch.dict(sys.modules, {'database.nzb_repair_activity': activity}), \
                patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})) as verify, \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')) as acknowledge, \
                patch.object(cleanup, '_refresh_verified_plex_item', return_value=False):
            cleanup.process_pending_mount_cleanups(item_id=19702)
        conn = self.connect()
        conn.execute('UPDATE mount_replacement_sagas SET next_attempt_at=NULL')
        conn.commit()
        conn.close()
        with patch.object(activity, 'get_db_connection', side_effect=self.connect), \
                patch.dict(sys.modules, {'database.nzb_repair_activity': activity}), \
                patch.object(cleanup, '_verify_replacement') as second_verify, \
                patch.object(cleanup, '_acknowledge') as second_ack, \
                patch.object(cleanup, '_refresh_verified_plex_item', return_value=True):
            cleanup.process_pending_mount_cleanups(item_id=19702)

        verify.assert_called_once()
        acknowledge.assert_called_once()
        second_verify.assert_not_called()
        second_ack.assert_not_called()
        conn = self.connect()
        saga = conn.execute('SELECT status, plex_refresh_status FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual('complete', saga['status'])
        self.assertEqual('complete', saga['plex_refresh_status'])

    def test_ambiguous_historical_replacement_is_unresolved_without_cleanup(self):
        self.create_stale_cleanup_fixture_schema()
        self.seed_deleted_pitt_item_and_replacement(duplicate_activity=False)
        conn = self.connect()
        conn.execute(
            """INSERT INTO media_items
               (id, state, filled_by_torrent_id, filled_by_magnet, title, type,
                season_number, episode_number, filled_by_file)
               VALUES (29702, 'Collected', 'nzb:other-pitt-id', '', 'The Pitt',
                       'episode', 1, 7, 'The.Pitt.S01E07.Other.mkv')"""
        )
        conn.commit()
        conn.close()

        remaining = cleanup.adopt_stale_replaced_nzbs([self.pitt_health_entry()])

        self.assertEqual([], remaining)
        conn = self.connect()
        saga_count = conn.execute('SELECT COUNT(*) FROM mount_replacement_sagas').fetchone()[0]
        cleanup_count = conn.execute('SELECT COUNT(*) FROM mount_replacement_cleanups').fetchone()[0]
        row = conn.execute('SELECT outcome FROM nzb_repair_activity').fetchone()
        conn.close()
        self.assertEqual(0, saga_count)
        self.assertEqual(0, cleanup_count)
        self.assertEqual('stale_entry_unresolved', row['outcome'])

    def test_genuinely_unmatched_broken_entry_remains_for_not_found_handling(self):
        self.create_stale_cleanup_fixture_schema()
        entry = self.pitt_health_entry(cli_debrid_id=99999)
        self.assertEqual([entry], cleanup.adopt_stale_replaced_nzbs([entry]))

    def test_current_old_uuid_is_not_adopted_as_stale(self):
        self.create_stale_cleanup_fixture_schema()
        conn = self.connect()
        conn.execute(
            """INSERT INTO media_items
               (id, state, filled_by_torrent_id, filled_by_magnet, title, type,
                season_number, episode_number)
               VALUES (9278, 'Collected', 'nzb:old-pitt-id', '', 'The Pitt',
                       'episode', 1, 7)"""
        )
        conn.commit()
        conn.close()
        entry = self.pitt_health_entry()
        self.assertEqual([entry], cleanup.adopt_stale_replaced_nzbs([entry]))

    def test_broken_current_replacement_never_acknowledges_old_file(self):
        self.create_stale_cleanup_fixture_schema()
        self.seed_deleted_pitt_item_and_replacement(duplicate_activity=False)
        cleanup.adopt_stale_replaced_nzbs([self.pitt_health_entry()])
        with patch.object(activity, 'get_db_connection', side_effect=self.connect), \
                patch.dict(sys.modules, {'database.nzb_repair_activity': activity}), \
                patch.object(cleanup, '_verify_replacement', return_value=(
                    'broken', 'usenet_segment_missing', {})), \
                patch.object(cleanup, '_acknowledge') as acknowledge, \
                patch.object(cleanup, '_refresh_verified_plex_item') as refresh:
            result = cleanup.process_pending_mount_cleanups(item_id=19702)
        acknowledge.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(1, result['probe_failed'])
        conn = self.connect()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        target = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('pending', saga['status'])
        self.assertEqual('pending', target['status'])

    def test_normalizes_only_playback_failures_per_file(self):
        entries = [{
            'entry_name': 'pack', 'protocol': 'nzb', 'status': 'broken',
            'broken_files': [
                {'entry_name': 'pack', 'file_name': 'E01.mkv', 'info_hash': 'a',
                 'cli_debrid_id': 1, 'reason': 'mount_read_error'},
                {'entry_name': 'pack', 'file_name': 'E02.mkv', 'info_hash': 'a',
                 'cli_debrid_id': 2, 'reason': 'media_probe_failed'},
                {'entry_name': 'pack', 'file_name': 'E03.mkv', 'info_hash': 'a',
                 'cli_debrid_id': 3, 'reason': 'usenet_segment_missing'},
            ],
        }]
        result = cleanup.split_playback_cleanup_targets(entries, protocol='nzb')
        self.assertEqual(2, len(result))
        exact = next(item for item in result if item.get('_playback_cleanup'))
        legacy = next(item for item in result if not item.get('_playback_cleanup'))
        self.assertEqual(2, exact['cli_debrid_id'])
        self.assertEqual('media_probe_failed', exact['failure_reason'])
        self.assertEqual('usenet_segment_missing', legacy['broken_files'][0]['reason'])
        self.assertNotIn('mount_read_error', {
            item.get('failure_reason') for item in result
        })

    def test_migrates_legacy_cleanup_table_before_creating_saga_index(self):
        conn = self.connect()
        conn.execute('DROP TABLE mount_replacement_cleanups')
        conn.execute('DROP TABLE mount_replacement_sagas')
        conn.execute("""CREATE TABLE mount_replacement_cleanups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cli_debrid_id INTEGER NOT NULL,
            protocol TEXT NOT NULL,
            entry_name TEXT NOT NULL,
            file_name TEXT NOT NULL,
            old_info_hash TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMP,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            UNIQUE(cli_debrid_id, old_info_hash, file_name)
        )""")
        conn.execute(
            """INSERT INTO mount_replacement_cleanups
               (cli_debrid_id, protocol, entry_name, file_name, old_info_hash, reason)
               VALUES (75299, 'nzb', 'Legacy Show', 'S01E01.mkv', 'old-id', 'mount_read_error')"""
        )
        conn.commit()
        conn.close()

        cleanup.create_mount_replacement_cleanup_table()

        conn = self.connect()
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(mount_replacement_cleanups)')}
        row = conn.execute('SELECT saga_id FROM mount_replacement_cleanups').fetchone()
        indexes = {row['name'] for row in conn.execute("PRAGMA index_list('mount_replacement_cleanups')")}
        conn.close()
        self.assertIn('saga_id', columns)
        self.assertIsNotNone(row['saga_id'])
        self.assertIn('idx_mount_cleanup_saga', indexes)

    def test_startup_reconciles_only_completed_saga_pending_activity(self):
        conn = self.connect()
        conn.execute("""CREATE TABLE nzb_repair_activity (
            id INTEGER PRIMARY KEY, replacement_nzb_id TEXT,
            replacement_title TEXT, outcome TEXT, updated_at TIMESTAMP
        )""")
        conn.executemany(
            "INSERT INTO nzb_repair_activity (id, outcome) VALUES (?, ?)",
            [(1, 'replacement_pending'), (2, 'replaced'),
             (3, 'replacement_pending'), (4, 'replacement_pending'),
             (5, 'replacement_pending')],
        )
        conn.executemany(
            """INSERT INTO mount_replacement_sagas
               (cli_debrid_id, protocol, status, activity_id,
                candidate_info_hash, candidate_title, completed_at)
               VALUES (?, 'nzb', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            [
                (1, 'complete', 1, 'working-id', 'Working.Release'),
                (2, 'complete', 2, 'historical-id', 'Historical.Release'),
                (3, 'pending', 3, 'pending-id', 'Pending.Release'),
                (5, 'complete', 5, None, 'Missing.Hash'),
            ],
        )
        conn.commit()
        conn.close()

        cleanup.create_mount_replacement_cleanup_table()

        conn = self.connect()
        rows = {
            row['id']: dict(row) for row in conn.execute(
                'SELECT * FROM nzb_repair_activity ORDER BY id'
            ).fetchall()
        }
        conn.close()
        self.assertEqual('replaced', rows[1]['outcome'])
        self.assertEqual('working-id', rows[1]['replacement_nzb_id'])
        self.assertEqual('Working.Release', rows[1]['replacement_title'])
        self.assertEqual('replaced', rows[2]['outcome'])
        self.assertIsNone(rows[2]['replacement_nzb_id'])
        self.assertEqual('replacement_pending', rows[3]['outcome'])
        self.assertEqual('replacement_pending', rows[4]['outcome'])
        self.assertEqual('replacement_pending', rows[5]['outcome'])

    def test_activity_update_can_join_callers_transaction(self):
        conn = self.connect()
        conn.execute("""CREATE TABLE nzb_repair_activity (
            id INTEGER PRIMARY KEY, replacement_nzb_id TEXT,
            replacement_title TEXT, outcome TEXT, repair_attempts INTEGER,
            last_repair_at TIMESTAMP, next_repair_at TIMESTAMP,
            triggered_by TEXT, updated_at TIMESTAMP
        )""")
        conn.execute(
            "INSERT INTO nzb_repair_activity (id, outcome) VALUES (44, 'replacement_pending')"
        )
        conn.commit()

        self.assertTrue(activity.update_repair_activity(
            44, connection=conn, replacement_nzb_id='working-id',
            replacement_title='Working.Release', outcome='replaced',
        ))
        observer = self.connect()
        self.assertEqual(
            'replacement_pending',
            observer.execute(
                'SELECT outcome FROM nzb_repair_activity WHERE id=44'
            ).fetchone()['outcome'],
        )
        observer.close()
        conn.commit()
        conn.close()
        observer = self.connect()
        row = observer.execute(
            'SELECT * FROM nzb_repair_activity WHERE id=44'
        ).fetchone()
        observer.close()
        self.assertEqual('replaced', row['outcome'])
        self.assertEqual('working-id', row['replacement_nzb_id'])
        self.assertEqual('Working.Release', row['replacement_title'])

    def test_pending_activity_exposes_current_saga_retry_reason(self):
        conn = self.connect()
        conn.execute("""CREATE TABLE nzb_repair_activity (
            id INTEGER PRIMARY KEY, broken_nzb_id TEXT, outcome TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute(
            "INSERT INTO nzb_repair_activity (id, broken_nzb_id, outcome) "
            "VALUES (44, 'old-id', 'replacement_pending')"
        )
        conn.execute(
            """INSERT INTO mount_replacement_sagas
               (cli_debrid_id, protocol, activity_id, last_error)
               VALUES (75299, 'nzb', 44, 'replacement_not_ready')"""
        )
        conn.commit()
        conn.close()
        with patch.object(activity, 'get_db_connection', side_effect=self.connect):
            rows, total = activity.get_repair_activity(source='usenet')
        self.assertEqual(1, total)
        self.assertEqual('replacement_not_ready', rows[0]['replacement_status_detail'])

    def test_collected_replacement_acknowledges_and_completes(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))

        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, result['completed'])
        conn = self.connect()
        row = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('complete', row['status'])

    def test_does_not_acknowledge_before_source_changes(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:old-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))

        with patch.object(cleanup, '_acknowledge') as acknowledge:
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_not_called()
        self.assertEqual(1, result['waiting'])

    def test_torrent_replacement_uses_new_magnet_hash(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75300, 'Collected', 'provider-new', "
            "'magnet:?xt=urn:btih:NEWHASH', 'Movie', 'movie', NULL, NULL)"
        )
        conn.commit()
        conn.close()
        target = self.target(
            cli_debrid_id=75300, protocol='torrent', info_hash='oldhash',
            entry_name='Movie', file_name='Movie.mkv',
        )
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(target))

        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')):
            result = cleanup.process_pending_mount_cleanups(item_id=75300)
        self.assertEqual(1, result['completed'])

    def test_missing_cli_mount_endpoint_stays_pending(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))

        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('retry', 'endpoint unavailable')):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, result['retried'])
        conn = self.connect()
        row = conn.execute('SELECT status, attempts, next_attempt_at FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual('pending', row['status'])
        self.assertEqual(1, row['attempts'])
        self.assertIsNotNone(row['next_attempt_at'])

    def test_stale_target_is_terminal_and_not_requeued(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        target = self.target()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(target))

        conn = self.connect()
        conn.execute('UPDATE mount_replacement_sagas SET last_reconciled_at=CURRENT_TIMESTAMP')
        conn.commit()
        conn.close()
        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('blocked', 'stale_target')):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, result['blocked'])
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(target))

        conn = self.connect()
        row = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('blocked', row['status'])

    def test_mount_request_preserves_structured_conflict_body(self):
        response = types.SimpleNamespace(
            status_code=409, content=b'{}',
            json=lambda: {'code': 'repair_busy', 'message': 'scan active'},
        )
        error = RuntimeError('409 conflict')
        error.response = response
        api_module = types.ModuleType('routes.api_tracker')
        api_module.api = types.SimpleNamespace(post=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
        settings_module = types.ModuleType('utilities.settings')
        settings_module.get_setting = lambda *_args, **kwargs: 'http://mount' if _args[1] == 'url' else ''
        with patch.dict(sys.modules, {
            'routes.api_tracker': api_module,
            'utilities.settings': settings_module,
        }):
            actual, body, request_error = cleanup._mount_request('/verify', {}, 10)
        self.assertIs(response, actual)
        self.assertEqual('repair_busy', body['code'])
        self.assertIsNone(request_error)

    def test_repair_busy_uses_short_retry_without_incrementing_attempts(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        with patch.object(
                cleanup, '_verify_replacement',
                return_value=('retry', 'scan active', {'code': 'repair_busy'})):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, result['retried'])
        conn = self.connect()
        saga = conn.execute('SELECT attempts, next_attempt_at FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual(0, saga['attempts'])
        self.assertIsNotNone(saga['next_attempt_at'])

    def test_registration_failure_does_not_probe(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        with patch.object(cleanup, '_ensure_candidate_registration', return_value=False), \
                patch.object(cleanup, '_verify_replacement') as verify:
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        verify.assert_not_called()
        self.assertEqual(1, result['retried'])
        conn = self.connect()
        saga = conn.execute('SELECT attempts, last_error FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual(0, saga['attempts'])
        self.assertEqual('replacement_not_ready', saga['last_error'])

    def test_queue_missing_candidate_registers_exact_stored_file(self):
        calls = []
        client = types.SimpleNamespace(
            is_enabled=lambda: True,
            get_exact_job=lambda _job_id: {'status': 'missing', 'entry': None},
            register_cli_ids_with_status=lambda job_id, ids: calls.append((job_id, ids)) or (True, False),
        )
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: client
        self.registration_patch.stop()
        try:
            with patch.dict(sys.modules, {'usenet.climount_client': client_module}):
                registered = cleanup._ensure_candidate_registration(
                    19702, 'new-pitt-id', {
                        'id': 19702, 'type': 'episode', 'season_number': 1,
                        'episode_number': 7, 'filled_by_file': 'The.Pitt.S01E07.FLUX.mkv',
                    },
                )
        finally:
            self.registration_patch.start()
        self.assertTrue(registered)
        self.assertEqual([
            ('new-pitt-id', {'The.Pitt.S01E07.FLUX.mkv': 19702}),
        ], calls)

    def test_registered_candidate_restores_source_overwritten_by_old_target(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:old-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        self.assertTrue(cleanup.set_mount_replacement_candidate(75299, 'working-id', 'Working.Release'))
        client = types.SimpleNamespace(
            is_enabled=lambda: True,
            get_exact_job=lambda _job_id: {
                'status': 'ready',
                'entry': {'files': {'Show.S03E01.mkv': {'size': 100}}},
            },
            register_cli_ids=lambda _job_id, _ids: True,
        )
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: client
        with patch.dict(sys.modules, {'usenet.climount_client': client_module}), \
                patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')):
            cleanup.process_pending_mount_cleanups(item_id=75299)
        conn = self.connect()
        item = conn.execute('SELECT filled_by_torrent_id FROM media_items WHERE id=75299').fetchone()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual('nzb:working-id', item['filled_by_torrent_id'])
        self.assertEqual('complete', saga['status'])

    def test_exact_candidate_file_selection_rejects_ambiguous_episode_pack(self):
        item = {'type': 'episode', 'season_number': 3, 'episode_number': 1}
        self.assertEqual(
            'Show.S03E01.mkv',
            cleanup._select_candidate_file(item, {'files': {
                'one': {'name': 'Show.S03E01.mkv', 'size': 100},
                'two': {'name': 'Show.S03E02.mkv', 'size': 100},
            }}),
        )
        self.assertEqual('', cleanup._select_candidate_file(item, {'files': {
            'one': {'name': 'unknown-one.mkv', 'size': 100},
            'two': {'name': 'unknown-two.mkv', 'size': 90},
        }}))

    def test_unavailable_old_saga_does_not_block_later_ready_saga(self):
        conn = self.connect()
        conn.executemany(
            "INSERT INTO media_items VALUES (?, 'Collected', ?, '', 'Show', 'episode', 3, ?)",
            [(75299, 'nzb:old-id', 1), (75300, 'nzb:new-two', 2)],
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        self.assertTrue(cleanup.set_mount_replacement_candidate(75299, 'new-one', 'First'))
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target(
            cli_debrid_id=75300, info_hash='old-two', file_name='S03E02.mkv')))
        self.assertTrue(cleanup.set_mount_replacement_candidate(75300, 'new-two', 'Second'))

        def recover(saga, *_args):
            self.assertEqual(75299, saga['cli_debrid_id'])
            return False, 'replacement_not_ready: exact candidate missing'

        with patch.object(cleanup, '_recover_recorded_candidate', side_effect=recover), \
                patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})) as verify, \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')), \
                patch.object(cleanup, '_refresh_verified_plex_item'):
            result = cleanup.process_pending_mount_cleanups()
        verify.assert_called_once_with(75300, 'new-two')
        self.assertGreaterEqual(result['retried'], 1)
        conn = self.connect()
        rows = conn.execute(
            'SELECT cli_debrid_id, status, attempts FROM mount_replacement_sagas ORDER BY cli_debrid_id'
        ).fetchall()
        conn.close()
        self.assertEqual('pending', rows[0]['status'])
        self.assertEqual(0, rows[0]['attempts'])
        self.assertEqual('complete', rows[1]['status'])

    def test_targeted_restore_does_not_overwrite_concurrent_source_change(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:old-id', '', "
            "'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        self.assertTrue(cleanup.set_mount_replacement_candidate(75299, 'working-id', 'Working'))
        conn = self.connect()
        saga = conn.execute('SELECT * FROM mount_replacement_sagas').fetchone()
        item = dict(conn.execute('SELECT * FROM media_items WHERE id=75299').fetchone())
        conn.close()

        def register(*_args):
            update = self.connect()
            update.execute(
                "UPDATE media_items SET filled_by_torrent_id='nzb:other-id' WHERE id=75299"
            )
            update.commit()
            update.close()
            return True

        client = types.SimpleNamespace(
            is_enabled=lambda: True,
            get_exact_job=lambda _job_id: {
                'status': 'ready',
                'entry': {'files': {'Show.S03E01.mkv': {'size': 100}}},
            },
            register_cli_ids=register,
        )
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: client
        with patch.dict(sys.modules, {'usenet.climount_client': client_module}):
            recovered, detail = cleanup._recover_recorded_candidate(
                saga, item, {'old-id'},
            )
        self.assertFalse(recovered)
        self.assertIn('source changed', detail)
        conn = self.connect()
        source = conn.execute(
            'SELECT filled_by_torrent_id FROM media_items WHERE id=75299'
        ).fetchone()['filled_by_torrent_id']
        conn.close()
        self.assertEqual('nzb:other-id', source)

    def test_duplicate_processor_is_single_flight(self):
        cleanup._PROCESS_LOCK.acquire()
        try:
            result = cleanup.process_pending_mount_cleanups()
        finally:
            cleanup._PROCESS_LOCK.release()
        self.assertEqual(1, result['busy'])

    def test_missing_media_row_is_abandoned_without_cleanup(self):
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        with patch.object(cleanup, '_acknowledge') as acknowledge, \
                patch.object(cleanup, '_refresh_verified_plex_item') as refresh:
            cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_not_called()
        refresh.assert_not_called()
        conn = self.connect()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual('abandoned', saga['status'])

    def test_collected_torrent_cleans_old_nzb_then_refreshes_plex(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'torrent-provider', "
            "'magnet:?xt=urn:btih:ABCDEF', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        with patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')) as acknowledge, \
                patch.object(cleanup, '_refresh_verified_plex_item') as refresh:
            cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_called_once()
        refresh.assert_called_once()
        conn = self.connect()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        target = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('superseded', saga['status'])
        self.assertEqual('complete', target['status'])

    def test_uncollected_torrent_does_not_cleanup_or_refresh(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Adding', 'torrent-provider', "
            "'magnet:?xt=urn:btih:ABCDEF', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        with patch.object(cleanup, '_acknowledge') as acknowledge, \
                patch.object(cleanup, '_refresh_verified_plex_item') as refresh:
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(1, result['waiting'])

    def test_failed_submission_without_job_id_does_not_block_completion(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        self.assertTrue(cleanup.record_mount_replacement_attempt(
            75299, title='failed.before.uuid', status='failed_submission', reason='error'
        ))
        usenet_module = types.ModuleType('usenet')
        usenet_module.get_usenet_client = lambda: types.SimpleNamespace()
        with patch.dict(sys.modules, {'usenet': usenet_module}), \
                patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')):
            cleanup.process_pending_mount_cleanups(item_id=75299)
        conn = self.connect()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        attempt = conn.execute('SELECT cleaned_at FROM mount_replacement_attempts').fetchone()
        conn.close()
        self.assertEqual('complete', saga['status'])
        self.assertIsNotNone(attempt['cleaned_at'])

    def test_sync_guard_blocks_only_old_source_for_active_saga(self):
        conn = self.connect()
        conn.execute("INSERT INTO mount_replacement_sagas (cli_debrid_id, protocol) VALUES (75299, 'nzb')")
        saga_id = conn.execute('SELECT id FROM mount_replacement_sagas').fetchone()['id']
        conn.execute(
            """INSERT INTO mount_replacement_cleanups
               (saga_id, cli_debrid_id, protocol, entry_name, file_name, old_info_hash, reason)
               VALUES (?, 75299, 'nzb', 'Show', 'E01.mkv', 'old-id', 'media_probe_failed')""",
            (saga_id,),
        )
        conn.commit()
        self.assertTrue(climount_sync._is_active_saga_old_source(
            conn, 75299, {'protocol': 'nzb', 'info_hash': 'old-id'}
        ))
        self.assertFalse(climount_sync._is_active_saga_old_source(
            conn, 75299, {'protocol': 'nzb', 'info_hash': 'new-id'}
        ))
        self.assertFalse(climount_sync._is_active_saga_old_source(
            conn, 75299, {'protocol': 'torrent', 'info_hash': 'old-id'}
        ))
        conn.close()

    def test_sync_uses_exact_old_cleanup_filename_not_current_candidate_filename(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', "
            "'Show', 'episode', 3, 1)"
        )
        conn.execute("ALTER TABLE media_items ADD COLUMN filled_by_file TEXT")
        conn.execute("UPDATE media_items SET filled_by_file='new-candidate.mkv' WHERE id=75299")
        conn.execute("INSERT INTO mount_replacement_sagas (cli_debrid_id, protocol) VALUES (75299, 'nzb')")
        saga_id = conn.execute('SELECT id FROM mount_replacement_sagas').fetchone()['id']
        conn.execute(
            """INSERT INTO mount_replacement_cleanups
               (saga_id, cli_debrid_id, protocol, entry_name, file_name, old_info_hash, reason)
               VALUES (?, 75299, 'nzb', 'Old Show', 'old-broken.mkv', 'old-id', 'media_probe_failed')""",
            (saga_id,),
        )
        conn.commit()
        ids = climount_sync._exact_cleanup_registration_ids(
            conn, [75299], {'protocol': 'nzb', 'info_hash': 'old-id'},
        )
        conn.close()
        self.assertEqual({'old-broken.mkv': 75299}, ids)

    def test_exact_registration_replaces_only_same_item_stale_filename(self):
        entry = {
            'cli_debrid_ids': {
                'stale-new-name.mkv': 75299,
                'healthy-sibling.mkv': 75300,
            },
        }
        self.assertEqual({
            'old-broken.mkv': 75299,
            'healthy-sibling.mkv': 75300,
        }, cleanup._exact_registration_map(entry, 'old-broken.mkv', 75299))

    def test_source_registration_preserves_healthy_pack_siblings(self):
        conn = self.connect()
        conn.execute("ALTER TABLE media_items ADD COLUMN filled_by_file TEXT")
        conn.executemany(
            """INSERT INTO media_items
               (id, state, filled_by_torrent_id, filled_by_magnet, title, type,
                season_number, episode_number, filled_by_file)
               VALUES (?, 'Collected', ?, '', 'Show', 'episode', 3, ?, ?)""",
            [
                (75299, 'nzb:new-id', 1, 'new-candidate.mkv'),
                (75300, 'nzb:old-id', 2, 'healthy-sibling.mkv'),
            ],
        )
        conn.execute(
            "INSERT INTO mount_replacement_sagas (cli_debrid_id, protocol) VALUES (75299, 'nzb')"
        )
        saga_id = conn.execute('SELECT id FROM mount_replacement_sagas').fetchone()['id']
        conn.execute(
            """INSERT INTO mount_replacement_cleanups
               (saga_id, cli_debrid_id, protocol, entry_name, file_name, old_info_hash, reason)
               VALUES (?, 75299, 'nzb', 'Old Show', 'old-broken.mkv', 'old-id', 'media_probe_failed')""",
            (saga_id,),
        )
        conn.commit()
        conn.close()

        self.assertEqual({
            'old-broken.mkv': 75299,
            'healthy-sibling.mkv': 75300,
        }, cleanup._registration_map_for_source(
            'old-id', 'old-broken.mkv', 75299,
            {'cli_debrid_ids': {'wrong-new-name.mkv': 75299}},
        ))

    def test_missing_queue_cleanup_still_attempts_storage_registration(self):
        client = types.SimpleNamespace(
            is_enabled=lambda: True,
            get_exact_job=lambda _job_id: {'status': 'missing', 'entry': None},
            register_cli_ids=lambda job_id, ids: (
                self.assertEqual('old-id', job_id) or
                self.assertEqual({'S03E01.mkv': 75299}, ids) or True
            ),
        )
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: client
        self.cleanup_registration_patch.stop()
        try:
            with patch.dict(sys.modules, {'usenet.climount_client': client_module}):
                status, message = cleanup._ensure_cleanup_registration({
                    **self.target(), 'old_info_hash': 'old-id',
                })
        finally:
            self.cleanup_registration_patch.start()
        self.assertEqual('ready', status)
        self.assertEqual('', message)

    def test_truly_removed_cleanup_reaches_acknowledgement_after_patch_404(self):
        calls = []
        client = types.SimpleNamespace(
            is_enabled=lambda: True,
            get_exact_job=lambda _job_id: {'status': 'missing', 'entry': None},
            register_cli_ids_with_status=lambda job_id, ids: (
                calls.append((job_id, ids)) or (False, True)
            ),
        )
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: client
        self.cleanup_registration_patch.stop()
        try:
            with patch.dict(sys.modules, {'usenet.climount_client': client_module}):
                status, _message = cleanup._ensure_cleanup_registration({
                    **self.target(), 'old_info_hash': 'old-id',
                })
        finally:
            self.cleanup_registration_patch.start()
        self.assertEqual('ready', status)
        self.assertEqual(1, len(calls))

    def test_missing_queue_transient_registration_failure_retries_before_ack(self):
        client = types.SimpleNamespace(
            is_enabled=lambda: True,
            get_exact_job=lambda _job_id: {'status': 'missing', 'entry': None},
            register_cli_ids_with_status=lambda _job_id, _ids: (False, False),
        )
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: client
        self.cleanup_registration_patch.stop()
        try:
            with patch.dict(sys.modules, {'usenet.climount_client': client_module}):
                status, message = cleanup._ensure_cleanup_registration({
                    **self.target(), 'old_info_hash': 'old-id',
                })
        finally:
            self.cleanup_registration_patch.start()
        self.assertEqual('retry', status)
        self.assertIn('registration failed', message)

    def test_ambiguous_cleanup_target_does_not_attempt_registration(self):
        client = types.SimpleNamespace(
            is_enabled=lambda: True,
            get_exact_job=lambda _job_id: {'status': 'ambiguous', 'entry': None},
            register_cli_ids=lambda *_args: self.fail('ambiguous target must not be registered'),
        )
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: client
        self.cleanup_registration_patch.stop()
        try:
            with patch.dict(sys.modules, {'usenet.climount_client': client_module}):
                status, message = cleanup._ensure_cleanup_registration({
                    **self.target(), 'old_info_hash': 'old-id',
                })
        finally:
            self.cleanup_registration_patch.start()
        self.assertEqual('retry', status)
        self.assertIn('ambiguous', message)

    def test_startup_requeues_only_known_registration_conflicts(self):
        conn = self.connect()
        conn.execute("""CREATE TABLE nzb_repair_activity (
            id INTEGER PRIMARY KEY, replacement_nzb_id TEXT,
            replacement_title TEXT, outcome TEXT, updated_at TIMESTAMP
        )""")
        conn.executemany(
            "INSERT INTO nzb_repair_activity (id, outcome) VALUES (?, 'replacement_cleanup_stale')",
            [(1,), (2,)],
        )
        conn.executemany(
            """INSERT INTO mount_replacement_sagas
               (cli_debrid_id, protocol, status, activity_id, candidate_info_hash, last_error)
               VALUES (?, 'nzb', 'blocked', ?, 'new-id', ?)""",
            [
                (1, 1, 'stale_target: cli_debrid_id does not match the mounted file registration'),
                (2, 2, 'stale_target: provider entry is missing while the mounted file is still active'),
            ],
        )
        saga_ids = [row['id'] for row in conn.execute(
            'SELECT id FROM mount_replacement_sagas ORDER BY cli_debrid_id'
        ).fetchall()]
        conn.executemany(
            """INSERT INTO mount_replacement_cleanups
               (saga_id, cli_debrid_id, protocol, entry_name, file_name, old_info_hash,
                reason, status, last_error)
               VALUES (?, ?, 'nzb', 'Old', 'old.mkv', 'old-id', 'media_probe_failed',
                       'blocked', ?)""",
            [
                (saga_ids[0], 1, 'stale_target: cli_debrid_id does not match the mounted file registration'),
                (saga_ids[1], 2, 'stale_target: provider entry is missing while the mounted file is still active'),
            ],
        )
        conn.commit()
        conn.close()

        cleanup.create_mount_replacement_cleanup_table()

        conn = self.connect()
        sagas = conn.execute(
            'SELECT cli_debrid_id, status FROM mount_replacement_sagas ORDER BY cli_debrid_id'
        ).fetchall()
        activities = conn.execute(
            'SELECT id, outcome FROM nzb_repair_activity ORDER BY id'
        ).fetchall()
        conn.close()
        self.assertEqual('pending', sagas[0]['status'])
        self.assertEqual('blocked', sagas[1]['status'])
        self.assertEqual('replacement_pending', activities[0]['outcome'])
        self.assertEqual('replacement_cleanup_stale', activities[1]['outcome'])

    def test_failed_candidate_is_not_acknowledged(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:first-bad', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))

        with patch.object(cleanup, '_verify_replacement', return_value=('broken', 'media_probe_failed', {})), \
                patch.object(cleanup, '_acknowledge') as acknowledge:
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_not_called()
        self.assertEqual(1, result['probe_failed'])
        conn = self.connect()
        saga = conn.execute('SELECT status, candidate_info_hash FROM mount_replacement_sagas').fetchone()
        target = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('probe_failed', saga['status'])
        self.assertEqual('first-bad', saga['candidate_info_hash'])
        self.assertEqual('pending', target['status'])

    def test_second_healthy_candidate_cleans_original_and_failed_candidate(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:first-bad', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        with patch.object(cleanup, '_verify_replacement', return_value=('broken', 'media_probe_failed', {})):
            cleanup.process_pending_mount_cleanups(item_id=75299)

        failed_target = self.target(entry_name='First candidate', file_name='S03E01.bad.mkv',
                                    info_hash='first-bad', failure_reason='media_probe_failed')
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(failed_target))
        conn = self.connect()
        conn.execute("UPDATE media_items SET filled_by_torrent_id='nzb:second-good' WHERE id=75299")
        conn.commit()
        conn.close()

        acknowledged = []
        def acknowledge(row):
            acknowledged.append(row['old_info_hash'])
            return 'complete', 'removed'

        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', side_effect=acknowledge):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual({'old-id', 'first-bad'}, set(acknowledged))
        self.assertEqual(2, result['completed'])
        conn = self.connect()
        saga = conn.execute('SELECT status, candidate_info_hash FROM mount_replacement_sagas').fetchone()
        statuses = [r[0] for r in conn.execute('SELECT status FROM mount_replacement_cleanups').fetchall()]
        conn.close()
        self.assertEqual('complete', saga['status'])
        self.assertEqual('second-good', saga['candidate_info_hash'])
        self.assertEqual(['complete', 'complete'], statuses)

    def test_healthy_candidate_cleans_failed_jobs_and_files_before_plex_refresh(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:working-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        self.assertTrue(cleanup.record_mount_replacement_attempt(
            75299, job_id='failed-job-id', title='first.failed',
            status='failed_submission', reason='terminal queue state',
        ))

        events = []
        client = types.SimpleNamespace(
            remove_nzb_exact=lambda job_id: events.append(('remove_job', job_id)) or True
        )
        usenet_module = types.ModuleType('usenet')
        usenet_module.get_usenet_client = lambda: client

        def acknowledge(row):
            events.append(('ack_file', row['old_info_hash']))
            return 'complete', 'removed'

        with patch.dict(sys.modules, {'usenet': usenet_module}), \
                patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', side_effect=acknowledge), \
                patch.object(cleanup, '_refresh_verified_plex_item',
                             side_effect=lambda _item: events.append(('plex_refresh', 'working-id'))):
            cleanup.process_pending_mount_cleanups(item_id=75299)

        self.assertEqual([
            ('remove_job', 'failed-job-id'),
            ('ack_file', 'old-id'),
            ('plex_refresh', 'working-id'),
        ], events)
        conn = self.connect()
        attempt = conn.execute(
            "SELECT cleaned_at FROM mount_replacement_attempts WHERE job_id='failed-job-id'"
        ).fetchone()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertIsNotNone(attempt['cleaned_at'])
        self.assertEqual('complete', saga['status'])

    def test_unknown_verification_keeps_everything_pending(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        with patch.object(cleanup, '_verify_replacement',
                          return_value=('unknown', 'replacement_not_ready', {})), \
                patch.object(cleanup, '_acknowledge') as acknowledge, \
                patch.object(cleanup, '_refresh_verified_plex_item') as refresh:
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(1, result['retried'])
        conn = self.connect()
        target = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual('pending', target['status'])
        self.assertEqual('pending', saga['status'])

    def test_activity_failure_rolls_back_finalization_and_retries_without_recleanup(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
        conn = self.connect()
        conn.execute('UPDATE mount_replacement_sagas SET activity_id=44')
        conn.commit()
        conn.close()

        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')) as acknowledge, \
                patch.object(cleanup, '_update_activity', return_value=False), \
                patch.object(cleanup, '_refresh_verified_plex_item') as refresh:
            first = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, acknowledge.call_count)
        self.assertEqual(1, first['retried'])
        refresh.assert_not_called()
        conn = self.connect()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        target = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('pending', saga['status'])
        self.assertEqual('complete', target['status'])

        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge') as acknowledge, \
                patch.object(cleanup, '_update_activity', return_value=True), \
                patch.object(cleanup, '_refresh_verified_plex_item') as refresh:
            cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_not_called()
        refresh.assert_called_once()
        conn = self.connect()
        saga = conn.execute('SELECT status FROM mount_replacement_sagas').fetchone()
        conn.close()
        self.assertEqual('complete', saga['status'])

    def test_source_change_during_probe_discards_result(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:first', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))

        def verify(*_args):
            conn = self.connect()
            conn.execute("UPDATE media_items SET filled_by_torrent_id='nzb:second' WHERE id=75299")
            conn.commit()
            conn.close()
            return 'healthy', '', {}

        with patch.object(cleanup, '_verify_replacement', side_effect=verify), \
                patch.object(cleanup, '_acknowledge') as acknowledge:
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        acknowledge.assert_not_called()
        self.assertEqual(1, result['waiting'])

    def test_one_activity_row_is_updated_across_candidates(self):
        activity_calls = []
        activity_module = types.ModuleType('database.nzb_repair_activity')
        activity_module.log_repair_activity = lambda **kwargs: activity_calls.append(('create', kwargs)) or 44
        activity_module.update_repair_activity = lambda activity_id, **kwargs: activity_calls.append(
            ('update', activity_id, kwargs)) or True
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:first-bad', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()

        with patch.dict(sys.modules, {'database.nzb_repair_activity': activity_module}):
            self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))
            with patch.object(cleanup, '_verify_replacement', return_value=('broken', 'media_probe_failed', {})):
                cleanup.process_pending_mount_cleanups(item_id=75299)
            self.assertTrue(cleanup.queue_mount_replacement_cleanup(
                self.target(entry_name='First candidate', file_name='bad.mkv',
                            info_hash='first-bad', failure_reason='media_probe_failed')))
            conn = self.connect()
            conn.execute("UPDATE media_items SET filled_by_torrent_id='nzb:second-good' WHERE id=75299")
            conn.commit()
            conn.close()
            with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                    patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')):
                cleanup.process_pending_mount_cleanups(item_id=75299)

        creates = [call for call in activity_calls if call[0] == 'create']
        updates = [call for call in activity_calls if call[0] == 'update']
        self.assertEqual(1, len(creates))
        self.assertTrue(all(call[1] == 44 for call in updates))
        self.assertTrue(all(call[2].get('replacement_nzb_id') is None for call in updates[:-1]))
        self.assertTrue(all(call[2].get('replacement_title') is None for call in updates[:-1]))
        self.assertEqual('replaced', updates[-1][2].get('outcome'))


class ExactCliMountJobLookupTests(unittest.TestCase):
    @staticmethod
    def client_and_module():
        routes_pkg = types.ModuleType('routes')
        routes_pkg.__path__ = []
        tracker = types.ModuleType('routes.api_tracker')
        tracker.api = types.SimpleNamespace(get=None)
        utilities_pkg = types.ModuleType('utilities')
        utilities_pkg.__path__ = []
        settings = types.ModuleType('utilities.settings')
        settings.get_setting = lambda *_args, **_kwargs: {}
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'usenet', 'climount_client.py')
        spec = importlib.util.spec_from_file_location('exact_climount_client_under_test', path)
        climount_client = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            'routes': routes_pkg,
            'routes.api_tracker': tracker,
            'utilities': utilities_pkg,
            'utilities.settings': settings,
        }):
            spec.loader.exec_module(climount_client)
        client = object.__new__(climount_client.CliMountClient)
        client.enabled = True
        client.base_url = 'http://mount'
        client.api_token = ''
        return client, climount_client

    def test_requires_exact_uuid_match(self):
        client, module = self.client_and_module()
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {'torrents': [{
                'info_hash': 'different-id', 'status': 'downloaded',
                'is_complete': True,
            }]},
        )
        with patch.object(module.api, 'get', return_value=response):
            self.assertEqual('missing', client.get_exact_job('wanted-id')['status'])

    def test_rejects_failed_and_ambiguous_exact_jobs(self):
        client, module = self.client_and_module()
        failed = types.SimpleNamespace(
            status_code=200,
            json=lambda: {'torrents': [{
                'info_hash': 'wanted-id', 'state': 'error', 'status': 'downloaded',
            }]},
        )
        with patch.object(module.api, 'get', return_value=failed):
            self.assertEqual('failed', client.get_exact_job('wanted-id')['status'])
        ambiguous = types.SimpleNamespace(
            status_code=200,
            json=lambda: {'torrents': [
                {'info_hash': 'wanted-id', 'status': 'downloaded'},
                {'info_hash': 'WANTED-ID', 'status': 'downloaded'},
            ]},
        )
        with patch.object(module.api, 'get', return_value=ambiguous):
            self.assertEqual('ambiguous', client.get_exact_job('wanted-id')['status'])

    def test_accepts_completed_exact_job_with_files(self):
        client, module = self.client_and_module()
        entry = {
            'info_hash': 'wanted-id', 'state': 'pausedUP', 'status': 'downloaded',
            'is_complete': True, 'files': {'Episode.mkv': {'size': 100}},
        }
        response = types.SimpleNamespace(
            status_code=200, json=lambda: {'torrents': [entry]},
        )
        with patch.object(module.api, 'get', return_value=response):
            result = client.get_exact_job('wanted-id')
        self.assertEqual('ready', result['status'])
        self.assertIs(entry, result['entry'])


if __name__ == '__main__':
    unittest.main()
