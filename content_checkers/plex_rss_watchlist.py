import logging
import os
import pickle
from datetime import datetime, timedelta
import feedparser
from typing import List, Dict, Any, Tuple, Union
from settings import get_setting
from database.database_reading import get_media_item_presence
from cli_battery.app.metadata_manager import MetadataManager
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.database import DatabaseManager

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
PLEX_RSS_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'plex_rss_cache.pkl')
CACHE_EXPIRY_DAYS = 7

def load_rss_cache(cache_file):
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as f:
                return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError) as e:
        logging.warning(f"Error loading Plex RSS cache: {e}. Creating a new cache.")
    return {}

def save_rss_cache(cache, cache_file):
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        logging.error(f"Error saving Plex RSS cache: {e}")

def extract_imdb_id(guid: str, title: str = None) -> str:
    """Extract IMDB ID from a Plex RSS item guid."""
    if 'imdb://' in guid:
        return guid.split('imdb://')[1].strip()
    elif 'tvdb://' in guid:
        tvdb_id = guid.split('tvdb://')[1].strip()
        try:
            # First check if we have the mapping in our database
            db_manager = DatabaseManager()
            imdb_id = db_manager.get_imdb_from_tvdb(tvdb_id)
            if imdb_id:
                logging.debug(f"Found IMDB ID {imdb_id} for TVDB ID {tvdb_id} in database")
                return imdb_id

            # If not in database, use Trakt to get it and store for future
            trakt = TraktMetadata()
            url = f"{trakt.base_url}/search/tvdb/{tvdb_id}?type=show"
            response = trakt._make_request(url)
            if response and response.status_code == 200:
                results = response.json()
                if results and len(results) > 0:
                    show = results[0]['show']
                    imdb_id = show['ids'].get('imdb')
                    if imdb_id:
                        # Store the mapping for future use
                        db_manager.add_tvdb_to_imdb_mapping(tvdb_id, imdb_id, 'show')
                        logging.debug(f"Successfully converted TVDB ID {tvdb_id} to IMDB ID {imdb_id} for {title if title else 'Unknown title'}")
                        return imdb_id
                    else:
                        logging.warning(f"Could not find IMDB ID for TVDB ID {tvdb_id} ({title if title else 'Unknown title'})")
            else:
                logging.warning(f"Failed to search TVDB ID {tvdb_id}. Status code: {response.status_code if response else 'No response'}")
        except Exception as e:
            logging.error(f"Error converting TVDB ID {tvdb_id} to IMDB ID: {str(e)}")
    return None

def get_wanted_from_plex_rss(rss_url: str, versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    all_wanted_items = []
    processed_items = []
    disable_caching = True  # Hardcoded to True
    cache = {} if disable_caching else load_rss_cache(PLEX_RSS_CACHE_FILE)
    current_time = datetime.now()

    # Validate URL
    if not rss_url or not isinstance(rss_url, str) or not rss_url.startswith('http'):
        logging.error(f"Invalid RSS URL: {rss_url}")
        return [([], versions)]

    try:
        logging.info(f"Fetching RSS feed from URL: {rss_url}")
        # Parse the RSS feed
        feed = feedparser.parse(rss_url)
        if feed.bozo:  # Check if there was an error parsing the feed
            logging.error(f"Error parsing RSS feed: {feed.bozo_exception}")
            return [([], versions)]

        logging.info(f"Successfully parsed RSS feed. Found {len(feed.entries)} entries")
        skipped_count = 0
        cache_skipped = 0

        for entry in feed.entries:
            try:
                # Extract IMDB ID from the guid
                if not hasattr(entry, 'guid'):
                    logging.debug(f"Entry missing guid: {entry.title if hasattr(entry, 'title') else 'Unknown title'}")
                    skipped_count += 1
                    continue

                imdb_id = extract_imdb_id(entry.guid, entry.title)
                if not imdb_id:
                    logging.debug(f"Could not extract IMDB ID from guid: {entry.guid}")
                    skipped_count += 1
                    continue

                logging.debug(f"Processing entry: {entry.title} (IMDB: {imdb_id})")

                # Get content type from RSS category
                media_type = 'movie'  # default to movie
                if hasattr(entry, 'category') and entry.category.lower() == 'show':
                    media_type = 'tv'

                # Check cache
                if not disable_caching:
                    cache_key = f"{imdb_id}_{media_type}"
                    cache_item = cache.get(cache_key)
                    if cache_item:
                        last_processed = datetime.fromtimestamp(cache_item['timestamp'])
                        cache_age = current_time - last_processed
                        if cache_age < timedelta(days=CACHE_EXPIRY_DAYS):
                            logging.debug(f"Skipping {media_type} '{entry.title}' (IMDB: {imdb_id}) - cached {cache_age.days} days ago")
                            cache_skipped += 1
                            continue
                        else:
                            logging.debug(f"Cache expired for {media_type} '{entry.title}' (IMDB: {imdb_id}) - last processed {cache_age.days} days ago")
                    else:
                        logging.debug(f"New item found: {media_type} '{entry.title}' (IMDB: {imdb_id})")

                    # Add or update cache entry
                    cache[cache_key] = {
                        'timestamp': current_time.timestamp(),
                        'data': {
                            'imdb_id': imdb_id,
                            'media_type': media_type
                        }
                    }

                # Create item dictionary
                item = {
                    'title': entry.title,
                    'imdb_id': imdb_id,
                    'media_type': media_type,
                    'source': 'plex_rss'
                }

                processed_items.append(item)
                logging.debug(f"Added {media_type} '{entry.title}' (IMDB: {imdb_id}) to processed items")

                if len(processed_items) >= 20:
                    all_wanted_items.append((processed_items.copy(), versions.copy()))
                    processed_items.clear()

            except Exception as e:
                logging.error(f"Error processing RSS entry: {str(e)}")
                continue

        if processed_items:
            all_wanted_items.append((processed_items.copy(), versions.copy()))

        if not disable_caching:
            save_rss_cache(cache, PLEX_RSS_CACHE_FILE)

        logging.info(f"Plex RSS Watchlist Summary:")
        logging.info(f"- Total entries: {len(feed.entries)}")
        logging.info(f"- Skipped (no IMDB ID): {skipped_count}")
        logging.info(f"- Items added to wanted: {sum(len(items) for items, _ in all_wanted_items)}")

        return all_wanted_items

    except Exception as e:
        logging.error(f"Error processing Plex RSS feed: {str(e)}")
        return [([], versions)]

def get_wanted_from_friends_plex_rss(rss_urls: Union[str, List[str]], versions: Dict[str, bool]) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """Get wanted items from one or more friends' Plex RSS feeds."""
    all_wanted_items = []
    
    # Convert single URL to list if needed
    if isinstance(rss_urls, str):
        rss_urls = [rss_urls]
    elif not rss_urls:
        logging.warning("No friend RSS URLs provided")
        return [([], versions)]
        
    for rss_url in rss_urls:
        if not rss_url or not isinstance(rss_url, str) or not rss_url.startswith('http'):
            logging.warning(f"Skipping invalid RSS URL: {rss_url}")
            continue
            
        try:
            items = get_wanted_from_plex_rss(rss_url, versions)
            if items and items[0] and items[0][0]:  # Check if we got any valid items
                all_wanted_items.extend(items)
                logging.info(f"Successfully processed friend's RSS feed: {rss_url}")
            else:
                logging.warning(f"No valid items found in friend's RSS feed: {rss_url}")
        except Exception as e:
            logging.error(f"Error processing friend's Plex RSS feed {rss_url}: {str(e)}")
            continue

    if not all_wanted_items:
        logging.warning("No items found in any friend's RSS feeds")
        
    return all_wanted_items
