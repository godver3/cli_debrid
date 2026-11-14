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


class WakeBackoffTest(unittest.TestCase):
    def test_backoff_steps_and_cap(self):
        from datetime import timedelta
        base = timedelta(minutes=30)
        got = [sleeping_queue.sleep_for_wake(base, n) for n in (0, 3, 4, 7, 8, 11, 12, 23, None)]
        self.assertEqual([g.total_seconds() / 60 for g in got],
                         [30, 30, 60, 60, 120, 120, 120, 120, 30])

    def test_process_waits_for_the_backed_off_sleep(self):
        from datetime import datetime, timedelta
        q = sleeping_queue.SleepingQueue()
        q.items = [{'id': 1, 'version': '1080p', 'wake_count': 5},   # 60 min sleep
                   {'id': 2, 'version': '1080p', 'wake_count': 1}]   # 30 min sleep
        asleep_since = datetime.now() - timedelta(minutes=45)
        q.sleeping_queue_times = {1: asleep_since, 2: asleep_since}
        qm = mock.Mock()
        qm.generate_identifier.side_effect = lambda item: str(item['id'])
        with mock.patch.object(sleeping_queue, 'load_config', return_value={}), \
             mock.patch.object(sleeping_queue, '_get_int_setting',
                               side_effect=lambda s, k, default=None: {'wake_limit': 24, 'sleep_duration_minutes': 30}.get(k, default)), \
             mock.patch.object(q, 'wake_items') as wake, \
             mock.patch.object(q, 'blacklist_items'):
            q.process(qm)
        woken = [i['id'] for i in wake.call_args.args[1]]
        self.assertEqual(woken, [2])


if __name__ == '__main__':
    unittest.main()
