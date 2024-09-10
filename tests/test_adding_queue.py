import unittest
from unittest.mock import patch, MagicMock, call
from queues.adding_queue import AddingQueue
from settings import get_setting
import logging

logging.basicConfig(level=logging.DEBUG)

class TestAddingQueue(unittest.TestCase):

    def setUp(self):
        self.queue_manager = MagicMock()
        self.adding_queue = AddingQueue()
        self.mock_releases = [
            {
                "title": "The Matrix 1999 1080p BluRay x264",
                "magnet": "magnet:?xt=urn:btih:5d8889678f072ad1f58bde7bf242fc0e48b81fd1",
                "is_cached": True,
                "seeders": 100,
            },
            {
                "title": "The Matrix 1999 2160p UHD BluRay x265 HDR",
                "magnet": "magnet:?xt=urn:btih:9f9165d9a281a9b8e782cd5176bbcc8256fd1871",
                "is_cached": True,
                "seeders": 75,
            },
            {
                "title": "The Matrix 1999 720p WEB-DL AAC2.0 H264",
                "magnet": "magnet:?xt=urn:btih:1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0t",
                "is_cached": False,
                "seeders": 50,
            },
            {
                "title": "The Matrix 1999 1080p BrRip x264",
                "magnet": "magnet:?xt=urn:btih:0a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t",
                "is_cached": False,
                "seeders": 25,
            },
            {
                "title": "The Matrix 1999 720p BluRay x264",
                "magnet": "magnet:?xt=urn:btih:2d8d55b4c64cfa1e43fccd3c30e67efa014d0bcc",
                "is_cached": False,
                "seeders": 10,
            }
        ]
        self.mock_item = {
            "id": "123",
            "title": "The Matrix",
            "year": "1999",
            "type": "movie",
            "version": "1080p",
            "imdb_id": "tt0133093",
        }

    @patch('queues.adding_queue.is_cached_on_rd')
    @patch('queues.adding_queue.add_to_real_debrid')
    @patch('queues.adding_queue.get_setting')
    @patch('queues.adding_queue.extract_hash_from_magnet')
    def test_process_item_full_mode(self, mock_extract_hash, mock_get_setting, mock_add_to_rd, mock_is_cached):
        mock_get_setting.return_value = "Full"
        mock_extract_hash.side_effect = lambda x: x.split(':')[-1]
        mock_is_cached.return_value = {r['magnet']: r['is_cached'] for r in self.mock_releases}
        
        # Set up add_to_real_debrid to succeed on the third attempt
        mock_add_to_rd.side_effect = [
            None,  # First attempt fails
            None,  # Second attempt fails
            {"status": "downloading", "files": ["The.Matrix.1999.1080p.BluRay.x264.mkv"]},  # Third attempt succeeds
            None,  # Fourth attempt (shouldn't be called)
            None,  # Fifth attempt (shouldn't be called)
        ]

        self.adding_queue.process_item(self.queue_manager, self.mock_item, self.mock_releases, "Full")

        logging.debug(f"mock_add_to_rd.call_count: {mock_add_to_rd.call_count}")
        logging.debug(f"mock_add_to_rd.call_args_list: {mock_add_to_rd.call_args_list}")

        self.assertEqual(mock_add_to_rd.call_count, 3)
        expected_calls = [call(release['magnet']) for release in self.mock_releases[:3]]
        mock_add_to_rd.assert_has_calls(expected_calls, any_order=False)

    @patch('queues.adding_queue.is_cached_on_rd')
    @patch('queues.adding_queue.add_to_real_debrid')
    @patch('queues.adding_queue.get_setting')
    @patch('queues.adding_queue.extract_hash_from_magnet')
    def test_process_item_hybrid_mode(self, mock_extract_hash, mock_get_setting, mock_add_to_rd, mock_is_cached):
        mock_get_setting.return_value = "Hybrid"
        mock_extract_hash.side_effect = lambda x: x.split(':')[-1]
        mock_is_cached.return_value = {r['magnet']: r['is_cached'] for r in self.mock_releases}
        mock_add_to_rd.return_value = {"status": "downloaded", "links": ["https://example.com/file"], "files": ["The.Matrix.1999.1080p.BluRay.x264.mkv"]}

        self.adding_queue.process_item(self.queue_manager, self.mock_item, self.mock_releases, "Hybrid")

        logging.debug(f"mock_add_to_rd.call_count: {mock_add_to_rd.call_count}")
        logging.debug(f"mock_add_to_rd.call_args_list: {mock_add_to_rd.call_args_list}")

        self.assertEqual(mock_add_to_rd.call_count, 1)
        mock_add_to_rd.assert_called_with(self.mock_releases[0]['magnet'])

    @patch('queues.adding_queue.is_cached_on_rd')
    @patch('queues.adding_queue.add_to_real_debrid')
    @patch('queues.adding_queue.get_setting')
    @patch('queues.adding_queue.extract_hash_from_magnet')
    def test_process_item_none_mode(self, mock_extract_hash, mock_get_setting, mock_add_to_rd, mock_is_cached):
        mock_get_setting.return_value = "None"
        mock_extract_hash.side_effect = lambda x: x.split(':')[-1]
        mock_is_cached.return_value = {r['magnet']: r['is_cached'] for r in self.mock_releases}
        mock_add_to_rd.return_value = {"status": "downloaded", "links": ["https://example.com/file"], "files": ["The.Matrix.1999.1080p.BluRay.x264.mkv"]}

        self.adding_queue.process_item(self.queue_manager, self.mock_item, self.mock_releases, "None")

        logging.debug(f"mock_add_to_rd.call_count: {mock_add_to_rd.call_count}")
        logging.debug(f"mock_add_to_rd.call_args_list: {mock_add_to_rd.call_args_list}")

        self.assertEqual(mock_add_to_rd.call_count, 1)
        mock_add_to_rd.assert_called_with(self.mock_releases[0]['magnet'])

if __name__ == '__main__':
    unittest.main()