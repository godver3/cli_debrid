import logging
from plexapi.myplex import MyPlexAccount
from typing import List, Dict, Any, Tuple
from settings import get_setting
from database.database_reading import get_media_item_presence

def get_plex_client():
    plex_token = get_setting('Plex', 'token')
    
    if not plex_token:
        logging.error("Plex token not configured. Please add Plex token in settings.")
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
        # Check if watchlist removal is enabled
        should_remove = get_setting('Debug', 'plex_watchlist_removal', False)
        keep_series = get_setting('Debug', 'plex_watchlist_keep_series', False)

        if should_remove:
            logging.debug(f"Plex watchlist removal is enabled")
        if keep_series:
            logging.debug(f"Plex watchlist keep series is enabled, remove only movies")

        logging
        
        # Get the watchlist directly from PlexAPI
        watchlist = account.watchlist()
        
        # Process each item in the watchlist
        for item in watchlist:
            # Extract IMDB ID from the guids
            imdb_id = None
            for guid in item.guids:
                if 'imdb://' in guid.id:
                    imdb_id = guid.id.split('//')[1]
                    break
            
            if not imdb_id:
                logging.warning(f"Skipping item due to missing IMDB ID: {item.title}")
                continue
            
            media_type = 'movie' if item.type == 'movie' else 'tv'
            
            # Check if the item is already collected
            item_state = get_media_item_presence(imdb_id=imdb_id)
            if item_state == "Collected" and should_remove:
                # If it's a TV show and we want to keep series, skip removal
                if media_type == 'tv' and keep_series:
                    logging.info(f"Keeping collected TV series in watchlist: {item.title} ({imdb_id})")
                else:
                    # Remove from watchlist using the PlexAPI object directly
                    try:
                        account.removeFromWatchlist([item])
                        logging.info(f"Removed collected item from watchlist: {item.title} ({imdb_id})")
                        continue
                    except Exception as e:
                        logging.error(f"Failed to remove collected item from watchlist: {item.title} ({imdb_id}): {str(e)}")
            
            wanted_item = {
                'imdb_id': imdb_id,
                'media_type': media_type
            }
            
            processed_items.append(wanted_item)
            
        logging.info(f"Retrieved {len(processed_items)} total items from Plex watchlist")
        
    except Exception as e:
        logging.error(f"Error fetching Plex watchlist: {str(e)}")
        return [([], versions)]
    
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items
