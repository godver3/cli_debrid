from flask import Blueprint, jsonify, request, render_template, session, make_response
from datetime import datetime, timedelta
from database import get_db_connection
from operator import itemgetter
from itertools import groupby
import asyncio
import aiohttp
import os
from settings import get_setting
from .models import user_required, onboarding_required 
from extensions import app_start_time
import time 
from database import get_recently_added_items, get_poster_url, get_collected_counts
from debrid.real_debrid import get_active_downloads

statistics_bp = Blueprint('statistics', __name__)

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
    next_week = today + timedelta(days=7)
    
    query = """
    SELECT DISTINCT title, release_date
    FROM media_items
    WHERE type = 'movie' AND release_date BETWEEN ? AND ?
    ORDER BY release_date, title
    """
    
    cursor.execute(query, (today.isoformat(), next_week.isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    # Group by release date
    grouped_results = {}
    for title, release_date in results:
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
    SELECT DISTINCT title, season_number, episode_number, release_date, airtime
    FROM media_items
    WHERE type = 'episode' AND release_date >= ? AND release_date <= ?
    ORDER BY release_date, airtime
    """
    
    cursor.execute(query, (two_days_ago.date().isoformat(), (now + timedelta(days=1)).date().isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    recently_aired = []
    airing_soon = []
    
    for result in results:
        title, season, episode, release_date, airtime = result
        air_datetime = datetime.combine(datetime.fromisoformat(release_date), datetime.strptime(airtime, '%H:%M').time())
        
        item = {
            'title': title,
            'season': season,
            'episode': episode,
            'air_datetime': air_datetime
        }
        
        if air_datetime <= now:
            recently_aired.append(item)
        else:
            airing_soon.append(item)
    
    return recently_aired, airing_soon

@statistics_bp.route('/set_compact_preference', methods=['POST'])
def set_compact_preference():
    data = request.json
    compact_view = data.get('compactView', False)
    
    # Save the preference to the user's session
    session['compact_view'] = compact_view
    
    # If you want to persist this preference for the user, you might save it to a database here
    
    return jsonify({'success': True, 'compactView': compact_view})

@statistics_bp.route('/')
@user_required
@onboarding_required
def index():
    os.makedirs('db_content', exist_ok=True)

    start_time = time.time()

    uptime = int(time.time() - app_start_time)

    collected_counts = get_collected_counts()
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    upcoming_releases = get_upcoming_releases()
    active_downloads, limit_downloads = get_active_downloads()
    now = datetime.now()
    
    # Fetch recently added items from the database
    recently_added_start = time.time()
    recently_added = asyncio.run(get_recently_added_items(movie_limit=5, show_limit=5))
    recently_added_end = time.time()
    
    cookie_value = request.cookies.get('use24HourFormat')
    use_24hour_format = cookie_value == 'true' if cookie_value is not None else True
    
    # Format times for recently aired and airing soon
    for item in recently_aired + airing_soon:
        item['formatted_time'] = format_datetime_preference(item['air_datetime'], use_24hour_format)
    
    # Format times for upcoming releases (if they have time information)
    for item in upcoming_releases:
        item['formatted_time'] = format_datetime_preference(item['release_date'], use_24hour_format)

    stats = {
        'uptime': uptime,
        'total_movies': collected_counts['total_movies'],
        'total_shows': collected_counts['total_shows'],
        'total_episodes': collected_counts['total_episodes'],
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases,
        'today': now.date(),
        'yesterday': (now - timedelta(days=1)).date(),
        'tomorrow': (now + timedelta(days=1)).date(),
        'recently_added_movies': recently_added['movies'],
        'recently_added_shows': recently_added['shows'],
        'use_24hour_format': use_24hour_format,
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases,
        'timezone': time.tzname[0],
        'active_downloads': active_downloads, 
        'limit_downloads': limit_downloads
    }
    
    compact_view = session.get('compact_view', False)

    end_time = time.time()
    total_time = end_time - start_time

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(stats)
    else:
        return render_template('statistics.html', stats=stats, compact_view=compact_view)
        
@statistics_bp.route('/set_time_preference', methods=['POST'])
def set_time_preference():
    data = request.json
    use_24hour_format = data.get('use24HourFormat', True)
    
    # Format times with the new preference
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    upcoming_releases = get_upcoming_releases()
    
    for item in recently_aired + airing_soon:
        item['formatted_time'] = format_datetime_preference(item['air_datetime'], use_24hour_format)
    
    for item in upcoming_releases:
        item['formatted_time'] = format_datetime_preference(item['release_date'], use_24hour_format)
    
    response = make_response(jsonify({
        'status': 'OK', 
        'use24HourFormat': use_24hour_format,
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases
    }))
    response.set_cookie('use24HourFormat', 
                        str(use_24hour_format).lower(), 
                        max_age=31536000,  # 1 year
                        path='/',  # Ensure cookie is available for entire site
                        httponly=False)  # Allow JavaScript access
    return response

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
                                recent_movies.append({
                                    'title': item['title'],
                                    'year': item.get('year'),
                                    'added_at': datetime.fromtimestamp(int(item['addedAt'])).strftime('%Y-%m-%d %H:%M:%S'),
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
                                        recent_shows[show_title] = {
                                            'title': show_title,
                                            'added_at': datetime.fromtimestamp(int(item['addedAt'])).strftime('%Y-%m-%d %H:%M:%S'),
                                            'poster_url': poster_url,
                                            'seasons': set()
                                        }
                            if show_title in recent_shows:
                                recent_shows[show_title]['seasons'].add(item['parentIndex'])
                                recent_shows[show_title]['added_at'] = max(
                                    recent_shows[show_title]['added_at'],
                                    datetime.fromtimestamp(int(item['addedAt'])).strftime('%Y-%m-%d %H:%M:%S')
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
    