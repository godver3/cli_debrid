import logging
import sys
import os

# Add the project root to the Python path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.append(project_root)

# Set required environment variables
os.environ['USER_CONFIG'] = '/user/config'

from queues.scraping_queue import ScrapingQueue
import config_manager
import settings

# Set up logging
logging.basicConfig(level=logging.INFO)

# Initialize api_logger
api_logger = logging.getLogger('api_calls')
api_logger.setLevel(logging.INFO)
api_logger.propagate = False
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
api_logger.addHandler(ch)

# Make api_logger global
import api_tracker
api_tracker.api_logger = api_logger

# Initialize config and queue
settings.ensure_settings_file()  # Make sure settings file exists
config = config_manager.load_config()  # Load config directly
queue = ScrapingQueue()  # Initialize without config
queue.config = config    # Set config after initialization

# Test item for American Dad S21E09
test_item = {
    'imdb_id': 'tt0397306',  # American Dad IMDB ID
    'tmdb_id': '1433',       # American Dad TMDB ID
    'title': 'American Dad!',
    'year': 2005,
    'type': 'episode',
    'version': 'default',
    'season_number': 21,
    'episode_number': 3,
    'genres': []
}

# Create a simple mock queue manager
class MockQueueManager:
    def generate_identifier(self, item):
        return f"{item['title']} S{item.get('season_number', 0)}E{item.get('episode_number', 0)}"

queue_manager = MockQueueManager()

# Run the scrape without multi-pack
print("\nTesting single episode scrape for American Dad S21E03...")
results, filtered_out = queue.scrape_with_fallback(test_item, False, queue_manager)

print(f"\nFound {len(results)} results for single episode:")
for i, result in enumerate(results, 1):
    print(f"\n{i}. Title: {result.get('title')}")
    print(f"   Size: {result.get('size', 0):.2f} GB")
    print(f"   Season/Episode Info: {result.get('parsed_info', {}).get('season_episode_info', {})}")
    print(f"   Source: {result.get('scraper', 'Unknown')}")

if filtered_out:
    print(f"\nFiltered out {len(filtered_out)} results for single episode:")
    for i, result in enumerate(filtered_out, 1):
        print(f"\n{i}. Title: {result.get('title')}")
        print(f"   Filter Reason: {result.get('filter_reason', 'Unknown')}")
        print(f"   Season/Episode Info: {result.get('parsed_info', {}).get('season_episode_info', {})}")
        print(f"   Source: {result.get('scraper', 'Unknown')}")

# Run the scrape with multi-pack
print("\nTesting multi-pack scrape for American Dad S21E03...")
results_multi, filtered_out_multi = queue.scrape_with_fallback(test_item, True, queue_manager)

print(f"\nFound {len(results_multi)} results for multi-pack:")
for i, result in enumerate(results_multi, 1):
    print(f"\n{i}. Title: {result.get('title')}")
    print(f"   Size: {result.get('size', 0):.2f} GB")
    print(f"   Season/Episode Info: {result.get('parsed_info', {}).get('season_episode_info', {})}")
    print(f"   Source: {result.get('scraper', 'Unknown')}")

if filtered_out_multi:
    print(f"\nFiltered out {len(filtered_out_multi)} results for multi-pack:")
    for i, result in enumerate(filtered_out_multi, 1):
        print(f"\n{i}. Title: {result.get('title')}")
        print(f"   Filter Reason: {result.get('filter_reason', 'Unknown')}")
        print(f"   Season/Episode Info: {result.get('parsed_info', {}).get('season_episode_info', {})}")
        print(f"   Source: {result.get('scraper', 'Unknown')}")
