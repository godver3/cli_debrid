"""main.verify_database_health — what the hourly "Check Database Health" task runs.

It had been stubbed to `return True`, so the task never checked anything. The old
body deleted a database that failed its check; a locked database raises
sqlite3.OperationalError (a DatabaseError), so re-enabling it as-is would have
deleted the live media_items.db whenever the check ran during a busy write.
The check is now read-only and treats a locked database as inconclusive.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class VerifyDatabaseHealthTest(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.dir = tempfile.mkdtemp(prefix='db_health_')
        patcher = mock.patch.dict(os.environ, {'USER_DB_CONTENT': self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('media_items.db', 'cli_battery.db'):
            conn = sqlite3.connect(os.path.join(self.dir, name))
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
            conn.commit()
            conn.close()

    def _path(self, name):
        return os.path.join(self.dir, name)

    def test_healthy_databases_pass(self):
        self.assertTrue(self.main.verify_database_health())

    def test_corrupt_database_fails_and_is_left_untouched(self):
        garbage = b'this is not a sqlite database' * 200
        with open(self._path('media_items.db'), 'wb') as f:
            f.write(garbage)
        self.assertFalse(self.main.verify_database_health())
        with open(self._path('media_items.db'), 'rb') as f:
            self.assertEqual(f.read(), garbage)
        self.assertEqual(sorted(os.listdir(self.dir)), ['cli_battery.db', 'media_items.db'])

    def test_locked_database_is_inconclusive_not_corrupt(self):
        real_connect = sqlite3.connect

        class _Locked:
            def execute(self, *_a):
                raise sqlite3.OperationalError('database is locked')

            def close(self):
                pass

        def connect(path, *a, **kw):
            return _Locked() if 'media_items.db' in str(path) else real_connect(path, *a, **kw)

        with mock.patch.object(self.main.sqlite3, 'connect', side_effect=connect):
            self.assertTrue(self.main.verify_database_health())
        self.assertTrue(os.path.exists(self._path('media_items.db')))

    def test_startup_skips_the_check(self):
        with open(self._path('media_items.db'), 'wb') as f:
            f.write(b'garbage' * 100)
        self.assertTrue(self.main.verify_database_health(startup=True))


if __name__ == '__main__':
    unittest.main()
