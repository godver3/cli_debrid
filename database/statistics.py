import logging
from .core import get_db_connection
import asyncio
import aiohttp
from .poster_management import get_poster_url
from routes.poster_cache import get_cached_poster_url, cache_poster_url, clean_expired_cache
from utilities.settings import get_setting
from flask import request, url_for
from urllib.parse import urlparse
from datetime import datetime
import time
from functools import wraps
import random
from debrid import get_debrid_provider, TooManyDownloadsError, ProviderUnavailableError
import threading
import sqlite3

def format_bytes(size):
    """Convert bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

# Cache for download stats
download_stats_cache = {
    'active_downloads': None,
    'usage_stats': None,
    'last_update': 0,
    'cache_duration': 300  # 5 minutes in seconds
}

def parse_size(size_str):
    """Convert human readable size string to bytes"""
    try:
        if not isinstance(size_str, str):
            return float(size_str)
            
        size_str = size_str.strip()
        if not size_str:
            return 0
            
        units = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
        number = float(''.join([c for c in size_str if c.isdigit() or c == '.']))
        unit = ''.join([c for c in size_str if c.isalpha()]).strip()
        
        if unit not in units:
            return float(size_str)  # Try direct conversion if no unit found
            
        return number * units[unit]
    except (ValueError, TypeError) as e:
        logging.error(f"Error parsing size string '{size_str}': {str(e)}")
        return 0

def get_cached_download_stats():
    """Get cached download stats or fetch new ones if cache is expired"""
    current_time = time.time()
    if (download_stats_cache['last_update'] + download_stats_cache['cache_duration'] < current_time or 
        download_stats_cache['active_downloads'] is None or 
        download_stats_cache['usage_stats'] is None):
        
        try:
            provider = get_debrid_provider()
            
            # Get active downloads
            try:
                active_count, limit = provider.get_active_downloads()
                raw_limit = limit if limit else provider.MAX_DOWNLOADS
                adjusted_limit = round(raw_limit)
                percentage = round((active_count / adjusted_limit * 100) if adjusted_limit > 0 else 0)
                
                status = 'normal'
                if percentage >= 90:
                    status = 'critical'
                elif percentage >= 75:
                    status = 'warning'
                
                download_stats_cache['active_downloads'] = {
                    'count': active_count,
                    'limit': adjusted_limit,
                    'percentage': percentage,
                    'status': status,
                    'error': None
                }
            except TooManyDownloadsError as e:
                import re
                match = re.search(r'(\d+)/(\d+)', str(e))
                if match:
                    active_count, limit = map(int, match.groups())
                    download_stats_cache['active_downloads'] = {
                        'count': active_count,
                        'limit': limit,
                        'percentage': round((active_count / limit * 100) if limit > 0 else 0),
                        'status': 'critical',
                        'error': 'too_many'
                    }
                else:
                    download_stats_cache['active_downloads'] = {
                        'count': 0,
                        'limit': 0,
                        'percentage': 0,
                        'status': 'error',
                        'error': 'too_many'
                    }
            except Exception as e:
                logging.error(f"Error getting active downloads: {str(e)}")
                download_stats_cache['active_downloads'] = {
                    'count': 0,
                    'limit': 0,
                    'percentage': 0,
                    'status': 'error',
                    'error': str(e)
                }
            
            # Get usage stats
            try:
                usage = provider.get_user_traffic()
                if not usage or usage.get('limit') is None:
                    download_stats_cache['usage_stats'] = {
                        'used': '0 GB',
                        'limit': '2000 GB',
                        'percentage': 0,
                        'error': None
                    }
                else:
                    # If we have formatted strings and percentage, use them directly
                    if isinstance(usage.get('used'), str) and isinstance(usage.get('limit'), str) and 'percentage' in usage:
                        download_stats_cache['usage_stats'] = {
                            'used': usage['used'],
                            'limit': usage['limit'],
                            'percentage': usage['percentage'],
                            'error': None
                        }
                    else:
                        # Handle numeric values (old Real-Debrid format)
                        used = usage.get('downloaded', usage.get('used', 0))
                        limit = usage.get('limit', 2000)
                        
                        # If values are numeric, assume they're in GB
                        if isinstance(used, (int, float)):
                            daily_used = used * 1024 * 1024 * 1024  # Convert GB to bytes
                            used_str = f"{used:.2f} GB"
                        else:
                            daily_used = parse_size(str(used))
                            used_str = str(used)
                            
                        if isinstance(limit, (int, float)):
                            daily_limit = limit * 1024 * 1024 * 1024  # Convert GB to bytes
                            limit_str = f"{limit:.2f} GB"
                        else:
                            daily_limit = parse_size(str(limit))
                            limit_str = str(limit)
                        
                        # Calculate percentage
                        percentage = round((daily_used / daily_limit) * 100) if daily_limit > 0 else 0
                        
                        download_stats_cache['usage_stats'] = {
                            'used': used_str,
                            'limit': limit_str,
                            'percentage': percentage,
                            'error': None
                        }
            except Exception as e:
                logging.error(f"Error getting usage stats: {str(e)}")
                logging.error(f"Raw usage data that caused error: {usage}")
                download_stats_cache['usage_stats'] = {
                    'used': '0 GB',
                    'limit': '2000 GB',
                    'percentage': 0,
                    'error': str(e)
                }

            download_stats_cache['last_update'] = current_time
            
        except ProviderUnavailableError as e:
            logging.error(f"Provider unavailable: {str(e)}")
            if download_stats_cache['active_downloads'] is None:
                download_stats_cache['active_downloads'] = {
                    'count': 0,
                    'limit': 0,
                    'percentage': 0,
                    'status': 'error',
                    'error': 'provider_unavailable'
                }
            if download_stats_cache['usage_stats'] is None:
                download_stats_cache['usage_stats'] = {
                    'used': '0 GB',
                    'limit': '2000 GB',
                    'percentage': 0,
                    'error': 'provider_unavailable'
                }
        except Exception as e:
            logging.error(f"Error updating download stats cache: {str(e)}")
            if download_stats_cache['active_downloads'] is None:
                download_stats_cache['active_downloads'] = {
                    'count': 0,
                    'limit': 0,
                    'percentage': 0,
                    'status': 'error',
                    'error': str(e)
                }
            if download_stats_cache['usage_stats'] is None:
                download_stats_cache['usage_stats'] = {
                    'used': '0 GB',
                    'limit': '2000 GB',
                    'percentage': 0,
                    'error': str(e)
                }

    return download_stats_cache['active_downloads'], download_stats_cache['usage_stats']

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

@cache_for_seconds(60)  # Cache collection counts for 1 minute
def get_collected_counts():
    """
    Get counts of collected media items.
    This function is optimized to use indexes and minimize computation.
    """
    import time
    overall_start = time.perf_counter()
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # First try to get data from the statistics summary table which should be faster
        summary_start = time.perf_counter()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
        if cursor.fetchone():
            cursor.execute('''
                SELECT total_movies, total_shows, total_episodes
                FROM statistics_summary
                WHERE id = 1
            ''')
            result = cursor.fetchone()
            if result and all(count is not None for count in result):
                summary_time = time.perf_counter() - summary_start
                logging.info(f"Statistics summary retrieval took {summary_time*1000:.2f}ms")
                return {
                    'total_movies': result[0],
                    'total_shows': result[1],
                    'total_episodes': result[2]
                }
        summary_time = time.perf_counter() - summary_start
        logging.info(f"Statistics summary check took {summary_time*1000:.2f}ms (cache miss)")
        
        # If we don't have the summary table or data is incomplete, fall back to direct counts
        # Use optimized individual queries with indexed fields
        
        # Get total movies count
        movies_start = time.perf_counter()
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items 
            WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
        ''')
        total_movies = cursor.fetchone()[0]
        movies_time = time.perf_counter() - movies_start
        logging.info(f"Movies count query took {movies_time*1000:.2f}ms")
        
        # Get total shows count
        shows_start = time.perf_counter()
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items 
            WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
        ''')
        total_shows = cursor.fetchone()[0]
        shows_time = time.perf_counter() - shows_start
        logging.info(f"Shows count query took {shows_time*1000:.2f}ms")
        
        # Get total episodes count
        episodes_start = time.perf_counter()
        cursor.execute('''
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT imdb_id, season_number, episode_number
                FROM media_items
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            )
        ''')
        total_episodes = cursor.fetchone()[0]
        episodes_time = time.perf_counter() - episodes_start
        logging.info(f"Episodes count query took {episodes_time*1000:.2f}ms")
        
        overall_time = time.perf_counter() - overall_start
        logging.info(f"Total get_collected_counts execution took {overall_time*1000:.2f}ms")
        
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
    import time
    overall_start = time.perf_counter()
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Simplified query - directly get most recent movies and episodes
        query_start = time.perf_counter()
        
        # Get most recent movies
        movie_query = """
        SELECT DISTINCT
            title,
            year,
            'movie' as type,
            collected_at,
            imdb_id,
            tmdb_id,
            version,
            filled_by_title,
            filled_by_file
        FROM media_items
        WHERE type = 'movie'
          AND collected_at IS NOT NULL 
          AND state IN ('Collected', 'Upgrading')
        ORDER BY collected_at DESC
        LIMIT ?
        """
        
        cursor.execute(movie_query, (movie_limit,))
        movie_results = cursor.fetchall()
        
        # Get most recent TV shows (using first episode as representative)
        show_query = """
        SELECT DISTINCT
            title,
            year,
            'episode' as type,
            collected_at,
            imdb_id,
            tmdb_id,
            version,
            filled_by_title,
            filled_by_file,
            season_number,
            episode_number
        FROM media_items
        WHERE type = 'episode'
          AND collected_at IS NOT NULL 
          AND state IN ('Collected', 'Upgrading')
        ORDER BY collected_at DESC
        LIMIT ?
        """
        
        cursor.execute(show_query, (show_limit,))
        show_results = cursor.fetchall()
        
        query_time = time.perf_counter() - query_start
        logging.info(f"Recently added items query took {query_time*1000:.2f}ms")
        
        movies_list = []
        shows_list = []
        
        # Process results and get poster urls
        poster_start = time.perf_counter()
        async with aiohttp.ClientSession() as session:
            # Process all items and create poster tasks in parallel
            poster_tasks = []
            
            process_start = time.perf_counter()
            
            # Process movies
            for row in movie_results:
                item = dict(row)
                media_item = {
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
                
                # Get cached poster URL or create task for batch fetch
                cached_url = get_cached_poster_url(item['tmdb_id'], 'movie')
                if cached_url:
                    media_item['poster_url'] = cached_url
                elif item['tmdb_id']:
                    task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], 'movie'))
                    poster_tasks.append((media_item, task))
                else:
                    if not get_setting('TMDB', 'api_key'):
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        media_item['poster_url'] = placeholder_url
                
                movies_list.append(media_item)
            
            # Process shows
            for row in show_results:
                item = dict(row)
                media_item = {
                    'title': item['title'],
                    'year': item['year'],
                    'type': 'show',
                    'collected_at': item['collected_at'],
                    'imdb_id': item['imdb_id'],
                    'tmdb_id': item['tmdb_id'],
                    'version': item['version'],
                    'filled_by_file': item['filled_by_file'],
                    'filled_by_title': item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file'],
                    'season_number': item['season_number'],
                    'episode_number': item['episode_number']
                }
                
                # Get cached poster URL or create task for batch fetch
                cached_url = get_cached_poster_url(item['tmdb_id'], 'tv')
                if cached_url:
                    media_item['poster_url'] = cached_url
                elif item['tmdb_id']:
                    task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], 'tv'))
                    poster_tasks.append((media_item, task))
                else:
                    if not get_setting('TMDB', 'api_key'):
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        media_item['poster_url'] = placeholder_url
                
                shows_list.append(media_item)
            
            process_time = time.perf_counter() - process_start
            logging.info(f"Processing items took {process_time*1000:.2f}ms")
            
            # Wait for all poster tasks to complete in parallel
            if poster_tasks:
                poster_fetch_start = time.perf_counter()
                results = await asyncio.gather(*[task for _, task in poster_tasks], return_exceptions=True)
                for (item, _), result in zip(poster_tasks, results):
                    if isinstance(result, Exception):
                        logging.error(f"Error fetching poster for {item['title']}: {result}")
                        if not get_setting('TMDB', 'api_key'):
                            placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                            item['poster_url'] = placeholder_url
                    elif result:
                        item['poster_url'] = result
                        cache_poster_url(item['tmdb_id'], 'movie' if item['type'] == 'movie' else 'tv', result)
                    elif not get_setting('TMDB', 'api_key'):
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        item['poster_url'] = placeholder_url
                poster_fetch_time = time.perf_counter() - poster_fetch_start
                logging.info(f"Fetching {len(poster_tasks)} posters took {poster_fetch_time*1000:.2f}ms")
        
        poster_total_time = time.perf_counter() - poster_start
        logging.info(f"Total poster processing took {poster_total_time*1000:.2f}ms")
        
        overall_time = time.perf_counter() - overall_start
        logging.info(f"Total get_recently_added_items execution took {overall_time*1000:.2f}ms")
        
        return {
            'movies': movies_list,
            'shows': shows_list
        }
    except Exception as e:
        logging.error(f"Error in get_recently_added_items: {str(e)}")
        return {'movies': [], 'shows': []}
    finally:
        conn.close()

@cache_for_seconds(30)
async def get_recently_upgraded_items(upgraded_limit=5):
    import time
    overall_start = time.perf_counter()
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Simplified query for upgrades - directly get the most recent upgrades
        query_start = time.perf_counter()
        upgraded_query = """
        SELECT 
            title,
            year,
            type,
            imdb_id,
            tmdb_id,
            version,
            filled_by_title,
            filled_by_file,
            upgrading_from,
            last_updated,
            collected_at,
            original_collected_at,
            season_number,
            episode_number
        FROM media_items
        WHERE upgraded = 1
          AND collected_at IS NOT NULL
        GROUP BY 
            CASE 
                WHEN type = 'movie' THEN title || year
                ELSE title || COALESCE(season_number, '') || COALESCE(episode_number, '')
            END
        ORDER BY collected_at DESC
        LIMIT ?
        """
        
        cursor.execute(upgraded_query, (upgraded_limit,))
        upgrade_results = cursor.fetchall()
        query_time = time.perf_counter() - query_start
        logging.info(f"Recently upgraded items query took {query_time*1000:.2f}ms")
        
        media_items = []
        poster_tasks = []
        
        # Process results and get poster urls
        poster_start = time.perf_counter()
        async with aiohttp.ClientSession() as session:
            process_start = time.perf_counter()
            for row in upgrade_results:
                item = dict(row)
                media_type = 'movie' if item['type'] == 'movie' else 'tv'
                
                # Make sure collected_at is available for sorting consistency
                if item['collected_at'] is None:
                    logging.warning(f"Upgraded item missing collected_at: {item['title']}")
                    continue
                
                media_item = {
                    'title': item['title'],
                    'year': item['year'],
                    'type': item['type'],
                    'version': item['version'],
                    'filled_by_file': item['filled_by_file'],
                    'filled_by_title': item['filled_by_title'] if item['filled_by_title'] else item['filled_by_file'],
                    'upgrading_from': item['upgrading_from'],
                    'last_updated': item['last_updated'],
                    'collected_at': item['collected_at'],
                    'original_collected_at': item['original_collected_at'],
                    'tmdb_id': item['tmdb_id']
                }
                
                if item['type'] == 'episode':
                    media_item.update({
                        'season_number': item['season_number'],
                        'episode_number': item['episode_number']
                    })
                
                # Get cached poster URL or create task for batch fetch
                cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                if cached_url:
                    media_item['poster_url'] = cached_url
                elif item['tmdb_id']:
                    task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                    poster_tasks.append((media_item, task))
                else:
                    if not get_setting('TMDB', 'api_key'):
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        media_item['poster_url'] = placeholder_url
                
                media_items.append(media_item)
            process_time = time.perf_counter() - process_start
            logging.info(f"Processing upgraded items took {process_time*1000:.2f}ms")
            
            # Wait for all poster tasks to complete in parallel
            if poster_tasks:
                poster_fetch_start = time.perf_counter()
                results = await asyncio.gather(*[task for _, task in poster_tasks], return_exceptions=True)
                for (item, _), result in zip(poster_tasks, results):
                    if isinstance(result, Exception):
                        logging.error(f"Error fetching poster for {item['title']}: {result}")
                        if not get_setting('TMDB', 'api_key'):
                            placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                            item['poster_url'] = placeholder_url
                    elif result:
                        item['poster_url'] = result
                        cache_poster_url(item['tmdb_id'], 'movie' if item['type'] == 'movie' else 'tv', result)
                    elif not get_setting('TMDB', 'api_key'):
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        item['poster_url'] = placeholder_url
                poster_fetch_time = time.perf_counter() - poster_fetch_start
                logging.info(f"Fetching {len(poster_tasks)} posters took {poster_fetch_time*1000:.2f}ms")
        
        poster_total_time = time.perf_counter() - poster_start
        logging.info(f"Total poster processing took {poster_total_time*1000:.2f}ms")
        
        overall_time = time.perf_counter() - overall_start
        logging.info(f"Total get_recently_upgraded_items execution took {overall_time*1000:.2f}ms")
        return media_items
    except Exception as e:
        logging.error(f"Error in get_recently_upgraded_items: {str(e)}", exc_info=True)
        return []
    finally:
        conn.close()

# Create a lock to prevent concurrent updates
statistics_update_lock = threading.Lock()
last_update_time = 0

def update_statistics_summary(force=False):
    """Update the statistics summary table with the latest data.
    This should be called periodically from the background task manager."""
    global last_update_time
    import time
    
    # Add throttling to prevent excessive updates
    current_time = time.time()
    if not force and current_time - last_update_time < 5:  # At least 5 seconds between updates
        logging.debug("Skipping statistics update - throttled (updated %0.2f seconds ago)", 
                      current_time - last_update_time)
        return

    # Use a lock to ensure only one update happens at a time
    if not statistics_update_lock.acquire(blocking=False):
        logging.debug("Statistics summary update already in progress, skipping")
        return
    
    try:
        update_start = time.perf_counter()
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # First check if the table exists
            table_check_start = time.perf_counter()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
            if not cursor.fetchone():
                # Table doesn't exist, close connection and create it first
                conn.close()
                from database.schema_management import create_statistics_summary_table
                create_statistics_summary_table()
                # Re-open connection after table creation
                conn = get_db_connection()
                cursor = conn.cursor()
            table_check_time = time.perf_counter() - table_check_start
            logging.info(f"Table check and creation took {table_check_time*1000:.2f}ms")
            
            # Check if we need to update
            update_check_start = time.perf_counter()
            if not force:
                cursor.execute("""
                    SELECT last_updated, datetime('now', '-1 minute')
                    FROM statistics_summary 
                    WHERE id=1
                """)
                last_update_check = cursor.fetchone()
                
                # If updated within the last minute, skip the update
                if last_update_check and last_update_check[0] >= last_update_check[1]:
                    logging.debug("Statistics updated recently, skipping update")
                    return
            update_check_time = time.perf_counter() - update_check_start
            logging.info(f"Update check took {update_check_time*1000:.2f}ms")
            
            # Get the latest statistics
            # Count unique collected movies
            movies_start = time.perf_counter()
            cursor.execute('''
                SELECT COUNT(DISTINCT imdb_id) 
                FROM media_items 
                WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
            ''')
            total_movies = cursor.fetchone()[0]
            movies_time = time.perf_counter() - movies_start
            logging.info(f"Movies count took {movies_time*1000:.2f}ms")

            # Count unique collected TV shows
            shows_start = time.perf_counter()
            cursor.execute('''
                SELECT COUNT(DISTINCT imdb_id) 
                FROM media_items 
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            ''')
            total_shows = cursor.fetchone()[0]
            shows_time = time.perf_counter() - shows_start
            logging.info(f"Shows count took {shows_time*1000:.2f}ms")

            # Count unique collected episodes - Optimized query
            episodes_start = time.perf_counter()
            cursor.execute('''
                SELECT COUNT(*) 
                FROM (
                    SELECT DISTINCT imdb_id, season_number, episode_number
                    FROM media_items 
                    WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
                )
            ''')
            total_episodes = cursor.fetchone()[0]
            episodes_time = time.perf_counter() - episodes_start
            logging.info(f"Episodes count took {episodes_time*1000:.2f}ms")
            
            # Get latest collected movie
            latest_movie_start = time.perf_counter()
            cursor.execute('''
                SELECT collected_at 
                FROM media_items 
                WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
                ORDER BY collected_at DESC
                LIMIT 1
            ''')
            latest_movie = cursor.fetchone()
            latest_movie_collected = latest_movie[0] if latest_movie else None
            latest_movie_time = time.perf_counter() - latest_movie_start
            logging.info(f"Latest movie query took {latest_movie_time*1000:.2f}ms")
            
            # Get latest collected episode
            latest_episode_start = time.perf_counter()
            cursor.execute('''
                SELECT collected_at
                FROM media_items 
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
                ORDER BY collected_at DESC
                LIMIT 1
            ''')
            latest_episode = cursor.fetchone()
            latest_episode_collected = latest_episode[0] if latest_episode else None
            latest_episode_time = time.perf_counter() - latest_episode_start
            logging.info(f"Latest episode query took {latest_episode_time*1000:.2f}ms")
            
            # Get latest upgraded item
            latest_upgrade_start = time.perf_counter()
            cursor.execute('''
                SELECT collected_at
                FROM media_items 
                WHERE upgraded = 1
                ORDER BY collected_at DESC
                LIMIT 1
            ''')
            latest_upgraded = cursor.fetchone()
            latest_upgraded_date = latest_upgraded[0] if latest_upgraded else None
            latest_upgrade_time = time.perf_counter() - latest_upgrade_start
            logging.info(f"Latest upgrade query took {latest_upgrade_time*1000:.2f}ms")
            
            # Update the summary table
            update_start = time.perf_counter()
            cursor.execute('''
                UPDATE statistics_summary
                SET total_movies = ?,
                    total_shows = ?,
                    total_episodes = ?,
                    latest_movie_collected = ?,
                    latest_episode_collected = ?,
                    latest_upgraded = ?,
                    last_updated = datetime('now', 'localtime')
                WHERE id = 1
            ''', (total_movies, total_shows, total_episodes, 
                latest_movie_collected, latest_episode_collected, latest_upgraded_date))
            
            conn.commit()
            update_time = time.perf_counter() - update_start
            logging.info(f"Final update and commit took {update_time*1000:.2f}ms")
            
            last_update_time = current_time
            
            total_time = time.perf_counter() - update_start
            logging.info(f"Total update_statistics_summary execution took {total_time*1000:.2f}ms")

        except Exception as e:
            logging.error(f"Error updating statistics summary: {str(e)}", exc_info=True)
        finally:
            conn.close()
    finally:
        statistics_update_lock.release()

def get_statistics_summary():
    """Get the statistics summary from the dedicated table"""
    import time
    overall_start = time.perf_counter()
    conn = None
    try:
        # Time database connection
        db_connect_start = time.perf_counter()
        conn = get_db_connection()
        cursor = conn.cursor()
        db_connect_time = time.perf_counter() - db_connect_start
        logging.info(f"Database connection took {db_connect_time*1000:.2f}ms")

        # Time table existence check
        table_check_start = time.perf_counter()
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
            if not cursor.fetchone():
                # Table doesn't exist, create it first
                if conn:
                    conn.close()
                    conn = None
                
                try:
                    create_start = time.perf_counter()
                    from database.schema_management import create_statistics_summary_table
                    create_statistics_summary_table()
                    create_time = time.perf_counter() - create_start
                    logging.info(f"Creating statistics table took {create_time*1000:.2f}ms")
                    
                    # Instead of recursively calling, continue with the logic inline
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    
                    # Initialize with direct counts
                    counts_start = time.perf_counter()
                    initial_counts = get_collected_counts()
                    counts_time = time.perf_counter() - counts_start
                    logging.info(f"Initial counts calculation took {counts_time*1000:.2f}ms")

                    insert_start = time.perf_counter()
                    conn.execute('''
                        INSERT OR IGNORE INTO statistics_summary 
                        (id, total_movies, total_shows, total_episodes, last_updated)
                        VALUES (1, ?, ?, ?, datetime('now', 'localtime'))
                    ''', (
                        initial_counts['total_movies'],
                        initial_counts['total_shows'],
                        initial_counts['total_episodes']
                    ))
                    conn.commit()
                    insert_time = time.perf_counter() - insert_start
                    logging.info(f"Initial data insertion took {insert_time*1000:.2f}ms")
                    
                    # Return the counts we just calculated
                    return initial_counts
                except Exception as e:
                    logging.error(f"Error creating statistics table: {str(e)}")
                    fallback_start = time.perf_counter()
                    result = get_collected_counts()  # Fallback on error
                    logging.info(f"Fallback counts took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
                    return result
        except sqlite3.OperationalError as e:
            if "no such table: sqlite_master" in str(e):
                logging.error("Database connection issue: sqlite_master not found")
            else:
                logging.error(f"SQLite error checking for statistics_summary table: {str(e)}")
            fallback_start = time.perf_counter()
            result = get_collected_counts()
            logging.info(f"Fallback counts after error took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
            return result
        table_check_time = time.perf_counter() - table_check_start
        logging.info(f"Table existence check took {table_check_time*1000:.2f}ms")
        
        # Time data retrieval and update check
        query_start = time.perf_counter()
        try:
            cursor.execute('''
                SELECT total_movies, total_shows, total_episodes, 
                    last_updated, 
                    datetime('now', '-5 minute')
                FROM statistics_summary 
                WHERE id = 1
            ''')
            result = cursor.fetchone()
        except sqlite3.OperationalError as e:
            if "no such table: statistics_summary" in str(e):
                logging.error("statistics_summary table doesn't exist despite earlier check")
                fallback_start = time.perf_counter()
                result = get_collected_counts()
                logging.info(f"Fallback counts after table error took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
                return result
            else:
                logging.error(f"SQLite error querying statistics_summary: {str(e)}")
                fallback_start = time.perf_counter()
                result = get_collected_counts()
                logging.info(f"Fallback counts after query error took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
                return result
        query_time = time.perf_counter() - query_start
        logging.info(f"Data retrieval query took {query_time*1000:.2f}ms")
        
        if not result:
            # No data yet, initialize it
            try:
                # Get fresh counts
                counts_start = time.perf_counter()
                counts = get_collected_counts()
                counts_time = time.perf_counter() - counts_start
                logging.info(f"Fresh counts calculation took {counts_time*1000:.2f}ms")
                
                # Insert the initial data
                insert_start = time.perf_counter()
                cursor.execute('''
                    INSERT OR IGNORE INTO statistics_summary 
                    (id, total_movies, total_shows, total_episodes, last_updated)
                    VALUES (1, ?, ?, ?, datetime('now', 'localtime'))
                ''', (counts['total_movies'], counts['total_shows'], counts['total_episodes']))
                conn.commit()
                insert_time = time.perf_counter() - insert_start
                logging.info(f"Data insertion took {insert_time*1000:.2f}ms")
                
                return counts
            except sqlite3.Error as e:
                logging.error(f"SQLite error initializing statistics_summary: {str(e)}")
                fallback_start = time.perf_counter()
                result = get_collected_counts()
                logging.info(f"Fallback counts after initialization error took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
                return result
                
        elif result[3] < result[4]:
            # Data exists but is too old
            if conn:
                conn.close()
                conn = None
            
            try:
                # Update data
                update_start = time.perf_counter()
                update_statistics_summary(force=True)
                update_time = time.perf_counter() - update_start
                logging.info(f"Statistics update took {update_time*1000:.2f}ms")
                
                # Open a new connection to get the fresh data
                new_conn_start = time.perf_counter()
                conn = get_db_connection()
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT total_movies, total_shows, total_episodes, last_updated
                    FROM statistics_summary 
                    WHERE id = 1
                ''')
                updated_result = cursor.fetchone()
                new_conn_time = time.perf_counter() - new_conn_start
                logging.info(f"New connection and data fetch took {new_conn_time*1000:.2f}ms")
                
                if updated_result:
                    return {
                        'total_movies': updated_result[0],
                        'total_shows': updated_result[1],
                        'total_episodes': updated_result[2],
                        'last_updated': updated_result[3]
                    }
                else:
                    logging.error("Failed to retrieve updated statistics")
                    fallback_start = time.perf_counter()
                    result = get_collected_counts()
                    logging.info(f"Fallback counts after update failure took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
                    return result
            except Exception as e:
                logging.error(f"Error updating statistics: {str(e)}")
                fallback_start = time.perf_counter()
                result = get_collected_counts()
                logging.info(f"Fallback counts after update error took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
                return result
            
        # Return the valid data we found
        overall_time = time.perf_counter() - overall_start
        logging.info(f"Total get_statistics_summary execution took {overall_time*1000:.2f}ms")
        return {
            'total_movies': result[0],
            'total_shows': result[1],
            'total_episodes': result[2],
            'last_updated': result[3]
        }
    except Exception as e:
        logging.error(f"Unexpected error getting statistics summary: {str(e)}", exc_info=True)
        fallback_start = time.perf_counter()
        result = get_collected_counts()  # Fallback to direct count
        logging.info(f"Final fallback counts took {(time.perf_counter() - fallback_start)*1000:.2f}ms")
        return result
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass