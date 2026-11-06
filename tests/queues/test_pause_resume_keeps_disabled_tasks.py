"""A queue pause/resume must not switch Task Manager-disabled tasks back on.

pause_queue tracked jobs that were already paused (disabled in the Task Manager)
and resume_queue resumed every tracked job, so any connectivity or DB-health
pause turned disabled tasks such as Repair Broken NZBs/Debrids back on.
"""
import unittest
from unittest import mock

import database  # noqa: F401  (app import order; run_program first is circular)
from queues.run_program import ProgramRunner


class _Job:
    def __init__(self, job_id, next_run_time):
        self.id = job_id
        self.next_run_time = next_run_time


class _Scheduler:
    state = 1

    def __init__(self, jobs):
        self.jobs = {j.id: j for j in jobs}

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def pause_job(self, job_id):
        self.jobs[job_id].next_run_time = None

    def resume_job(self, job_id):
        self.jobs[job_id].next_run_time = 'soon'


class PauseResumeKeepsDisabledTasksTest(unittest.TestCase):
    def _runner(self):
        runner = ProgramRunner.__new__(ProgramRunner)
        runner.scheduler = _Scheduler([
            _Job('task_repair_broken_nzbs', None),        # disabled before the pause
            _Job('task_repair_broken_debrids', 'soon'),   # disabled during the pause
            _Job('Wanted', 'soon'),
        ])
        runner.scheduler_lock = mock.MagicMock()
        runner.paused_jobs_by_queue = set()
        runner.task_intervals = {'task_repair_broken_nzbs': 21600, 'task_repair_broken_debrids': 21600, 'Wanted': 5}
        runner.enabled_tasks = {'task_repair_broken_debrids', 'Wanted'}
        runner.queue_processing_map = {'Wanted': 'process_wanted'}
        runner.last_pause_time = None
        runner.last_resume_time = None
        runner.pause_resume_cooldown = 0
        runner.pause_info = {'reason_string': 'test', 'error_type': 'CONNECTION_ERROR'}
        runner.db_health_pause_info = None
        runner.queue_paused = False
        return runner

    def test_disabled_tasks_stay_paused_after_resume(self):
        runner = self._runner()
        with mock.patch('queues.queue_manager.QueueManager'):
            runner.pause_queue()
            runner.disable_task('task_repair_broken_debrids')
            runner.resume_queue()
        jobs = runner.scheduler.jobs
        self.assertIsNone(jobs['task_repair_broken_nzbs'].next_run_time)
        self.assertIsNone(jobs['task_repair_broken_debrids'].next_run_time)
        self.assertIsNotNone(jobs['Wanted'].next_run_time)


if __name__ == '__main__':
    unittest.main()
