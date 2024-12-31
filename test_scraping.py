import logging
from scraper.scraper import scrape
from queues.scraping_queue import ScrapingQueue
from pprint import pprint
import os
from api_tracker import setup_api_logging

# Set required environment variables
os.environ['USER_CONFIG'] = '/user/config'
os.environ['USER_DB_CONTENT'] = '/user/db_content'
os.environ['USER_LOGS'] = '/user/logs'

# Configure logging
logging.basicConfig(level=logging.INFO)
setup_api_logging()

# Mock queue manager class
class MockQueueManager:
    def generate_identifier(self, item):
        if item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        return f"{item['title']}_{item['imdb_id']}_{item['version']}"

def test_both_scraping_methods():
    # Test data for American Dad S21E10
    test_item = {
        'imdb_id': 'tt0397306',
        'tmdb_id': None,  # Added tmdb_id
        'title': 'American Dad!',
        'year': 2005,
        'type': 'episode',
        'season_number': 21,
        'episode_number': 10,
        'version': '1080p',
        'filled_by_title': 'American Dad S21E10 Idiot Rich 1080p DSNP WEB-DL DDP5 1 H 264-NTb[TGx]',
        'genres': []  # Added genres
    }

    print("\n=== Testing direct scraper.scrape() ===")
    direct_results, _ = scrape(
        imdb_id=test_item['imdb_id'],
        tmdb_id=test_item['tmdb_id'],
        title=test_item['title'],
        year=test_item['year'],
        content_type=test_item['type'],
        version=test_item['version'],
        season=test_item['season_number'],
        episode=test_item['episode_number'],
        genres=test_item['genres']
    )
    
    print("\nDirect scrape results:")
    for idx, result in enumerate(direct_results):
        print(f"Result {idx}: {result.get('title')}")

    print("\n=== Testing ScrapingQueue.scrape_with_fallback() ===")
    scraping_queue = ScrapingQueue()
    mock_queue_manager = MockQueueManager()
    queue_results, _ = scraping_queue.scrape_with_fallback(test_item, False, mock_queue_manager)
    
    print("\nQueue scrape results:")
    for idx, result in enumerate(queue_results):
        print(f"Result {idx}: {result.get('title')}")

    # Compare results
    print("\n=== Comparing Results ===")
    direct_titles = set(r.get('title') for r in direct_results)
    queue_titles = set(r.get('title') for r in queue_results)
    
    print("\nTitles only in direct scrape:")
    for title in direct_titles - queue_titles:
        print(f"- {title}")
    
    print("\nTitles only in queue scrape:")
    for title in queue_titles - direct_titles:
        print(f"- {title}")

    # Check if our current filled_by_title exists in either result set
    current_title = test_item['filled_by_title']
    print(f"\nChecking for current title: {current_title}")
    print(f"Found in direct scrape: {current_title in direct_titles}")
    print(f"Found in queue scrape: {current_title in queue_titles}")

if __name__ == "__main__":
    test_both_scraping_methods()
