import sys
import os
import logging
from datetime import datetime

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the function we want to test
from database.collected_items import add_collected_items

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

def create_sample_media_items():
    return [
        {
            'title': 'Test Movie 1',
            'imdb_id': 'tt1234567',
            'tmdb_id': '1234',
            'year': 2023,
            'release_date': '2023-01-01',
            'genres': ['Action', 'Sci-Fi'],
            'location': '/path/to/test_movie_1.mp4',
            'type': 'movie'
        },
        {
            'title': 'Test TV Show',
            'imdb_id': 'tt7654321',
            'tmdb_id': '5678',
            'year': 2023,
            'release_date': '2023-02-01',
            'genres': ['Drama', 'Mystery'],
            'season_number': 1,
            'episode_number': 1,
            'episode_title': 'Pilot',
            'location': '/path/to/test_tv_show_s01e01.mp4',
            'type': 'episode'
        },
        {
            'title': 'TMDB Only Movie',
            'imdb_id': None,
            'tmdb_id': '9876',
            'year': 2023,
            'release_date': '2023-03-01',
            'genres': ['Comedy'],
            'location': '/path/to/tmdb_only_movie.mp4',
            'type': 'movie'
        }
    ]

def test_add_collected_items():
    logging.info("Starting test of add_collected_items function")
    
    # Create sample media items
    media_items = create_sample_media_items()
    
    try:
        # Call the function we're testing
        add_collected_items(media_items)
        logging.info("add_collected_items function completed successfully")
    except Exception as e:
        logging.error(f"Error occurred while testing add_collected_items: {str(e)}", exc_info=True)
    
    logging.info("Test completed")

if __name__ == "__main__":
    test_add_collected_items()
