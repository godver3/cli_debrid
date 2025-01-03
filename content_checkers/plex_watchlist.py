import logging
from plexapi.myplex import MyPlexAccount
from typing import List, Dict, Any, Tuple
from settings import get_setting
from database import get_media_item_presence

def get_plex_client():
    plex_token = get_setting('Plex', 'token')
    plex_url = get_setting('Plex', 'url')
    
    if not plex_token:
        logging.error("Plex token not configured. Please add Plex token in settings.")
        return None
        
    if not plex_url:
        logging.error("Plex URL not configured. Please add Plex URL in settings.")
        return None
    
    try:
        account = MyPlexAccount(token=plex_token)
        return account
    except Exception as e:
        logging.error(f"Error connecting to Plex: {e}")
        return None

def get_wanted_from_plex_watchlist(versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    processed_items = []
    
    account = get_plex_client()
    if not account:
        return [([], versions)]
    
    try:
        watchlist = account.watchlist()
        
        for item in watchlist:
            # Debug print
            logging.info(f"Processing Plex watchlist item: {item.title}")
            
            # Get IMDB ID if available
            guid = item.guid
            imdb_id = None
            
            if 'imdb://' in guid:
                imdb_id = guid.split('imdb://')[1].split('?')[0]
            
            if not imdb_id:
                logging.warning(f"Skipping item due to missing IMDB ID: {item.title}")
                continue
            
            media_type = 'movie' if item.type == 'movie' else 'tv'
            
            wanted_item = {
                'imdb_id': imdb_id,
                'title': item.title,
                'year': item.year,
                'type': media_type
            }
            
            # Debug print
            logging.info(f"Processed item: {wanted_item}")
            processed_items.append(wanted_item)
            
    except Exception as e:
        logging.error(f"Error fetching Plex watchlist: {e}")
        return [([], versions)]
    
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items
