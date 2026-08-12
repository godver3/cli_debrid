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

    def test_collected_replacement_acknowledges_and_completes(self):
        conn = self.connect()
        conn.execute(
            "INSERT INTO media_items VALUES (75299, 'Collected', 'nzb:new-id', '', 'Show', 'episode', 3, 1)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(self.target()))

        with patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')), \
                patch.object(cleanup, '_log_cleanup_activity'):
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

        with patch.object(cleanup, '_acknowledge', return_value=('complete', 'removed')), \
                patch.object(cleanup, '_log_cleanup_activity'):
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

        with patch.object(cleanup, '_acknowledge', return_value=('retry', 'endpoint unavailable')):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, result['retried'])
        conn = self.connect()
        row = conn.execute('SELECT status, attempts, next_attempt_at FROM mount_replacement_cleanups').fetchone()
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

        with patch.object(cleanup, '_acknowledge', return_value=('blocked', 'stale_target')), \
                patch.object(cleanup, '_log_cleanup_activity'):
            result = cleanup.process_pending_mount_cleanups(item_id=75299)
        self.assertEqual(1, result['blocked'])
        self.assertTrue(cleanup.queue_mount_replacement_cleanup(target))

        conn = self.connect()
        row = conn.execute('SELECT status FROM mount_replacement_cleanups').fetchone()
        conn.close()
        self.assertEqual('blocked', row['status'])


if __name__ == '__main__':
    unittest.main()
