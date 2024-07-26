import sys
import os
import json
import logging
from datetime import datetime

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from content_checkers.mdb_list import get_wanted_from_mdblists

# Set up logging to both console and file
log_filename = f"mdblist_helper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler()
    ]
)

def print_wanted_items(wanted_items):
    logging.info("\n=== Wanted Movies ===")
    for movie in wanted_items['movies']:
        logging.info(f"Title: {movie['title']} ({movie['year']})")
        logging.info(f"IMDB ID: {movie['imdb_id']}")
        logging.info(f"TMDB ID: {movie['tmdb_id']}")
        logging.info(f"Release Date: {movie['release_date']}")
        logging.info("---")

    logging.info("\n=== Wanted TV Show Episodes ===")
    for episode in wanted_items['episodes']:
        logging.info(f"Show: {episode['title']} ({episode['year']})")
        logging.info(f"IMDB ID: {episode['imdb_id']}")
        logging.info(f"TMDB ID: {episode['tmdb_id']}")
        logging.info(f"Season: {episode['season_number']}")
        logging.info(f"Episode: {episode['episode_number']} - {episode['episode_title']}")
        logging.info(f"Release Date: {episode['release_date']}")
        logging.info("---")

def main():
    try:
        logging.info("Starting MDBList wanted items fetch")
        wanted_items = get_wanted_from_mdblists()
        
        logging.info("\n=== Summary ===")
        logging.info(f"Total wanted movies: {len(wanted_items['movies'])}")
        logging.info(f"Total wanted TV show episodes: {len(wanted_items['episodes'])}")
        
        print_wanted_items(wanted_items)
        
        # Save the output to a JSON file
        json_filename = f"wanted_items_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(json_filename, 'w') as f:
            json.dump(wanted_items, f, indent=2)
        logging.info(f"\nFull results saved to '{json_filename}'")
        
    except Exception as e:
        logging.exception(f"An error occurred: {str(e)}")
        raise

if __name__ == "__main__":
    main()
    logging.info(f"Log file saved as '{log_filename}'")
