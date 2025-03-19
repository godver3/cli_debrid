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
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # First try to get data from the statistics summary table which should be faster
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
        if cursor.fetchone():
            cursor.execute('''
                SELECT total_movies, total_shows, total_episodes
                FROM statistics_summary
                WHERE id = 1
            ''')
            result = cursor.fetchone()
            if result and all(count is not None for count in result):
                return {
                    'total_movies': result[0],
                    'total_shows': result[1],
                    'total_episodes': result[2]
                }
        
        # If we don't have the summary table or data is incomplete, fall back to direct counts
        # Use optimized individual queries with indexed fields
        # Get total movies count
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items 
            WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
        ''')
        total_movies = cursor.fetchone()[0]
        
        # Get total shows count
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items 
            WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
        ''')
        total_shows = cursor.fetchone()[0]
        
        # Get total episodes count
        cursor.execute('''
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT imdb_id, season_number, episode_number
                FROM media_items
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            )
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
        
        # Fixed query - using subqueries to properly apply ORDER BY and LIMIT before UNION
        combined_query = """
        SELECT * FROM (
            -- First get the most recent movies
            SELECT 
                title,
                year,
                'movie' as type,
                collected_at,
                imdb_id,
                tmdb_id,
                version,
                filled_by_title,
                filled_by_file,
                NULL as season_number,
                NULL as episode_number
            FROM media_items
            WHERE type = 'movie' 
              AND collected_at IS NOT NULL 
              AND state IN ('Collected', 'Upgrading')
            GROUP BY title, year -- Group to get one movie per title/year
            ORDER BY collected_at DESC
            LIMIT ?
        )
        
        UNION ALL
        
        SELECT * FROM (
            -- Then get the most recent show episodes
            SELECT 
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
            GROUP BY title
            ORDER BY collected_at DESC
            LIMIT ?
        )
        """
        
        cursor.execute(combined_query, (movie_limit, show_limit))
        results = cursor.fetchall()
        
        movies_list = []
        shows_list = []
        
        # Process results and get poster urls
        async with aiohttp.ClientSession() as session:
            # Process all items and create poster tasks in parallel
            poster_tasks = []
            
            for row in results:
                item = dict(row)
                media_item = {
                    'title': item['title'],
                    'year': item['year'],
                    'type': 'movie' if item['type'] == 'movie' else 'show',
                    'collected_at': item['collected_at'],
                    'imdb_id': item['imdb_id'],
                    'tmdb_id': item['tmdb_id'],
                    'version': item['version'],
                    'filled_by_file': item['filled_by_file'],
                    'filled_by_title': item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file']
                }
                
                if item['type'] == 'episode':
                    media_item.update({
                        'season_number': item['season_number'],
                        'episode_number': item['episode_number']
                    })
                
                # Get cached poster URL or create task for batch fetch
                media_type = 'movie' if item['type'] == 'movie' else 'tv'
                cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                
                if cached_url:
                    media_item['poster_url'] = cached_url
                elif item['tmdb_id']:
                    task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                    poster_tasks.append((media_item, task))
                else:
                    # Use placeholder if no TMDB ID
                    if not get_setting('TMDB', 'api_key'):
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        media_item['poster_url'] = placeholder_url
                
                if item['type'] == 'movie':
                    movies_list.append(media_item)
                else:
                    shows_list.append(media_item)
            
            # Wait for all poster tasks to complete in parallel
            if poster_tasks:
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
        
        # Clean expired cache entries periodically instead of every request
        if random.random() < 0.1:  # 10% chance to clean cache
            clean_expired_cache()

        return {
            'movies': movies_list[:movie_limit],
            'shows': shows_list[:show_limit]
        }
    except Exception as e:
        logging.error(f"Error in get_recently_added_items: {str(e)}")
        return {'movies': [], 'shows': []}
    finally:
        conn.close()

@cache_for_seconds(30)
async def get_recently_upgraded_items(upgraded_limit=5):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Simplified query for upgrades - directly get the most recent upgrades
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
        
        media_items = []
        poster_tasks = []
        
        async with aiohttp.ClientSession() as session:
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
            
            # Wait for all poster tasks to complete in parallel
            if poster_tasks:
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
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # First check if the table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
            if not cursor.fetchone():
                # Table doesn't exist, close connection and create it first
                conn.close()
                from database.schema_management import create_statistics_summary_table
                create_statistics_summary_table()
                # Re-open connection after table creation
                conn = get_db_connection()
                cursor = conn.cursor()
            
            # Check if we need to update
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
            
            # Check if we need to initialize
            cursor.execute("SELECT id FROM statistics_summary WHERE id=1")
            if not cursor.fetchone():
                # Initialize if not exists
                cursor.execute('''
                INSERT INTO statistics_summary 
                (id, total_movies, total_shows, total_episodes, last_updated)
                VALUES (1, 0, 0, 0, datetime('now', 'localtime'))
                ''')
                conn.commit()
            
            # Get the latest statistics
            # Count unique collected movies
            cursor.execute('''
                SELECT COUNT(DISTINCT imdb_id) 
                FROM media_items 
                WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
            ''')
            total_movies = cursor.fetchone()[0]

            # Count unique collected TV shows
            cursor.execute('''
                SELECT COUNT(DISTINCT imdb_id) 
                FROM media_items 
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            ''')
            total_shows = cursor.fetchone()[0]

            # Count unique collected episodes
            cursor.execute('''
                SELECT COUNT(DISTINCT imdb_id || '-' || season_number || '-' || episode_number) 
                FROM media_items 
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            ''')
            total_episodes = cursor.fetchone()[0]
            
            # Get latest collected movie
            cursor.execute('''
                SELECT collected_at 
                FROM media_items 
                WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
                ORDER BY collected_at DESC
                LIMIT 1
            ''')
            latest_movie = cursor.fetchone()
            latest_movie_collected = latest_movie[0] if latest_movie else None
            
            # Get latest collected episode
            cursor.execute('''
                SELECT collected_at
                FROM media_items 
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
                ORDER BY collected_at DESC
                LIMIT 1
            ''')
            latest_episode = cursor.fetchone()
            latest_episode_collected = latest_episode[0] if latest_episode else None
            
            # Get latest upgraded item
            cursor.execute('''
                SELECT collected_at
                FROM media_items 
                WHERE upgraded = 1
                ORDER BY collected_at DESC
                LIMIT 1
            ''')
            latest_upgraded = cursor.fetchone()
            latest_upgraded_date = latest_upgraded[0] if latest_upgraded else None
            
            # Update the summary table
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
            last_update_time = current_time

            
        except Exception as e:
            logging.error(f"Error updating statistics summary: {str(e)}", exc_info=True)
        finally:
            conn.close()
    finally:
        statistics_update_lock.release()

def get_statistics_summary():
    """Get the statistics summary from the dedicated table"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # First check if the table exists
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
            if not cursor.fetchone():
                # Table doesn't exist, create it first
                if conn:
                    conn.close()
                    conn = None
                
                try:
                    from database.schema_management import create_statistics_summary_table
                    create_statistics_summary_table()
                    
                    # Instead of recursively calling, continue with the logic inline
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    
                    # Initialize with direct counts
                    initial_counts = get_collected_counts()
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
                    
                    # Return the counts we just calculated
                    return initial_counts
                except Exception as e:
                    logging.error(f"Error creating statistics table: {str(e)}")
                    return get_collected_counts()  # Fallback on error
        except sqlite3.OperationalError as e:
            if "no such table: sqlite_master" in str(e):
                logging.error("Database connection issue: sqlite_master not found")
            else:
                logging.error(f"SQLite error checking for statistics_summary table: {str(e)}")
            return get_collected_counts()
        
        # Check if summary data exists and needs updating
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
                return get_collected_counts()
            else:
                logging.error(f"SQLite error querying statistics_summary: {str(e)}")
                return get_collected_counts()
        
        if not result:
            # No data yet, initialize it
            try:
                # Get fresh counts
                counts = get_collected_counts()
                
                # Insert the initial data
                cursor.execute('''
                    INSERT OR IGNORE INTO statistics_summary 
                    (id, total_movies, total_shows, total_episodes, last_updated)
                    VALUES (1, ?, ?, ?, datetime('now', 'localtime'))
                ''', (counts['total_movies'], counts['total_shows'], counts['total_episodes']))
                conn.commit()
                
                return counts
            except sqlite3.Error as e:
                logging.error(f"SQLite error initializing statistics_summary: {str(e)}")
                return get_collected_counts()
                
        elif result[3] < result[4]:
            # Data exists but is too old
            if conn:
                conn.close()
                conn = None
            
            try:
                # Update data
                update_statistics_summary(force=True)
                
                # Open a new connection to get the fresh data
                conn = get_db_connection()
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT total_movies, total_shows, total_episodes, last_updated
                    FROM statistics_summary 
                    WHERE id = 1
                ''')
                updated_result = cursor.fetchone()
                
                if updated_result:
                    return {
                        'total_movies': updated_result[0],
                        'total_shows': updated_result[1],
                        'total_episodes': updated_result[2],
                        'last_updated': updated_result[3]
                    }
                else:
                    logging.error("Failed to retrieve updated statistics")
                    return get_collected_counts()
            except Exception as e:
                logging.error(f"Error updating statistics: {str(e)}")
                return get_collected_counts()
            
        # Return the valid data we found
        return {
            'total_movies': result[0],
            'total_shows': result[1],
            'total_episodes': result[2],
            'last_updated': result[3]
        }
    except Exception as e:
        logging.error(f"Unexpected error getting statistics summary: {str(e)}", exc_info=True)
        return get_collected_counts()  # Fallback to direct count
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass