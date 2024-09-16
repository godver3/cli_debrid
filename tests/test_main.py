import unittest
from unittest.mock import patch, MagicMock, mock_open, call
import os
import shutil
import logging
import main

class TestMainFunctions(unittest.TestCase):

    @patch('os.makedirs')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('logging.getLogger')
    @patch('logging.error')  # Mock logging.error
    def test_setup_logging(self, mock_logging_error, mock_get_logger, mock_open, mock_exists, mock_makedirs):
        # Setup mocks
        mock_exists.side_effect = lambda x: False if 'logs' in x else True
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger

        # Call the function
        main.setup_logging()

        # Assertions
        mock_makedirs.assert_called_once_with('user/logs')
        self.assertGreaterEqual(mock_open.call_count, 3)  # Ensure at least three log files are created
        mock_logger.setLevel.assert_any_call(logging.WARNING)

    @patch('os.makedirs')
    @patch('os.path.exists')
    def test_setup_directories(self, mock_exists, mock_makedirs):
        mock_exists.return_value = False

        main.setup_directories()

        mock_makedirs.assert_called_once_with('user/db_content')

    @patch('shutil.copy2')
    @patch('os.path.exists')
    @patch('logging.info')
    @patch('logging.warning')
    def test_backup_config(self, mock_warning, mock_info, mock_exists, mock_copy2):
        # Test when config exists
        mock_exists.side_effect = lambda x: True if 'user/config/config.json' in x else False
        main.backup_config()
        mock_copy2.assert_called_once_with('user/config/config.json', 'user/config/config_backup.json')
        mock_info.assert_called_once_with('Backup of config.json created: user/config/config_backup.json')

        # Test when config does not exist
        mock_copy2.reset_mock()
        mock_info.reset_mock()
        mock_exists.side_effect = lambda x: False
        main.backup_config()
        mock_warning.assert_called_once_with('config.json not found, no backup created.')

    @patch('builtins.open', new_callable=mock_open, read_data='1.2.3')
    def test_get_version(self, mock_open):
        version = main.get_version()
        self.assertEqual(version, '1.2.3')

        # Test when file does not exist
        mock_open.side_effect = FileNotFoundError
        version = main.get_version()
        self.assertEqual(version, '0.0.0')

    @patch('main.api.post')
    @patch('logging.error')  # Mock logging.error
    def test_update_web_ui_state(self, mock_logging_error, mock_post):
        # Test successful API call
        main.update_web_ui_state('running')
        mock_post.assert_called_once_with('http://localhost:5000/api/update_program_state', json={'state': 'running'})

        # Test API call fails
        mock_post.side_effect = main.api.exceptions.RequestException
        main.update_web_ui_state('running')
        mock_logging_error.assert_called_once_with("Failed to update web UI state")

if __name__ == '__main__':
    unittest.main()
