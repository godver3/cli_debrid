import logging
import os
import pickle
from datetime import datetime, timedelta
import feedparser
from typing import List, Dict, Any, Optional, Tuple, Union
from utilities.settings import get_setting
from database.database_reading import get_media_item_presence, get_media_item_presence_overall
import requests
import xml.etree.ElementTree as ET

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
PLEX_RSS_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'plex_rss_cache.pkl')
CACHE_EXPIRY_DAYS = 7
# Plex caps the watchlist RSS feed at 25 items (no pagination) and its CDN may serve it up
# to ~48h stale; see https://forums.plex.tv/t/watchlist-rss-feed-capped-at-25-items/933961
PLEX_RSS_ITEM_CAP = 25

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
    """Extract IMDB ID from a Plex RSS item guid (tvdb:// ids are converted without Trakt)."""
    if 'imdb://' in guid:
        return guid.split('imdb://')[1].strip()
    elif 'tvdb://' in guid:
        tvdb_id = guid.split('tvdb://')[1].strip()
        try:
            from cli_battery.app.tvdb_client import convert_tvdb_to_imdb
            imdb_id = convert_tvdb_to_imdb(tvdb_id)
            if imdb_id:
                logging.debug(f"Converted TVDB ID {tvdb_id} to IMDB ID {imdb_id} for {title if title else 'Unknown title'}")
                return imdb_id
            logging.warning(f"Could not convert TVDB ID {tvdb_id} to an IMDB ID for {title if title else 'Unknown title'} "
                            f"(needs a TVDB or TMDB API key in settings)")
        except Exception as e:
            logging.error(f"Error converting TVDB ID {tvdb_id} to IMDB ID: {str(e)}")
    return None

def _local_name(tag: str) -> str:
    return tag.rsplit('}', 1)[-1].lower()

def fetch_plex_rss_entries(rss_url: str) -> List[Dict[str, Any]]:
    """Fetch a Plex RSS feed and return [{'title', 'guids', 'category'}] per item.

    Plex items can carry more than one <guid> (imdb/tmdb/tvdb); feedparser keeps only one,
    so the XML is parsed directly. feedparser is the fallback for malformed XML, and its
    entries are still used when it flags a non-fatal 'bozo' problem.
    """
    response = requests.get(rss_url, timeout=30)
    response.raise_for_status()
    content = response.content

    try:
        root = ET.fromstring(content)
        entries = []
        for item in root.iter():
            if _local_name(item.tag) not in ('item', 'entry'):
                continue
            entry = {'title': 'Unknown title', 'guids': [], 'category': None}
            for child in item:
                name = _local_name(child.tag)
                text = (child.text or '').strip()
                if name == 'title' and text:
                    entry['title'] = text
                elif name in ('guid', 'id') and text:
                    entry['guids'].append(text)
                elif name == 'category':
                    entry['category'] = text or child.get('term')
            entries.append(entry)
        return entries
    except ET.ParseError as e:
        logging.warning(f"Plex RSS feed is not well-formed XML ({e}); falling back to feedparser.")

    feed = feedparser.parse(content)
    if feed.bozo:
        if not feed.entries:
            raise ValueError(f"Error parsing RSS feed: {feed.bozo_exception}")
        logging.warning(f"Plex RSS feed parsed with warnings ({feed.bozo_exception}); using the {len(feed.entries)} entries that were read.")
    entries = []
    for e in feed.entries:
        guids = [g for g in (e.get('guid'), e.get('id')) if g]
        entries.append({'title': e.get('title', 'Unknown title'), 'guids': list(dict.fromkeys(guids)), 'category': e.get('category')})
    return entries

def resolve_imdb_and_type(guids: List[str], title: str, media_type: str = None) -> Tuple[Optional[str], Optional[str]]:
    """Pick an IMDB ID from an item's guids: imdb:// first, then tmdb:// and tvdb:// conversion.

    ``media_type`` is 'movie'/'tv', or None when the feed item has no <category>. Then a tmdb://
    guid is treated as a movie (the previous default; TMDB numbers movies and shows separately, so
    trying a show first could match an unrelated title) and a tvdb:// guid means a show.
    Returns (imdb_id, media_type).
    """
    for guid in guids:
        if 'imdb://' in guid:
            return guid.split('imdb://')[1].strip(), media_type
    tmdb_types = [media_type or 'movie']  # no <category>: movie, as before (TMDB numbers shows and movies separately, so guessing show first could pick an unrelated title)
    for guid in guids:
        if 'tmdb://' in guid:
            tmdb_id = guid.split('tmdb://')[1].strip()
            for candidate in tmdb_types:
                try:
                    from cli_battery.app.direct_api import DirectAPI
                    imdb_id, source = DirectAPI().tmdb_to_imdb(tmdb_id, media_type='show' if candidate == 'tv' else 'movie')
                    if imdb_id:
                        logging.debug(f"Converted TMDB ID {tmdb_id} to IMDB ID {imdb_id} for {title} via {source}")
                        return imdb_id, candidate
                    logging.warning(f"Could not convert TMDB ID {tmdb_id} ({candidate}) to IMDB ID for {title}")
                except Exception as e:
                    logging.error(f"Error converting TMDB ID {tmdb_id} to IMDB ID for {title}: {e}")
    for guid in guids:
        if 'tvdb://' in guid:
            imdb_id = extract_imdb_id(guid, title)
            if imdb_id:
                return imdb_id, media_type or 'tv'
    return None, media_type

def resolve_imdb_from_guids(guids: List[str], title: str, media_type: str) -> str:
    return resolve_imdb_and_type(guids, title, media_type)[0]

def get_wanted_from_plex_rss(rss_url: str, versions: Dict[str, bool], read_only: bool = False, detail: str = 'Plex RSS') -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """Fetch a Plex RSS watchlist. read_only=True never suppresses collected titles (used by label sync)."""
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
        entries = fetch_plex_rss_entries(rss_url)

        logging.info(f"Successfully parsed RSS feed. Found {len(entries)} entries")
        if entries and len(entries) % PLEX_RSS_ITEM_CAP == 0:
            # Plex's cap is 25 for a user feed; friends feeds have been seen returning exactly 50.
            logging.warning(
                f"Plex RSS feed returned exactly {len(entries)} items. Plex caps watchlist RSS feeds at "
                f"{PLEX_RSS_ITEM_CAP} items per feed and may serve them up to ~48h stale, so older watchlist items "
                f"are probably not visible. Use the 'My Plex Watchlist', 'Plex Friends Watchlist' or "
                f"'Other Plex Watchlist' source to see the full list."
            )
        skipped_count = 0
        skipped_titles = []
        cache_skipped = 0
        removed_count = 0
        retained_series_count = 0

        # Get removal settings
        should_remove = get_setting('Debug', 'plex_watchlist_removal', False)
        keep_series = get_setting('Debug', 'plex_watchlist_keep_series', False)

        if should_remove:
            logging.debug("Plex RSS item removal (if collected) enabled")
            if keep_series:
                logging.debug("Keeping collected TV series from RSS")
        
        for entry in entries:
            try:
                entry_title = entry['title']
                # Content type from the RSS category; None when the feed omits it, in which
                # case the guids decide (tmdb tried as show then movie, tvdb means show).
                category = (entry.get('category') or '').strip().lower()
                media_type = 'tv' if category == 'show' else ('movie' if category == 'movie' else None)

                if not entry['guids']:
                    logging.debug(f"Entry missing guid: {entry_title}")
                    skipped_count += 1
                    skipped_titles.append(entry_title)
                    continue

                imdb_id, media_type = resolve_imdb_and_type(entry['guids'], entry_title, media_type)
                media_type = media_type or 'movie'
                if not imdb_id:
                    logging.debug(f"Could not extract IMDB ID from guids: {entry['guids']} for title: {entry_title}")
                    skipped_count += 1
                    skipped_titles.append(entry_title)
                    continue

                logging.debug(f"Processing entry: {entry_title} (IMDB: {imdb_id})")

                # Check if the item is already collected
                item_state = get_media_item_presence_overall(imdb_id=imdb_id)
                monitor_missing_episodes_only = False
                # read_only (label sync) lists every title: it must not hide collected ones.
                if item_state in ("Collected", "Partial") and should_remove and not read_only:
                    from content_checkers.plex_watchlist import should_remove_from_watchlist
                    suppress, reason = should_remove_from_watchlist(imdb_id, media_type, item_state)
                    if suppress:
                        logging.debug(f"Skipping (simulating removal) '{entry_title}' from RSS: {imdb_id} - {reason}")
                        removed_count += 1
                        continue # Simulate removal by suppressing the RSS item from ingestion.
                    if media_type == 'tv':
                        logging.debug(f"Retaining and processing collected TV series from RSS: {imdb_id} ('{entry_title}') - {reason}")
                        retained_series_count += 1
                        monitor_missing_episodes_only = True

                # Check cache
                if not disable_caching:
                    cache_key = f"{imdb_id}_{media_type}"
                    cache_item = cache.get(cache_key)
                    if cache_item:
                        last_processed = datetime.fromtimestamp(cache_item['timestamp'])
                        cache_age = current_time - last_processed
                        if cache_age < timedelta(days=CACHE_EXPIRY_DAYS):
                            logging.debug(f"Skipping {media_type} '{entry_title}' (IMDB: {imdb_id}) - cached {cache_age.days} days ago")
                            cache_skipped += 1
                            continue
                        else:
                            logging.debug(f"Cache expired for {media_type} '{entry_title}' (IMDB: {imdb_id}) - last processed {cache_age.days} days ago")
                    else:
                        logging.debug(f"New item found: {media_type} '{entry_title}' (IMDB: {imdb_id})")

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
                    'title': entry_title,
                    'imdb_id': imdb_id,
                    'media_type': media_type,
                    'source': 'plex_rss',
                    'content_source_detail': detail,
                    'monitor_missing_episodes_only': monitor_missing_episodes_only,
                }

                processed_items.append(item)
                logging.debug(f"Added {media_type} '{entry_title}' (IMDB: {imdb_id}) to processed items")

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
        logging.info(f"- Total entries: {len(entries)}")
        logging.info(f"- Skipped (no IMDB ID): {skipped_count}")
        if skipped_titles:
            logging.info(f"- Skipped titles: {', '.join(repr(t) for t in skipped_titles[:25])}")
        if should_remove:
            logging.info(f"- Items 'removed' (collected and not kept): {removed_count}")
            logging.info(f"- Retained TV series processed: {retained_series_count}")
        if not disable_caching:
            logging.info(f"- Items skipped (cached): {cache_skipped}")
        logging.info(f"- Items added to wanted: {sum(len(items) for items, _ in all_wanted_items)}")

        return all_wanted_items

    except Exception as e:
        logging.error(f"Error processing Plex RSS feed: {str(e)}")
        return [([], versions)]

def _friend_feed_label(rss_url: str) -> str:
    """Non-secret label for a friend's feed: the URL is a credential, so only its tail is shown."""
    return f"Friend RSS ...{rss_url.rstrip('/')[-6:]}"

def get_wanted_from_friends_plex_rss(rss_urls: Union[str, List[str]], versions: Dict[str, bool], read_only: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
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
            items = get_wanted_from_plex_rss(rss_url, versions, read_only=read_only, detail=_friend_feed_label(rss_url))
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
