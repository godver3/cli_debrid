import asyncio
import os
import sqlite3
import logging
from plexapi.server import PlexServer
from settings import get_setting
from cli_battery.app.trakt_metadata import TraktMetadata
from datetime import datetime, timedelta
import requests

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
                UNIQUE(title, type, watched_at) ON CONFLICT REPLACE,
                UNIQUE(show_title, season, episode, watched_at) ON CONFLICT REPLACE
            )
        ''')
        
        # Check if source column exists, if not add it
        cursor.execute("PRAGMA table_info(watch_history)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'source' not in columns:
            cursor.execute('ALTER TABLE watch_history ADD COLUMN source TEXT')
            logging.info("Added 'source' column to watch_history table")
        
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
        batch_size = 100
        current_batch = []
        
        for i, item in enumerate(account_history, 1):
            if i % 100 == 0 or i == total_items:
                logging.info(f"Processing account item {i}/{total_items} ({(i/total_items*100):.1f}%)")
            
            current_batch.append(item)
            
            if len(current_batch) >= batch_size or i == total_items:
                batch_results = await process_watch_history_items(cursor, current_batch, trakt, 'account', processed)
                if batch_results:
                    cursor.executemany('''
                        INSERT OR REPLACE INTO watch_history 
                        (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                         season, episode, show_title, duration, watch_progress, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', batch_results)
                    conn.commit()
                    processed['account_items'] += len(batch_results)
                current_batch = []
        
        # Process server libraries
        logging.info("\nFetching history from Plex server libraries...")
        
        # Process each library
        for library in plex.library.sections():
            logging.info(f"Processing library: {library.title}")
            current_batch = []
            
            if library.type == 'movie':
                # Get watched movies
                watched_movies = library.search(unwatched=False)
                total_movies = len(watched_movies)
                logging.info(f"Found {total_movies} potentially watched movies in {library.title}")
                
                for i, video in enumerate(watched_movies, 1):
                    if i % 50 == 0 or i == total_movies:
                        logging.info(f"Processing movie {i}/{total_movies} in {library.title}")
                    
                    if video.isWatched:
                        current_batch.append(video)
                        
                        if len(current_batch) >= batch_size or i == total_movies:
                            batch_results = await process_watch_history_items(cursor, current_batch, trakt, 'server', processed)
                            if batch_results:
                                cursor.executemany('''
                                    INSERT OR REPLACE INTO watch_history 
                                    (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                                     season, episode, show_title, duration, watch_progress, source)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ''', batch_results)
                                conn.commit()
                            current_batch = []
                            
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
                                current_batch.append(episode)
                                
                                if len(current_batch) >= batch_size:
                                    batch_results = await process_watch_history_items(cursor, current_batch, trakt, 'server', processed)
                                    if batch_results:
                                        cursor.executemany('''
                                            INSERT OR REPLACE INTO watch_history 
                                            (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                                             season, episode, show_title, duration, watch_progress, source)
                                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                        ''', batch_results)
                                        conn.commit()
                                    current_batch = []
                                    
                    except Exception as e:
                        logging.error(f"Error processing show {show.title}: {str(e)}")
                        continue
                
                # Process any remaining episodes in the last batch
                if current_batch:
                    batch_results = await process_watch_history_items(cursor, current_batch, trakt, 'server', processed)
                    if batch_results:
                        cursor.executemany('''
                            INSERT OR REPLACE INTO watch_history 
                            (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                             season, episode, show_title, duration, watch_progress, source)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', batch_results)
                        conn.commit()
                    current_batch = []
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
    """Helper function to prepare movie data for insertion/update in the database"""
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
            
            if not should_update:
                logging.debug(f"Skipping duplicate movie entry for '{title}' (already have more recent or complete entry)")
                return None
        
        # Return data for batch insert/update
        logging.debug(f"Prepared movie entry for '{title}'")
        return (
            title, 'movie',
            watched_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(watched_at, datetime) else watched_at,
            media_id, imdb_id, tmdb_id, tvdb_id,
            None, None, None,  # season, episode, show_title
            duration, view_offset, source
        )
        
    except Exception as e:
        logging.error(f"Error preparing movie '{title}': {str(e)}")
        return None

def insert_or_update_episode(cursor, title, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                           season, episode, show_title, duration, view_offset, source):
    """Helper function to prepare episode data for insertion/update in the database"""
    try:
        if season is None or episode is None:
            logging.warning(f"Skipping episode '{title}': Missing season or episode number")
            return None
            
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
            
            if not should_update:
                logging.debug(f"Skipping duplicate episode entry for '{show_title} S{season}E{episode}' (already have more recent or complete entry)")
                return None
        
        # Return data for batch insert/update
        logging.debug(f"Prepared episode entry for '{show_title} S{season}E{episode}'")
        return (
            title, 'episode',
            watched_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(watched_at, datetime) else watched_at,
            media_id, imdb_id, tmdb_id, tvdb_id,
            season, episode, show_title,
            duration, view_offset, source
        )
        
    except Exception as e:
        logging.error(f"Error preparing episode '{title}': {str(e)}")
        return None

async def process_watch_history_items(cursor, items, trakt, source, processed):
    """Process a batch of watch history items at once"""
    try:
        # Separate movies and episodes
        movies = []
        episodes = []
        
        # First pass - separate items and get external IDs
        for item in items:
            title = getattr(item, 'title', None)
            if not title:
                continue
                
            # Handle both account history (viewedAt) and server items (lastViewedAt)
            watched_at = getattr(item, 'viewedAt', None) or getattr(item, 'lastViewedAt', None)
            if not watched_at:
                continue

            item_type = getattr(item, 'type', None)
            if item_type == 'movie':
                movies.append(item)
            elif item_type == 'episode':
                episodes.append(item)

        results = []
        
        def get_date_str(dt):
            """Helper to get date string from datetime or string"""
            if isinstance(dt, datetime):
                return dt.strftime('%Y-%m-%d')
            elif isinstance(dt, str):
                return dt.split()[0]
            return None
        
        # Process movies in batch
        if movies:
            # Get all existing movies for this batch
            movie_titles = [getattr(m, 'title', '') for m in movies]
            placeholders = ','.join(['?' for _ in movie_titles])
            cursor.execute(f'''
                SELECT title, watched_at, imdb_id 
                FROM watch_history 
                WHERE type = 'movie' 
                AND title IN ({placeholders})
            ''', movie_titles)
            # Convert rows to dictionary for easier lookup
            existing_movies = {}
            for row in cursor.fetchall():
                watched_date = get_date_str(row[1])
                existing_movies[(row[0], watched_date)] = row[2]
            
            # Process each movie
            for movie in movies:
                title = getattr(movie, 'title', None)
                watched_at = getattr(movie, 'viewedAt', None) or getattr(movie, 'lastViewedAt', None)
                watched_date = get_date_str(watched_at)
                
                # Check if we already have this movie for this day
                if (title, watched_date) in existing_movies:
                    # Skip if we already have it with an IMDb ID
                    if existing_movies[(title, watched_date)]:
                        continue
                
                # Get external IDs
                imdb_id = None
                tmdb_id = None
                tvdb_id = None
                
                if hasattr(movie, 'guids'):
                    for guid in movie.guids:
                        guid_str = str(guid.id)
                        if 'imdb://' in guid_str:
                            imdb_id = guid_str.split('imdb://')[1].split('?')[0]
                        elif 'tmdb://' in guid_str:
                            tmdb_id = guid_str.split('tmdb://')[1].split('?')[0]
                        elif 'tvdb://' in guid_str:
                            tvdb_id = guid_str.split('tvdb://')[1].split('?')[0]
                
                media_id = str(getattr(movie, 'ratingKey', None))
                duration = getattr(movie, 'duration', None)
                view_offset = getattr(movie, 'viewOffset', None)
                
                # Format watched_at for database
                if isinstance(watched_at, datetime):
                    watched_at = watched_at.strftime('%Y-%m-%d %H:%M:%S')
                
                results.append((
                    title, 'movie', watched_at, media_id, imdb_id, tmdb_id, tvdb_id,
                    None, None, None,  # season, episode, show_title
                    duration, view_offset, source
                ))
                processed['movies'] += 1

        # Process episodes in batch
        if episodes:
            # Get all existing episodes for this batch
            episode_keys = [(getattr(e, 'grandparentTitle', ''), 
                           getattr(e, 'seasonNumber', None), 
                           getattr(e, 'index', None)) for e in episodes]
            placeholders = ','.join(['(?,?,?)' for _ in episode_keys])
            flat_params = [item for key in episode_keys for item in key]
            cursor.execute(f'''
                SELECT show_title, season, episode, watched_at, imdb_id 
                FROM watch_history 
                WHERE type = 'episode' 
                AND (show_title, season, episode) IN ({placeholders})
            ''', flat_params)
            # Convert rows to dictionary for easier lookup
            existing_episodes = {}
            for row in cursor.fetchall():
                watched_date = get_date_str(row[3])
                existing_episodes[(row[0], row[1], row[2], watched_date)] = row[4]
            
            # Process each episode
            for episode in episodes:
                show_title = getattr(episode, 'grandparentTitle', None)
                season = getattr(episode, 'seasonNumber', None)
                episode_num = getattr(episode, 'index', None)
                watched_at = getattr(episode, 'viewedAt', None) or getattr(episode, 'lastViewedAt', None)
                watched_date = get_date_str(watched_at)
                
                if not all([show_title, season is not None, episode_num is not None]):
                    continue
                
                # Check if we already have this episode for this day
                if (show_title, season, episode_num, watched_date) in existing_episodes:
                    # Skip if we already have it with an IMDb ID
                    if existing_episodes[(show_title, season, episode_num, watched_date)]:
                        continue
                
                # Get external IDs
                imdb_id = None
                tmdb_id = None
                tvdb_id = None
                
                #logging.info(f"Getting IDs for episode: {show_title} S{season}E{episode_num}")
                try:
                    # Try to get show IDs first
                    if hasattr(episode, 'grandparentKey') and episode.grandparentKey:
                        show = episode.show()
                        if show and hasattr(show, 'guids'):
                            #logging.info(f"Found show object with guids for '{show_title}'")
                            for guid in show.guids:
                                guid_str = str(guid.id)
                                #logging.info(f"Processing show guid: {guid_str}")
                                if 'imdb://' in guid_str:
                                    imdb_id = guid_str.split('imdb://')[1].split('?')[0]
                                    #logging.info(f"Found show IMDb ID: {imdb_id}")
                                elif 'tmdb://' in guid_str:
                                    tmdb_id = guid_str.split('tmdb://')[1].split('?')[0]
                                    #logging.info(f"Found show TMDb ID: {tmdb_id}")
                                elif 'tvdb://' in guid_str:
                                    tvdb_id = guid_str.split('tvdb://')[1].split('?')[0]
                                    #logging.info(f"Found show TVDb ID: {tvdb_id}")
                    else:
                        # Try to get IDs from grandparentRatingKey if available
                        if hasattr(episode, 'grandparentRatingKey') and episode.grandparentRatingKey:
                            try:
                                #logging.info(f"Trying to get show via grandparentRatingKey: {episode.grandparentRatingKey}")
                                show = episode._server.fetchItem(episode.grandparentRatingKey)
                                if show and hasattr(show, 'guids'):
                                    #logging.info(f"Found show object via grandparentRatingKey for '{show_title}'")
                                    for guid in show.guids:
                                        guid_str = str(guid.id)
                                        #logging.info(f"Processing show guid: {guid_str}")
                                        if 'imdb://' in guid_str:
                                            imdb_id = guid_str.split('imdb://')[1].split('?')[0]
                                            #logging.info(f"Found show IMDb ID: {imdb_id}")
                                        elif 'tmdb://' in guid_str:
                                            tmdb_id = guid_str.split('tmdb://')[1].split('?')[0]
                                            #logging.info(f"Found show TMDb ID: {tmdb_id}")
                                        elif 'tvdb://' in guid_str:
                                            tvdb_id = guid_str.split('tvdb://')[1].split('?')[0]
                                            #logging.info(f"Found show TVDb ID: {tvdb_id}")
                            except Exception as e:
                                logging.warning(f"Failed to get show via grandparentRatingKey for '{show_title}': {str(e)}")
                        else:
                            logging.warning(f"No grandparentKey or grandparentRatingKey available for '{show_title}'")
                except Exception as e:
                    logging.warning(f"Failed to get show IDs for '{show_title}': {str(e)}", exc_info=True)
                
                # If we couldn't get show IDs, try to find them via Trakt
                if not imdb_id:
                    #logging.info(f"No IMDb ID found from Plex, trying Trakt lookup for show: {show_title}")
                    try:
                        imdb_id = await find_imdb_id(cursor, {'type': 'episode', 'grandparentTitle': show_title}, show_title, trakt)
                        if imdb_id:
                            #logging.info(f"Found show IMDb ID via Trakt: {imdb_id}")
                            pass
                        else:
                            logging.warning(f"Failed to find show IMDb ID via Trakt for: {show_title}")
                    except Exception as e:
                        logging.warning(f"Error during Trakt lookup for '{show_title}': {str(e)}", exc_info=True)
                
                title = getattr(episode, 'title', None)
                media_id = str(getattr(episode, 'ratingKey', None))
                duration = getattr(episode, 'duration', None)
                view_offset = getattr(episode, 'viewOffset', None)
                
                # Format watched_at for database
                if isinstance(watched_at, datetime):
                    watched_at = watched_at.strftime('%Y-%m-%d %H:%M:%S')
                
                results.append((
                    title, 'episode', watched_at, media_id, imdb_id, tmdb_id, tvdb_id,
                    season, episode_num, show_title,
                    duration, view_offset, source
                ))
                processed['episodes'] += 1
                processed['server_items'] += 1
        
        return results
        
    except Exception as e:
        logging.error(f"Error processing batch: {str(e)}")
        return []

async def test_plex_history_comparison():
    """
    Simple test function that compares total watch history counts from two Plex sources:
    1. account.history() - history directly from Plex account
    2. server.history(accountID=1) - history from the Plex server for admin account
    
    Prints total counts from both sources for comparison.
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
        
        # Log account details
        logging.info("\nAccount Information:")
        logging.info(f"Username: {account.username}")
        logging.info(f"Account ID (Plex): {account.id}")
        logging.info(f"Email: {account.email}")
        
        # Get history from account with increased maxresults
        logging.info("\nFetching history from Plex account...")
        account_history = account.history(maxresults=50000)  # Try to get more history
        account_total = len(account_history)
        
        # Log sample of account history with dates
        if account_history:
            logging.info("\nSample items from account history:")
            # Get first and last items to see date range
            first_item = account_history[-1] if account_history else None
            last_item = account_history[0] if account_history else None
            
            if first_item and last_item:
                first_date = getattr(first_item, 'viewedAt', 'N/A')
                last_date = getattr(last_item, 'viewedAt', 'N/A')
                logging.info(f"\nDate range in account history:")
                logging.info(f"Earliest: {first_date}")
                logging.info(f"Latest: {last_date}")
            
            logging.info("\nMost recent items:")
            for item in account_history[:3]:
                logging.info(f"- Title: {getattr(item, 'title', 'N/A')}")
                logging.info(f"  Type: {getattr(item, 'type', 'N/A')}")
                logging.info(f"  Account ID: {getattr(item, 'accountID', 'N/A')}")
                logging.info(f"  Rating Key: {getattr(item, 'ratingKey', 'N/A')}")
                logging.info(f"  Viewed At: {getattr(item, 'viewedAt', 'N/A')}")
        
        # Get history from server for admin account (ID=1)
        logging.info("\nFetching history from Plex server...")
        server_history = plex.history(accountID=1, maxresults=50000)  # Try to get more history
        server_total = len(server_history)
        
        # Log sample of server history with dates
        if server_history:
            logging.info("\nSample items from server history (admin account):")
            # Get first and last items to see date range
            first_item = server_history[-1] if server_history else None
            last_item = server_history[0] if server_history else None
            
            if first_item and last_item:
                first_date = getattr(first_item, 'viewedAt', 'N/A')
                last_date = getattr(last_item, 'viewedAt', 'N/A')
                logging.info(f"\nDate range in server history:")
                logging.info(f"Earliest: {first_date}")
                logging.info(f"Latest: {last_date}")
            
            logging.info("\nMost recent items:")
            for item in server_history[:3]:
                logging.info(f"- Title: {getattr(item, 'title', 'N/A')}")
                logging.info(f"  Type: {getattr(item, 'type', 'N/A')}")
                logging.info(f"  Account ID: {getattr(item, 'accountID', 'N/A')}")
                logging.info(f"  Rating Key: {getattr(item, 'ratingKey', 'N/A')}")
                logging.info(f"  Viewed At: {getattr(item, 'viewedAt', 'N/A')}")
        
        # Get total history count for reference
        all_users_history = plex.history(maxresults=50000)
        all_users_total = len(all_users_history)
        
        # Print comparison
        logging.info("\nHistory Count Comparison:")
        logging.info(f"Account history total items: {account_total}")
        logging.info(f"Server history total items (admin account): {server_total}")
        logging.info(f"Total server history (all users): {all_users_total}")
        
        # Calculate difference
        diff = account_total - server_total
        logging.info(f"\nDifference (Account - Server): {diff:+d} items")
        
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

async def test_direct_plex_history():
    """
    Test function that directly queries the Plex API for history using different parameters
    to try to find older history entries.
    """
    try:
        token = "URyzrzpcCWsuZe7ZCeRs"
        base_url = "https://plex.tv/api/v2"
        headers = {
            'Accept': 'application/json',
            'X-Plex-Token': token
        }
        
        logging.info("\nTesting direct Plex API calls for history...")
        
        # First get account info
        account_url = f"{base_url}/user"
        account_response = requests.get(account_url, headers=headers)
        if account_response.status_code == 200:
            account_data = account_response.json()
            logging.info(f"\nAccount Info:")
            logging.info(f"UUID: {account_data.get('uuid', 'N/A')}")
            logging.info(f"Username: {account_data.get('username', 'N/A')}")
            logging.info(f"Email: {account_data.get('email', 'N/A')}")
        
        # Try different date ranges
        date_ranges = [
            ("Past 5 years", datetime.now() - timedelta(days=365*5)),
            ("Past 10 years", datetime.now() - timedelta(days=365*10)),
            ("Since 2010", datetime(2010, 1, 1))
        ]
        
        for range_name, start_date in date_ranges:
            logging.info(f"\nTrying to fetch history for {range_name}...")
            history_url = f"{base_url}/history/all"
            params = {
                'X-Plex-Token': token,
                'mindate': start_date.strftime('%Y-%m-%d'),
                'limit': 50000
            }
            
            try:
                response = requests.get(history_url, headers=headers, params=params)
                if response.status_code == 200:
                    history_data = response.json()
                    items = history_data.get('MediaContainer', {}).get('Metadata', [])
                    if items:
                        oldest = min(items, key=lambda x: x.get('viewedAt', 0))
                        newest = max(items, key=lambda x: x.get('viewedAt', 0))
                        logging.info(f"Found {len(items)} items")
                        logging.info(f"Oldest item: {datetime.fromtimestamp(oldest.get('viewedAt', 0))}")
                        logging.info(f"Newest item: {datetime.fromtimestamp(newest.get('viewedAt', 0))}")
                        
                        # Show a few sample items
                        logging.info("\nSample items:")
                        for item in items[:3]:
                            logging.info(f"- Title: {item.get('title', 'N/A')}")
                            logging.info(f"  Type: {item.get('type', 'N/A')}")
                            logging.info(f"  Viewed At: {datetime.fromtimestamp(item.get('viewedAt', 0))}")
                    else:
                        logging.info("No items found in this date range")
                else:
                    logging.info(f"Failed to get history: {response.status_code}")
                    logging.info(f"Response: {response.text}")
            except Exception as e:
                logging.error(f"Error querying for {range_name}: {str(e)}")
        
        # Try the v1 API endpoint as well
        logging.info("\nTrying Plex API v1 endpoint...")
        v1_url = "https://plex.tv/pms/history/all"
        params = {
            'X-Plex-Token': token,
            'limit': 50000
        }
        
        try:
            response = requests.get(v1_url, headers=headers, params=params)
            if response.status_code == 200:
                # V1 API returns XML, log the raw response for inspection
                logging.info(f"V1 API Response length: {len(response.text)} bytes")
                logging.info("First 500 characters of response:")
                logging.info(response.text[:500])
            else:
                logging.info(f"Failed to get v1 history: {response.status_code}")
                logging.info(f"Response: {response.text}")
        except Exception as e:
            logging.error(f"Error querying v1 API: {str(e)}")
            
    except Exception as e:
        logging.error(f"Error during direct API test: {str(e)}")

def sync_test_direct_plex_history():
    """
    Synchronous wrapper for test_direct_plex_history
    """
    return asyncio.run(test_direct_plex_history())
