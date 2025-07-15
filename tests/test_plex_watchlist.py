import unittest
from unittest.mock import patch, MagicMock
from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist
from plexapi.video import Movie, Show

class TestPlexWatchlist(unittest.TestCase):
    def setUp(self):
        # Mock settings
        self.settings_patcher = patch('content_checkers.plex_watchlist.get_setting')
        self.mock_get_setting = self.settings_patcher.start()
        
        # Set disable_content_source_caching to True
        def mock_get_setting(section, key, default=None):
            if section == 'Debug' and key == 'disable_content_source_caching':
                return True
            return False
        self.mock_get_setting.side_effect = mock_get_setting

        # Mock DirectAPI
        self.direct_api_patcher = patch('content_checkers.plex_watchlist.DirectAPI')
        self.mock_direct_api = self.direct_api_patcher.start()
        self.mock_api_instance = MagicMock()
        self.mock_direct_api.return_value = self.mock_api_instance

        # Mock Plex client
        self.plex_client_patcher = patch('content_checkers.plex_watchlist.get_plex_client')
        self.mock_plex_client = self.plex_client_patcher.start()
        self.mock_account = MagicMock()
        self.mock_plex_client.return_value = self.mock_account

        # Mock media item presence check
        self.media_presence_patcher = patch('content_checkers.plex_watchlist.get_media_item_presence')
        self.mock_media_presence = self.media_presence_patcher.start()
        self.mock_media_presence.return_value = None  # Not collected by default

    def tearDown(self):
        self.settings_patcher.stop()
        self.direct_api_patcher.stop()
        self.plex_client_patcher.stop()
        self.media_presence_patcher.stop()

    def _create_mock_guid(self, provider, id_value):
        mock_guid = MagicMock()
        mock_guid.id = f"{provider}://{id_value}"
        return mock_guid

    def test_tmdb_to_imdb_conversion(self):
        """Test that items with only TMDB IDs are properly converted to IMDB IDs"""
        # Mock TMDB to IMDB conversion
        self.mock_api_instance.tmdb_to_imdb.return_value = ('tt1234567', 'trakt')

        # Create a mock movie with only TMDB ID
        mock_movie = MagicMock(spec=Movie)
        mock_movie.title = "Test Movie"
        mock_movie.type = "movie"
        mock_movie.guids = [self._create_mock_guid('tmdb', '12345')]

        # Set up watchlist mock
        self.mock_account.watchlist.return_value = [mock_movie]

        # Call the function
        versions = {'default': True}
        result = get_wanted_from_plex_watchlist(versions)

        # Verify the conversion was attempted
        self.mock_api_instance.tmdb_to_imdb.assert_called_once_with('12345', media_type='movie')

        # Check that the result contains our converted item
        self.assertEqual(len(result), 1)
        items, _ = result[0]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['imdb_id'], 'tt1234567')
        self.assertEqual(items[0]['media_type'], 'movie')

    def test_tmdb_to_imdb_conversion_failure(self):
        """Test that items are properly skipped when TMDB to IMDB conversion fails"""
        # Mock TMDB to IMDB conversion failure
        self.mock_api_instance.tmdb_to_imdb.return_value = (None, None)

        # Create a mock movie with only TMDB ID
        mock_movie = MagicMock(spec=Movie)
        mock_movie.title = "Test Movie"
        mock_movie.type = "movie"
        mock_movie.guids = [self._create_mock_guid('tmdb', '12345')]

        # Set up watchlist mock
        self.mock_account.watchlist.return_value = [mock_movie]

        # Call the function
        versions = {'default': True}
        result = get_wanted_from_plex_watchlist(versions)

        # Verify the conversion was attempted
        self.mock_api_instance.tmdb_to_imdb.assert_called_once_with('12345', media_type='movie')

        # Check that no items were returned (conversion failed)
        self.assertEqual(len(result), 1)
        items, _ = result[0]
        self.assertEqual(len(items), 0)

    def test_mixed_id_types(self):
        """Test handling of items with both IMDB and TMDB IDs"""
        # Create a mock movie with both IMDB and TMDB IDs
        mock_movie = MagicMock(spec=Movie)
        mock_movie.title = "Test Movie"
        mock_movie.type = "movie"
        mock_movie.guids = [
            self._create_mock_guid('imdb', 'tt9876543'),
            self._create_mock_guid('tmdb', '12345')
        ]

        # Set up watchlist mock
        self.mock_account.watchlist.return_value = [mock_movie]

        # Call the function
        versions = {'default': True}
        result = get_wanted_from_plex_watchlist(versions)

        # Verify the conversion was NOT attempted (since IMDB ID was present)
        self.mock_api_instance.tmdb_to_imdb.assert_not_called()

        # Check that the result contains our item with the correct IMDB ID
        self.assertEqual(len(result), 1)
        items, _ = result[0]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['imdb_id'], 'tt9876543')
        self.assertEqual(items[0]['media_type'], 'movie')

if __name__ == '__main__':
    unittest.main() 