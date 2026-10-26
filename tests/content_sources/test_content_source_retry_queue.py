"""Failed content-source removals are queued and retried by task_retry_content_source_removals.

database/content_source_retry.py existed since 0.7.26 but nothing called it: a
delete whose Overseerr/Trakt removal timed out was simply logged, and the next
content-source sync added the item back. remove_from_content_source now queues
the item when a source fails, and the task retries it with backoff.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

SOURCES = {'Overseerr_1': {'type': 'Overseerr', 'enabled': True, 'display_name': 'Overseerr',
                           'media_type': 'All', 'url': 'http://overseerr', 'api_key': 'k'}}
ITEM = {'id': 5, 'imdb_id': 'tt0000005', 'tmdb_id': 55, 'type': 'movie', 'title': 'Movie',
        'content_source': 'Overseerr_1', 'collected_at': datetime(2024, 1, 1)}


def _fake_get_setting(section, key=None, default=None):
    if section == 'Content Sources' and key is None:
        return SOURCES
    return default


class ContentSourceRetryQueueTest(unittest.TestCase):
    def setUp(self):
        import database.content_source_retry as csr
        import utilities.deletion_manager as dm_mod
        self.csr, self.dm_mod = csr, dm_mod
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.remove, self.db)
        for patcher in [
            mock.patch.object(csr, 'get_db_connection', side_effect=lambda: sqlite3.connect(self.db)),
            mock.patch.object(dm_mod, 'get_setting', side_effect=_fake_get_setting),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        csr.create_retry_queue_table()
        self.removal = mock.patch('content_checkers.overseerr.remove_from_overseerr_by_tmdb_id').start()
        self.addCleanup(mock.patch.stopall)

    def _rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute('SELECT imdb_id, retry_count, completed, item_data, last_error '
                                'FROM content_source_removal_retry').fetchall()
        finally:
            conn.close()

    def _make_due(self):
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE content_source_removal_retry SET next_retry_at = ?',
                     (datetime.now() - timedelta(minutes=1),))
        conn.commit()
        conn.close()

    def test_failed_removal_is_queued_then_retried_until_it_succeeds(self):
        self.removal.side_effect = TimeoutError('Overseerr timed out')
        result = self.dm_mod.DeletionManager().remove_from_content_source(dict(ITEM))
        self.assertEqual(result['sources_failed'], ['Overseerr'])

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][2]), ('tt0000005', 0))
        self.assertNotIn('collected_at', rows[0][3])  # datetimes aren't stored

        self.assertEqual(self.csr.process_retry_queue(), 0)  # first retry isn't due yet

        self.removal.side_effect = None
        self.removal.return_value = {'success': True, 'message': 'removed'}
        self._make_due()
        self.assertEqual(self.csr.process_retry_queue(), 1)
        self.assertEqual(self._rows()[0][2], 1)  # completed
        self.assertEqual(self.removal.call_args.kwargs['imdb_id'], 'tt0000005')
        self.assertEqual(len(self._rows()), 1)  # the retry itself didn't queue another row

    def test_gives_up_after_max_retries_instead_of_staying_pending(self):
        self.removal.side_effect = TimeoutError('down')
        self.dm_mod.DeletionManager().remove_from_content_source(dict(ITEM))
        for _ in range(5):
            self._make_due()
            self.assertEqual(self.csr.process_retry_queue(), 1)
        imdb_id, retry_count, completed, _data, last_error = self._rows()[0]
        self.assertEqual(completed, 1)
        self.assertIn('Max retries exceeded', last_error)
        self._make_due()
        self.assertEqual(self.csr.process_retry_queue(), 0)

    def test_requeue_while_pending_does_not_use_up_retries(self):
        self.removal.side_effect = TimeoutError('down')
        for _ in range(6):  # e.g. the same show deleted again before its retry ran
            self.dm_mod.DeletionManager().remove_from_content_source(dict(ITEM))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][1], rows[0][2]), (0, 0))
        self._make_due()
        self.assertEqual(self.csr.process_retry_queue(), 1)  # still picked up

    def test_successful_removal_queues_nothing(self):
        self.removal.return_value = {'success': True, 'message': 'removed'}
        self.dm_mod.DeletionManager().remove_from_content_source(dict(ITEM))
        self.assertEqual(self._rows(), [])


if __name__ == '__main__':
    unittest.main()
