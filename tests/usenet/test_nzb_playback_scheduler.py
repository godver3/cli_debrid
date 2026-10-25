import ast
import os
import unittest
from tests._source import runner_source, runner_method, runner_tree  # noqa: F401


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestNZBPlaybackScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = runner_tree()

    def _assigned_literal(self, attribute, literal_type):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(target, ast.Attribute) and target.attr == attribute
                   for target in node.targets) and isinstance(node.value, literal_type):
                return node.value
        self.fail(f'No literal assignment found for self.{attribute}')

    def test_completion_task_is_enabled_at_fifteen_seconds(self):
        intervals = self._assigned_literal('task_intervals', ast.Dict)
        values = {
            key.value: value
            for key, value in zip(intervals.keys, intervals.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        enabled = self._assigned_literal('enabled_tasks', ast.Set)
        enabled_names = {
            item.value for item in enabled.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }

        self.assertEqual(
            ast.literal_eval(values['task_nzb_playback_repair_completion']), 15,
        )
        self.assertIn('task_nzb_playback_repair_completion', enabled_names)

        completion = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == 'task_nzb_playback_repair_completion'
        )
        referenced_names = {
            node.id for node in ast.walk(completion) if isinstance(node, ast.Name)
        }
        self.assertIn('process_pending_playback_repairs', referenced_names)
        self.assertNotIn('run_repair', referenced_names)


if __name__ == '__main__':
    unittest.main()
