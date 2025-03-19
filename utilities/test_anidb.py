import logging
import sys
import os
import time

# Add the root directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, Any
from utilities.anidb_functions import get_anidb_metadata_for_item, format_filename_with_anidb
from utilities.settings import get_setting

# Configure logging
logging.basicConfig(level=logging.DEBUG,
                   format='%(asctime)s - %(levelname)s - %(message)s')

# Mock settings for testing
MOCK_SETTINGS = {
    'Debug': {
        'use_anidb_metadata': True,
        'symlink_preserve_extension': True,
        'anidb_episode_template': '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title}'
    }
}

# Override get_setting for testing
def mock_get_setting(section: str, key: str, default: Any = None) -> Any:
    """Mock get_setting that always returns values from MOCK_SETTINGS."""
    if section in MOCK_SETTINGS and key in MOCK_SETTINGS[section]:
        return MOCK_SETTINGS[section][key]
    return default  # Return the default if not found in mock settings

# Replace the actual get_setting with our mock
sys.modules['settings'].get_setting = mock_get_setting

def test_anidb_functions():
    """Test AniDB functions with a sample anime item."""
    
    # Sample anime items to test with
    test_items = [
        {
            'id': 12345,
            'type': 'episode',
            'is_anime': True,
            'title': 'Jujutsu Kaisen',
            'year': '2020',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': 'Ryomen Sukuna',
            'quality': '1080p',
            'version': 'HDTV',
            'filled_by_file': 'Jujutsu.Kaisen.S01E01.1080p.mkv',
            'filled_by_title': 'Jujutsu Kaisen',
            'state': 'checking'
        },
        {
            'id': 12346,
            'type': 'episode',
            'is_anime': True,
            'title': 'One Piece',
            'year': '1999',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': 'I\'m Luffy! The Man Who\'s Gonna Be King of the Pirates!',
            'quality': '1080p',
            'version': 'HDTV',
            'filled_by_file': 'One.Piece.E001.1080p.mkv',
            'filled_by_title': 'One Piece',
            'state': 'checking'
        },
        {
            'id': 12347,
            'type': 'episode',
            'is_anime': True,
            'title': 'MF Ghost',
            'year': '2023',
            'season_number': 1,
            'episode_number': 13,  # Testing episode from second cour
            'episode_title': 'Episode 13',
            'quality': '1080p',
            'version': 'HDTV',
            'filled_by_file': 'MF.Ghost.S01E13.1080p.mkv',
            'filled_by_title': 'MF Ghost',
            'state': 'checking'
        }
    ]
    
    for test_item in test_items:
        logging.info(f"\nTesting with anime: {test_item['title']}")
        
        # Test getting metadata
        logging.info("Testing get_anidb_metadata_for_item...")
        metadata = get_anidb_metadata_for_item(test_item)
        if metadata:
            logging.info("Successfully retrieved AniDB metadata:")
            for key, value in metadata.items():
                logging.info(f"  {key}: {value}")
        else:
            logging.error("Failed to retrieve AniDB metadata")
        
        # Test filename formatting
        logging.info("\nTesting format_filename_with_anidb...")
        filename = format_filename_with_anidb(test_item, '.mkv')
        if filename:
            logging.info(f"Successfully formatted filename: {filename}")
        else:
            logging.error("Failed to format filename")
        
        # Add a delay between tests to respect rate limiting
        time.sleep(2)

if __name__ == '__main__':
    test_anidb_functions() 