import unittest
import os
import shutil
import tempfile
from flask import session
from web_server import app
import logging
from unittest.mock import patch, MagicMock
from flask_session import Session
from cachelib.file import FileSystemCache
import api_tracker
from extensions import StaleFileHandleSessionInterface

class TestSessionHandling(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        # Create a temporary directory for session files
        self.temp_session_dir = tempfile.mkdtemp()
        app.config['SESSION_FILE_DIR'] = self.temp_session_dir
        app.config['SESSION_TYPE'] = 'filesystem'
        app.config['SESSION_FILE_MODE'] = 0o600
        
        # Use our custom session interface
        app.session_interface = StaleFileHandleSessionInterface(
            app.config['SESSION_FILE_DIR'],
            app.config['SESSION_FILE_THRESHOLD'],
            app.config['SESSION_FILE_MODE']
        )
        Session(app)  # Initialize Flask-Session
        self.app = app.test_client()
        
        # Setup API logging with a mock logger
        self.mock_logger = MagicMock()
        api_tracker.api_logger = self.mock_logger
        
        # Mock configuration and API dependencies
        self.patches = [
            patch('routes.utils.get_setting', return_value=False),
            patch('debrid.real_debrid.client.RealDebridProvider.get_active_downloads', return_value=(0, 0)),
            patch('debrid.real_debrid.client.RealDebridProvider.get_user_traffic', return_value={}),
        ]
        for patcher in self.patches:
            patcher.start()
        
        # Capture logging output
        self.log_capture = []
        self.handler = logging.StreamHandler()
        self.handler.setLevel(logging.WARNING)
        logging.getLogger().addHandler(self.handler)

    def tearDown(self):
        # Clean up the temporary directory
        shutil.rmtree(self.temp_session_dir, ignore_errors=True)
        logging.getLogger().removeHandler(self.handler)
        
        # Stop all patches
        for patcher in self.patches:
            patcher.stop()
        
        # Clean up the mock logger
        if hasattr(api_tracker, 'api_logger'):
            delattr(api_tracker, 'api_logger')

    def test_stale_file_handle_recovery(self):
        """Test that the application handles stale file handles gracefully"""
        
        with self.app as client:
            # First request to set up a session
            with patch.object(FileSystemCache, 'set', side_effect=OSError(116, "Stale file handle")):
                # Make a request that would trigger session handling
                response = client.get('/')
                
                # Verify the request didn't fail
                self.assertIn(response.status_code, [200, 302])

    def test_session_directory_permissions(self):
        """Test handling of session directory permission issues"""
        
        # Make session directory read-only
        os.chmod(self.temp_session_dir, 0o444)
        
        try:
            with self.app as client:
                # Attempt to make a request that would create a session
                response = client.get('/')
                
                # Verify the request still succeeds
                self.assertIn(response.status_code, [200, 302])
        finally:
            # Restore permissions so cleanup can succeed
            os.chmod(self.temp_session_dir, 0o755)

    def test_session_recovery_after_clear(self):
        """Test that sessions can be recreated after being cleared"""
        
        with self.app as client:
            # Set some session data
            with client.session_transaction() as sess:
                sess['test_key'] = 'test_value'
            
            # First request should succeed
            response = client.get('/')
            self.assertIn(response.status_code, [200, 302])
            
            # Now simulate a stale file handle error on the next request
            with patch.object(FileSystemCache, 'get', side_effect=OSError(116, "Stale file handle")):
                # This should trigger session recreation
                response = client.get('/')
                # Should get a successful response
                self.assertIn(response.status_code, [200, 302])
                
                # Set new session data
                with client.session_transaction() as sess:
                    sess['new_key'] = 'new_value'
                
                # Make another request to verify session works
                response = client.get('/')
                self.assertIn(response.status_code, [200, 302])

if __name__ == '__main__':
    unittest.main() 