import unittest
from unittest.mock import patch, MagicMock
from queue_manager import QueueManager
from datetime import datetime

class TestQueueManager(unittest.TestCase):

    def setUp(self):
        self.queue_manager = QueueManager()
        # Mock all queue objects
        for queue_name in self.queue_manager.queues:
            self.queue_manager.queues[queue_name] = MagicMock()

    def test_singleton_instance(self):
        another_instance = QueueManager()
        self.assertIs(self.queue_manager, another_instance)

    def test_initialize(self):
        expected_queues = ["Wanted", "Scraping", "Adding", "Checking", "Sleeping", "Unreleased", "Blacklisted"]
        self.assertEqual(set(self.queue_manager.queues.keys()), set(expected_queues))

    @patch('queue_manager.logging')
    def test_update_all_queues(self, mock_logging):
        for queue in self.queue_manager.queues.values():
            queue.update = MagicMock()
        
        self.queue_manager.update_all_queues()
        
        for queue in self.queue_manager.queues.values():
            queue.update.assert_called_once()
        mock_logging.debug.assert_called_once_with("Updating all queues")

    def test_get_queue_contents(self):
        for queue in self.queue_manager.queues.values():
            queue.get_contents = MagicMock(return_value=[])
        
        contents = self.queue_manager.get_queue_contents()
        
        self.assertEqual(set(contents.keys()), set(self.queue_manager.queues.keys()))
        for queue in self.queue_manager.queues.values():
            queue.get_contents.assert_called_once()

    def test_generate_identifier(self):
        movie_item = {
            'type': 'movie',
            'title': 'Test Movie',
            'imdb_id': 'tt1234567',
            'version': '1.0'
        }
        episode_item = {
            'type': 'episode',
            'title': 'Test Show',
            'imdb_id': 'tt7654321',
            'season_number': 1,
            'episode_number': 5,
            'version': '2.0'
        }
        
        movie_identifier = QueueManager.generate_identifier(movie_item)
        episode_identifier = QueueManager.generate_identifier(episode_item)
        
        self.assertEqual(movie_identifier, "movie_Test Movie_tt1234567_1.0")
        self.assertEqual(episode_identifier, "episode_Test Show_tt7654321_S01E05_2.0")

    @patch('queue_manager.update_media_item_state')
    @patch('queue_manager.get_media_item_by_id')
    @patch('queue_manager.wake_count_manager.get_wake_count')
    def test_move_to_wanted(self, mock_get_wake_count, mock_get_media_item, mock_update_state):
        item = {'id': 1, 'type': 'movie', 'title': 'Test Movie'}
        mock_get_wake_count.return_value = 0
        mock_get_media_item.return_value = item
        
        # Mock the add_item and remove_item methods
        self.queue_manager.queues["Wanted"].add_item = MagicMock()
        self.queue_manager.queues["Checking"].remove_item = MagicMock()
        
        self.queue_manager.move_to_wanted(item, "Checking")
        
        mock_update_state.assert_called_once_with(1, 'Wanted', filled_by_title=None, filled_by_magnet=None)
        self.queue_manager.queues["Wanted"].add_item.assert_called_once_with(item)
        self.queue_manager.queues["Checking"].remove_item.assert_called_once_with(item)

    # Add more test methods for other QueueManager methods...

if __name__ == '__main__':
    unittest.main()