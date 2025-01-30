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
    Retrieves the user's complete Plex watch history and stores it in the database.
    Returns a dictionary with counts of processed movies and episodes.
    """
    try:
        trakt = TraktMetadata()
        processed = {
            'movies': 0,
            'episodes': 0
        }
        
        # Get Plex connection details
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')
        
        if not plex_url or not plex_token:
            logging.error("Plex URL or token not configured")
            return {'movies': 0, 'episodes': 0}
            
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
                watch_progress INTEGER
            )
        ''')
        
        # Get all watched history from all libraries
        logging.info("Fetching watch history from all Plex libraries...")
        account = plex.myPlexAccount()
        logging.info(f"Authenticated as user: {account.username} (ID: {account.id})")
        
        # Get history directly from the account instead of the server
        logging.info("Fetching history directly from account...")
        history = account.history()
        
        # Debug first few items
        logging.info("\nFirst 5 history items:")
        for i, item in enumerate(history[:5]):
            logging.info(f"Item {i + 1}:")
            logging.info(f"  Title: {getattr(item, 'title', 'N/A')}")
            logging.info(f"  Type: {getattr(item, 'type', 'N/A')}")
            logging.info(f"  Account ID: {getattr(item, 'accountID', 'N/A')}")
            logging.info("---")
        
        total_items = len(history)
        logging.info(f"Found {total_items} items in watch history")
        
        for i, item in enumerate(history, 1):
            try:
                if i % 100 == 0 or i == total_items:
                    logging.info(f"Processing item {i}/{total_items} ({(i/total_items*100):.1f}%)")
                
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
                
                # Skip non-video content
                if item_info['type'] not in ['movie', 'episode']:
                    logging.info(f"Skipping non-video content - Item {i} '{item_info['title']}' (Type: {item_info['type']})")
                    continue

                # Extract basic info and validate required fields
                title = item_info['title']
                if not title and item_info['type'] == 'episode' and item_info['grandparentTitle'] and item_info['seasonNumber'] is not None and item_info['index'] is not None:
                    # Generate title for episodes like "Friends - S04E24"
                    title = f"{item_info['grandparentTitle']} - S{item_info['seasonNumber']:02d}E{item_info['index']:02d}"
                    logging.info(f"Generated title for episode: {title}")
                
                if not title:
                    logging.warning(f"Skipping item {i}: Missing required field 'title'. Item info: {item_info}")
                    continue
                    
                watched_at = item_info['viewedAt']
                if not watched_at:
                    logging.warning(f"Skipping item {i} '{title}': Missing required field 'viewedAt'. Item info: {item_info}")
                    continue
                
                # Determine media type from Plex type field
                media_type = item_info['type']
                if not media_type:
                    # Fallback to checking seasonNumber if type is not available
                    media_type = 'episode' if item_info['seasonNumber'] is not None else 'movie'
                
                # Get external IDs
                imdb_id = None
                tmdb_id = None
                tvdb_id = None
                
                # Get GUIDs from the item and log detailed information
                guid_info = {
                    'has_guids_attr': hasattr(item, 'guids'),
                    'guids_count': len(item.guids) if hasattr(item, 'guids') else 0,
                    'raw_guid': getattr(item, 'guid', None),
                    'guid_list': []
                }
                
                if hasattr(item, 'guids'):
                    for guid in item.guids:
                        guid_data = {
                            'id': str(guid.id),
                            'type': type(guid).__name__,
                            'attributes': {attr: str(getattr(guid, attr)) for attr in dir(guid) 
                                         if not attr.startswith('_') and not callable(getattr(guid, attr))}
                        }
                        guid_info['guid_list'].append(guid_data)
                        
                        guid_str = str(guid.id)
                        if 'imdb://' in guid_str:
                            imdb_id = guid_str.split('imdb://')[1].split('?')[0]
                        elif 'tmdb://' in guid_str:
                            tmdb_id = guid_str.split('tmdb://')[1].split('?')[0]
                        elif 'tvdb://' in guid_str:
                            tvdb_id = guid_str.split('tvdb://')[1].split('?')[0]
                
                if not imdb_id:
                    # First check if we have this item cached in our database
                    cached_query = None
                    if media_type == 'movie':
                        cached_query = '''
                            SELECT imdb_id FROM watch_history 
                            WHERE title = ? AND type = 'movie' AND imdb_id IS NOT NULL 
                            ORDER BY watched_at DESC LIMIT 1
                        '''
                        cursor.execute(cached_query, (title,))
                    else:
                        # For TV shows, just match on show title since we want the show's IMDb ID
                        show_title = item_info['grandparentTitle']
                        if show_title:
                            cached_query = '''
                                SELECT imdb_id FROM watch_history 
                                WHERE show_title = ? 
                                AND type = 'episode' AND imdb_id IS NOT NULL 
                                ORDER BY watched_at DESC LIMIT 1
                            '''
                            cursor.execute(cached_query, (show_title,))
                    
                    if cached_query:
                        result = cursor.fetchone()
                        if result and result[0]:
                            imdb_id = result[0]
                            logging.info(f"Found cached IMDb ID {imdb_id} for {'movie' if media_type == 'movie' else 'show'} '{title if media_type == 'movie' else show_title}'")

                if not imdb_id:
                    # Try to find IMDb ID using Trakt
                    try:
                        if media_type == 'movie':
                            search_year = None
                            # Try to extract year from title if it's in parentheses
                            if '(' in title and ')' in title:
                                title_parts = title.split('(')
                                if len(title_parts) > 1:
                                    year_part = title_parts[1].split(')')[0]
                                    if year_part.isdigit():
                                        search_year = int(year_part)
                                        title = title_parts[0].strip()
                            
                            # Search by title
                            url = f"{trakt.base_url}/search/movie?query={title}"
                            if search_year:
                                url += f"&years={search_year}"
                            response = trakt._make_request(url)
                            if response and response.status_code == 200:
                                results = response.json()
                                if results:
                                    movie_data = results[0]['movie']
                                    imdb_id = movie_data['ids'].get('imdb')
                                    if imdb_id:
                                        logging.info(f"Found IMDb ID {imdb_id} for movie '{title}' via Trakt")
                        else:
                            # For TV shows, we only need the show title
                            show_title = item_info['grandparentTitle']
                            if show_title:
                                # Search for show
                                url = f"{trakt.base_url}/search/show?query={show_title}"
                                response = trakt._make_request(url)
                                if response and response.status_code == 200:
                                    results = response.json()
                                    if results:
                                        show_data = results[0]['show']
                                        imdb_id = show_data['ids'].get('imdb')
                                        if imdb_id:
                                            logging.info(f"Found IMDb ID {imdb_id} for show '{show_title}' via Trakt")
                    except Exception as e:
                        logging.warning(f"Error looking up IMDb ID via Trakt for '{title}': {str(e)}")
                
                if not imdb_id:
                    logging.info(f"No IMDb ID found for '{title}'. GUID information: {guid_info}")
                
                # Get rating key (media_id)
                media_id = str(item_info['ratingKey']) if item_info['ratingKey'] else None
                
                # Generate synthetic media_id if missing
                if not media_id:
                    if media_type == 'movie':
                        # Use title and year if available, or just title
                        if '(' in title and ')' in title:
                            media_id = f"synthetic_movie_{title.replace(' ', '_')}"
                        else:
                            media_id = f"synthetic_movie_{title.replace(' ', '_')}"
                    else:
                        # For episodes, use show, season, and episode
                        show_title = item_info['grandparentTitle']
                        season = item_info['seasonNumber']
                        episode = item_info['index']
                        if show_title and season is not None and episode is not None:
                            media_id = f"synthetic_{show_title.replace(' ', '_')}_{season}_{episode}"
                        else:
                            logging.warning(f"Skipping item {i} '{title}': Missing required fields for synthetic ID. Item info: {item_info}")
                            continue
                
                if not media_id:
                    logging.warning(f"Skipping item {i} '{title}': Missing required field 'ratingKey' and couldn't generate synthetic ID. Item info: {item_info}")
                    continue
                
                # Before inserting, check if we already have this item for today
                if media_type == 'movie':
                    cursor.execute('''
                        SELECT imdb_id, watched_at 
                        FROM watch_history 
                        WHERE title = ? 
                        AND type = 'movie' 
                        AND date(watched_at) = date(?)
                    ''', (title, watched_at))
                else:
                    cursor.execute('''
                        SELECT imdb_id, watched_at 
                        FROM watch_history 
                        WHERE show_title = ? 
                        AND season = ? 
                        AND episode = ? 
                        AND type = 'episode'
                        AND date(watched_at) = date(?)
                    ''', (item_info['grandparentTitle'], item_info['seasonNumber'], item_info['index'], watched_at))
                
                existing = cursor.fetchone()
                if existing:
                    existing_imdb, existing_watched = existing
                    # Convert existing_watched string to datetime for comparison
                    if isinstance(existing_watched, str):
                        existing_watched = datetime.strptime(existing_watched, '%Y-%m-%d %H:%M:%S')
                    
                    # If existing entry has no IMDb ID and we have one, or if this is more recent, update it
                    if (existing_imdb is None and imdb_id is not None) or (watched_at and existing_watched and watched_at > existing_watched):
                        if media_type == 'movie':
                            cursor.execute('''
                                UPDATE watch_history 
                                SET imdb_id = ?, tmdb_id = ?, tvdb_id = ?, 
                                    watched_at = ?, duration = ?, watch_progress = ?
                                WHERE title = ? 
                                AND type = 'movie' 
                                AND date(watched_at) = date(?)
                            ''', (
                                imdb_id, tmdb_id, tvdb_id,
                                watched_at, item_info['duration'], item_info['viewOffset'],
                                title, existing_watched
                            ))
                        else:
                            cursor.execute('''
                                UPDATE watch_history 
                                SET imdb_id = ?, tmdb_id = ?, tvdb_id = ?,
                                    watched_at = ?, duration = ?, watch_progress = ?
                                WHERE show_title = ? 
                                AND season = ? 
                                AND episode = ? 
                                AND type = 'episode'
                                AND date(watched_at) = date(?)
                            ''', (
                                imdb_id, tmdb_id, tvdb_id,
                                watched_at, item_info['duration'], item_info['viewOffset'],
                                item_info['grandparentTitle'], item_info['seasonNumber'], item_info['index'], existing_watched
                            ))
                        logging.info(f"Updated existing {'movie' if media_type == 'movie' else 'episode'} '{title}' with new IMDb ID or watch time")
                    continue

                # If no existing entry found, proceed with insert
                if media_type == 'movie':
                    cursor.execute('''
                        INSERT OR REPLACE INTO watch_history 
                        (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, duration, watch_progress)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        title, 'movie', watched_at, media_id,
                        imdb_id, tmdb_id, tvdb_id,
                        item_info['duration'],
                        item_info['viewOffset']
                    ))
                    processed['movies'] += 1
                    if processed['movies'] % 50 == 0:
                        logging.info(f"Processed {processed['movies']} movies")
                else:
                    # Validate episode-specific fields
                    season = item_info['seasonNumber']
                    episode = item_info['index']
                    show_title = item_info['grandparentTitle']
                    
                    if season is None or episode is None:
                        logging.warning(f"Skipping episode {i} '{title}': Missing season or episode number. Item info: {item_info}")
                        continue
                        
                    cursor.execute('''
                        INSERT OR REPLACE INTO watch_history 
                        (title, type, watched_at, media_id, imdb_id, tmdb_id, tvdb_id, 
                         season, episode, show_title, duration, watch_progress)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        title, 'episode', watched_at, media_id,
                        imdb_id, tmdb_id, tvdb_id,
                        season, episode, show_title,
                        item_info['duration'],
                        item_info['viewOffset']
                    ))
                    processed['episodes'] += 1
                    if processed['episodes'] % 100 == 0:
                        logging.info(f"Processed {processed['episodes']} episodes")
            
            except Exception as e:
                logging.error(f"Error processing item {title}: {str(e)}")
                continue
        
        conn.commit()
        conn.close()
        
        logging.info(f"Watch history sync complete! Processed {processed['movies']} movies and {processed['episodes']} episodes")
        return processed
        
    except Exception as e:
        logging.error(f"Error getting watch history: {str(e)}")
        return {'movies': 0, 'episodes': 0}

def sync_get_watch_history_from_plex():
    """
    Synchronous wrapper for get_watch_history_from_plex
    """
    return asyncio.run(get_watch_history_from_plex())
