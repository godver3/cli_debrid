import logging
from .core import get_db_connection
import asyncio
import aiohttp
from .poster_management import get_poster_url
from poster_cache import get_cached_poster_url, cache_poster_url, clean_expired_cache
from settings import get_setting
from flask import request, url_for
from urllib.parse import urlparse
from datetime import datetime
import time
from functools import wraps

def cache_for_seconds(seconds):
    """Cache the result of a function for the specified number of seconds."""
    def decorator(func):
        cache = {}
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            now = time.time()
            
            # Create a cache key from the function name and arguments
            key = (func.__name__, args, frozenset(kwargs.items()))
            
            # Check if we have a cached value and it's still valid
            if key in cache:
                result, timestamp = cache[key]
                if now - timestamp < seconds:
                    logging.debug(f"Cache hit for {func.__name__}")
                    return result
            
            # If no valid cached value, call the function
            result = await func(*args, **kwargs)
            cache[key] = (result, now)
            return result
            
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            now = time.time()
            
            # Create a cache key from the function name and arguments
            key = (func.__name__, args, frozenset(kwargs.items()))
            
            # Check if we have a cached value and it's still valid
            if key in cache:
                result, timestamp = cache[key]
                if now - timestamp < seconds:
                    logging.debug(f"Cache hit for {func.__name__}")
                    return result
            
            # If no valid cached value, call the function
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result
            
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator

def get_collected_counts():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Count unique collected movies
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id) 
            FROM media_items 
            WHERE type = 'movie' AND state = 'Collected'
        ''')
        total_movies = cursor.fetchone()[0]

        # Count unique collected TV shows
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id) 
            FROM media_items 
            WHERE type = 'episode' AND state = 'Collected'
        ''')
        total_shows = cursor.fetchone()[0]

        # Count unique collected episodes
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id || '-' || season_number || '-' || episode_number) 
            FROM media_items 
            WHERE type = 'episode' AND state = 'Collected'
        ''')
        total_episodes = cursor.fetchone()[0]

        return {
            'total_movies': total_movies,
            'total_shows': total_shows,
            'total_episodes': total_episodes
        }
    except Exception as e:
        logging.error(f"Error getting collected counts: {str(e)}")
        return {'total_movies': 0, 'total_shows': 0, 'total_episodes': 0}
    finally:
        conn.close()

@cache_for_seconds(30)
async def get_recently_added_items(movie_limit=5, show_limit=5):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Query for movies - limit directly in SQL
        movie_query = """
        WITH RankedMovies AS (
            SELECT 
                title,
                year,
                type,
                collected_at,
                imdb_id,
                tmdb_id,
                version,
                filled_by_title,
                filled_by_file,
                state,
                ROW_NUMBER() OVER (PARTITION BY title, year ORDER BY collected_at DESC) as rn
            FROM media_items
            WHERE type = 'movie' AND collected_at IS NOT NULL AND state = 'Collected'
        )
        SELECT *
        FROM RankedMovies
        WHERE rn = 1
        ORDER BY collected_at DESC
        LIMIT ?
        """
        
        # Query for episodes - limit directly in SQL
        episode_query = """
        WITH RankedEpisodes AS (
            SELECT 
                title,
                year,
                type,
                season_number,
                episode_number,
                collected_at,
                imdb_id,
                tmdb_id,
                version,
                filled_by_title,
                filled_by_file,
                ROW_NUMBER() OVER (PARTITION BY title ORDER BY collected_at DESC) as rn
            FROM media_items
            WHERE type = 'episode' AND collected_at IS NOT NULL AND state = 'Collected'
        )
        SELECT *
        FROM RankedEpisodes
        WHERE rn = 1
        ORDER BY collected_at DESC
        LIMIT ?
        """
        
        cursor.execute(movie_query, (movie_limit,))
        movie_results = cursor.fetchall()
        
        cursor.execute(episode_query, (show_limit,))
        episode_results = cursor.fetchall()
        
        movies_list = []
        shows_list = []
        
        async with aiohttp.ClientSession() as session:
            # Process movies
            for row in movie_results:
                item = dict(row)
                movie_item = {
                    'title': item['title'],
                    'year': item['year'],
                    'type': 'movie',
                    'collected_at': item['collected_at'],
                    'imdb_id': item['imdb_id'],
                    'tmdb_id': item['tmdb_id'],
                    'version': item['version'],
                    'filled_by_file': item['filled_by_file'],
                    'filled_by_title': item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file']
                }
                
                # Get cached poster URL or add to list for batch fetch
                cached_url = get_cached_poster_url(item['tmdb_id'], 'movie')
                if cached_url:
                    movie_item['poster_url'] = cached_url
                movies_list.append((movie_item, item['tmdb_id']))
            
            # Process episodes
            for row in episode_results:
                item = dict(row)
                show_item = {
                    'title': item['title'],
                    'year': item['year'],
                    'type': 'show',
                    'collected_at': item['collected_at'],
                    'imdb_id': item['imdb_id'],
                    'tmdb_id': item['tmdb_id'],
                    'season_number': item['season_number'],
                    'episode_number': item['episode_number'],
                    'version': item['version'],
                    'filled_by_file': item['filled_by_file'],
                    'filled_by_title': item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file']
                }
                
                # Get cached poster URL or add to list for batch fetch
                cached_url = get_cached_poster_url(item['tmdb_id'], 'tv')
                if cached_url:
                    show_item['poster_url'] = cached_url
                shows_list.append((show_item, item['tmdb_id']))
            
            # Batch fetch missing poster URLs
            movie_tasks = []
            show_tasks = []
            
            for item, tmdb_id in movies_list:
                if 'poster_url' not in item and tmdb_id:
                    task = asyncio.create_task(get_poster_url(session, tmdb_id, 'movie'))
                    movie_tasks.append((item, task))
            
            for item, tmdb_id in shows_list:
                if 'poster_url' not in item and tmdb_id:
                    task = asyncio.create_task(get_poster_url(session, tmdb_id, 'tv'))
                    show_tasks.append((item, task))
            
            # Wait for all poster tasks to complete
            if movie_tasks:
                movie_results = await asyncio.gather(*[task for _, task in movie_tasks], return_exceptions=True)
                for (item, _), result in zip(movie_tasks, movie_results):
                    if isinstance(result, Exception):
                        logging.error(f"Error fetching poster for movie with TMDB ID {item['tmdb_id']}: {result}")
                    elif result:
                        item['poster_url'] = result
                        cache_poster_url(item['tmdb_id'], 'movie', result)
                    else:
                        if not get_setting('TMDB', 'api_key'):
                            logging.warning("TMDB API key not set, using placeholder images")
                            placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                            item['poster_url'] = placeholder_url
            
            if show_tasks:
                show_results = await asyncio.gather(*[task for _, task in show_tasks], return_exceptions=True)
                for (item, _), result in zip(show_tasks, show_results):
                    if isinstance(result, Exception):
                        logging.error(f"Error fetching poster for show with TMDB ID {item['tmdb_id']}: {result}")
                    elif result:
                        item['poster_url'] = result
                        cache_poster_url(item['tmdb_id'], 'tv', result)
                    else:
                        if not get_setting('TMDB', 'api_key'):
                            logging.warning("TMDB API key not set, using placeholder images")
                            placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                            item['poster_url'] = placeholder_url
        
        # Clean expired cache entries
        clean_expired_cache()

        return {
            'movies': [item for item, _ in movies_list],
            'shows': [item for item, _ in shows_list]
        }
    except Exception as e:
        logging.error(f"Error in get_recently_added_items: {str(e)}")
        return {'movies': [], 'shows': []}
    finally:
        conn.close()

async def get_recently_upgraded_items(upgraded_limit=5):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Query for upgrades using the new upgraded flag
        upgraded_query = """
        WITH LatestUpgrades AS (
            SELECT 
                title,
                year,
                type,
                collected_at,
                imdb_id,
                tmdb_id,
                version,
                filled_by_title,
                filled_by_file,
                last_updated,
                season_number,
                episode_number,
                ROW_NUMBER() OVER (PARTITION BY title, year ORDER BY last_updated DESC) as rn
            FROM media_items
            WHERE upgraded = 1
        )
        SELECT 
            title,
            year,
            type,
            collected_at,
            imdb_id,
            tmdb_id,
            version,
            filled_by_title,
            filled_by_file,
            last_updated,
            season_number,
            episode_number
        FROM LatestUpgrades
        WHERE rn = 1
        ORDER BY last_updated DESC
        LIMIT ?
        """
        
        # Fetch more items initially to ensure we get enough unique ones
        cursor.execute(upgraded_query, (upgraded_limit * 2,))
        upgrade_results = cursor.fetchall()
        
        # Debug logging
        #logging.info(f"Found {len(upgrade_results)} upgraded items")
        #for row in upgrade_results:
        #    item = dict(row)
        #    logging.info(f"Upgraded item: {item['title']} - Type: {item['type']} - Last Updated: {item['last_updated']}")
        #    if item['type'] == 'episode':
        #        logging.info(f"  Season: {item['season_number']} Episode: {item['episode_number']}")
        
        media_items = {}
        
        async with aiohttp.ClientSession() as session:
            poster_tasks = []
            
            # Process items until we have enough unique ones
            for row in upgrade_results:
                item = dict(row)
                key = f"{item['title']}-{item['year']}"
                
                if key not in media_items:
                    media_items[key] = {
                        **item,
                        'versions': [item['version']],
                        'filled_by_title': [item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file']],
                        'collected_at': item['collected_at']
                    }
                    
                    # Debug logging for items being added to media_items
                    #logging.info(f"Adding to media_items: {key}")
                    
                    # Set media_type based on the item type from database
                    if item['type'] == 'movie':
                        media_items[key]['media_type'] = 'movie'
                    else:
                        media_items[key]['media_type'] = 'tv'
                        
                    # Add poster task
                    if item['tmdb_id']:
                        poster_task = asyncio.create_task(
                            get_poster_url(session, item['tmdb_id'], media_items[key]['media_type'])
                        )
                        poster_tasks.append((key, poster_task))
                        
                    # Break if we have enough unique items
                    if len(media_items) >= upgraded_limit:
                        break
                else:
                    # Debug logging for duplicate items
                    #logging.info(f"Duplicate item found: {key}")
                    if item['version'] not in media_items[key]['versions']:
                        media_items[key]['versions'].append(item['version'])
                    if item['filled_by_title'] and item['filled_by_title'] not in media_items[key]['filled_by_title']:
                        media_items[key]['filled_by_title'].append(item['filled_by_title'])
                    elif item['filled_by_file'] and item['filled_by_file'] not in media_items[key]['filled_by_title']:
                        media_items[key]['filled_by_title'].append(item['filled_by_file'])
            
            # Wait for all poster tasks to complete
            for key, task in poster_tasks:
                try:
                    poster_url = await task
                    media_items[key]['poster_url'] = poster_url
                except Exception as e:
                    logging.error(f"Error getting poster for {key}: {str(e)}")
                    media_items[key]['poster_url'] = None
        
        # Convert to list and sort by last_updated
        items_list = list(media_items.values())
        items_list.sort(key=lambda x: x['last_updated'], reverse=True)
        
        # Debug logging for final sorted list
        #logging.info("Final sorted items:")
        #for item in items_list:
            #logging.info(f"  {item['title']} - Last Updated: {item['last_updated']}")
        
        return items_list
    except Exception as e:
        logging.error(f"Error in get_recently_upgraded_items: {str(e)}", exc_info=True)
        return []
    finally:
        conn.close()