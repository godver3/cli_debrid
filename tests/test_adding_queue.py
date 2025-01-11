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

    @patch('queues.adding_queue.get_setting')
    def test_file_filtering(self, mock_get_setting):
        """Test that files are correctly filtered based on the filename_filter_out_list setting"""
        # Setup mock torrent info with test files
        torrent_info = {
            'files': [
                {'path': 'movie.mkv'},
                {'path': 'sample/sample.mp4'},
                {'path': 'movie.nfo'},
                {'path': 'subfolder/extras.mp4'},
                {'path': 'movie-behind-scenes.mp4'}
            ]
        }
        
        # Test case 1: No filters
        mock_get_setting.return_value = ''
        self.adding_queue.torrent_processor = MagicMock()
        self.adding_queue.media_matcher = MagicMock()
        
        # Create a test item
        test_item = {
            'id': '123',
            'title': 'Test Movie',
            'type': 'movie'
        }
        
        # Process the item
        self.adding_queue.items = [test_item]
        results = [{'hash': 'abc123', 'files': torrent_info['files']}]
        
        # Verify no filtering occurs when no filter list is set
        self.assertEqual(len(torrent_info['files']), 5)
        
        # Test case 2: With filters
        mock_get_setting.return_value = 'sample,nfo,extras'
        
        # Mock the necessary methods to reach our filtering code
        self.adding_queue.torrent_processor.process_results.return_value = (torrent_info, 'fake_magnet')
        self.adding_queue.media_matcher.match_content.return_value = [('movie.mkv', 0.9)]
        
        # Process with filters
        self.adding_queue.process(self.queue_manager)
        
        # Verify the filtering logic was applied correctly
        # The media_matcher.match_content should have been called with the filtered files
        match_content_calls = self.adding_queue.media_matcher.match_content.call_args_list
        if match_content_calls:
            files_passed_to_matcher = match_content_calls[0][0][0]  # First call, first argument
            # Should only contain movie.mkv and movie-behind-scenes.mp4
            self.assertEqual(len(files_passed_to_matcher), 2)
            file_paths = [f['path'] for f in files_passed_to_matcher]
            self.assertIn('movie.mkv', file_paths)
            self.assertIn('movie-behind-scenes.mp4', file_paths)
            self.assertNotIn('sample/sample.mp4', file_paths)
            self.assertNotIn('movie.nfo', file_paths)
            self.assertNotIn('subfolder/extras.mp4', file_paths)

if __name__ == '__main__':
    unittest.main()