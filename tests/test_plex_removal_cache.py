import unittest
from unittest.mock import patch, MagicMock
import os
import time
from datetime import datetime, timedelta
import pickle
import sys
from utilities.plex_removal_cache import cache_plex_removal, process_removal_cache, CACHE_FILE

# Create a mock module for plex_functions
mock_plex_functions = MagicMock()
mock_plex_functions.remove_file_from_plex = MagicMock(return_value=True)
sys.modules['utilities.plex_functions'] = mock_plex_functions

class TestPlexRemovalCache(unittest.TestCase):
    def setUp(self):
        # Create a temporary cache file location
        self.test_cache_file = os.path.join(os.path.dirname(CACHE_FILE), 'test_plex_removal_cache.pkl')
        self.original_cache_file = CACHE_FILE
        # Patch the CACHE_FILE location
        patcher = patch('utilities.plex_removal_cache.CACHE_FILE', self.test_cache_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        
        # Clean up any existing test cache file
        if os.path.exists(self.test_cache_file):
            os.remove(self.test_cache_file)

    def tearDown(self):
        # Clean up test cache file
        if os.path.exists(self.test_cache_file):
            os.remove(self.test_cache_file)

    def test_cache_movie_removal(self):
        """Test caching a movie removal request"""
        # Cache a movie removal
        movie_title = "Test Movie"
        movie_path = "/path/to/movie.mkv"
        cache_plex_removal(movie_title, movie_path)

        # Verify cache file exists and contains correct data
        self.assertTrue(os.path.exists(self.test_cache_file))
        with open(self.test_cache_file, 'rb') as f:
            cache = pickle.load(f)
        
        self.assertIn(movie_path, cache)
        cached_item = cache[movie_path][0]
        self.assertEqual(cached_item[0], movie_title)  # title
        self.assertEqual(cached_item[1], movie_path)   # path
        self.assertIsNone(cached_item[2])             # episode_title
        self.assertIsInstance(cached_item[3], float)  # timestamp

    def test_cache_episode_removal(self):
        """Test caching an episode removal request"""
        # Cache an episode removal
        show_title = "Test Show"
        episode_path = "/path/to/episode.mkv"
        episode_title = "Test Episode"
        cache_plex_removal(show_title, episode_path, episode_title)

        # Verify cache file exists and contains correct data
        with open(self.test_cache_file, 'rb') as f:
            cache = pickle.load(f)
        
        self.assertIn(episode_path, cache)
        cached_item = cache[episode_path][0]
        self.assertEqual(cached_item[0], show_title)    # title
        self.assertEqual(cached_item[1], episode_path)  # path
        self.assertEqual(cached_item[2], episode_title) # episode_title
        self.assertIsInstance(cached_item[3], float)   # timestamp

    def test_process_cache_respects_age(self):
        """Test that cache processing respects the minimum age requirement"""
        # Reset the mock's call count
        mock_plex_functions.remove_file_from_plex.reset_mock()
        
        # Cache items with different ages
        now = time.time()
        
        # Create a cache with items of different ages
        cache = {
            '/path/old.mkv': [('Old Movie', '/path/old.mkv', None, now - 25*3600)],  # 25 hours old
            '/path/new.mkv': [('New Movie', '/path/new.mkv', None, now - 23*3600)],  # 23 hours old
            '/path/old_ep.mkv': [('Old Show', '/path/old_ep.mkv', 'Old Episode', now - 25*3600)],  # 25 hours old
            '/path/new_ep.mkv': [('New Show', '/path/new_ep.mkv', 'New Episode', now - 23*3600)]   # 23 hours old
        }
        
        # Write the test cache
        os.makedirs(os.path.dirname(self.test_cache_file), exist_ok=True)
        with open(self.test_cache_file, 'wb') as f:
            pickle.dump(cache, f)

        # Process the cache
        process_removal_cache(min_age_hours=24)

        # Verify only old items were processed
        self.assertEqual(mock_plex_functions.remove_file_from_plex.call_count, 2)  # Should only process the 2 old items
        
        # Verify the calls were made with correct parameters
        mock_plex_functions.remove_file_from_plex.assert_any_call('Old Movie', '/path/old.mkv', None)
        mock_plex_functions.remove_file_from_plex.assert_any_call('Old Show', '/path/old_ep.mkv', 'Old Episode')

        # Verify cache was updated
        with open(self.test_cache_file, 'rb') as f:
            updated_cache = pickle.load(f)
        
        # Only new items should remain
        self.assertEqual(len(updated_cache), 2)
        self.assertIn('/path/new.mkv', updated_cache)
        self.assertIn('/path/new_ep.mkv', updated_cache)
        self.assertNotIn('/path/old.mkv', updated_cache)
        self.assertNotIn('/path/old_ep.mkv', updated_cache)

    def test_duplicate_cache_entries(self):
        """Test that duplicate cache entries are handled correctly"""
        # Cache the same item twice
        movie_path = "/path/to/movie.mkv"
        cache_plex_removal("Test Movie", movie_path)
        time.sleep(0.1)  # Ensure different timestamps
        cache_plex_removal("Test Movie", movie_path)

        # Verify cache contains both entries for the same key
        with open(self.test_cache_file, 'rb') as f:
            cache = pickle.load(f)
        
        self.assertEqual(len(cache[movie_path]), 2)
        self.assertNotEqual(cache[movie_path][0][3], cache[movie_path][1][3])  # Different timestamps

if __name__ == '__main__':
    unittest.main() 