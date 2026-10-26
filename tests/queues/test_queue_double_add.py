"""Moving an item to Sleeping or Checking lists it once.

move_to_sleeping and move_to_checking used to add_item again after
_move_item_to_queue had already added it, so the Queues page showed the item
twice (until the next 30 s DB refresh) and Sleeping sent its notification twice.
"""
import logging
import unittest
from datetime import datetime, timedelta
from unittest import mock

import database  # noqa: F401  (app import order; queue_manager first is circular)
from queues import queue_manager as qm_module
from queues.checking_queue import CheckingQueue
from queues.queue_manager import QueueManager
from queues.sleeping_queue import SleepingQueue


def _episode(state):
    return {
        'id': 27627, 'type': 'episode', 'title': 'Marshals', 'imdb_id': 'tt36849871',
        'season_number': 2, 'episode_number': 4, 'version': '1080p', 'state': state,
    }


class _Queue:
    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)

    def remove_item(self, item):
        self.items = [i for i in self.items if i['id'] != item['id']]


def _manager(**queues):
    # Bypass the singleton so the test does not build every real queue.
    manager = object.__new__(QueueManager)
    manager.queues = queues
    manager.queue_timer = mock.Mock(get_item_timing=mock.Mock(return_value={}))
    manager.item_tracker = logging.getLogger('item_tracker_test')
    return manager


def _move(new_state, **fields):
    return mock.patch.object(qm_module, 'update_media_item_state',
                             side_effect=lambda item_id, state, **kw: {**_episode(new_state), **fields, **kw})


class MoveToSleepingTest(unittest.TestCase):
    def setUp(self):
        self.sleeping = SleepingQueue()
        self.adding = _Queue()
        self.adding.add_item(_episode('Adding'))
        self.manager = _manager(Adding=self.adding, Sleeping=self.sleeping)
        enabled = mock.Mock(json={'success': True, 'enabled_notifications': {'x': {}}})
        for target, kwargs in (
            ('database.get_wake_count', {'return_value': 3, 'create': True}),
            ('routes.settings_routes.get_enabled_notifications_for_category', {'return_value': enabled}),
        ):
            mock.patch(target, **kwargs).start()
        self.send_notifications = mock.patch('routes.notifications.send_notifications').start()
        self.addCleanup(mock.patch.stopall)

    def test_listed_once_with_wake_count_and_one_notification(self):
        with _move('Sleeping'):
            self.manager.move_to_sleeping(self.adding.items[0], 'Adding')

        self.assertEqual([i['id'] for i in self.sleeping.items], [27627])
        self.assertEqual(self.sleeping.items[0]['wake_count'], 3)
        self.assertIn(27627, self.sleeping.sleeping_queue_times)
        self.assertEqual(self.adding.items, [])
        self.assertEqual(self.send_notifications.call_count, 1)

    def test_still_wakes_to_wanted_after_sleep_duration(self):
        with _move('Sleeping'):
            self.manager.move_to_sleeping(self.adding.items[0], 'Adding')
        self.sleeping.sleeping_queue_times[27627] = datetime.now() - timedelta(minutes=31)

        manager = mock.Mock(generate_identifier=QueueManager.generate_identifier)
        with mock.patch('queues.sleeping_queue.load_config', return_value={}), \
             mock.patch('queues.sleeping_queue.get_global_wake_limit', return_value=24), \
             mock.patch('queues.sleeping_queue._get_int_setting', return_value=30), \
             mock.patch('database.increment_wake_count', return_value=4, create=True):
            self.sleeping.process(manager)

        manager.move_to_wanted.assert_called_once()
        self.assertEqual(manager.move_to_wanted.call_args.args[0]['id'], 27627)
        manager.initiate_final_check_or_blacklist.assert_not_called()

    def test_add_item_replaces_an_existing_entry_without_notifying_again(self):
        self.sleeping.add_item(_episode('Sleeping'))
        self.sleeping.add_item(_episode('Sleeping'))

        self.assertEqual([i['id'] for i in self.sleeping.items], [27627])
        self.assertEqual(self.send_notifications.call_count, 1)


class MoveToCheckingTest(unittest.TestCase):
    def test_listed_once_and_notified_once_as_downloading(self):
        checking = CheckingQueue.__new__(CheckingQueue)
        checking.items = []
        checking.checking_queue_times = {}
        adding = _Queue()
        adding.add_item({**_episode('Adding'), 'downloading': True})
        manager = _manager(Adding=adding, Checking=checking)
        enabled = mock.Mock(json={'success': True, 'enabled_notifications': {'x': {}}})

        with _move('Checking'), \
             mock.patch('database.database_reading.get_media_item_by_id', return_value={'upgrading': False}), \
             mock.patch('routes.settings_routes.get_enabled_notifications_for_category', return_value=enabled), \
             mock.patch('routes.notifications.send_notifications') as send_notifications:
            manager.move_to_checking(adding.items[0], 'Adding', 'Marshals.S02E04.1080p', 'magnet:?', 'f.mkv')

        self.assertEqual([i['id'] for i in checking.items], [27627])
        self.assertTrue(checking.items[0]['downloading'])
        self.assertIn(27627, checking.checking_queue_times)
        self.assertEqual(send_notifications.call_count, 1)
        self.assertEqual(send_notifications.call_args.args[0][0]['new_state'], 'Downloading')


if __name__ == '__main__':
    unittest.main()
