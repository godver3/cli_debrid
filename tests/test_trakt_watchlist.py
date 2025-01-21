import unittest
import sys
import os

# Add parent directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
from content_checkers.trakt import get_wanted_from_trakt_watchlist
import json

class TestTraktWatchlist(unittest.TestCase):
    @patch('content_checkers.trakt.get_setting')
    @patch('content_checkers.trakt.get_media_item_presence')
    @patch('content_checkers.trakt.check_for_updates')
    @patch('content_checkers.trakt.ensure_trakt_auth')
    @patch('content_checkers.trakt.get_trakt_sources')
    @patch('content_checkers.trakt.fetch_items_from_trakt')
    @patch('content_checkers.trakt.api.post')
    def test_watchlist_removal(self, mock_post, mock_fetch, mock_sources, mock_auth, 
                             mock_updates, mock_presence, mock_settings):
        # Mock settings
        def mock_get_setting(section, key, default=None):
            if section == 'Debug' and key == 'trakt_watchlist_removal':
                return True
            if section == 'Debug' and key == 'trakt_watchlist_keep_series':
                return True
            return default
        mock_settings.side_effect = mock_get_setting

        # Mock other dependencies
        mock_updates.return_value = True
        mock_auth.return_value = "fake_token"
        mock_sources.return_value = {
            'watchlist': [{'enabled': True, 'versions': {'hdr': True}}],
            'lists': []
        }

        # Create sample watchlist items
        sample_items = [
            {
                'movie': {
                    'title': 'Test Movie',
                    'ids': {'imdb': 'tt1234567', 'trakt': 1}
                }
            },
            {
                'show': {
                    'title': 'Test Show',
                    'ids': {'imdb': 'tt7654321', 'trakt': 2}
                }
            }
        ]
        mock_fetch.return_value = sample_items

        # Mock item presence to simulate collected items
        def mock_get_presence(imdb_id):
            return "Collected"
        mock_presence.side_effect = mock_get_presence

        # Mock successful API response for removal
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Run the function
        result = get_wanted_from_trakt_watchlist()

        # Verify that movie was removed and show was kept
        self.assertEqual(len(result), 1)  # One watchlist source
        processed_items = result[0][0]  # Get items from first source
        self.assertEqual(len(processed_items), 1)  # Only show should remain
        self.assertEqual(processed_items[0]['imdb_id'], 'tt7654321')  # Show's IMDB ID
        self.assertEqual(processed_items[0]['media_type'], 'tv')

        # Verify removal API call was made for movie only
        mock_post.assert_called_once()
        call_args = mock_post.call_args[1]
        self.assertIn('movies', call_args['json'])
        self.assertEqual(call_args['json']['movies'][0]['ids']['imdb'], 'tt1234567')

    @patch('content_checkers.trakt.get_setting')
    @patch('content_checkers.trakt.get_media_item_presence')
    @patch('content_checkers.trakt.check_for_updates')
    @patch('content_checkers.trakt.ensure_trakt_auth')
    @patch('content_checkers.trakt.get_trakt_sources')
    @patch('content_checkers.trakt.fetch_items_from_trakt')
    @patch('content_checkers.trakt.api.post')
    def test_watchlist_removal_all(self, mock_post, mock_fetch, mock_sources, mock_auth, 
                                 mock_updates, mock_presence, mock_settings):
        # Mock settings - remove all collected items
        def mock_get_setting(section, key, default=None):
            if section == 'Debug' and key == 'trakt_watchlist_removal':
                return True
            if section == 'Debug' and key == 'trakt_watchlist_keep_series':
                return False
            return default
        mock_settings.side_effect = mock_get_setting

        # Mock other dependencies
        mock_updates.return_value = True
        mock_auth.return_value = "fake_token"
        mock_sources.return_value = {
            'watchlist': [{'enabled': True, 'versions': {'hdr': True}}],
            'lists': []
        }

        # Create sample watchlist items
        sample_items = [
            {
                'movie': {
                    'title': 'Test Movie',
                    'ids': {'imdb': 'tt1234567', 'trakt': 1}
                }
            },
            {
                'show': {
                    'title': 'Test Show',
                    'ids': {'imdb': 'tt7654321', 'trakt': 2}
                }
            }
        ]
        mock_fetch.return_value = sample_items

        # Mock item presence to simulate collected items
        def mock_get_presence(imdb_id):
            return "Collected"
        mock_presence.side_effect = mock_get_presence

        # Mock successful API responses for removal
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Run the function
        result = get_wanted_from_trakt_watchlist()

        # Verify that both items were removed
        self.assertEqual(len(result), 1)  # One watchlist source
        processed_items = result[0][0]  # Get items from first source
        self.assertEqual(len(processed_items), 0)  # No items should remain

        # Verify removal API calls were made for both items
        self.assertEqual(mock_post.call_count, 2)
        calls = mock_post.call_args_list
        
        # Check first call (movie)
        first_call = calls[0][1]
        self.assertIn('movies', first_call['json'])
        self.assertEqual(first_call['json']['movies'][0]['ids']['imdb'], 'tt1234567')
        
        # Check second call (show)
        second_call = calls[1][1]
        self.assertIn('shows', second_call['json'])
        self.assertEqual(second_call['json']['shows'][0]['ids']['imdb'], 'tt7654321')

if __name__ == '__main__':
    unittest.main() 