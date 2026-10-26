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


class DatabaseHealthTaskPauseTest(unittest.TestCase):
    """task_check_database_health pauses once, holds the pause, and resumes on a passing check.

    The pause used to pause this task too, and the fail-safe watchdog force-resumed
    every pause after 3 minutes, so a corrupt database gave an hourly 3-minute pause
    and an hourly notification saying a restart would recreate the databases.
    """
    def setUp(self):
        import main
        from queues.runner.maintenance_tasks import MaintenanceTasks
        self.main = main
        runner = MaintenanceTasks()
        runner.queue_paused = False
        runner.pause_info = {}

        def pause():
            runner.queue_paused = True

        def resume():
            runner.queue_paused = False
            runner.pause_info = {}
        runner.pause_queue = mock.Mock(side_effect=pause)
        runner.resume_queue = mock.Mock(side_effect=resume)
        self.runner = runner
        self.notify = mock.patch('routes.notifications.send_program_crash_notification').start()
        self.addCleanup(mock.patch.stopall)

    def _run(self, healthy):
        with mock.patch.object(self.main, 'verify_database_health', return_value=healthy):
            self.runner.task_check_database_health()

    def test_pauses_and_notifies_once_then_resumes_when_healthy(self):
        self._run(False)
        self._run(False)
        self.assertEqual(self.runner.pause_queue.call_count, 1)
        self.assertEqual(self.notify.call_count, 1)
        self.assertNotIn('recreate', self.notify.call_args.args[0])
        self.runner.resume_queue.assert_not_called()

        self._run(True)
        self.runner.resume_queue.assert_called_once()
        self.assertFalse(self.runner.queue_paused)

    def test_passing_check_leaves_other_pauses_alone(self):
        self.runner.queue_paused = True
        self.runner.pause_info = {'error_type': 'CONNECTION_ERROR'}
        self._run(True)
        self.runner.resume_queue.assert_not_called()

    def test_watchdog_does_not_force_resume_a_db_health_pause(self):
        from queues.run_program import ProgramRunner
        fake = mock.Mock(queue_paused=True, pause_info={'error_type': 'DB_HEALTH'},
                         connectivity_failure_time=None)
        ProgramRunner._fail_safe_resume_if_stuck(fake)
        fake.resume_queue.assert_not_called()
        self.assertIsNone(fake.connectivity_failure_time)


if __name__ == '__main__':
    unittest.main()
