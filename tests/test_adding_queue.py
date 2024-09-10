import unittest
from unittest.mock import patch, MagicMock, call
from queues.adding_queue import AddingQueue
import logging

logging.basicConfig(level=logging.DEBUG)

class TestAddingQueue(unittest.TestCase):

    def setUp(self):
        self.queue_manager = MagicMock()
        self.adding_queue = AddingQueue()
        self.mock_item = {
            "id": "123",
            "title": "The Matrix",
            "year": "1999",
            "type": "movie",
            "version": "1080p",
            "imdb_id": "tt0133093",
        }

    def generate_mock_releases(self, count=10):
        releases = []
        for i in range(count):
            is_cached = i % 2 == 1  # Alternating cached/uncached
            release = {
                "title": f"The Matrix 1999 {1080 + i*10}p {'BluRay' if is_cached else 'WEB-DL'} x264",
                "magnet": f"magnet:?xt=urn:btih:{i*10:x}{'0'*30}",
                "seeders": 100 - i*10,
            }
            releases.append(release)
        return releases

    @patch('queues.adding_queue.is_cached_on_rd')
    @patch('queues.adding_queue.add_to_real_debrid')
    @patch('queues.adding_queue.get_setting')
    @patch('queues.adding_queue.extract_hash_from_magnet')
    @patch('queues.adding_queue.is_magnet_not_wanted')
    @patch('queues.adding_queue.update_media_item_state')
    @patch('queues.adding_queue.get_active_downloads')
    def test_process_item_hybrid_mode(self, mock_get_active_downloads, mock_update_state, mock_is_not_wanted, 
                                      mock_extract_hash, mock_get_setting, mock_add_to_rd, mock_is_cached):
        mock_get_setting.return_value = "Hybrid"
        mock_extract_hash.side_effect = lambda x: x.split(':')[-1]
        mock_is_not_wanted.return_value = False
        
        # Test case 1: All results are uncached, download limit reached
        mock_get_active_downloads.return_value = {'nb': 32, 'limit': 32}
        mock_releases = self.generate_mock_releases()
        mock_is_cached.return_value = {r['magnet']: False for r in mock_releases}

        self.adding_queue.process_item(self.queue_manager, self.mock_item, mock_releases, "Hybrid")

        # Log the actual calls for debugging
        logging.debug(f"add_to_real_debrid calls: {mock_add_to_rd.call_count}")
        logging.debug(f"move_to_pending_uncached calls: {self.queue_manager.move_to_pending_uncached.call_count}")

        # Verify that add_to_real_debrid was not called due to download limit
        self.assertEqual(mock_add_to_rd.call_count, 0)
        # Verify that move_to_pending_uncached was called
        self.assertEqual(self.queue_manager.move_to_pending_uncached.call_count, 1)
        # Verify that the item was not blacklisted
        mock_update_state.assert_not_called()

        # Reset mocks for the next test case
        mock_add_to_rd.reset_mock()
        self.queue_manager.move_to_pending_uncached.reset_mock()
        self.queue_manager.move_to_checking.reset_mock()
        mock_update_state.reset_mock()

        # Test case 2: First few are uncached, last few are cached, download limit not reached
        mock_get_active_downloads.return_value = {'nb': 1, 'limit': 32}
        mock_releases = self.generate_mock_releases()
        mock_is_cached.return_value = {r['magnet']: i >= 7 for i, r in enumerate(mock_releases)}
        mock_add_to_rd.side_effect = [
            {'status': 'downloaded', 'id': f'id_{i}', 'files': [{'path': f'file_{i}.mkv'}]} if i >= 7
            else {'status': 'uncached', 'files': [], 'torrent_id': None}
            for i in range(len(mock_releases))
        ]

        self.adding_queue.process_item(self.queue_manager, self.mock_item, mock_releases, "Hybrid")

        # Log the actual calls for debugging
        logging.debug(f"add_to_real_debrid calls: {mock_add_to_rd.call_count}")
        logging.debug(f"move_to_checking calls: {self.queue_manager.move_to_checking.call_count}")

        # Verify that it tried to add results until it found a cached one
        self.assertEqual(mock_add_to_rd.call_count, 8)
        # Verify that move_to_checking was called once for the cached result
        self.assertEqual(self.queue_manager.move_to_checking.call_count, 1)
        # Verify that the item was not blacklisted
        mock_update_state.assert_not_called()

if __name__ == '__main__':
    unittest.main()