"""SleepingQueue.update runs every 30 s; it must use the wake_count already on
each row instead of one get_wake_count query per item."""
import unittest
from unittest import mock

from queues.sleeping_queue import SleepingQueue


class SleepingQueueUpdateTest(unittest.TestCase):
    def test_update_uses_row_wake_count(self):
        rows = [{"id": 1, "wake_count": 2}, {"id": 2, "wake_count": None}]
        q = SleepingQueue()
        q.sleeping_queue_times[99] = object()  # stale entry
        with mock.patch("database.get_all_media_items", return_value=rows, create=True), \
             mock.patch("database.get_wake_count", side_effect=AssertionError("per-item query"), create=True):
            q.update()
        self.assertEqual([i["wake_count"] for i in q.items], [2, 0])
        q.clean_up_sleeping_data()
        self.assertEqual(set(q.sleeping_queue_times), {1, 2})


if __name__ == "__main__":
    unittest.main()
