"""Overseerr sources on one server run together when one of them finds new requests.

Reported by Stormy: Overseerr_1 adds 1080p and Overseerr_2 adds 2160p, both on the same
Overseerr server, each on its own ~4.5 minute timer. Overseerr_2 added a movie, the 2160p
copy was collected within a minute, cli_debrid triggered Overseerr's Plex scan, and the
request left Overseerr's approved list before Overseerr_1's next run. No 1080p item was
ever created.
"""
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import database  # noqa: F401  (app import order; the runner first is circular)
from queues.run_program import ProgramRunner
from tests._source import runner_method

URL = 'http://seerr:5055'


class _Job:
    def __init__(self, next_run_time):
        self.next_run_time = next_run_time

    def modify(self, next_run_time):
        self.next_run_time = next_run_time


class _Scheduler:
    def __init__(self, jobs):
        self.jobs = jobs

    def get_job(self, job_id):
        return self.jobs.get(job_id)


def _source(versions, url=URL, enabled=True):
    return {'type': 'Overseerr', 'url': url, 'enabled': enabled, 'versions': versions}


class SiblingOverseerrRunTests(unittest.TestCase):
    def setUp(self):
        self.later = datetime.now(timezone.utc) + timedelta(minutes=4)
        self.jobs = {
            'task_Overseerr_1_wanted': _Job(self.later),
            'task_Overseerr_3_wanted': _Job(self.later),
            'task_Overseerr_4_wanted': _Job(None),  # disabled in the Task Manager
            'task_Overseerr_5_wanted': _Job(self.later),
        }
        self.runner = ProgramRunner.__new__(ProgramRunner)
        self.runner.scheduler = _Scheduler(self.jobs)
        self.runner.scheduler_lock = threading.RLock()
        self.sources = {
            'Overseerr_1': _source(['1080p']),
            'Overseerr_2': _source(['2160p'], url=URL + '/'),
            'Overseerr_3': _source(['720p'], url='http://other-seerr:5055'),
            'Overseerr_4': _source(['1080p']),
            'Overseerr_5': _source(['1080p'], enabled=False),
        }

    def _run(self, source):
        with mock.patch('queues.runner.content_sources.get_all_settings',
                        return_value={'Content Sources': self.sources}):
            self.runner._run_sibling_overseerr_sources_now(source, self.sources[source])

    def test_same_server_sibling_runs_now(self):
        self._run('Overseerr_2')
        self.assertLess(self.jobs['task_Overseerr_1_wanted'].next_run_time, self.later)

    def test_other_server_disabled_and_paused_sources_untouched(self):
        self._run('Overseerr_2')
        self.assertEqual(self.jobs['task_Overseerr_3_wanted'].next_run_time, self.later)
        self.assertIsNone(self.jobs['task_Overseerr_4_wanted'].next_run_time)
        self.assertEqual(self.jobs['task_Overseerr_5_wanted'].next_run_time, self.later)

    def test_overseerr_run_with_new_requests_calls_it(self):
        method = runner_method('process_content_source')
        self.assertIn("if source_type == 'Overseerr' and items_processed > 0:", method)
        self.assertIn('self._run_sibling_overseerr_sources_now(source, data)', method)


if __name__ == '__main__':
    unittest.main()
