#!/usr/bin/env python3
"""
Basic test to verify the test setup and imports work correctly.
"""

import unittest
import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class TestBasicSetup(unittest.TestCase):
    """Basic test to verify the test environment is set up correctly."""
    
    def test_imports(self):
        """Test that we can import the required modules."""
        try:
            from scraper.functions.filter_results import filter_results
            self.assertTrue(True, "Successfully imported filter_results")
        except ImportError as e:
            self.fail(f"Failed to import filter_results: {e}")
    
    def test_ptt_parser_import(self):
        """Test that we can import the PTT parser."""
        try:
            from scraper.functions.ptt_parser import parse_with_ptt
            self.assertTrue(True, "Successfully imported parse_with_ptt")
        except ImportError as e:
            self.fail(f"Failed to import parse_with_ptt: {e}")
    
    def test_mock_creation(self):
        """Test that we can create basic mock objects."""
        from unittest.mock import Mock
        
        mock_api = Mock()
        mock_api.get_show_aliases.return_value = ({}, None)
        
        self.assertIsNotNone(mock_api)
        self.assertEqual(mock_api.get_show_aliases(), ({}, None))
    
    def test_basic_assertions(self):
        """Test that basic assertions work."""
        self.assertEqual(1, 1)
        self.assertTrue(True)
        self.assertFalse(False)
        self.assertIsNone(None)
        self.assertIsNotNone("test")

if __name__ == '__main__':
    unittest.main()
