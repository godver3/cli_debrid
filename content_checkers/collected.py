import logging
from typing import List, Dict, Any, Tuple
from database import get_all_media_items
from settings import get_all_settings

def get_wanted_from_collected() -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    collected_sources = [data for source, data in content_sources.items() if source.startswith('Collected') and data.get('enabled', False)]
    
    if not collected_sources:
        logging.warning("No enabled Collected sources found in settings.")
        return []

    all_wanted_items = []

    for source in collected_sources:
        versions = source.get('versions', {})

        wanted_items = get_all_media_items(state="Wanted", media_type="episode")
        collected_items = get_all_media_items(state="Collected", media_type="episode")
        
        all_items = wanted_items + collected_items
        consolidated_items = {}

        for item in all_items:
            imdb_id = item['imdb_id']
            if imdb_id not in consolidated_items:
                consolidated_items[imdb_id] = {
                    'imdb_id': imdb_id,
                    'media_type': 'tv'
                }

        result = list(consolidated_items.values())

        # Debug printing
        logging.info(f"Retrieved {len(result)} unique TV shows from local database")
        for item in result:
            logging.debug(f"IMDB ID: {item['imdb_id']}, Media Type: {item['media_type']}")

        all_wanted_items.append((result, versions))

    return all_wanted_items