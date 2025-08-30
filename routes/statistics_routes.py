from flask import Blueprint, jsonify, request, render_template, session, make_response
from datetime import datetime, timedelta
from operator import itemgetter
from itertools import groupby
import time
import logging
import asyncio
import aiohttp
import os
from utilities.settings import get_setting, set_setting
from .models import user_required, onboarding_required 
from routes.extensions import app_start_time
from debrid import get_debrid_provider, TooManyDownloadsError, ProviderUnavailableError
from .program_operation_routes import get_program_status
import json
import math
from functools import wraps
# Provider-agnostic: avoid direct Real-Debrid import
from typing import Optional, Dict, List, Any
import calendar

def cache_for_seconds(seconds):
    """Cache the result of a function for the specified number of seconds."""
    def decorator(func):
        cache = {}
        
        @wraps(func)
        def wrapper(*args, **kwargs):
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
            
        return wrapper
    return decorator

def get_cached_active_downloads():
    """Get active downloads with caching"""
    from database import get_cached_download_stats
    active_downloads, _ = get_cached_download_stats()
    return active_downloads

def get_cached_user_traffic():
    """Get user traffic with caching"""
    from database import get_cached_download_stats
    _, usage_stats = get_cached_download_stats()
    return usage_stats

statistics_bp = Blueprint('statistics', __name__)
root_bp = Blueprint('root', __name__)

def get_airing_soon():
    from database import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()
    
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    
    query = """
    SELECT title, release_date, airtime
    FROM media_items
    WHERE type = 'episode' AND release_date BETWEEN ? AND ?
    ORDER BY release_date, airtime
    """
    
    cursor.execute(query, (today.isoformat(), tomorrow.isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    # Group by title and take the earliest air date/time for each show
    grouped_results = []
    for key, group in groupby(results, key=itemgetter(0)):
        group_list = list(group)
        grouped_results.append({
            'title': key,
            'air_date': group_list[0][1],
            'air_time': group_list[0][2]
        })
    
    return grouped_results

#@cache_for_seconds(300)  # Cache for 5 minutes since releases don't change frequently
def get_upcoming_releases():
    from database import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()
    
    today = datetime.now().date()
    next_month = today + timedelta(days=28)
    
    # Simplified query - directly filter out existing movies in the main query
    query = """
    SELECT m.title, m.release_date, m.tmdb_id, m.imdb_id
    FROM media_items m
    WHERE m.type = 'movie' 
      AND m.release_date BETWEEN ? AND ?
      AND NOT EXISTS (
          SELECT 1 
          FROM media_items e 
          WHERE e.tmdb_id = m.tmdb_id 
            AND e.type = 'movie'
            AND e.state IN ('Collected', 'Upgrading', 'Checking')
      )
    GROUP BY m.release_date, m.title  -- Group by date and title to remove duplicates
    ORDER BY m.release_date ASC
    """
    
    cursor.execute(query, (today.isoformat(), next_month.isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    # Group by release date
    grouped_results = {}
    for title, release_date, tmdb_id, imdb_id in results:
        if release_date not in grouped_results:
            grouped_results[release_date] = []
        grouped_results[release_date].append({
            'title': title,
            'tmdb_id': tmdb_id,
            'imdb_id': imdb_id
        })
    
    # Format the results
    formatted_results = [
        {
            'titles': [item['title'] for item in items],
            'tmdb_ids': [item['tmdb_id'] for item in items if item['tmdb_id']],
            'imdb_ids': [item['imdb_id'] for item in items if item['imdb_id']],
            'release_date': date
        }
        for date, items in grouped_results.items()
    ]
    
    return formatted_results

#@cache_for_seconds(300) # Consider caching if appropriate
def get_movies_for_calendar(days_past: int = 7, days_future: int = 28, start_date_override_iso: Optional[str] = None, end_date_override_iso: Optional[str] = None):
    from database import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()
    
    today = datetime.now().date()
    
    if start_date_override_iso and end_date_override_iso:
        query_start_date_iso = start_date_override_iso
        query_end_date_iso = end_date_override_iso
    else:
        # Define date range: e.g., 7 days in the past to 28 days in the future
        # Adjust these timedelta values as needed
        start_range_date = today - timedelta(days=days_past)
        end_range_date = today + timedelta(days=days_future)
        query_start_date_iso = start_range_date.isoformat()
        query_end_date_iso = end_range_date.isoformat()
    
    # Query to get movies within the date range, along with their current state.
    # We group by essential fields to get unique movie releases.
    # MAX(m.state) is used to get a definitive state if there happen to be multiple entries
    # for the exact same movie release (which should be rare for type 'movie').
    query = """
    SELECT 
        m.title, 
        m.release_date, 
        m.tmdb_id, 
        m.imdb_id,
        COALESCE(MAX(m.state), 'Unknown') as state 
    FROM media_items m
    WHERE m.type = 'movie' 
      AND m.release_date BETWEEN ? AND ?
    GROUP BY m.title, m.release_date, m.tmdb_id, m.imdb_id
    ORDER BY m.release_date ASC, m.title ASC
    """
    
    cursor.execute(query, (query_start_date_iso, query_end_date_iso))
    results = cursor.fetchall()
    conn.close()
    
    movies_data = []
    for title, release_date_str, tmdb_id, imdb_id, state in results:
        movies_data.append({
            'title': title,
            'release_date': release_date_str, # This is a string 'YYYY-MM-DD'
            'tmdb_id': tmdb_id,
            'imdb_id': imdb_id,
            'state': state # Current state from DB
        })
    return movies_data

#@cache_for_seconds(600)  # Increase cache to 10 minutes since show airtimes don't change frequently
def get_recently_aired_and_airing_soon(days_past: int = 2, days_future: int = 1, start_date_override_iso: Optional[str] = None, end_date_override_iso: Optional[str] = None):
    from metadata.metadata import get_show_airtime_by_imdb_id, _get_local_timezone
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Get timezone using our robust function
        local_tz = _get_local_timezone()
        now = datetime.now(local_tz) # This 'now' is for classification, not for query range if overridden
        
        if start_date_override_iso and end_date_override_iso:
            query_start_date_for_sql = start_date_override_iso
            query_end_date_for_sql = end_date_override_iso
        else:
            # Default behavior if overrides are not provided
            two_days_ago = now - timedelta(days=days_past)
            tomorrow = now + timedelta(days=days_future)
            # Get date range for query
            query_start_date_for_sql = two_days_ago.date().isoformat()
            query_end_date_for_sql = tomorrow.date().isoformat()
        
        # New optimized approach:
        # 1. First, get all unique episode combinations in one query
        # 2. Process results in memory (which was already fast in the original code)
        
        prefilter_start = time.perf_counter()
        
        # Use a single optimized query with GROUP BY instead of temporary tables and joins
        optimized_query = """
        SELECT 
            title,
            season_number,
            episode_number,
            release_date,
            airtime,
            imdb_id,
            tmdb_id,
            -- Determine effective state: if any version is 'Collected', report 'Collected'.
            -- Otherwise, take the MAX state from non-blacklisted items in the group.
            CASE
                WHEN SUM(CASE WHEN state = 'Collected' THEN 1 ELSE 0 END) > 0 THEN 'Collected'
                ELSE MAX(state)
            END as state,
            MAX(upgrading_from) as upgrading_from -- Get upgrading_from if present
        FROM media_items
        WHERE type = 'episode' 
          AND release_date BETWEEN ? AND ?
          AND state != 'Blacklisted'  -- Exclude blacklisted items
        GROUP BY title, season_number, episode_number
        ORDER BY release_date, airtime, title
        LIMIT 1000  -- Limit to reasonable number of results
        """
        
        # Log the query plan to help optimize in the future
        try:
            cursor.execute("EXPLAIN QUERY PLAN " + optimized_query, (query_start_date_for_sql, query_end_date_for_sql))
            query_plan = cursor.fetchall()
        except Exception as e:
            logging.warning(f"Could not get query plan: {e}")
        
        query_start = time.perf_counter()
        cursor.execute(optimized_query, (query_start_date_for_sql, query_end_date_for_sql))
        results = cursor.fetchall()
        query_time = time.perf_counter() - query_start
        
        # Log how many shows we found
        show_count = len(set(row[0] for row in results))
        
        # If we have no results, return early
        if not results:
            return [], []
        
        recently_aired = []
        airing_soon = []
        
        # Group episodes by show, season, and release date - for better performance
        processing_start = time.perf_counter()
        shows = {}
        
        # Precompute current time once instead of checking repeatedly
        now_timestamp = now.timestamp()
        
        for result in results:
            title, season, episode, release_date, airtime, imdb_id, tmdb_id, state, upgrading_from = result
            try:
                release_date = datetime.fromisoformat(release_date) if isinstance(release_date, str) else release_date
                
                # Get default airtime if none provided
                if not airtime:
                    airtime = "19:00"  # Default to 7 PM
                
                # Parse airtime more efficiently
                try:
                    if isinstance(airtime, str):
                        hour, minute = map(int, airtime.split(':'))
                        airtime = datetime.min.replace(hour=hour, minute=minute).time()
                    elif not isinstance(airtime, time):
                        airtime = datetime.min.replace(hour=19, minute=0).time()
                except (ValueError, TypeError):
                    airtime = datetime.min.replace(hour=19, minute=0).time()
                
                # Create datetime in local timezone more efficiently
                air_datetime = datetime.combine(release_date.date(), airtime)
                air_datetime = air_datetime.replace(tzinfo=local_tz) if hasattr(local_tz, 'localize') else air_datetime.replace(tzinfo=local_tz)
                
                # Determine display status
                display_status = 'uncollected' # Default
                if state == 'Collected':
                    display_status = 'collected'
                elif state == 'Upgrading': # Item is in UpgradingQueue, waiting for hourly scrape
                    display_status = 'collected' # Shows as normal/collected
                elif (state == 'Adding' or state == 'Checking') and upgrading_from:
                    # Item is being added to debrid for an upgrade (Adding)
                    # OR item is downloaded and being verified (Checking)
                    display_status = 'checking_upgrade' # Should be blue
                # If state is 'Wanted', 'Searching', etc., it will remain 'uncollected'
                
                # Create a key for grouping
                show_key = f"{title}_{season}_{release_date.date()}"
                
                if show_key not in shows:
                    shows[show_key] = {
                        'title': title,
                        'season': season,
                        'episodes': set(),
                        'air_datetime': air_datetime,
                        'release_date': release_date.date(),
                        'display_status': display_status, # Store first status found for the group
                        'imdb_id': imdb_id,
                        'tmdb_id': tmdb_id
                    }
                
                # If any episode in the group is collected or checking_upgrade, prioritize that status
                if display_status == 'collected' and shows[show_key]['display_status'] != 'collected':
                    shows[show_key]['display_status'] = 'collected'
                elif display_status == 'checking_upgrade' and shows[show_key]['display_status'] == 'uncollected':
                    shows[show_key]['display_status'] = 'checking_upgrade'
                
                shows[show_key]['episodes'].add(episode)
            
            except (ValueError, AttributeError) as e:
                logging.error(f"Error parsing date/time for {title}: {e}")
                continue
        
        # Faster processing for episode ranges
        for show in shows.values():
            if not show['episodes']:
                continue
                
            episodes = sorted(list(show['episodes']))
            
            # Fast consecutive range algorithm
            ranges = []
            if episodes:
                range_start = episodes[0]
                range_end = range_start
                
                for ep in episodes[1:]:
                    if ep == range_end + 1:
                        range_end = ep
                    else:
                        ranges.append((range_start, range_end))
                        range_start = ep
                        range_end = ep
                
                # Add the last range
                ranges.append((range_start, range_end))
                
                # Format episode ranges
                episode_parts = []
                for start, end in ranges:
                    if start == end:
                        episode_parts.append(f"E{start:02d}")
                    else:
                        episode_parts.append(f"E{start:02d}-{end:02d}")
                
                episode_range = ", ".join(episode_parts)
                
                formatted_item = {
                    'title': f"{show['title']} S{show['season']:02d}{episode_range}",
                    'air_datetime': show['air_datetime'],
                    'sort_key': show['air_datetime'].isoformat(),
                    'display_status': show['display_status'], # Use the determined status
                    'imdb_id': show['imdb_id'],
                    'tmdb_id': show['tmdb_id'],
                    'season_number': show['season'],
                    'episode_number': episodes[0]  # Use first episode number for single episodes or ranges
                }
                
                # Use timestamp comparison which is faster than datetime comparison
                if show['air_datetime'].timestamp() <= now_timestamp:
                    recently_aired.append(formatted_item)
                else:
                    airing_soon.append(formatted_item)
        
        processing_time = time.perf_counter() - processing_start
        
        # Sort both lists by air datetime
        recently_aired.sort(key=lambda x: x['air_datetime'], reverse=True)  # Most recent first
        airing_soon.sort(key=lambda x: x['air_datetime'])  # Soonest first
        
        # Limit to reasonable numbers
        recently_aired = recently_aired[:500]
        airing_soon = airing_soon[:500]
        
        return recently_aired, airing_soon
    except Exception as e:
        logging.error(f"Error in get_recently_aired_and_airing_soon: {str(e)}", exc_info=True)
        return [], []
    finally:
        # Clean up any resources
        if conn:
            conn.close()

@root_bp.route('/set_compact_preference', methods=['POST'])
def set_compact_preference():
    data = request.json
    compact_view = data.get('compactView', False)
    
    # Save the preference using settings system
    set_setting('UI Settings', 'compact_view', compact_view)
    
    return jsonify({'success': True, 'compactView': compact_view})

@root_bp.route('/')
@user_required
@onboarding_required
def root():
    overall_start_time = time.perf_counter()

    # Check if running in limited environment
    from utilities.set_supervisor_env import is_limited_environment
    limited_env = is_limited_environment()

    # Get view preferences from settings
    settings_fetch_start = time.perf_counter()
    use_24hour_format = get_setting('UI Settings', 'use_24hour_format', True)
    compact_view = get_setting('UI Settings', 'compact_view', False)

    # Check if user is on mobile using User-Agent
    user_agent_check_start = time.perf_counter()
    user_agent = request.headers.get('User-Agent', '').lower()
    is_mobile = any(device in user_agent for device in ['mobile', 'android', 'iphone', 'ipad', 'ipod'])

    # Force compact view on mobile devices
    if is_mobile:
        compact_view_mobile_start = time.perf_counter()
        compact_view = True
    
    # Get all statistics data
    stats = {}
    
    # Get timezone using our robust function
    tz_uptime_start = time.perf_counter()
    from metadata.metadata import _get_local_timezone
    local_tz = _get_local_timezone()
    stats['timezone'] = str(local_tz)
    stats['uptime'] = int(time.time() - app_start_time)
    
    # Get collection counts from the optimized summary function
    collection_start = time.perf_counter()
    from database.statistics import get_statistics_summary
    counts = get_statistics_summary()
    stats['total_movies'] = counts['total_movies']
    stats['total_shows'] = counts['total_shows']
    stats['total_episodes'] = counts['total_episodes']
    
    # Get active downloads and usage stats (skip in limited environment)
    downloads_start = time.perf_counter()
    if not limited_env:
        try:
            from database import get_cached_download_stats
            active_downloads, usage_stats = get_cached_download_stats()
            stats['active_downloads_data'] = active_downloads
            stats['usage_stats_data'] = usage_stats
        except Exception as e:
            logging.error(f"Error getting download stats: {str(e)}")
            stats['active_downloads_data'] = {
                'count': 0,
                'limit': 15,  # Default fallback limit
                'percentage': 0,
                'status': 'error'
            }
            stats['usage_stats_data'] = {
                'used': '0 GB',
                'limit': '2000 GB',
                'percentage': 0,
                'error': 'provider_error'
            }
    else:
        # In limited environment, provide empty stats
        stats['active_downloads_data'] = None
        stats['usage_stats_data'] = None
    
    # --- Read Cached Library Size for Initial Display ---
    library_cache_read_start = time.perf_counter()
    cached_size_data = _read_size_cache()
    if cached_size_data:
        stats['total_library_size'] = f"{cached_size_data['size_str']} (cached)"
    else:
        # Default if cache is missing, invalid, or expired
        stats['total_library_size'] = "Click Refresh" # Changed default text

    # Get recently aired and upcoming shows
    shows_start = time.perf_counter()
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    
    # Get upcoming releases
    releases_start = time.perf_counter()
    upcoming_releases = get_upcoming_releases()
    formatting_upcoming_releases_start = time.perf_counter()
    for release in upcoming_releases:
        release['formatted_date'] = format_date(release['release_date'])

    # Get recently added items and upgraded items
    recent_start = time.perf_counter()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Get recently added items
        get_recent_added_items_start = time.perf_counter()
        from database import get_recently_added_items
        recently_added_data = loop.run_until_complete(get_recently_added_items())
        
        recently_added_processing_start = time.perf_counter()
        recently_added = {
            'movies': [],
            'shows': []
        }
        
        # Process movies
        if 'movies' in recently_added_data:
            for movie in recently_added_data['movies']:
                movie['formatted_date'] = format_datetime_preference(
                    movie['collected_at'], 
                    use_24hour_format
                )
                movie['formatted_collected_at'] = movie['formatted_date']
                recently_added['movies'].append(movie)
        
        # Process shows
        if 'shows' in recently_added_data:
            for show in recently_added_data['shows']:
                show['formatted_date'] = format_datetime_preference(
                    show['collected_at'], 
                    use_24hour_format
                )
                show['formatted_collected_at'] = show['formatted_date']
                recently_added['shows'].append(show)
        
        # Get recently upgraded items
        get_recent_upgraded_start = time.perf_counter()
        upgrade_enabled = get_setting('Scraping', 'enable_upgrading', False)
        recently_upgraded_processing_start = time.perf_counter()
        if upgrade_enabled:
            from database import get_recently_upgraded_items
            recently_upgraded_async_start = time.perf_counter()
            recently_upgraded = loop.run_until_complete(get_recently_upgraded_items())
            for item in recently_upgraded:
                # Format the upgrade date using collected_at for better differentiation
                item['formatted_date'] = format_datetime_preference(
                    item['collected_at'], 
                    use_24hour_format
                )
                
                # For original_collected_at, use the existing value if available
                if item.get('original_collected_at'):
                    item['original_collected_at'] = format_datetime_preference(
                        item['original_collected_at'],
                        use_24hour_format
                    )
                else:
                    # If original_collected_at is not available, we don't need to create a fake one
                    # as the UI only needs to show when the upgrade happened
                    item['original_collected_at'] = 'Unknown'
        else:
            recently_upgraded = []
    
    finally:
        loop.close()
    
    # Check if TMDB API key is set
    api_key_check_start = time.perf_counter()
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    stats['tmdb_api_key_set'] = bool(tmdb_api_key)
    
    # Format dates for recently aired and airing soon
    date_formatting_start = time.perf_counter()
    for item in recently_aired:
        item['formatted_datetime'] = format_datetime_preference(
            item['air_datetime'], 
            use_24hour_format
        )
    
    for item in airing_soon:
        item['formatted_datetime'] = format_datetime_preference(
            item['air_datetime'], 
            use_24hour_format
        )
    
    # Log total time
    render_start_time = time.perf_counter()
    template_rendered = render_template('statistics.html',
                         stats=stats,
                         recently_aired=recently_aired,
                         airing_soon=airing_soon,
                         upcoming_releases=upcoming_releases,
                         recently_added=recently_added,
                         recently_upgraded=recently_upgraded,
                         use_24hour_format=use_24hour_format,
                         compact_view=compact_view,
                         limited_env=limited_env)
    logging.debug(f"Statistics page load: Total route processing took {(time.perf_counter() - overall_start_time)*1000:.2f}ms. END")

    return template_rendered

@root_bp.route('/set_time_preference', methods=['POST'])
@user_required
@onboarding_required
def set_time_preference():
    try:
        data = request.json
        use_24hour_format = data.get('use24HourFormat', True)
        
        # Save to settings
        set_setting('UI Settings', 'use_24hour_format', use_24hour_format)
        
        # Create new event loop for async operations
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Get recently added items
            from database import get_recently_added_items
            recently_added = loop.run_until_complete(get_recently_added_items(movie_limit=5, show_limit=5))
            
            # Format recently added items
            for item in recently_added.get('movies', []) + recently_added.get('shows', []):
                if 'collected_at' in item and item['collected_at'] is not None:
                    try:
                        # Try parsing with microseconds
                        collected_at = datetime.strptime(item['collected_at'], '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        try:
                            # Try parsing without microseconds
                            collected_at = datetime.strptime(item['collected_at'], '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            collected_at = None
                    
                    if collected_at:
                        item['formatted_date'] = format_datetime_preference(collected_at, use_24hour_format)
                        item['formatted_collected_at'] = format_datetime_preference(collected_at, use_24hour_format)
                    else:
                        item['formatted_date'] = 'Unknown'
                        item['formatted_collected_at'] = 'Unknown'
                else:
                    item['formatted_date'] = 'Unknown'
                    item['formatted_collected_at'] = 'Unknown'
            
            # Get recently aired and upcoming shows
            recently_aired, airing_soon = get_recently_aired_and_airing_soon()
            
            # Format times for recently aired and upcoming shows
            for item in recently_aired + airing_soon:
                if 'air_datetime' in item:
                    item['formatted_datetime'] = format_datetime_preference(item['air_datetime'], use_24hour_format)
            
            # Get and format upcoming releases
            upcoming_releases = get_upcoming_releases()
            for release in upcoming_releases:
                release['formatted_date'] = format_date(release['release_date'])
            
            # Get recently upgraded items
            upgrade_enabled = get_setting('Scraping', 'enable_upgrading', False)
            if upgrade_enabled:
                from database import get_recently_upgraded_items
                recently_upgraded = loop.run_until_complete(get_recently_upgraded_items())
                for item in recently_upgraded:
                    # Format the upgrade date using collected_at for better differentiation
                    item['formatted_date'] = format_datetime_preference(
                        item['collected_at'], 
                        use_24hour_format
                    )
                    
                    # For original_collected_at, use the existing value if available
                    if item.get('original_collected_at'):
                        item['original_collected_at'] = format_datetime_preference(
                            item['original_collected_at'],
                            use_24hour_format
                        )
                    else:
                        # If original_collected_at is not available, we don't need to create a fake one
                        # as the UI only needs to show when the upgrade happened
                        item['original_collected_at'] = 'Unknown'
            else:
                recently_upgraded = []
                
            return jsonify({
                'status': 'OK',
                'use24HourFormat': use_24hour_format,
                'recently_aired': recently_aired,
                'airing_soon': airing_soon,
                'upcoming_releases': upcoming_releases,
                'recently_upgraded': recently_upgraded,
                'recently_added': recently_added
            })
            
        finally:
            loop.close()
            
    except Exception as e:
        logging.error(f"Error in set_time_preference: {str(e)}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': 'An error occurred while updating time preferences'
        }), 500

@statistics_bp.route('/active_downloads', methods=['GET'])
@user_required
def active_downloads():
    """Get active downloads and limits for the debrid provider"""
    try:
        from database import get_cached_download_stats
        active_downloads, _ = get_cached_download_stats()
        if active_downloads['error'] == 'too_many':
            return jsonify(active_downloads), 429  # Too Many Requests
        elif active_downloads['error'] == 'provider_unavailable':
            return jsonify(active_downloads), 503  # Service Unavailable
        elif active_downloads['error']:
            return jsonify(active_downloads), 500  # Internal Server Error
        return jsonify(active_downloads)
    except Exception as e:
        logging.error(f"Error getting active downloads: {str(e)}")
        return jsonify({
            'error': 'failed',
            'message': str(e)
        }), 500

@statistics_bp.route('/api/active_downloads', methods=['GET'])
@user_required
def active_downloads_api():
    try:
        from database import get_cached_download_stats
        active_downloads, _ = get_cached_download_stats()
        return jsonify({'active': active_downloads})
    except Exception as e:
        logging.error(f"Error getting active downloads: {str(e)}")
        return jsonify({
            'active': {
                'count': 0,
                'limit': 0,
                'percentage': 0,
                'status': 'error'
            }
        })

@statistics_bp.route('/recently_added')
@user_required
@onboarding_required
def recently_added():
    cookie_value = request.cookies.get('use24HourFormat')
    use_24hour_format = cookie_value == 'true' if cookie_value is not None else True

    recently_added_start = time.time()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    from database import get_recently_added_items
    recently_added = loop.run_until_complete(get_recently_added_items(movie_limit=5, show_limit=5))
    recently_added_end = time.time()
    logging.debug(f"Time for get_recently_added_items: {recently_added_end - recently_added_start:.2f} seconds")

    # Format times for recently added items
    for item in recently_added['movies'] + recently_added['shows']:
        if 'collected_at' in item and item['collected_at'] is not None:
            try:
                # Try parsing with microseconds
                collected_at = datetime.strptime(item['collected_at'], '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                try:
                    # Try parsing without microseconds
                    collected_at = datetime.strptime(item['collected_at'], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    item['formatted_collected_at'] = 'Unknown'
                    continue
            item['formatted_collected_at'] = format_datetime_preference(collected_at, use_24hour_format)
        else:
            item['formatted_collected_at'] = 'Unknown'

    return jsonify({'recently_added': recently_added})

async def get_recent_from_plex(movie_limit=5, show_limit=5):
    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    
    if not plex_url or not plex_token:
        return {'movies': [], 'shows': []}
    
    headers = {
        'X-Plex-Token': plex_token,
        'Accept': 'application/json'
    }

    async def fetch_metadata(session, item_key):
        metadata_url = f"{plex_url}{item_key}?includeGuids=1"
        async with session.get(metadata_url, headers=headers) as response:
            return await response.json()

    async with aiohttp.ClientSession() as session:
        # Get library sections
        async with session.get(f"{plex_url}/library/sections", headers=headers) as response:
            sections = await response.json()

        recent_movies = []
        recent_shows = {}

        for section in sections['MediaContainer']['Directory']:
            if section['type'] == 'movie':
                async with session.get(f"{plex_url}/library/sections/{section['key']}/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size={movie_limit}", headers=headers) as response:
                    data = await response.json()
                    for item in data['MediaContainer'].get('Metadata', []):
                        metadata = await fetch_metadata(session, item['key'])
                        if 'MediaContainer' in metadata and 'Metadata' in metadata['MediaContainer']:
                            full_metadata = metadata['MediaContainer']['Metadata'][0]
                            tmdb_id = next((guid['id'] for guid in full_metadata.get('Guid', []) if guid['id'].startswith('tmdb://')), None)
                            if tmdb_id:
                                tmdb_id = tmdb_id.split('://')[1]
                                from metadata.metadata import get_poster_url
                                poster_url = await get_poster_url(session, tmdb_id, 'movie')
                                added_at = datetime.fromtimestamp(int(item['addedAt']))
                                recent_movies.append({
                                    'title': item['title'],
                                    'year': item.get('year'),
                                    'added_at': added_at,
                                    'poster_url': poster_url
                                })
            elif section['type'] == 'show':
                async with session.get(f"{plex_url}/library/sections/{section['key']}/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=100", headers=headers) as response:
                    data = await response.json()
                    for item in data['MediaContainer'].get('Metadata', []):
                        if item['type'] == 'episode' and len(recent_shows) < show_limit:
                            show_title = item['grandparentTitle']
                            if show_title not in recent_shows:
                                show_metadata = await fetch_metadata(session, item['grandparentKey'])
                                if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                    full_show_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                    tmdb_id = next((guid['id'] for guid in full_show_metadata.get('Guid', []) if guid['id'].startswith('tmdb://')), None)
                                    if tmdb_id:
                                        tmdb_id = tmdb_id.split('://')[1]
                                        from metadata.metadata import get_poster_url
                                        poster_url = await get_poster_url(session, tmdb_id, 'tv')
                                        added_at = datetime.fromtimestamp(int(item['addedAt']))
                                        recent_shows[show_title] = {
                                            'title': show_title,
                                            'added_at': added_at,
                                            'poster_url': poster_url,
                                            'seasons': set()
                                        }
                            if show_title in recent_shows:
                                recent_shows[show_title]['seasons'].add(item['parentIndex'])
                                recent_shows[show_title]['added_at'] = max(
                                    recent_shows[show_title]['added_at'],
                                    datetime.fromtimestamp(int(item['addedAt']))
                                )
                            if len(recent_shows) == show_limit:
                                break

        recent_shows = list(recent_shows.values())
        for show in recent_shows:
            show['seasons'] = sorted(show['seasons'])
        recent_shows.sort(key=lambda x: x['added_at'], reverse=True)

    return {
        'movies': recent_movies[:movie_limit],
        'shows': recent_shows[:show_limit]
    }

def format_date(date_string):
    if not date_string:
        return ''
    try:
        date = datetime.fromisoformat(date_string)
        return date.strftime('%Y-%m-%d')
    except ValueError:
        return date_string

def format_time(date_input):
    if not date_input:
        return ''
    try:
        if isinstance(date_input, str):
            date = datetime.fromisoformat(date_input.rstrip('Z'))  # Remove 'Z' if present
        elif isinstance(date_input, datetime):
            date = date_input
        else:
            return ''
        return date.strftime('%H:%M:%S')
    except ValueError:
        return ''
    
def format_datetime_preference(date_input, use_24hour_format):
    if not date_input:
        return ''
    try:
        # Get timezone using our robust function
        from metadata.metadata import _get_local_timezone
        local_tz = _get_local_timezone()
        
        # Convert string to datetime if necessary
        if isinstance(date_input, str):
            try:
                # Try parsing with microseconds
                date_input = datetime.strptime(date_input, '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                try:
                    # Try parsing without microseconds
                    date_input = datetime.strptime(date_input, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    try:
                        # Try parsing ISO format
                        date_input = datetime.fromisoformat(date_input.rstrip('Z'))
                    except ValueError:
                        return str(date_input)  # Return original string if all parsing fails
        
        # Ensure the datetime is timezone-aware
        if date_input.tzinfo is None:
            date_input = local_tz.localize(date_input) if hasattr(local_tz, 'localize') else date_input.replace(tzinfo=local_tz)
        
        now = datetime.now(local_tz)
        today = now.date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        if date_input.date() == today:
            day_str = "Today"
        elif date_input.date() == yesterday:
            day_str = "Yesterday"
        elif date_input.date() == tomorrow:
            day_str = "Tomorrow"
        else:
            day_str = date_input.strftime("%a, %d %b %Y")

        time_format = "%H:%M" if use_24hour_format else "%I:%M %p"
        formatted_time = date_input.strftime(time_format)
        
        # Remove leading zero from hour in 12-hour format
        if not use_24hour_format:
            formatted_time = formatted_time.lstrip("0")
        
        return f"{day_str} {formatted_time}"
    except Exception as e:
        logging.error(f"Error formatting datetime: {str(e)}")
        return str(date_input)  # Return original string if any error occurs

def format_bytes(bytes_value, decimals=2):
    """Format bytes to human readable string"""
    if bytes_value == 0:
        return "0 B"
    
    k = 1024
    dm = decimals
    sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB']
    
    i = int(math.log(bytes_value, k))
    return f"{round(bytes_value / (k ** i), dm)} {sizes[i]}"

@statistics_bp.route('/usage_stats', methods=['GET'])
@user_required
def usage_stats():
    """Get daily usage statistics from the debrid provider"""
    try:
        from database import get_cached_download_stats
        _, usage_stats = get_cached_download_stats()
        return jsonify({'daily': usage_stats})
    except Exception as e:
        logging.error(f"Error getting usage stats: {str(e)}")
        return jsonify({
            'daily': {
                'used': '0 GB',
                'limit': '2000 GB',
                'percentage': 0,
                'error': 'failed'
            }
        })

@statistics_bp.route('/api/index')
@user_required
def index_api():
    """API endpoint that returns the same data as the index route but in JSON format"""
    stats = {}
    
    # Check if running in limited environment
    from utilities.set_supervisor_env import is_limited_environment
    limited_env = is_limited_environment()
    
    # Get timezone using our robust function
    from metadata.metadata import _get_local_timezone
    local_tz = _get_local_timezone()
    stats['timezone'] = str(local_tz)
    stats['uptime'] = int(time.time() - app_start_time)
    
    # Get program status
    stats['program_status'] = get_program_status()
    
    # Get collection counts
    from database.statistics import get_statistics_summary
    counts = get_statistics_summary()
    stats['total_movies'] = counts['total_movies']
    stats['total_shows'] = counts['total_shows']
    stats['total_episodes'] = counts['total_episodes']
    
    # Get active downloads and usage stats (skip in limited environment)
    if not limited_env:
        active_downloads = get_cached_active_downloads()
        usage_stats = get_cached_user_traffic()
        stats['active_downloads_data'] = active_downloads
        stats['usage_stats_data'] = usage_stats
    else:
        stats['active_downloads_data'] = None
        stats['usage_stats_data'] = None
    
    # Get recently aired and airing soon
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    
    # Get upcoming releases
    upcoming_releases = get_upcoming_releases()
    for release in upcoming_releases:
        release['formatted_date'] = format_date(release['release_date'])
    
    # Get recently added and upgraded items
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Get recently added items
        from database import get_recently_added_items

        recently_added_data = loop.run_until_complete(get_recently_added_items())
        recently_added = {
            'movies': [],
            'shows': []
        }
        
        use_24hour_format = get_setting('UI Settings', 'use_24hour_format', True)
        
        # Process movies
        if 'movies' in recently_added_data:
            for movie in recently_added_data['movies']:
                movie['formatted_date'] = format_datetime_preference(
                    movie['collected_at'],
                    use_24hour_format
                )
                recently_added['movies'].append(movie)
        
        # Process shows
        if 'shows' in recently_added_data:
            for show in recently_added_data['shows']:
                show['formatted_date'] = format_datetime_preference(
                    show['collected_at'],
                    use_24hour_format
                )
                recently_added['shows'].append(show)
        
        # Get recently upgraded items
        upgrade_enabled = get_setting('Scraping', 'enable_upgrading', False)
        if upgrade_enabled:
            from database import get_recently_upgraded_items
            recently_upgraded = loop.run_until_complete(get_recently_upgraded_items())
            for item in recently_upgraded:
                # Format the upgrade date using collected_at for better differentiation
                item['formatted_date'] = format_datetime_preference(
                    item['collected_at'], 
                    use_24hour_format
                )
                
                # For original_collected_at, use the existing value if available
                if item.get('original_collected_at'):
                    item['original_collected_at'] = format_datetime_preference(
                        item['original_collected_at'],
                        use_24hour_format
                    )
                else:
                    # If original_collected_at is not available, we don't need to create a fake one
                    # as the UI only needs to show when the upgrade happened
                    item['original_collected_at'] = 'Unknown'
        else:
            recently_upgraded = []
    
    finally:
        loop.close()
    
    # Check if TMDB API key is set
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    stats['tmdb_api_key_set'] = bool(tmdb_api_key)
    
    # Format dates for recently aired and airing soon
    for item in recently_aired:
        item['formatted_datetime'] = format_datetime_preference(
            item['air_datetime'],
            use_24hour_format
        )
    
    for item in airing_soon:
        item['formatted_datetime'] = format_datetime_preference(
            item['air_datetime'],
            use_24hour_format
        )
    
    return jsonify({
        'stats': stats,
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases,
        'recently_added': recently_added,
        'recently_upgraded': recently_upgraded,
        'use_24hour_format': use_24hour_format
    })

@statistics_bp.route('/move_to_wanted', methods=['POST'])
@user_required
def move_to_wanted():
    """Move an item back to Wanted state and disable not wanted checks"""
    try:
        data = request.json
        imdb_id = data.get('imdb_id')
        tmdb_id = data.get('tmdb_id')
        season_number = data.get('season_number')
        episode_number = data.get('episode_number')
        
        if not (imdb_id or tmdb_id):
            return jsonify({'success': False, 'error': 'IMDb ID or TMDB ID is required'}), 400
            
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build query based on item type
        if season_number is not None and episode_number is not None:
            # Episode
            query = """
                UPDATE media_items 
                SET state = 'Wanted',
                    filled_by_file = NULL,
                    filled_by_title = NULL,
                    filled_by_magnet = NULL,
                    filled_by_torrent_id = NULL,
                    collected_at = NULL,
                    last_updated = ?,
                    disable_not_wanted_check = TRUE,
                    location_on_disk = NULL,
                    original_path_for_symlink = NULL,
                    original_scraped_torrent_title = NULL,
                    upgrading_from = NULL,
                    version = TRIM(version, '*'),
                    upgrading = NULL
                WHERE (imdb_id = ? OR tmdb_id = ?)
                AND season_number = ? 
                AND episode_number = ?
                AND state IN ('Collected', 'Upgrading')
            """
            params = (datetime.now(), imdb_id, tmdb_id, season_number, episode_number)
        else:
            # Movie
            query = """
                UPDATE media_items 
                SET state = 'Wanted',
                    filled_by_file = NULL,
                    filled_by_title = NULL,
                    filled_by_magnet = NULL,
                    filled_by_torrent_id = NULL,
                    collected_at = NULL,
                    last_updated = ?,
                    disable_not_wanted_check = TRUE,
                    location_on_disk = NULL,
                    original_path_for_symlink = NULL,
                    original_scraped_torrent_title = NULL,
                    upgrading_from = NULL,
                    version = TRIM(version, '*'),
                    upgrading = NULL
                WHERE (imdb_id = ? OR tmdb_id = ?)
                AND type = 'movie'
                AND state IN ('Collected', 'Upgrading')
            """
            params = (datetime.now(), imdb_id, tmdb_id)
            
        cursor.execute(query, params)
        conn.commit()
        
        if cursor.rowcount > 0:
            return jsonify({'success': True}), 200
        else:
            return jsonify({'success': False, 'error': 'No matching items found or items not in Collected/Upgrading state'}), 404
            
    except Exception as e:
        logging.error(f"Error moving item to Wanted state: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# Define the path for the size cache file
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
SIZE_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'library_size_cache.json')
CACHE_EXPIRY_HOURS = 72 # How long to consider the cached value valid (e.g., 3 days)

# Helper function to read the cache
def _read_size_cache() -> Optional[Dict]:
    try:
        if os.path.exists(SIZE_CACHE_FILE):
            with open(SIZE_CACHE_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict) and 'size_str' in data and 'timestamp' in data:
                     try:
                        cached_time = datetime.fromisoformat(data['timestamp'])
                        if datetime.utcnow() - cached_time < timedelta(hours=CACHE_EXPIRY_HOURS):
                            # logging.debug("Read valid library size cache for initial page load.")
                            return data
                        else:
                            # logging.info("Cached library size expired for initial page load.")
                            return None
                     except ValueError:
                         # logging.warning("Invalid timestamp format in cache file.")
                         return None
                else:
                    # logging.warning("Invalid data format in cache file.")
                    return None
        else:
            # logging.debug("Library size cache file not found for initial load.")
            return None
    except Exception as e:
        logging.error(f"Failed to read library size cache for initial load: {e}")
        return None

@statistics_bp.route('/api/library_size', methods=['GET'])
@user_required
def get_library_size_api():
    """API endpoint to calculate and return the total library size, using cache on failure."""
    start_time = time.perf_counter()
    size_str = "N/A" # Default/initial state
    is_cached_value = False
    calculation_error = None # Store the specific error if calculation fails

    try:
        provider = get_debrid_provider()
        # Gate behavior using capability flags rather than concrete type
        if hasattr(provider, 'get_total_library_size'):
            logging.info("Fetching library size via API request...")
            try:
                # Run the async function which now writes cache on success
                calculated_size = asyncio.run(provider.get_total_library_size())

                # Check if the calculation resulted in an error state
                if calculated_size is None or calculated_size.startswith("Error"):
                     calculation_error = calculated_size if calculated_size else "Error (Unknown)"
                     logging.warning(f"Library size calculation failed ({calculation_error}). Attempting to read from cache.")
                     # Fall through to read cache below
                else:
                    # Calculation was successful
                    size_str = calculated_size

            except RuntimeError as e:
                 logging.error(f"Asyncio runtime error calculating library size: {e}", exc_info=True)
                 calculation_error = "Error (Async)"
                 # Fall through to read cache below

            # --- Attempt to read cache ONLY if calculation failed ---
            if calculation_error:
                cached_data = _read_size_cache()
                if cached_data:
                    size_str = f"{cached_data['size_str']} (cached)"
                    is_cached_value = True
                else:
                    # If cache is unavailable/invalid/expired, return the original calculation error
                    size_str = calculation_error

            log_prefix = "(Cached) " if is_cached_value else ""
            logging.info(f"{log_prefix}Library size retrieval took {(time.perf_counter() - start_time)*1000:.2f}ms. Result: {size_str}")

        else:
            size_str = "N/A (Not RD)"
            logging.info("Library size requested, but provider is not RealDebrid.")

    except ProviderUnavailableError:
        logging.warning("Debrid provider unavailable when calculating library size.")
        calculation_error = "Error (Provider)"
        # Try reading cache
        cached_data = _read_size_cache()
        if cached_data:
            size_str = f"{cached_data['size_str']} (cached)"
            is_cached_value = True
        else:
            size_str = calculation_error # Return provider error if cache fails
    except Exception as e:
        logging.error(f"Error calculating library size via API: {e}", exc_info=True)
        calculation_error = "Error (Server)"
         # Try reading cache
        cached_data = _read_size_cache()
        if cached_data:
            size_str = f"{cached_data['size_str']} (cached)"
            is_cached_value = True
        else:
             size_str = calculation_error # Return server error if cache fails

    return jsonify({'total_library_size': size_str})

@statistics_bp.route('/calendar')
@user_required
@onboarding_required
def calendar_view():
    events: List[Dict[str, Any]] = []
    use_24hour_format = get_setting('UI Settings', 'use_24hour_format', True)
    compact_view = get_setting('UI Settings', 'compact_view', False)

    from metadata.metadata import _get_local_timezone
    local_tz = _get_local_timezone()
    now_aware = datetime.now(local_tz) # Timezone-aware current time
    today_date = now_aware.date()
    yesterday_date = today_date - timedelta(days=1) 
    tomorrow_date = today_date + timedelta(days=1) # Calculate tomorrow_date

    # Define the 3-week view window for data pulling and grid display
    # Monday of the previous week
    view_start_date = today_date - timedelta(days=today_date.weekday() + 7)
    # Sunday of the following week (exactly 21 days, so 20 days after start)
    view_end_date = view_start_date + timedelta(days=20)

    calendar_pull_start_date_iso = view_start_date.isoformat()
    calendar_pull_end_date_iso = view_end_date.isoformat()

    # 1. Get TV Show Data using the 3-week range
    recently_aired, airing_soon = get_recently_aired_and_airing_soon(
        start_date_override_iso=calendar_pull_start_date_iso,
        end_date_override_iso=calendar_pull_end_date_iso
    )

    for item in recently_aired + airing_soon:
        event_date = item['air_datetime'].date()
        time_str = item['air_datetime'].strftime("%H:%M" if use_24hour_format else "%I:%M %p")
        if not use_24hour_format and time_str.startswith("0"):
            time_str = time_str[1:]

        events.append({
            'date': event_date,
            'time': time_str,
            'title': item['title'],
            'type': 'tv_show',
            'display_status': item.get('display_status', 'uncollected').lower().replace(' ', '_'),
            'imdb_id': item.get('imdb_id'),
            'tmdb_id': item.get('tmdb_id'),
            'sort_datetime': item['air_datetime']
        })

    # 2. Get Movies using the 3-week range
    calendar_movies = get_movies_for_calendar(
        start_date_override_iso=calendar_pull_start_date_iso,
        end_date_override_iso=calendar_pull_end_date_iso
    )

    for movie in calendar_movies:
        try:
            release_date_str = movie['release_date']
            if not isinstance(release_date_str, str):
                release_date_str = release_date_str.isoformat()

            release_date_obj = datetime.strptime(release_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError) as e:
            logging.error(f"Could not parse release_date '{movie['release_date']}' for movie '{movie['title']}': {e}")
            continue

        naive_movie_datetime = datetime.combine(release_date_obj, datetime.min.time())
        aware_movie_datetime = local_tz.localize(naive_movie_datetime) if hasattr(local_tz, 'localize') and naive_movie_datetime.tzinfo is None else naive_movie_datetime.replace(tzinfo=local_tz)

        movie_state = movie.get('state', 'Unknown').lower()
        display_status = movie_state

        if release_date_obj < today_date:
            if movie_state in ['wanted', 'searching', 'unknown', 'not_collected']:
                display_status = 'premiered_uncollected'
            elif movie_state == 'collected':
                display_status = 'collected'
            elif movie_state == 'upgrading':
                display_status = 'upgrading'
            elif movie_state == 'checking':
                 display_status = 'checking_upgrade'
        elif release_date_obj >= today_date:
            if movie_state in ['wanted', 'searching', 'unknown', 'not_collected']:
                 display_status = 'upcoming_uncollected'
            elif movie_state == 'collected':
                 display_status = 'upcoming_collected'
            else:
                 display_status = 'upcoming'

        events.append({
            'date': release_date_obj,
            'time': None,
            'title': movie['title'],
            'type': 'movie',
            'display_status': display_status.replace(' ', '_'),
            'imdb_id': movie.get('imdb_id'),
            'tmdb_id': movie.get('tmdb_id'),
            'sort_datetime': aware_movie_datetime
        })

    # 3. Sort all events
    events.sort(key=lambda x: x['sort_datetime'])

    # --- Generate `three_week_grid_days` data structure ---
    three_week_grid_days = []
    current_day_for_grid = view_start_date
    for _ in range(3):  # Iterate for 3 weeks
        week_days = []
        for _ in range(7):  # Iterate for 7 days in a week
            week_days.append(current_day_for_grid)
            current_day_for_grid += timedelta(days=1)
        three_week_grid_days.append(week_days)
    
    # Create a header string for the 3-week grid view
    grid_header_start_str = view_start_date.strftime("%b %d, %Y")
    grid_header_end_str = view_end_date.strftime("%b %d, %Y")
    three_week_grid_header = f"Schedule: {grid_header_start_str} - {grid_header_end_str}"

    # 4. Group events by date (for both grid and timeline)
    grouped_events: Dict[str, Dict[str, Any]] = {} # Corrected type hint
    for event in events:
        date_str = event['date'].isoformat()
        if date_str not in grouped_events:
            day_name = event['date'].strftime("%A")
            month_day_str = event['date'].strftime("%B %d") # For display_str
            year_str = event['date'].strftime("%Y")
            
            # Create display_str for timeline headers
            display_date_str_for_timeline = ""
            if event['date'] == today_date:
                display_date_str_for_timeline = f"Today, {month_day_str}"
            elif event['date'] == (today_date + timedelta(days=1)):
                display_date_str_for_timeline = f"Tomorrow, {month_day_str}"
            elif event['date'] == (today_date - timedelta(days=1)):
                display_date_str_for_timeline = "Yesterday"
            else:
                display_date_str_for_timeline = f"{day_name}, {month_day_str}, {year_str}"

            grouped_events[date_str] = {'display_str_timeline': display_date_str_for_timeline, 'items': []}
        
        grouped_events[date_str]['items'].append(event)
        
    # sorted_dates_for_timeline is primarily for ordering the timeline section if needed
    # The grid will iterate through month_days_for_grid and access grouped_events by date_iso_str
    sorted_dates_for_timeline = sorted(grouped_events.keys())

    return render_template('calendar_view.html',
                           # Data for the new 3-Week Grid
                           three_week_grid_days=three_week_grid_days,
                           three_week_grid_header=three_week_grid_header,
                           # view_start_date and view_end_date are no longer needed for timeline filter in template
                           # but might be useful if other parts of the template expect them.
                           # For this specific request, the timeline filter will use today_date and yesterday_date.
                           
                           today_date=today_date, 
                           yesterday_date=yesterday_date, 
                           tomorrow_date=tomorrow_date, # Pass tomorrow_date to template
                           
                           # Data for Timeline (and for populating grid cells)
                           grouped_events=grouped_events,
                           sorted_dates_for_timeline=sorted_dates_for_timeline,
                           
                           timedelta=timedelta, 
                           
                           use_24hour_format=use_24hour_format,
                           compact_view=compact_view)