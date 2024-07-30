import logging
from datetime import datetime, timedelta
from database import create_database, add_wanted_items, update_media_item_state, get_all_media_items
from queue_manager import QueueManager
from upgrading_db import create_upgrading_table, get_items_to_check, update_check_count, remove_from_upgrading
import random
from unittest.mock import patch

logging.basicConfig(level=logging.DEBUG)

def create_mock_item(tmdb_id, title, year, item_type='movie', season_number=None, episode_number=None):
    return {
        'tmdb_id': str(tmdb_id),
        'title': title,
        'year': year,
        'type': item_type,
        'season_number': season_number,
        'episode_number': episode_number,
        'release_date': datetime.now().strftime('%Y-%m-%d')
    }

def create_mock_scrape_result(title, quality):
    return {
        'title': f"{title} {quality}",
        'magnet': f"magnet:?xt=urn:btih:{random.randbytes(20).hex()}"
    }

def mock_scrape(item):
    qualities = ['720p', '1080p', '2160p']
    return [create_mock_scrape_result(item['title'], quality) for quality in qualities]

# Mock external API calls
def mock_get_overseerr_details(*args, **kwargs):
    return {'title': 'Mocked Title', 'releaseDate': '2023-01-01'}

def mock_get_release_date(*args, **kwargs):
    return '2023-01-01'

def mock_scrape(*args, **kwargs):
    return [create_mock_scrape_result('Mocked Title', '1080p')]

@patch('metadata.metadata.get_overseerr_show_details', mock_get_overseerr_details)
@patch('metadata.metadata.get_overseerr_movie_details', mock_get_overseerr_details)
@patch('metadata.metadata.get_release_date', mock_get_release_date)
@patch('scraper.scraper.scrape', mock_scrape)
def simulate_upgrading_process():
    # Initialize databases
    create_database()
    create_upgrading_table()

    # Create a QueueManager instance
    qm = QueueManager()

    # Add some mock items
    mock_items = [
        create_mock_item(1234, 'Test Movie 1', 2021),
        create_mock_item(2345, 'Test Movie 2', 2022),
        create_mock_item(3456, 'Test Series', 2023, 'episode', 1, 1)
    ]
    add_wanted_items(mock_items)

    # Retrieve added items from the database
    db_items = get_all_media_items()
    if not db_items:
        logging.error("No items found in the database. Exiting simulation.")
        return

    # Simulate the process over 4 days
    simulation_days = 4
    hours_per_tick = 1
    ticks_per_day = 24 // hours_per_tick
    total_ticks = simulation_days * ticks_per_day

    for tick in range(total_ticks):
        current_time = datetime.now() + timedelta(hours=tick * hours_per_tick)
        logging.info(f"Simulation time: {current_time}")

        # Process queues
        qm.process_wanted()
        qm.process_scraping()
        qm.process_adding()
        qm.process_checking()

        # Simulate items being collected
        if tick % 4 == 0:  # Every 4 hours in simulation time
            for item in db_items:
                update_media_item_state(item['id'], 'Collected', 
                                        filled_by_title=f"{item['title']} 1080p",
                                        filled_by_magnet="magnet:?xt=urn:btih:example")

        # Process upgrading queue
        qm.process_upgrading()

        # Check upgrading items
        upgrading_items = get_items_to_check()
        for item in upgrading_items:
            logging.info(f"Checking for upgrade: {item['title']}")
            scrape_results = mock_scrape(item)
            best_result = scrape_results[0]
            if best_result['title'] != item['filled_by_title']:
                logging.info(f"Found potential upgrade for {item['title']}: {best_result['title']}")
                update_media_item_state(item['original_id'], 'Adding', 
                                        filled_by_title=best_result['title'],
                                        filled_by_magnet=best_result['magnet'])
            update_check_count(item['original_id'])

        # Simulate processing the Adding queue
        qm.process_adding()

        # Log queue states
        for queue_name, queue_items in qm.queues.items():
            logging.info(f"{queue_name} queue: {len(queue_items)} items")

    # Final check
    upgrading_items = get_items_to_check()
    logging.info(f"Final number of items in Upgrading queue: {len(upgrading_items)}")

if __name__ == "__main__":
    simulate_upgrading_process()
