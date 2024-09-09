import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from database.collected_items import add_collected_items

class TestAddCollectedItems(unittest.TestCase):

    @patch('database.collected_items.get_db_connection')
    @patch('database.collected_items.get_existing_airtime')
    @patch('database.collected_items.get_show_airtime_by_imdb_id')
    def test_add_new_movie(self, mock_get_show_airtime, mock_get_existing_airtime, mock_get_db_connection):
        # Setup
        mock_conn = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = []

        # Test data
        new_movie = {
            'location': '/path/to/movie.mp4',
            'imdb_id': 'tt1234567',
            'tmdb_id': '7654321',
            'title': 'Test Movie',
            'year': 2023,
            'release_date': '2023-01-01',
            'addedAt': datetime.now().timestamp(),
            'genres': ['Action', 'Sci-Fi']
        }

        # Call function
        add_collected_items([new_movie])

        # Assertions
        mock_conn.execute.assert_any_call('''
            INSERT INTO media_items
            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, genres, filled_by_file)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', unittest.mock.ANY)  # We use ANY here because we can't predict the exact datetime values

        # Verify commit was called
        mock_conn.commit.assert_called_once()

    @patch('database.collected_items.get_db_connection')
    @patch('database.collected_items.get_existing_airtime')
    @patch('database.collected_items.get_show_airtime_by_imdb_id')
    def test_add_new_episode(self, mock_get_show_airtime, mock_get_existing_airtime, mock_get_db_connection):
        # Setup
        mock_conn = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_existing_airtime.return_value = None
        mock_get_show_airtime_by_imdb_id.return_value = '20:00'

        # Test data
        new_episode = {
            'location': '/path/to/episode.mp4',
            'imdb_id': 'tt7654321',
            'tmdb_id': '1234567',
            'title': 'Test Show',
            'year': 2023,
            'release_date': '2023-01-01',
            'addedAt': datetime.now().timestamp(),
            'genres': ['Drama', 'Mystery'],
            'season_number': 1,
            'episode_number': 1,
            'episode_title': 'Pilot'
        }

        # Call function
        add_collected_items([new_episode])

        # Assertions
        mock_conn.execute.assert_any_call('''
            INSERT INTO media_items
            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, genres, filled_by_file)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', unittest.mock.ANY)  # We use ANY here because we can't predict the exact datetime values

        # Verify commit was called
        mock_conn.commit.assert_called_once()

    @patch('database.collected_items.get_db_connection')
    @patch('database.collected_items.get_existing_airtime')
    @patch('database.collected_items.get_show_airtime_by_imdb_id')
    def test_update_existing_item(self, mock_get_show_airtime, mock_get_existing_airtime, mock_get_db_connection):
        # Setup
        mock_conn = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = [
            {'id': 1, 'imdb_id': 'tt1234567', 'tmdb_id': '7654321', 'type': 'movie', 'season_number': None, 'episode_number': None, 'state': 'Checking', 'version': 'unknown', 'filled_by_file': 'existing_movie.mp4'}
        ]

        # Test data
        existing_movie = {
            'location': '/path/to/existing_movie.mp4',
            'imdb_id': 'tt1234567',
            'tmdb_id': '7654321',
            'title': 'Existing Movie',
            'year': 2022,
            'release_date': '2022-12-31',
            'addedAt': datetime.now().timestamp(),
            'genres': ['Drama', 'Thriller']
        }

        # Call function
        add_collected_items([existing_movie])

        # Assertions
        mock_conn.execute.assert_any_call('''
            UPDATE media_items
            SET state = ?, last_updated = ?, collected_at = ?
            WHERE id = ?
        ''', ('Collected', unittest.mock.ANY, unittest.mock.ANY, 1))

        # Verify commit was called
        mock_conn.commit.assert_called_once()

    @patch('database.collected_items.get_db_connection')
    @patch('database.collected_items.get_existing_airtime')
    @patch('database.collected_items.get_show_airtime_by_imdb_id')
    def test_full_scan_remove_missing_items(self, mock_get_show_airtime, mock_get_existing_airtime, mock_get_db_connection):
        # Setup
        mock_conn = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = [
            {'id': 1, 'imdb_id': 'tt1111111', 'tmdb_id': '1111111', 'type': 'movie', 'season_number': None, 'episode_number': None, 'state': 'Collected', 'version': 'unknown', 'filled_by_file': 'missing_movie.mp4'},
            {'id': 2, 'imdb_id': 'tt2222222', 'tmdb_id': '2222222', 'type': 'episode', 'season_number': 1, 'episode_number': 1, 'state': 'Collected', 'version': 'unknown', 'filled_by_file': 'existing_episode.mp4'}
        ]

        # Test data
        existing_episode = {
            'location': '/path/to/existing_episode.mp4',
            'imdb_id': 'tt2222222',
            'tmdb_id': '2222222',
            'title': 'Existing Show',
            'year': 2023,
            'release_date': '2023-01-01',
            'addedAt': datetime.now().timestamp(),
            'genres': ['Comedy'],
            'season_number': 1,
            'episode_number': 1,
            'episode_title': 'Pilot'
        }

        # Call function
        add_collected_items([existing_episode], recent=False)

        # Assertions
        mock_conn.execute.assert_any_call('''
            UPDATE media_items
            SET state = ?, last_updated = ?, collected_at = NULL, filled_by_file = NULL
            WHERE id = ?
        ''', ('Wanted', unittest.mock.ANY, 1))

        # Verify commit was called
        mock_conn.commit.assert_called_once()

if __name__ == '__main__':
    unittest.main()