from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.direct_api import DirectAPI
import logging
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_metadata():
    # Test with multiple movies and shows
    test_items = [
        {"type": "movie", "title": "Oppenheimer", "imdb_id": "tt15398776"},
        {"type": "movie", "title": "Barbie", "imdb_id": "tt1517268"},
        {"type": "movie", "title": "Mission: Impossible - Dead Reckoning Part One", "imdb_id": "tt9603212"},
        {"type": "show", "title": "Lioness", "imdb_id": "tt13111078"}
    ]
    
    api = DirectAPI()
    
    # Test movies first
    logger.info("\n=== TESTING MOVIES ===")
    for item in [i for i in test_items if i['type'] == 'movie']:
        logger.info(f"\nTesting {item['title']} (IMDb: {item['imdb_id']})")
        
        # Get full metadata
        logger.info("Getting full movie metadata:")
        metadata, source = api.get_movie_metadata(item['imdb_id'])
        if metadata and 'aliases' in metadata:
            logger.info(f"Aliases from full metadata (source: {source}):")
            logger.info(json.dumps(metadata['aliases'], indent=2))
        else:
            logger.warning("No aliases found in full movie metadata")
        
        # Get aliases directly
        logger.info("\nGetting movie aliases directly:")
        aliases, source = api.get_movie_aliases(item['imdb_id'])
        if aliases:
            logger.info(f"Aliases from direct call (source: {source}):")
            logger.info(json.dumps(aliases, indent=2))
        else:
            logger.warning("No aliases found through direct call")
    
    # Test shows
    logger.info("\n=== TESTING SHOWS ===")
    for item in [i for i in test_items if i['type'] == 'show']:
        logger.info(f"\nTesting {item['title']} (IMDb: {item['imdb_id']})")
        
        # Get full metadata
        logger.info("Getting full show metadata:")
        metadata, source = api.get_show_metadata(item['imdb_id'])
        if metadata and 'aliases' in metadata:
            logger.info(f"Aliases from full metadata (source: {source}):")
            logger.info(json.dumps(metadata['aliases'], indent=2))
        else:
            logger.warning("No aliases found in full show metadata")
        
        # Get aliases directly
        logger.info("\nGetting show aliases directly:")
        aliases, source = api.get_show_aliases(item['imdb_id'])
        if aliases:
            logger.info(f"Aliases from direct call (source: {source}):")
            logger.info(json.dumps(aliases, indent=2))
        else:
            logger.warning("No aliases found through direct call")

if __name__ == "__main__":
    test_metadata() 