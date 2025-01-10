from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.direct_api import DirectAPI
import logging
import json
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_show_metadata():
    # Test with Lioness
    imdb_id = "tt13111078"
    
    # Run the test twice to verify caching
    for i in range(2):
        logger.info(f"\n=== Run #{i+1} ===")
        logger.info(f"Testing show metadata for Lioness (IMDb ID: {imdb_id})")
        
        # Test DirectAPI functionality first
        logger.info("\nTesting DirectAPI get_show_aliases:")
        api = DirectAPI()
        aliases, source = api.get_show_aliases(imdb_id)
        if aliases:
            logger.info(f"Aliases from DirectAPI (source: {source}):")
            logger.info(json.dumps(aliases, indent=2))
        else:
            logger.warning("No aliases found through DirectAPI")
        
        # Test TraktMetadata functionality for comparison
        logger.info("\nTesting TraktMetadata aliases:")
        trakt = TraktMetadata()
        show_data = trakt._get_show_data(imdb_id)
        if show_data and 'ids' in show_data:
            slug = show_data['ids']['slug']
            aliases = trakt._get_show_aliases(slug)
            if aliases:
                logger.info("Show aliases from TraktMetadata:")
                logger.info(json.dumps(aliases, indent=2))
            else:
                logger.warning("No aliases found through TraktMetadata")
        
        # Test full show metadata to verify aliases are included
        logger.info("\nTesting full show metadata:")
        show_metadata = trakt.get_show_metadata(imdb_id)
        if show_metadata and 'aliases' in show_metadata:
            logger.info("Aliases in full show metadata:")
            logger.info(json.dumps(show_metadata['aliases'], indent=2))
        else:
            logger.warning("No aliases found in full show metadata")
        
        if i == 0:
            logger.info("\nWaiting 2 seconds before second run...")
            time.sleep(2)

if __name__ == "__main__":
    test_show_metadata() 