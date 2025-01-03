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
from settings import get_setting
from .models import user_required, onboarding_required 
from extensions import app_start_time
from database import get_recently_added_items, get_poster_url, get_collected_counts, get_recently_upgraded_items
from debrid import get_debrid_provider, TooManyDownloadsError, ProviderUnavailableError
from metadata.metadata import get_show_airtime_by_imdb_id
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

@cache_for_seconds(30)
def get_cached_active_downloads():
    """Get active downloads with caching"""
    provider = get_debrid_provider()
    return provider.get_active_downloads()

@cache_for_seconds(30)
def get_cached_user_traffic():
    """Get user traffic with caching"""
    provider = get_debrid_provider()
    return provider.get_user_traffic()

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
    SELECT DISTINCT m.title, m.release_date, m.tmdb_id
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
    for title, release_date, tmdb_id in results:
        if tmdb_id not in existing_tmdb_ids:  # Only include if not in our collection
            if release_date not in grouped_results:
                grouped_results[release_date] = set()
            grouped_results[release_date].add(title)
    
    # Format the results
    formatted_results = [
        {'titles': list(titles), 'release_date': date}
        for date, titles in grouped_results.items()
    ]
    
    return formatted_results

def get_recently_aired_and_airing_soon():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    now = datetime.now()
    two_days_ago = now - timedelta(days=2)
    
    query = """
    WITH RankedEpisodes AS (
        SELECT 
            title,
            season_number,
            episode_number,
            release_date,
            airtime,
            imdb_id,
            ROW_NUMBER() OVER (PARTITION BY title, season_number, episode_number ORDER BY release_date DESC, airtime DESC) as rn
        FROM media_items
        WHERE type = 'episode' 
        AND release_date >= ? 
        AND release_date <= ?
    )
    SELECT DISTINCT 
        title,
        season_number,
        episode_number,
        release_date,
        airtime,
        imdb_id
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
        title, season, episode, release_date, airtime, imdb_id = result
        try:
            release_date = datetime.fromisoformat(release_date)
            
            if airtime is None or airtime == '':
                airtime = get_show_airtime_by_imdb_id(imdb_id)
            
            try:
                airtime = datetime.strptime(airtime, '%H:%M').time()
            except ValueError:
                logging.warning(f"Invalid airtime format for {title}: {airtime}. Using default.")
                airtime = datetime.strptime("19:00", '%H:%M').time()
            
            air_datetime = datetime.combine(release_date.date(), airtime)
            
            # Create a key for grouping
            show_key = f"{title}_{season}_{release_date.date()}"
            
            if show_key not in shows:
                shows[show_key] = {
                    'title': title,
                    'season': season,
                    'episodes': set(),
                    'air_datetime': air_datetime,
                    'release_date': release_date.date()
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
            if start == end:
                episode_parts.append(f"E{start:02d}")
            else:
                episode_parts.append(f"E{start:02d}-{end:02d}")
        episode_range = ", ".join(episode_parts)
        
        formatted_item = {
            'title': f"{show['title']} S{show['season']:02d}{episode_range}",
            'air_datetime': show['air_datetime'],
            'sort_key': show['air_datetime'].isoformat(),
            'formatted_datetime': format_datetime_preference(show['air_datetime'], session.get('use_24hour_format', False))
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
    
    # Save the preference to the user's session
    session['compact_view'] = compact_view
    
    # Create a response
    response = make_response(jsonify({'success': True, 'compactView': compact_view}))
    
    # Set a cookie to persist the preference
    response.set_cookie('compact_view', 
                        str(compact_view).lower(), 
                        max_age=31536000,  # 1 year
                        path='/',  # Ensure cookie is available for entire site
                        httponly=False)  # Allow JavaScript access
    
    return response

@root_bp.route('/')
@user_required
@onboarding_required
def root():
    start_time = time.perf_counter()
    
    # Initialize session if not already set
    if 'use_24hour_format' not in session:
        session['use_24hour_format'] = True  # Default to 24-hour format
    if 'compact_view' not in session:
        session['compact_view'] = False  # Default to non-compact view
    
    # Get view preferences from session
    use_24hour_format = session.get('use_24hour_format', True)
    compact_view = session.get('compact_view', False)
    
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
            session['compact_view'] = new_compact_view
            compact_view = new_compact_view
            if request.headers.get('Accept') == 'application/json':
                return jsonify({'success': True, 'compact_view': compact_view})
    
    # Get all statistics data
    stats = {}
    stats['timezone'] = time.tzname[0]
    stats['uptime'] = int(time.time() - app_start_time)
    
    # Get collection counts
    collection_start = time.perf_counter()
    counts = get_collected_counts()
    stats['total_movies'] = counts['total_movies']
    stats['total_shows'] = counts['total_shows']
    stats['total_episodes'] = counts['total_episodes']
    logging.info(f"Collection counts took {(time.perf_counter() - collection_start)*1000:.2f}ms")
    
    # Get active downloads
    downloads_start = time.perf_counter()
    try:
        active_count, limit = get_cached_active_downloads()
        stats['active_downloads'] = active_count
        stats['active_downloads_error'] = None
    except TooManyDownloadsError as e:
        logging.warning(f"Too many active downloads: {str(e)}")
        stats['active_downloads'] = str(e)
        stats['active_downloads_error'] = 'too_many'
    except Exception as e:
        logging.error(f"Error getting active downloads: {str(e)}")
        stats['active_downloads'] = 0
        stats['active_downloads_error'] = 'error'
    logging.info(f"Active downloads check took {(time.perf_counter() - downloads_start)*1000:.2f}ms")
    
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
    
    # Set up async event loop for recently added and upgraded items
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
        recently_upgraded = loop.run_until_complete(get_recently_upgraded_items())
        
        # Format dates for upgraded items
        for item in recently_upgraded:
            item['formatted_date'] = format_datetime_preference(
                item['last_updated'], 
                use_24hour_format
            )
    
    finally:
        loop.close()
    logging.info(f"Recently added and upgraded items took {(time.perf_counter() - recent_start)*1000:.2f}ms")
    
    # Check if TMDB API key is set
    api_start = time.time()
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    stats['tmdb_api_key_set'] = bool(tmdb_api_key)
    
    # Get active downloads data
    try:
        count, limit = get_cached_active_downloads()
        
        if not count or not limit:
            stats['active_downloads_data'] = {
                'count': 0,
                'limit': 0,
                'percentage': 0,
                'status': 'normal'
            }
        else:
            percentage = round((count / limit * 100) if limit > 0 else 0)
            status = 'normal'
            if percentage >= 90:
                status = 'critical'
            elif percentage >= 75:
                status = 'warning'
                
            stats['active_downloads_data'] = {
                'count': count,
                'limit': limit,
                'percentage': percentage,
                'status': status
            }
    except Exception as e:
        logging.error(f"Error getting active downloads: {str(e)}")
        stats['active_downloads_data'] = {
            'count': 0,
            'limit': 0,
            'percentage': 0,
            'status': 'error'
        }
    logging.info(f"API checks took {(time.perf_counter() - api_start)*1000:.2f}ms")
    
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
                # Convert GB to bytes for calculation
                daily_used = float(usage.get('downloaded', 0)) * 1024 * 1024 * 1024  # GB to bytes
                daily_limit = float(usage.get('limit', 2000)) * 1024 * 1024 * 1024  # GB to bytes
                
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
    data = request.json
    use_24hour_format = data.get('use24HourFormat', True)
    
    # Save to session
    session['use_24hour_format'] = use_24hour_format
    
    # Format times with the new preference
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    upcoming_releases = get_upcoming_releases()
    
    for item in recently_aired + airing_soon:
        item['formatted_time'] = format_datetime_preference(item['air_datetime'], use_24hour_format)
    
    for item in upcoming_releases:
        item['formatted_time'] = format_datetime_preference(item['release_date'], use_24hour_format)
    
    # Get recently added items and format their times
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    recently_added = loop.run_until_complete(get_recently_added_items(movie_limit=5, show_limit=5))
    for item in recently_added['movies'] + recently_added['shows']:
        if 'collected_at' in item and item['collected_at'] is not None:
            collected_at = datetime.strptime(item['collected_at'], '%Y-%m-%d %H:%M:%S')
            item['formatted_collected_at'] = format_datetime_preference(collected_at, use_24hour_format)
        else:
            item['formatted_collected_at'] = 'Unknown'

    upgrade_enabled = get_setting('Scraping', 'enable_upgrading', 'False')
    upgrade_enabled_set = bool(upgrade_enabled)
    if upgrade_enabled_set:
        upgrading_enabled = True
        recently_upgraded = loop.run_until_complete(get_recently_upgraded_items(upgraded_limit=5))
        for item in recently_upgraded:
            if 'collected_at' in item and item['collected_at'] is not None:
                collected_at = datetime.strptime(item['collected_at'], '%Y-%m-%d %H:%M:%S')
                item['formatted_collected_at'] = format_datetime_preference(collected_at, use_24hour_format)
            else:
                item['formatted_collected_at'] = 'Unknown'
    else:
        upgrading_enabled = False
        recently_upgraded = []
        
    response = make_response(jsonify({
        'status': 'OK', 
        'use24HourFormat': use_24hour_format,
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases,
        'recently_upgraded': recently_upgraded,
        'upgrading_enabled': upgrading_enabled,
        'recently_added': recently_added
    }))
    response.set_cookie('use24HourFormat', 
                        str(use_24hour_format).lower(), 
                        max_age=31536000,  # 1 year
                        path='/',  # Ensure cookie is available for entire site
                        httponly=False)  # Allow JavaScript access
    return response

@statistics_bp.route('/active_downloads', methods=['GET'])
@user_required
@cache_for_seconds(30)
def active_downloads():
    """Get active downloads and limits for the debrid provider"""
    try:
        provider = get_debrid_provider()
        active_count, limit = provider.get_active_downloads()
        return jsonify({
            'active_count': active_count,
            'limit': limit,
            'percentage': round((active_count / limit) * 100) if limit > 0 else 0,
            'error': None
        })
    except TooManyDownloadsError as e:
        logging.warning(f"Too many active downloads: {str(e)}")
        # Parse out the counts from the error message
        import re
        match = re.search(r'(\d+)/(\d+)', str(e))
        if match:
            active_count, limit = map(int, match.groups())
            return jsonify({
                'active_count': active_count,
                'limit': limit,
                'percentage': round((active_count / limit) * 100) if limit > 0 else 0,
                'error': 'too_many'
            })
        return jsonify({
            'error': 'too_many',
            'message': str(e)
        }), 429  # Too Many Requests
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
        provider = get_debrid_provider()
        count, limit = provider.get_active_downloads()
        
        if not count or not limit:
            return jsonify({
                'active': {
                    'count': 0,
                    'limit': 0,
                    'percentage': 0,
                    'status': 'normal'
                }
            })

        # Calculate percentage and determine status
        percentage = round((count / limit * 100) if limit > 0 else 0)
        
        status = 'normal'
        if percentage >= 90:
            status = 'critical'
        elif percentage >= 75:
            status = 'warning'
            
        return jsonify({
            'active': {
                'count': count,
                'limit': limit,
                'percentage': percentage,
                'status': status
            }
        })
        
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
            collected_at = datetime.strptime(item['collected_at'], '%Y-%m-%d %H:%M:%S')
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
        if isinstance(date_input, str):
            date = datetime.fromisoformat(date_input.rstrip('Z'))  # Remove 'Z' if present
        elif isinstance(date_input, datetime):
            date = date_input
        else:
            return str(date_input)
        
        now = datetime.now()
        today = now.date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        if date.date() == today:
            day_str = "Today"
        elif date.date() == yesterday:
            day_str = "Yesterday"
        elif date.date() == tomorrow:
            day_str = "Tomorrow"
        else:
            day_str = date.strftime("%a, %d %b %Y")

        time_format = "%H:%M" if use_24hour_format else "%I:%M %p"
        formatted_time = date.strftime(time_format)
        
        # Remove leading zero from hour in 12-hour format
        if not use_24hour_format:
            formatted_time = formatted_time.lstrip("0")
        
        return f"{day_str} {formatted_time}"
    except ValueError:
        return str(date_input)  # Return original string if parsing fails

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
@cache_for_seconds(30)
def usage_stats():
    """Get daily usage statistics from the debrid provider"""
    try:
        provider = get_debrid_provider()
        usage = provider.get_user_traffic()
        
        if not usage or usage.get('limit') is None:
            return jsonify({
                'daily': {
                    'used': '0 GB',
                    'limit': '2000 GB',
                    'percentage': 0,
                    'error': None
                }
            })

        try:
            # Convert GB to bytes for calculation
            daily_used = float(usage.get('downloaded', 0)) * 1024 * 1024 * 1024  # GB to bytes
            daily_limit = float(usage.get('limit', 2000)) * 1024 * 1024 * 1024  # GB to bytes
            
            percentage = round((daily_used / daily_limit) * 100) if daily_limit > 0 else 0
            
            response = {
                'daily': {
                    'used': format_bytes(daily_used),
                    'limit': format_bytes(daily_limit),
                    'percentage': percentage,
                    'error': None
                }
            }
            return jsonify(response)
            
        except (TypeError, ValueError) as e:
            logging.error(f"Error converting usage values: {e}")
            logging.error(f"Raw usage data that caused error: {usage}")
            return jsonify({
                'daily': {
                    'used': '0 GB',
                    'limit': '2000 GB',
                    'percentage': 0,
                    'error': 'conversion_error'
                }
            })
            
    except Exception as e:
        logging.error(f"Error getting usage stats: {str(e)}")
        return jsonify({
            'daily': {
                'used': '0 GB',
                'limit': '2000 GB',
                'percentage': 0,
                'error': 'provider_error'
            }
        })