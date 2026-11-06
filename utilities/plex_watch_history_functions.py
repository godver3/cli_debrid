import asyncio
import os
import sqlite3
import logging
from plexapi.server import PlexServer
from utilities.settings import get_setting
from cli_battery.app import trakt_client
from datetime import datetime

async def get_watch_history_from_plex():
    """
    Retrieves the user's complete Plex watch history from both account history and server libraries,
    then stores it in the database. This dual-source approach ensures maximum coverage of watch history.
    Returns a dictionary with counts of processed movies and episodes.
    """
    try:
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
                batch_results = await process_watch_history_items(cursor, current_batch, trakt_client, 'account', processed)
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
                            batch_results = await process_watch_history_items(cursor, current_batch, trakt_client, 'server', processed)
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
                                    batch_results = await process_watch_history_items(cursor, current_batch, trakt_client, 'server', processed)
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
                    batch_results = await process_watch_history_items(cursor, current_batch, trakt_client, 'server', processed)
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


async def find_imdb_id(cursor, item_info, title, trakt_client):
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
            
            url = f"{trakt_client.TRAKT_BASE_URL}/search/movie?query={title}"
            if search_year:
                url += f"&years={search_year}"
            response = trakt_client._make_request(url)
            if response and response.status_code == 200:
                results = response.json()
                if results:
                    return results[0]['movie']['ids'].get('imdb')
        else:
            show_title = item_info['grandparentTitle']
            if show_title:
                url = f"{trakt_client.TRAKT_BASE_URL}/search/show?query={show_title}"
                response = trakt_client._make_request(url)
                if response and response.status_code == 200:
                    results = response.json()
                    if results:
                        return results[0]['show']['ids'].get('imdb')
    except Exception as e:
        logging.warning(f"Error looking up IMDb ID via Trakt for '{title}': {str(e)}")
    
    return None


async def process_watch_history_items(cursor, items, trakt_client, source, processed):
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
                            logging.debug(f"No grandparentKey or grandparentRatingKey available for '{show_title}', will try Trakt lookup")
                except Exception as e:
                    logging.warning(f"Failed to get show IDs for '{show_title}': {str(e)}", exc_info=True)
                
                # If we couldn't get show IDs, try to find them via Trakt
                if not imdb_id:
                    #logging.info(f"No IMDb ID found from Plex, trying Trakt lookup for show: {show_title}")
                    try:
                        imdb_id = await find_imdb_id(cursor, {'type': 'episode', 'grandparentTitle': show_title}, show_title, trakt_client)
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


def sync_get_watch_history_from_plex():
    """
    Synchronous wrapper for get_watch_history_from_plex
    """
    return asyncio.run(get_watch_history_from_plex())


