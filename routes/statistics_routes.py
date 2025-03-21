from flask import Blueprint, jsonify, request, render_template, session, make_response
from datetime import datetime, timedelta
from database import get_db_connection
from operator import itemgetter
from itertools import groupby
import time
import logging
import asyncio
import aiohttp
import os
from settings import get_setting, set_setting
from .models import user_required, onboarding_required 
from extensions import app_start_time
from database import get_recently_added_items, get_poster_url, get_collected_counts, get_recently_upgraded_items
from debrid import get_debrid_provider, TooManyDownloadsError, ProviderUnavailableError
from metadata.metadata import get_show_airtime_by_imdb_id, _get_local_timezone
from database.statistics import get_cached_download_stats
from .program_operation_routes import program_is_running, program_is_initializing
import json
import math
from functools import wraps

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
    active_downloads, _ = get_cached_download_stats()
    return active_downloads

def get_cached_user_traffic():
    """Get user traffic with caching"""
    _, usage_stats = get_cached_download_stats()
    return usage_stats

statistics_bp = Blueprint('statistics', __name__)
root_bp = Blueprint('root', __name__)

def get_airing_soon():
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

def get_upcoming_releases():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    today = datetime.now().date()
    next_week = today + timedelta(days=28)
    
    # First get all upcoming releases
    query = """
    SELECT DISTINCT m.title, m.release_date, m.tmdb_id, m.imdb_id
    FROM media_items m
    WHERE m.type = 'movie' AND m.release_date BETWEEN ? AND ?
    ORDER BY m.release_date ASC
    """
    
    cursor.execute(query, (today.isoformat(), next_week.isoformat()))
    results = cursor.fetchall()
    
    # Get existing movies in our collection
    existing_query = """
    SELECT DISTINCT tmdb_id 
    FROM media_items 
    WHERE type = 'movie' AND state IN ('Collected', 'Upgrading', 'Checking')
    """
    cursor.execute(existing_query)
    existing_tmdb_ids = {row[0] for row in cursor.fetchall()}
    
    conn.close()
    
    # Group by release date, excluding existing movies
    grouped_results = {}
    for title, release_date, tmdb_id, imdb_id in results:
        if tmdb_id not in existing_tmdb_ids:  # Only include if not in our collection
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

def get_recently_aired_and_airing_soon():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get timezone using our robust function
    local_tz = _get_local_timezone()
    now = datetime.now(local_tz)
    two_days_ago = now - timedelta(days=2)
    
    query = """
    WITH CollectedEpisodes AS (
        -- First get all episodes that are collected/upgrading
        SELECT DISTINCT 
            title,
            season_number,
            episode_number
        FROM media_items
        WHERE type = 'episode'
        AND state IN ('Collected', 'Upgrading')
    ),
    RankedEpisodes AS (
        -- Then get our main episode data with collection status
        SELECT 
            m.title,
            m.season_number,
            m.episode_number,
            m.release_date,
            m.airtime,
            m.imdb_id,
            m.tmdb_id,
            CASE WHEN c.title IS NOT NULL THEN 1 ELSE 0 END as is_collected,
            ROW_NUMBER() OVER (PARTITION BY m.title, m.season_number, m.episode_number ORDER BY m.release_date DESC, m.airtime DESC) as rn
        FROM media_items m
        LEFT JOIN CollectedEpisodes c ON 
            c.title = m.title 
            AND c.season_number = m.season_number 
            AND c.episode_number = m.episode_number
        WHERE m.type = 'episode' 
        AND m.release_date >= ? 
        AND m.release_date <= ?
    )
    SELECT DISTINCT 
        title,
        season_number,
        episode_number,
        release_date,
        airtime,
        imdb_id,
        tmdb_id,
        is_collected
    FROM RankedEpisodes
    WHERE rn = 1
    ORDER BY release_date, airtime, title, season_number, episode_number
    """
    
    cursor.execute(query, (two_days_ago.date().isoformat(), (now + timedelta(days=1)).date().isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    recently_aired = []
    airing_soon = []
    
    # Group episodes by show, season, and release date
    shows = {}
    
    for result in results:
        title, season, episode, release_date, airtime, imdb_id, tmdb_id, is_collected = result
        try:
            release_date = datetime.fromisoformat(release_date)
            
            if airtime is None or airtime == '':
                airtime = get_show_airtime_by_imdb_id(imdb_id)
            
            try:
                airtime = datetime.strptime(airtime, '%H:%M').time()
            except ValueError:
                logging.warning(f"Invalid airtime format for {title}: {airtime}. Using default.")
                airtime = datetime.strptime("19:00", '%H:%M').time()
            
            # Create datetime in local timezone
            air_datetime = datetime.combine(release_date.date(), airtime)
            air_datetime = air_datetime.replace(tzinfo=local_tz)
            
            # Create a key for grouping
            show_key = f"{title}_{season}_{release_date.date()}"
            
            if show_key not in shows:
                shows[show_key] = {
                    'title': title,
                    'season': season,
                    'episodes': set(),
                    'air_datetime': air_datetime,
                    'release_date': release_date.date(),
                    'is_collected': bool(is_collected),
                    'imdb_id': imdb_id,
                    'tmdb_id': tmdb_id
                }
            
            shows[show_key]['episodes'].add(episode)
        
        except ValueError as e:
            logging.error(f"Error parsing date/time for {title}: {e}")
            continue
    
    # Process grouped shows
    for show in shows.values():
        episodes = sorted(list(show['episodes']))
        
        # Find consecutive ranges
        ranges = []
        range_start = episodes[0]
        prev = episodes[0]
        
        for curr in episodes[1:]:
            if curr != prev + 1:
                ranges.append((range_start, prev))
                range_start = curr
            prev = curr
        ranges.append((range_start, prev))
        
        # Format episode ranges
        episode_parts = []
        for start, end in ranges:
            if start is None or end is None:
                continue
            if start == end:
                episode_parts.append(f"E{start:02d}")
            else:
                episode_parts.append(f"E{start:02d}-{end:02d}")
        episode_range = ", ".join(episode_parts)
        
        formatted_item = {
            'title': f"{show['title']} S{show['season']:02d}{episode_range}",
            'air_datetime': show['air_datetime'],
            'sort_key': show['air_datetime'].isoformat(),
            'formatted_datetime': format_datetime_preference(show['air_datetime'], session.get('use_24hour_format', False)),
            'is_collected': show['is_collected'],
            'imdb_id': show['imdb_id'],
            'tmdb_id': show['tmdb_id'],
            'season_number': show['season'],
            'episode_number': episodes[0]  # Use first episode number for single episodes or ranges
        }
        
        if show['air_datetime'] <= now:
            recently_aired.append(formatted_item)
        else:
            airing_soon.append(formatted_item)
    
    return recently_aired, airing_soon

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
    start_time = time.perf_counter()
    
    # Get view preferences from settings
    use_24hour_format = get_setting('UI Settings', 'use_24hour_format', True)
    compact_view = get_setting('UI Settings', 'compact_view', False)
    
    # Check if user is on mobile using User-Agent
    user_agent = request.headers.get('User-Agent', '').lower()
    is_mobile = any(device in user_agent for device in ['mobile', 'android', 'iphone', 'ipad', 'ipod'])
    
    # Force compact view on mobile devices
    if is_mobile:
        compact_view = True
    else:
        # Handle compact toggle only for desktop
        toggle_compact = request.args.get('toggle_compact')
        if toggle_compact is not None:
            # Convert string value to boolean
            new_compact_view = toggle_compact.lower() == 'true'
            set_setting('UI Settings', 'compact_view', new_compact_view)
            compact_view = new_compact_view
            if request.headers.get('Accept') == 'application/json':
                return jsonify({'success': True, 'compact_view': compact_view})
    
    # Get all statistics data
    stats = {}
    
    # Get timezone using our robust function
    local_tz = _get_local_timezone()
    stats['timezone'] = str(local_tz)
    stats['uptime'] = int(time.time() - app_start_time)
    
    # Get collection counts
    collection_start = time.perf_counter()
    counts = get_collected_counts()
    stats['total_movies'] = counts['total_movies']
    stats['total_shows'] = counts['total_shows']
    stats['total_episodes'] = counts['total_episodes']
    logging.info(f"Collection counts took {(time.perf_counter() - collection_start)*1000:.2f}ms")
    
    # Get active downloads and usage stats
    downloads_start = time.perf_counter()
    try:
        active_downloads, usage_stats = get_cached_download_stats()
        stats['active_downloads_data'] = active_downloads
        stats['usage_stats_data'] = usage_stats
    except Exception as e:
        logging.error(f"Error getting download stats: {str(e)}")
        stats['active_downloads_data'] = {
            'count': 0,
            'limit': round(provider.MAX_DOWNLOADS * 0.75),
            'percentage': 0,
            'status': 'error'
        }
        stats['usage_stats_data'] = {
            'used': '0 GB',
            'limit': '2000 GB',
            'percentage': 0,
            'error': 'provider_error'
        }
    logging.info(f"Download stats check took {(time.perf_counter() - downloads_start)*1000:.2f}ms")
    
    # Get recently aired and upcoming shows
    shows_start = time.perf_counter()
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    logging.info(f"Recently aired and upcoming shows took {(time.perf_counter() - shows_start)*1000:.2f}ms")
    
    # Format dates and times according to preferences
    formatting_start = time.perf_counter()
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
    logging.info(f"Date/time formatting took {(time.perf_counter() - formatting_start)*1000:.2f}ms")
    
    # Get upcoming releases
    releases_start = time.perf_counter()
    upcoming_releases = get_upcoming_releases()
    for release in upcoming_releases:
        release['formatted_date'] = format_date(release['release_date'])
    logging.info(f"Upcoming releases took {(time.perf_counter() - releases_start)*1000:.2f}ms")
    
    # Get recently added items and upgraded items
    recent_start = time.perf_counter()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Get recently added items
        recently_added_data = loop.run_until_complete(get_recently_added_items())
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
                recently_added['shows'].append(show)
        
        # Get recently upgraded items
        upgrade_enabled = get_setting('Scraping', 'enable_upgrading', False)
        if upgrade_enabled:
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
    logging.info(f"Recently added and upgraded items took {(time.perf_counter() - recent_start)*1000:.2f}ms")
    
    # Check if TMDB API key is set
    api_start = time.time()
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    stats['tmdb_api_key_set'] = bool(tmdb_api_key)
    
    # Get usage stats data
    usage_start = time.perf_counter()
    try:
        usage = get_cached_user_traffic()
        
        if not usage or usage.get('limit') is None:
            stats['usage_stats_data'] = {
                'used': '0 GB',
                'limit': '2000 GB',
                'percentage': 0,
                'error': None
            }
        else:
            try:
                # Strip 'GB' from values and convert to float
                used_gb = float(usage.get('used', '0 GB').replace(' GB', ''))
                limit_gb = float(usage.get('limit', '2000 GB').replace(' GB', ''))
                
                # Convert GB to bytes for calculation
                daily_used = used_gb * 1024 * 1024 * 1024  # GB to bytes
                daily_limit = limit_gb * 1024 * 1024 * 1024  # GB to bytes
                
                percentage = round((daily_used / daily_limit) * 100) if daily_limit > 0 else 0
                
                stats['usage_stats_data'] = {
                    'used': format_bytes(daily_used),
                    'limit': format_bytes(daily_limit),
                    'percentage': percentage,
                    'error': None
                }
            except (TypeError, ValueError) as e:
                logging.error(f"Error converting usage values: {e}")
                logging.error(f"Raw usage data that caused error: {usage}")
                stats['usage_stats_data'] = {
                    'used': '0 GB',
                    'limit': '2000 GB',
                    'percentage': 0,
                    'error': 'conversion_error'
                }
    except Exception as e:
        logging.error(f"Error getting usage stats: {str(e)}")
        stats['usage_stats_data'] = {
            'used': '0 GB',
            'limit': '2000 GB',
            'percentage': 0,
            'error': 'provider_error'
        }
    logging.info(f"Usage stats took {(time.perf_counter() - usage_start)*1000:.2f}ms")

    # Log total time
    logging.info(f"Total route processing took {(time.perf_counter() - start_time)*1000:.2f}ms")

    return render_template('statistics.html',
                         stats=stats,
                         recently_aired=recently_aired,
                         airing_soon=airing_soon,
                         upcoming_releases=upcoming_releases,
                         recently_added=recently_added,
                         recently_upgraded=recently_upgraded,
                         use_24hour_format=use_24hour_format,
                         compact_view=compact_view)

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
                release['formatted_date'] = format_date(release.get('release_date'))
            
            # Get recently upgraded items
            upgrade_enabled = get_setting('Scraping', 'enable_upgrading', False)
            if upgrade_enabled:
                recently_upgraded = loop.run_until_complete(get_recently_upgraded_items(upgraded_limit=5))
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
    
    # Get timezone using our robust function
    local_tz = _get_local_timezone()
    stats['timezone'] = str(local_tz)
    stats['uptime'] = int(time.time() - app_start_time)
    
    # Get program status
    stats['program_status'] = {
        'running': program_is_running(),
        'initializing': program_is_initializing()
    }
    
    # Get collection counts
    counts = get_collected_counts()
    stats['total_movies'] = counts['total_movies']
    stats['total_shows'] = counts['total_shows']
    stats['total_episodes'] = counts['total_episodes']
    
    # Get active downloads and usage stats
    active_downloads = get_cached_active_downloads()
    usage_stats = get_cached_user_traffic()
    stats['active_downloads_data'] = active_downloads
    stats['usage_stats_data'] = usage_stats
    
    # Get recently aired and upcoming shows
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