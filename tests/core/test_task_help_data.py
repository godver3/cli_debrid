"""Task Manager info text (static/js/task_help_data.json) covers every scheduled task."""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _scheduled_task_names():
    """Keys of ProgramRunner's base task_intervals dict, read from source (no app import)."""
    with open(os.path.join(ROOT, 'queues', 'run_program.py')) as f:
        src = f.read()
    start = src.index('self.task_intervals = {')
    block = src[start:src.index('\n        }\n', start)]
    return set(re.findall(r"^\s+'([^']+)':", block, re.M))


class TaskHelpDataTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, 'static', 'js', 'task_help_data.json')) as f:
            self.data = json.load(f)

    def test_every_scheduled_task_has_an_entry(self):
        names = _scheduled_task_names()
        self.assertGreater(len(names), 50)
        missing = sorted(names - set(self.data['tasks']))
        self.assertEqual(missing, [], 'add these tasks to task_help_data.json')

    def test_no_entries_for_tasks_that_no_longer_exist(self):
        stale = sorted(set(self.data['tasks']) - _scheduled_task_names())
        self.assertEqual(stale, [])

    def test_entries_are_complete(self):
        entries = dict(self.data['tasks'], content_source=self.data['content_source'])
        for name, entry in entries.items():
            with self.subTest(task=name):
                self.assertTrue(entry.get('description', '').strip())
                self.assertTrue(entry.get('action', '').strip())
                self.assertTrue(entry.get('runs', '').strip())


if __name__ == '__main__':
    unittest.main()
