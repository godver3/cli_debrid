"""Only one Wanted pass runs at a time.

The Request button ran a Wanted pass inside the HTTP request after adding
American Dad! (401 episodes) while the scheduled Wanted tick ran another. Both
batch-moved the same 401 episodes to Scraping, fighting over the DB lock, and
the request didn't return for about five minutes.
"""
import threading
import unittest

import database  # noqa: F401  (app import order; queue_manager first is circular)
from queues.queue_manager import QueueManager


class _SlowWanted:
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def process(self, manager):
        self.calls += 1
        self.started.set()
        self.release.wait(5)


class WantedSinglePassTests(unittest.TestCase):
    def test_second_pass_skips_while_one_runs(self):
        manager = object.__new__(QueueManager)
        manager.paused = False
        wanted = _SlowWanted()
        manager.queues = {"Wanted": wanted}

        first = threading.Thread(target=manager.process_wanted)
        first.start()
        self.assertTrue(wanted.started.wait(5))
        manager.process_wanted()  # returns at once instead of running a second pass
        wanted.release.set()
        first.join(5)
        self.assertEqual(wanted.calls, 1)

        manager.process_wanted()  # the lock is free again afterwards
        self.assertEqual(wanted.calls, 2)


if __name__ == '__main__':
    unittest.main()
