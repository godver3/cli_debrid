"""Scraping and Sleeping share one wake-limit rule (queues/sleeping_queue.resolve_wake_limit)."""
import unittest
from unittest import mock

from queues import sleeping_queue
from queues.sleeping_queue import NEVER_SLEEP, resolve_wake_limit


class ResolveWakeLimitTest(unittest.TestCase):
    def test_positive_version_value_wins_even_above_global(self):
        self.assertEqual(resolve_wake_limit({'wake_count': 48}, 24), 48)
        self.assertEqual(resolve_wake_limit({'wake_count': 3}, 24), 3)

    def test_numeric_string_is_accepted(self):
        self.assertEqual(resolve_wake_limit({'wake_count': '6'}, 24), 6)

    def test_minus_one_never_sleeps(self):
        self.assertEqual(resolve_wake_limit({'wake_count': -1}, 24), NEVER_SLEEP)

    def test_unset_values_fall_back_to_global(self):
        for raw in (None, '', 0, 'None', 'abc'):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_wake_limit({'wake_count': raw}, 24), 24)
        self.assertEqual(resolve_wake_limit({}, 24), 24)
        self.assertEqual(resolve_wake_limit(None, 24), 24)

    def test_legacy_max_wake_count_key_is_ignored(self):
        self.assertEqual(resolve_wake_limit({'max_wake_count': 2}, 24), 24)

    def test_global_limit_defaults_to_schema_default(self):
        with mock.patch.object(sleeping_queue, 'get_setting', return_value=''):
            self.assertEqual(sleeping_queue.get_global_wake_limit(), 24)
        with mock.patch.object(sleeping_queue, 'get_setting', return_value='10'):
            self.assertEqual(sleeping_queue.get_global_wake_limit(), 10)


if __name__ == '__main__':
    unittest.main()
