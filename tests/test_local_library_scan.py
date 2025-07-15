import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import shutil
from datetime import datetime
from utilities.local_library_scan import check_local_file_for_item

class TestLocalLibraryScan(unittest.TestCase):
    def setUp(self):
        # Create temporary directories for testing
        self.temp_dir = tempfile.mkdtemp()
        self.original_path = os.path.join(self.temp_dir, 'original')
        self.symlink_path = os.path.join(self.temp_dir, 'symlinks')
        os.makedirs(self.original_path)
        os.makedirs(self.symlink_path)

        # Mock settings
        self.settings_patcher = patch('utilities.local_library_scan.get_setting')
        self.mock_get_setting = self.settings_patcher.start()
        self.mock_get_setting.side_effect = self._mock_get_setting

        # Mock database update
        self.db_patcher = patch('database.database_writing.update_media_item')
        self.mock_update_media_item = self.db_patcher.start()

    def tearDown(self):
        # Clean up temporary directories
        shutil.rmtree(self.temp_dir)
        self.settings_patcher.stop()
        self.db_patcher.stop()

    def _mock_get_setting(self, section, key, default=None):
        if section == 'File Management':
            if key == 'original_files_path':
                return self.original_path
            elif key == 'symlinked_files_path':
                return self.symlink_path
            elif key == 'symlink_organize_by_type':
                return True
        elif section == 'Debug':
            if key == 'symlink_movie_template':
                return '{title} ({year})/{title} ({year}) - {imdb_id} - {version} - ({original_filename})'
        return default

    def _create_test_file(self, folder_name, file_name):
        """Helper to create a test file and return its path"""
        folder_path = os.path.join(self.original_path, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        file_path = os.path.join(folder_path, file_name)
        with open(file_path, 'w') as f:
            f.write('test content')
        return file_path

    def _create_test_item(self, file_name, folder_name):
        """Helper to create a test item with all required fields"""
        return {
            'id': 1,  # Add required id field
            'filled_by_file': file_name,
            'filled_by_title': folder_name,
            'title': 'Test Movie',
            'year': '2024',
            'release_date': '2024-01-01',
            'type': 'movie',  # Add required type field
            'imdb_id': 'tt1234567',  # Add required imdb_id field
            'version': 'WEBDL-1080p'  # Add version field
        }

    def test_basic_path_search(self):
        """Test finding a file in the basic path"""
        # Create test file
        file_name = 'test.mkv'
        folder_name = 'Test.Movie.2024.1080p'
        self._create_test_file(folder_name, file_name)

        # Create test item with all required fields
        item = self._create_test_item(file_name, folder_name)

        # Test the function
        result = check_local_file_for_item(item)
        self.assertTrue(result)
        self.mock_update_media_item.assert_called_once()

    def test_stripped_extension_path(self):
        """Test finding a file when the folder name has extension stripped"""
        # Create test file in folder without extension
        file_name = 'test.mkv'
        folder_with_ext = 'Test.Movie.2024.1080p.mkv'
        folder_without_ext = 'Test.Movie.2024.1080p'
        self._create_test_file(folder_without_ext, file_name)

        # Create test item with folder name including extension
        item = self._create_test_item(file_name, folder_with_ext)

        # Test the function
        result = check_local_file_for_item(item)
        self.assertTrue(result)
        self.mock_update_media_item.assert_called_once()

    @patch('utilities.local_library_scan.find_file')
    def test_extended_search(self, mock_find_file):
        """Test extended search functionality"""
        # Setup mock find_file to return a path
        found_path = os.path.join(self.original_path, 'Different.Folder', 'test.mkv')
        mock_find_file.return_value = found_path
        
        # Create the file in a different location
        self._create_test_file('Different.Folder', 'test.mkv')

        # Create test item with incorrect folder name
        item = self._create_test_item('test.mkv', 'Wrong.Folder')

        # Test the function with extended search
        result = check_local_file_for_item(item, extended_search=True)
        self.assertTrue(result)
        mock_find_file.assert_called_once()
        self.mock_update_media_item.assert_called_once()

    def test_webhook_retries(self):
        """Test webhook retry mechanism"""
        file_name = 'test.mkv'
        folder_name = 'Test.Movie.2024.1080p'

        # Create test item
        item = self._create_test_item(file_name, folder_name)

        # Start with file not existing
        with patch('time.sleep') as mock_sleep:  # Mock sleep to speed up test
            result = check_local_file_for_item(item, is_webhook=True)
            self.assertFalse(result)
            # Should have attempted retries
            self.assertGreater(mock_sleep.call_count, 0)

        # Create file after initial failure
        self._create_test_file(folder_name, file_name)
        result = check_local_file_for_item(item, is_webhook=True)
        self.assertTrue(result)
        self.mock_update_media_item.assert_called_once()

    def test_downloading_skip_extended_search(self):
        """Test that extended search is skipped for downloading files"""
        with patch('debrid.get_debrid_provider') as mock_get_provider:
            # Mock debrid provider to indicate file is downloading
            mock_provider = MagicMock()
            mock_provider.get_torrent_info.return_value = {'progress': 50}
            mock_get_provider.return_value = mock_provider

            item = self._create_test_item('test.mkv', 'Wrong.Folder')
            item['filled_by_torrent_id'] = '123'  # Add torrent ID

            with patch('utilities.local_library_scan.find_file') as mock_find_file:
                result = check_local_file_for_item(item, extended_search=True)
                self.assertFalse(result)
                # find_file should not have been called because file is downloading
                mock_find_file.assert_not_called()

if __name__ == '__main__':
    unittest.main() 