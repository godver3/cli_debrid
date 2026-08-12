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


cleanup = _load_cleanup_module()


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
        self.db_patch.stop()
        os.unlink(self.db_path)

    @staticmethod
    def target(**overrides):
        value = {
            'entry_name': 'The Sopranos S03',
            'file_name': 'S03E01.mkv',
            'info_hash': 'old-id',
            'cli_debrid_id': 75299,
            'failure_reason': 'mount_read_error',
            'protocol': 'nzb',
            '_playback_cleanup': True,
        }
        value.update(overrides)
        return value

    def test_normalizes_only_playback_failures_per_file(self):
        entries = [{
            'entry_name': 'pack', 'protocol': 'nzb', 'status': 'broken',
            'broken_files': [
                {'entry_name': 'pack', 'file_name': 'E01.mkv', 'info_hash': 'a',
                 'cli_debrid_id': 1, 'reason': 'mount_read_error'},
                {'entry_name': 'pack', 'file_name': 'E02.mkv', 'info_hash': 'a',
                 'cli_debrid_id': 2, 'reason': 'usenet_segment_missing'},
            ],
        }]
        result = cleanup.split_playback_cleanup_targets(entries, protocol='nzb')
        self.assertEqual(2, len(result))
        exact = next(item for item in result if item.get('_playback_cleanup'))
        legacy = next(item for item in result if not item.get('_playback_cleanup'))
        self.assertEqual(1, exact['cli_debrid_id'])
        self.assertEqual('mount_read_error', exact['failure_reason'])
        self.assertEqual('usenet_segment_missing', legacy['broken_files'][0]['reason'])

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

        with patch.object(cleanup, '_verify_replacement', return_value=('healthy', '', {})), \
                patch.object(cleanup, '_acknowledge', return_value=('blocked', 'stale_target')):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, result['blocked'])
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(target))

        conn = self.connect()
        row = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('blocked', row['status'])

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


if __name__ == '__main__':
    unittest.main()
