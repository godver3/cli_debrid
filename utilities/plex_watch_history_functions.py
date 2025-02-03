import asyncio
import os
import sqlite3
import logging
from plexapi.server import PlexServer
from settings import get_setting
from cli_battery.app.trakt_metadata import TraktMetadata
from datetime import datetime

async def get_watch_history_from_plex():
    """
    Retrieves the user's complete Plex watch history from both account history and server libraries,
    then stores it in the database. This dual-source approach ensures maximum coverage of watch history.
    Returns a dictionary with counts of processed movies and episodes.
    """
    try:
        trakt = TraktMetadata()
        processed = {
            'movies': 0,
            'episodes': 0,
            'account_items': 0,
            'server_items': 0
        }
        
        # Get Plex connection details
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')
        
        if not plex_url or not plex_token:
            logging.error("Plex URL or token not configured")
            return {'movies': 0, 'episodes': 0, 'account_items': 0, 'server_items': 0}
            
        logging.info("Connecting to Plex server...")
        # Connect to Plex server
        plex = PlexServer(plex_url, plex_token)
        
        # Set up database
        db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        db_path = os.path.join(db_dir, 'watch_history.db')
        
        logging.info("Setting up watch history database...")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create table if it doesn't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watch_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                type TEXT NOT NULL,
                watched_at TIMESTAMP,
                media_id TEXT,
                imdb_id TEXT,
                tmdb_id TEXT,
                tvdb_id TEXT,
                season INTEGER,
                episode INTEGER,
                show_title TEXT,
                duration INTEGER,
                watch_progress INTEGER,
                source TEXT,
                UNIQUE(title, type, date(watched_at)) ON CONFLICT REPLACE,
                UNIQUE(show_title, season, episode, date(watched_at)) ON CONFLICT REPLACE
            )
        ''')
        
        # Get all watched history from account
        logging.info("Fetching history from Plex account...")
        account = plex.myPlexAccount()
        logging.info(f"Authenticated as user: {account.username} (ID: {account.id})")
        
        # Get history directly from the account
        account_history = account.history()
        
        # Process account history
        total_items = len(account_history)
        logging.info(f"Found {total_items} items in account history")
        
        # Process account history items
        for i, item in enumerate(account_history, 1):
            try:
                if i % 100 == 0 or i == total_items:
                    logging.info(f"Processing account item {i}/{total_items} ({(i/total_items*100):.1f}%)")
                
                # Get all available item information for logging
                item_info = {
                    'title': getattr(item, 'title', None),
                    'type': getattr(item, 'type', None),
                    'viewedAt': getattr(item, 'viewedAt', None),
                    'ratingKey': getattr(item, 'ratingKey', None),
                    'seasonNumber': getattr(item, 'seasonNumber', None),
                    'index': getattr(item, 'index', None),
                    'grandparentTitle': getattr(item, 'grandparentTitle', None),
                    'duration': getattr(item, 'duration', None),
                    'viewOffset': getattr(item, 'viewOffset', None),
                    'guid': getattr(item, 'guid', None),
                    'raw_guids': str([{'id': g.id, 'attrs': dir(g)} for g in item.guids]) if hasattr(item, 'guids') else 'No guids attribute',
                    'has_guids_attr': hasattr(item, 'guids'),
                    'guids_len': len(item.guids) if hasattr(item, 'guids') else 0
                }
                
                success = await process_watch_history_item(cursor, item_info, item, trakt, 'account', processed)
                if success:
                    processed['account_items'] += 1
                
            except Exception as e:
                logging.error(f"Error processing account item: {str(e)}")
                continue
        
        # Get history from server libraries
        logging.info("\nFetching history from Plex server libraries...")
        
        # Process each library
        for library in plex.library.sections():
            logging.info(f"Processing library: {library.title}")
            
            if library.type == 'movie':
                # Get watched movies
                watched_movies = library.search(unwatched=False)
                total_movies = len(watched_movies)
                logging.info(f"Found {total_movies} potentially watched movies in {library.title}")
                
                for i, video in enumerate(watched_movies, 1):
                    if i % 50 == 0 or i == total_movies:
                        logging.info(f"Processing movie {i}/{total_movies} in {library.title}")
                    
                    if video.isWatched:
                        try:
                            # Create item_info dictionary for server items
                            item_info = {
                                'title': video.title,
                                'type': 'movie',
                                'viewedAt': getattr(video, 'lastViewedAt', None),
                                'ratingKey': video.ratingKey,
                                'duration': video.duration,
                                'viewOffset': getattr(video, 'viewOffset', None),
                                'guid': video.guid,
                                'raw_guids': str([{'id': g.id, 'attrs': dir(g)} for g in video.guids]) if hasattr(video, 'guids') else 'No guids attribute',
                                'has_guids_attr': hasattr(video, 'guids'),
                                'guids_len': len(video.guids) if hasattr(video, 'guids') else 0
                            }
                            
                            success = await process_watch_history_item(cursor, item_info, video, trakt, 'server', processed)
                            if success:
                                processed['server_items'] += 1
                                
                        except Exception as e:
                            logging.error(f"Error processing server movie {video.title}: {str(e)}")
                            continue
                        
            elif library.type == 'show':
                # Get watched episodes
                shows = library.search()
                total_shows = len(shows)
                logging.info(f"Processing {total_shows} shows in {library.title}")
                
                for show_idx, show in enumerate(shows, 1):
                    if show_idx % 10 == 0 or show_idx == total_shows:
                        logging.info(f"Processing show {show_idx}/{total_shows} in {library.title}")
                    
                    try:
                        episodes = show.episodes()
                        for episode in episodes:
                            if episode.isWatched:
                                # Create item_info dictionary for server items
                                item_info = {
                                    'title': episode.title,
                                    'type': 'episode',
                                    'viewedAt': getattr(episode, 'lastViewedAt', None),
                                    'ratingKey': episode.ratingKey,
                                    'seasonNumber': episode.seasonNumber,
                                    'index': episode.index,
                                    'grandparentTitle': show.title,
                                    'duration': episode.duration,
                                    'viewOffset': getattr(episode, 'viewOffset', None),
                                    'guid': episode.guid,
                                    'raw_guids': str([{'id': g.id, 'attrs': dir(g)} for g in episode.guids]) if hasattr(episode, 'guids') else 'No guids attribute',
                                    'has_guids_attr': hasattr(episode, 'guids'),
                                    'guids_len': len(episode.guids) if hasattr(episode, 'guids') else 0
                                }
                                
                                success = await process_watch_history_item(cursor, item_info, episode, trakt, 'server', processed)
                                if success:
                                    processed['server_items'] += 1
                                    
                    except Exception as e:
                        logging.error(f"Error processing show {show.title}: {str(e)}")
                        continue
            else:
                logging.info(f"Skipping library type: {library.type}")
        
        conn.commit()
        conn.close()
        
        logging.info("\nWatch history sync complete!")
        logging.info(f"Account history items processed: {processed['account_items']}")
        logging.info(f"Server library items processed: {processed['server_items']}")
        logging.info(f"Total movies: {processed['movies']}")
        logging.info(f"Total episodes: {processed['episodes']}")
        
        return processed
        
    except Exception as e:
        logging.error(f"Error getting watch history: {str(e)}")
        return {'movies': 0, 'episodes': 0, 'account_items': 0, 'server_items': 0}

async def process_watch_history_item(cursor, item_info, item, trakt, source, processed):
    """
    Helper function to process a single watch history item and insert/update it in the database.
    Returns True if the item was successfully processed, False otherwise.
    """
    try:
        # Skip non-video content
        if item_info['type'] not in ['movie', 'episode']:
            logging.info(f"Skipping non-video content - '{item_info['title']}' (Type: {item_info['type']})")
            return False

        # Extract basic info and validate required fields
        title = item_info['title']
        if not title and item_info['type'] == 'episode' and item_info['grandparentTitle'] and item_info['seasonNumber'] is not None and item_info['index'] is not None:
            # Generate title for episodes like "Friends - S04E24"
            title = f"{item_info['grandparentTitle']} - S{item_info['seasonNumber']:02d}E{item_info['index']:02d}"
            logging.info(f"Generated title for episode: {title}")
        
        if not title:
            logging.warning(f"Skipping item: Missing required field 'title'. Item info: {item_info}")
            return False
            
        watched_at = item_info['viewedAt']
        if not watched_at:
            logging.warning(f"Skipping item '{title}': Missing required field 'viewedAt'. Item info: {item_info}")
            return False
        
        # Get external IDs
        imdb_id = None
        tmdb_id = None
        tvdb_id = None
        
        if hasattr(item, 'guids'):
            for guid in item.guids:
                guid_str = str(guid.id)
                if 'imdb://' in guid_str:
                    imdb_id = guid_str.split('imdb://')[1].split('?')[0]
                elif 'tmdb://' in guid_str:
                    tmdb_id = guid_str.split('tmdb://')[1].split('?')[0]
                elif 'tvdb://' in guid_str:
                    tvdb_id = guid_str.split('tvdb://')[1].split('?')[0]
        
        if not imdb_id:
            # Try to find IMDb ID using existing database entry or Trakt
            imdb_id = await find_imdb_id(cursor, item_info, title, trakt)
        
        # Get or generate media_id
        media_id = str(item_info['ratingKey']) if item_info['ratingKey'] else generate_synthetic_media_id(item_info, title)
        
        if not media_id:
            logging.warning(f"Skipping item '{title}': Could not generate media ID. Item info: {item_info}")
            return False
        
        # Insert or update the database entry
        if item_info['type'] == 'movie':
            success = insert_or_update_movie(cursor, title, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                                          item_info['duration'], item_info['viewOffset'], source)
            if success:
                processed['movies'] += 1
        else:
            success = insert_or_update_episode(cursor, title, watched_at, media_id, imdb_id, tmdb_id, tvdb_id,
                                            item_info['seasonNumber'], item_info['index'], item_info['grandparentTitle'],
                                            item_info['duration'], item_info['viewOffset'], source)
            if success:
                processed['episodes'] += 1
        
        return success
        
    except Exception as e:
        logging.error(f"Error processing item '{title}': {str(e)}")
        return False

async def find_imdb_id(cursor, item_info, title, trakt):
    """Helper function to find IMDb ID from database cache or Trakt"""
    media_type = item_info['type']
    
    # First check database cache
    if media_type == 'movie':
        cursor.execute('''
            SELECT imdb_id FROM watch_history 
            WHERE title = ? AND type = 'movie' AND imdb_id IS NOT NULL 
            ORDER BY watched_at DESC LIMIT 1
        ''', (title,))
    else:
        show_title = item_info['grandparentTitle']
        if show_title:
            cursor.execute('''
                SELECT imdb_id FROM watch_history 
                WHERE show_title = ? 
                AND type = 'episode' AND imdb_id IS NOT NULL 
                ORDER BY watched_at DESC LIMIT 1
            ''', (show_title,))
    
    result = cursor.fetchone()
    if result and result[0]:
        return result[0]
    
    # Try Trakt if no cached ID found
    try:
        if media_type == 'movie':
            search_year = None
            if '(' in title and ')' in title:
                title_parts = title.split('(')
                if len(title_parts) > 1:
                    year_part = title_parts[1].split(')')[0]
                    if year_part.isdigit():
                        search_year = int(year_part)
                        title = title_parts[0].strip()
            
            url = f"{trakt.base_url}/search/movie?query={title}"
            if search_year:
                url += f"&years={search_year}"
            response = trakt._make_request(url)
            if response and response.status_code == 200:
                results = response.json()
                if results:
                    return results[0]['movie']['ids'].get('imdb')
        else:
            show_title = item_info['grandparentTitle']
            if show_title:
                url = f"{trakt.base_url}/search/show?query={show_title}"
                response = trakt._make_request(url)
                if response and response.status_code == 200:
                    results = response.json()
                    if results:
                        return results[0]['show']['ids'].get('imdb')
    except Exception as e:
        logging.warning(f"Error looking up IMDb ID via Trakt for '{title}': {str(e)}")
    
    return None

def generate_synthetic_media_id(item_info, title):
    """Helper function to generate a synthetic media ID when one is not available"""
    media_type = item_info['type']
    
    if media_type == 'movie':
        return f"synthetic_movie_{title.replace(' ', '_')}"
    else:
        show_title = item_info['grandparentTitle']
        season = item_info['seasonNumber']
        episode = item_info['index']
        if show_title and season is not None and episode is not None:
            return f"synthetic_{show_title.replace(' ', '_')}_{season}_{episode}"
    return None

def insert_or_update_movie(cursor, title, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, duration, view_offset, source):
    """Helper function to insert or update a movie entry in the database"""
    try:
        # First check if we already have this movie for this day
        cursor.execute('''
            SELECT id, watched_at, imdb_id 
            FROM watch_history 
            WHERE title = ? 
            AND type = 'movie' 
            AND date(watched_at) = date(?)
        ''', (title, watched_at))
        
        existing = cursor.fetchone()
        if existing:
            existing_id, existing_watched_str, existing_imdb = existing
            
            # Convert string timestamps to datetime objects for comparison
            try:
                if isinstance(watched_at, str):
                    watched_at = datetime.strptime(watched_at, '%Y-%m-%d %H:%M:%S')
                if isinstance(existing_watched_str, str):
                    existing_watched = datetime.strptime(existing_watched_str, '%Y-%m-%d %H:%M:%S')
                else:
                    existing_watched = existing_watched_str
            except Exception as e:
                logging.warning(f"Error parsing dates for '{title}': {str(e)}")
                existing_watched = None
            
            # If existing entry has no IMDb ID and we have one, or if this is more recent, update it
            should_update = (existing_imdb is None and imdb_id is not None)
            if not should_update and watched_at and existing_watched:
                try:
                    should_update = watched_at > existing_watched
                except Exception as e:
                    logging.warning(f"Error comparing dates for '{title}': {str(e)}")
                    should_update = False
            
            if should_update:
                cursor.execute('''
                    UPDATE watch_history 
                    SET watched_at = ?, media_id = ?, imdb_id = ?, tmdb_id = ?, tvdb_id = ?,
                        duration = ?, watch_progress = ?, source = ?
                    WHERE id = ?
                ''', (
                    watched_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(watched_at, datetime) else watched_at,
                    media_id, imdb_id, tmdb_id, tvdb_id,
                    duration, view_offset, source, existing_id
                ))
                logging.debug(f"Updated existing movie entry for '{title}'")
            else:
                logging.debug(f"Skipping duplicate movie entry for '{title}' (already have more recent or complete entry)")
            return True
            
        # If no existing entry, insert new one
        cursor.execute('''
            INSERT INTO watch_history 
            (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, duration, watch_progress, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            title, 'movie',
            watched_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(watched_at, datetime) else watched_at,
            media_id, imdb_id, tmdb_id, tvdb_id,
            duration, view_offset, source
        ))
        logging.debug(f"Inserted new movie entry for '{title}'")
        return True
    except Exception as e:
        logging.error(f"Error inserting/updating movie '{title}': {str(e)}")
        return False

def insert_or_update_episode(cursor, title, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                           season, episode, show_title, duration, view_offset, source):
    """Helper function to insert or update an episode entry in the database"""
    try:
        if season is None or episode is None:
            logging.warning(f"Skipping episode '{title}': Missing season or episode number")
            return False
            
        # First check if we already have this episode for this day
        cursor.execute('''
            SELECT id, watched_at, imdb_id 
            FROM watch_history 
            WHERE show_title = ? 
            AND season = ? 
            AND episode = ? 
            AND type = 'episode'
            AND date(watched_at) = date(?)
        ''', (show_title, season, episode, watched_at))
        
        existing = cursor.fetchone()
        if existing:
            existing_id, existing_watched_str, existing_imdb = existing
            
            # Convert string timestamps to datetime objects for comparison
            try:
                if isinstance(watched_at, str):
                    watched_at = datetime.strptime(watched_at, '%Y-%m-%d %H:%M:%S')
                if isinstance(existing_watched_str, str):
                    existing_watched = datetime.strptime(existing_watched_str, '%Y-%m-%d %H:%M:%S')
                else:
                    existing_watched = existing_watched_str
            except Exception as e:
                logging.warning(f"Error parsing dates for '{title}': {str(e)}")
                existing_watched = None
            
            # If existing entry has no IMDb ID and we have one, or if this is more recent, update it
            should_update = (existing_imdb is None and imdb_id is not None)
            if not should_update and watched_at and existing_watched:
                try:
                    should_update = watched_at > existing_watched
                except Exception as e:
                    logging.warning(f"Error comparing dates for '{title}': {str(e)}")
                    should_update = False
            
            if should_update:
                cursor.execute('''
                    UPDATE watch_history 
                    SET title = ?, watched_at = ?, media_id = ?, imdb_id = ?, tmdb_id = ?, tvdb_id = ?,
                        duration = ?, watch_progress = ?, source = ?
                    WHERE id = ?
                ''', (
                    title,
                    watched_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(watched_at, datetime) else watched_at,
                    media_id, imdb_id, tmdb_id, tvdb_id,
                    duration, view_offset, source, existing_id
                ))
                logging.debug(f"Updated existing episode entry for '{show_title} S{season}E{episode}'")
            else:
                logging.debug(f"Skipping duplicate episode entry for '{show_title} S{season}E{episode}' (already have more recent or complete entry)")
            return True
            
        # If no existing entry, insert new one
        cursor.execute('''
            INSERT INTO watch_history 
            (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
             season, episode, show_title, duration, watch_progress, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            title, 'episode',
            watched_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(watched_at, datetime) else watched_at,
            media_id, imdb_id, tmdb_id, tvdb_id,
            season, episode, show_title,
            duration, view_offset, source
        ))
        logging.debug(f"Inserted new episode entry for '{show_title} S{season}E{episode}'")
        return True
    except Exception as e:
        logging.error(f"Error inserting/updating episode '{title}': {str(e)}")
        return False

async def test_plex_history_comparison():
    """
    Test function that compares watch history from two Plex sources:
    1. account.history() - history directly from Plex account
    2. PlexServer - history from all libraries on the server
    
    Prints detailed statistics and any discrepancies found.
    """
    try:
        # Get Plex connection details
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')
        
        if not plex_url or not plex_token:
            logging.error("Plex URL or token not configured")
            return
            
        logging.info("Connecting to Plex server...")
        plex = PlexServer(plex_url, plex_token)
        account = plex.myPlexAccount()
        
        # Get history from account
        logging.info("Fetching history from Plex account...")
        account_history = account.history()
        
        # Count account history items by type
        account_counts = {'movies': 0, 'episodes': 0, 'other': 0}
        account_items = {}  # Store unique identifiers for comparison
        
        for item in account_history:
            item_type = getattr(item, 'type', 'other')
            if item_type == 'movie':
                account_counts['movies'] += 1
                key = ('movie', getattr(item, 'title', ''), getattr(item, 'year', ''))
                account_items[key] = account_items.get(key, 0) + 1
            elif item_type == 'episode':
                account_counts['episodes'] += 1
                show = getattr(item, 'grandparentTitle', '')
                season = getattr(item, 'seasonNumber', '')
                episode = getattr(item, 'index', '')
                key = ('episode', show, season, episode)
                account_items[key] = account_items.get(key, 0) + 1
            else:
                account_counts['other'] += 1
        
        # Get history from server libraries
        logging.info("Fetching history from Plex server libraries...")
        server_counts = {'movies': 0, 'episodes': 0, 'other': 0}
        server_items = {}  # Store unique identifiers for comparison
        
        # Process each library
        for library in plex.library.sections():
            logging.info(f"Processing library: {library.title}")
            
            if library.type == 'movie':
                # Get watched movies
                for video in library.search(unwatched=False):
                    if video.isWatched:
                        server_counts['movies'] += 1
                        key = ('movie', video.title, getattr(video, 'year', ''))
                        server_items[key] = server_items.get(key, 0) + 1
                        
            elif library.type == 'show':
                # Get watched episodes
                for show in library.search():
                    for episode in show.episodes():
                        if episode.isWatched:
                            server_counts['episodes'] += 1
                            key = ('episode', show.title, episode.seasonNumber, episode.index)
                            server_items[key] = server_items.get(key, 0) + 1
            else:
                logging.info(f"Skipping library type: {library.type}")
        
        # Compare most recent items
        logging.info("\nMost recent items from account history:")
        for item in account_history[:5]:
            title = getattr(item, 'title', 'N/A')
            type_ = getattr(item, 'type', 'N/A')
            show = getattr(item, 'grandparentTitle', '')
            if show:
                title = f"{show} - {title}"
            logging.info(f"- {title} ({type_})")
        
        # Print comparison
        logging.info("\nComparison Summary:")
        logging.info("Account History Counts:")
        logging.info(f"- Movies: {account_counts['movies']}")
        logging.info(f"- Episodes: {account_counts['episodes']}")
        logging.info(f"- Other: {account_counts['other']}")
        logging.info(f"Total: {sum(account_counts.values())}")
        
        logging.info("\nServer Library Counts:")
        logging.info(f"- Movies: {server_counts['movies']}")
        logging.info(f"- Episodes: {server_counts['episodes']}")
        logging.info(f"Total: {sum(server_counts.values())}")
        
        # Calculate differences
        movie_diff = account_counts['movies'] - server_counts['movies']
        episode_diff = account_counts['episodes'] - server_counts['episodes']
        
        logging.info("\nDifferences (Account - Server):")
        logging.info(f"- Movies: {movie_diff:+d}")
        logging.info(f"- Episodes: {episode_diff:+d}")
        
        # Find specific differences
        logging.info("\nDetailed Differences:")
        
        # Items in account but not in server
        account_only = set(account_items.keys()) - set(server_items.keys())
        if account_only:
            logging.info("\nItems in account history but not marked watched on server:")
            for item in sorted(account_only)[:5]:  # Show first 5 differences
                if item[0] == 'movie':
                    logging.info(f"- Movie: {item[1]} ({item[2]})")
                else:
                    logging.info(f"- Episode: {item[1]} S{item[2]}E{item[3]}")
        
        # Items in server but not in account
        server_only = set(server_items.keys()) - set(account_items.keys())
        if server_only:
            logging.info("\nItems marked watched on server but not in account history:")
            for item in sorted(server_only)[:5]:  # Show first 5 differences
                if item[0] == 'movie':
                    logging.info(f"- Movie: {item[1]} ({item[2]})")
                else:
                    logging.info(f"- Episode: {item[1]} S{item[2]}E{item[3]}")
                    
    except Exception as e:
        logging.error(f"Error during history comparison: {str(e)}")

def sync_test_plex_history_comparison():
    """
    Synchronous wrapper for test_plex_history_comparison
    """
    return asyncio.run(test_plex_history_comparison())

def sync_get_watch_history_from_plex():
    """
    Synchronous wrapper for get_watch_history_from_plex
    """
    return asyncio.run(get_watch_history_from_plex())

def sync_test_plex_history_sync():
    """
    Synchronous wrapper for test_plex_history_sync
    """
    return asyncio.run(test_plex_history_comparison())
