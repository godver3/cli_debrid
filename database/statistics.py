import logging
from .core import get_db_connection
import asyncio
import aiohttp
from .poster_management import get_poster_url
from poster_cache import get_cached_poster_url, cache_poster_url, clean_expired_cache
from settings import get_setting
from flask import request, url_for
from urllib.parse import urlparse

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

async def get_recently_added_items(movie_limit=5, show_limit=5):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Query for movies
        movie_query = """
        SELECT title, year, type, collected_at, imdb_id, tmdb_id, version, filled_by_title, filled_by_file
        FROM media_items
        WHERE type = 'movie' AND collected_at IS NOT NULL
        GROUP BY title, year
        ORDER BY MAX(collected_at) DESC
        LIMIT ?
        """
        
        # Query for episodes
        episode_query = """
        SELECT title, year, type, season_number, episode_number, collected_at, imdb_id, tmdb_id, version, filled_by_title, filled_by_file
        FROM media_items
        WHERE type = 'episode' AND collected_at IS NOT NULL
        GROUP BY title
        ORDER BY MAX(collected_at) DESC
        LIMIT ?
        """
        
        cursor.execute(movie_query, (movie_limit,))
        movie_results = cursor.fetchall()
        
        cursor.execute(episode_query, (show_limit,))
        episode_results = cursor.fetchall()
        
        logging.debug(f"Initial movie results: {len(movie_results)}")
        for movie in movie_results:
            logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Version: {movie['version']}")
        
        consolidated_movies = {}
        shows = {}
        
        async with aiohttp.ClientSession() as session:
            poster_tasks = []
            
            # Process movies
            for row in movie_results:
                item = dict(row)
                key = f"{item['title']}-{item['year']}"
                if key not in consolidated_movies:
                    consolidated_movies[key] = {
                        **item,
                        'versions': [item['version']],
                        'filled_by_title': [item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file']],
                        'collected_at': item['collected_at']
                    }
                    media_type = 'movie'
                    cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                    if cached_url:
                        consolidated_movies[key]['poster_url'] = cached_url
                    else:
                        poster_task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                        poster_tasks.append((consolidated_movies[key], poster_task, media_type))
                else:
                    consolidated_movies[key]['versions'].append(item['version'])
                    consolidated_movies[key]['filled_by_title'].append(item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file'])
                    consolidated_movies[key]['collected_at'] = max(consolidated_movies[key]['collected_at'], item['collected_at'])
                
                logging.debug(f"Consolidated movie: {key} - Versions: {consolidated_movies[key]['versions']}")
            
            # Process episodes
            for row in episode_results:
                item = dict(row)
                media_type = 'tv'
                
                if item['title'] not in shows:
                    show_item = {
                        'title': item['title'],
                        'year': item['year'],
                        'type': 'show',
                        'collected_at': item['collected_at'],
                        'imdb_id': item['imdb_id'],
                        'tmdb_id': item['tmdb_id'],
                        'seasons': [item['season_number']],
                        'latest_episode': (item['season_number'], item['episode_number']),
                        'filled_by_title': [item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file']],
                        'versions': [item['version']]
                    }
                    cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                    if cached_url:
                        show_item['poster_url'] = cached_url
                    else:
                        poster_task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                        poster_tasks.append((show_item, poster_task, media_type))
                    shows[item['title']] = show_item
                else:
                    show = shows[item['title']]
                    if item['season_number'] not in show['seasons']:
                        show['seasons'].append(item['season_number'])
                    show['collected_at'] = max(show['collected_at'], item['collected_at'])
                    show['filled_by_title'].append(item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file'])
                    show['latest_episode'] = max(show['latest_episode'], (item['season_number'], item['episode_number']))
                    if item['version'] not in show['versions']:
                        show['versions'].append(item['version'])
                
                logging.debug(f"Processed show: {item['title']} - Versions: {shows[item['title']]['versions']}")
            
            # Wait for all poster URL tasks to complete
            poster_results = await asyncio.gather(*[task for _, task, _ in poster_tasks], return_exceptions=True)
            
            # Assign poster URLs to items and cache them
            for (item, _, media_type), result in zip(poster_tasks, poster_results):
                if isinstance(result, Exception):
                    logging.error(f"Error fetching poster for {media_type} with TMDB ID {item['tmdb_id']}: {result}")
                elif result:
                    item['poster_url'] = result
                    cache_poster_url(item['tmdb_id'], media_type, result)
                else:
                    logging.info(f"get_setting('TMDB', 'api_key'): {get_setting('TMDB', 'api_key')}")
                    if get_setting('TMDB', 'api_key') == "":
                        logging.warning("TMDB API key not set, using placeholder images")
                        
                        # Generate the placeholder URL
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        
                        # Check if the request is secure (HTTPS)
                        if request.is_secure:
                            # If it's secure, ensure the URL uses HTTPS
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='https').geturl()
                        else:
                            # If it's not secure, use HTTP
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='http').geturl()
                        
                        item['poster_url'] = placeholder_url
                    
                    logging.warning(f"No poster URL found for {media_type} with TMDB ID {item['tmdb_id']}")
        
        # Convert consolidated_movies dict to list and sort
        movies_list = list(consolidated_movies.values())
        movies_list.sort(key=lambda x: x['collected_at'], reverse=True)
        
        # Convert shows dict to list and sort
        shows_list = list(shows.values())
        shows_list.sort(key=lambda x: x['collected_at'], reverse=True)
        
        logging.debug("Before limit_and_process:")
        for movie in movies_list:
            logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Versions: {movie['versions']}")
        for show in shows_list:
            logging.debug(f"Show: {show['title']} - Versions: {show['versions']}")
        
        # Final processing and limiting to 5 unique items based on title
        def limit_and_process(items, limit=5):
            unique_items = {}
            for item in items:
                if len(unique_items) >= limit:
                    break
                if item['title'] not in unique_items:
                    if 'seasons' in item:
                        item['seasons'].sort()
                    item['versions'].sort()
                    item['versions'] = ', '.join(item['versions'])  # Join versions into a string
                    unique_items[item['title']] = item
                logging.debug(f"Processed item: {item['title']} - Versions: {item['versions']}")
            return list(unique_items.values())

        movies_list = limit_and_process(movies_list)
        shows_list = limit_and_process(shows_list)

        logging.debug("After limit_and_process:")
        for movie in movies_list:
            logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Versions: {movie['versions']}")
        for show in shows_list:
            logging.debug(f"Show: {show['title']} - Versions: {show['versions']}")

        # Clean expired cache entries
        clean_expired_cache()

        return {
            'movies': movies_list,
            'shows': shows_list
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
        
        # Query for upgrades
        upgraded_query = """
        SELECT title, year, type, collected_at, imdb_id, tmdb_id, version, filled_by_title, filled_by_file, upgrading_from
        FROM media_items
        WHERE upgrading_from IS NOT NULL
        GROUP BY title, year
        ORDER BY MAX(collected_at) DESC
        LIMIT ?
        """
        
        cursor.execute(upgraded_query, (upgraded_limit,))
        upgrade_results = cursor.fetchall()
        
        logging.debug(f"Initial upgraded results: {len(upgrade_results)}")
        for media in upgrade_results:
            logging.debug(f"Upgraded: {media['title']} ({media['year']}) - Type: {media['type']} - Version: {media['version']}")
        
        media_items = {}
        
        async with aiohttp.ClientSession() as session:
            poster_tasks = []
            
            # Process items
            for row in upgrade_results:
                item = dict(row)
                key = f"{item['title']}-{item['year']}"
                if key not in media_items:
                    media_items[key] = {
                        **item,
                        'versions': [item['version']],
                        'filled_by_title': [item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file']],
                        'upgrading_from': [item['upgrading_from']],
                        'collected_at': item['collected_at']
                    }
                    # Set media_type based on the item type from database
                    media_type = 'tv' if item['type'] in ['show', 'episode'] else 'movie'
                    logging.debug(f"Setting media_type to {media_type} for {item['title']} (type: {item['type']})")
                    
                    cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                    if cached_url:
                        media_items[key]['poster_url'] = cached_url
                    else:
                        poster_task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                        poster_tasks.append((media_items[key], poster_task, media_type))
                else:
                    media_items[key]['versions'].append(item['version'])
                    media_items[key]['filled_by_title'].append(item['filled_by_title'] if item['filled_by_title'] is not None else item['filled_by_file'])
                    media_items[key]['upgrading_from'].append(item['upgrading_from'])
                    media_items[key]['collected_at'] = max(media_items[key]['collected_at'], item['collected_at'])
                
                logging.debug(f"Upgraded Media: {key} - Versions: {media_items[key]['versions']}")
            
            
            # Wait for all poster URL tasks to complete
            poster_results = await asyncio.gather(*[task for _, task, _ in poster_tasks], return_exceptions=True)
            
            # Assign poster URLs to items and cache them
            for (item, _, media_type), result in zip(poster_tasks, poster_results):
                if isinstance(result, Exception):
                    logging.error(f"Error fetching poster for {media_type} with TMDB ID {item['tmdb_id']}: {result}")
                elif result:
                    item['poster_url'] = result
                    cache_poster_url(item['tmdb_id'], media_type, result)
                else:
                    logging.info(f"get_setting('TMDB', 'api_key'): {get_setting('TMDB', 'api_key')}")
                    if get_setting('TMDB', 'api_key') == "":
                        logging.warning("TMDB API key not set, using placeholder images")
                        
                        # Generate the placeholder URL
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        
                        # Check if the request is secure (HTTPS)
                        if request.is_secure:
                            # If it's secure, ensure the URL uses HTTPS
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='https').geturl()
                        else:
                            # If it's not secure, use HTTP
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='http').geturl()
                        
                        item['poster_url'] = placeholder_url
                    
                    logging.warning(f"No poster URL found for {media_type} with TMDB ID {item['tmdb_id']}")
        
        # Convert consolidated_movies dict to list and sort
        upgraded_list = list(media_items.values())
        
        # Log the values before sorting
        for item in upgraded_list:
            logging.debug(f"Before sorting: {item['title']} - collected_at: {item['collected_at']}")
        
        # Update the sorting key to handle None values
        upgraded_list.sort(key=lambda x: x['collected_at'] or '', reverse=True)
        
        # Log the values after sorting
        for item in upgraded_list:
            logging.debug(f"After sorting: {item['title']} - collected_at: {item['collected_at']}")
        
        logging.debug("Before limit_and_process:")
        for upgraded in upgraded_list:
            logging.debug(f"Upgraded: {upgraded['title']} ({upgraded['year']}) - Versions: {upgraded['versions']}")
        
        # Final processing and limiting to 5 unique items based on title
        def limit_and_process(items, limit=5):
            unique_items = {}
            for item in items:
                if len(unique_items) >= limit:
                    break
                if item['title'] not in unique_items:
                    if 'seasons' in item:
                        item['seasons'].sort()
                    item['versions'].sort()
                    item['versions'] = ', '.join(item['versions'])  # Join versions into a string
                    unique_items[item['title']] = item
                logging.debug(f"Processed item: {item['title']} - Versions: {item['versions']}")
            return list(unique_items.values())

        upgraded_list = limit_and_process(upgraded_list)

        logging.debug("After limit_and_process:")
        for upgraded in upgraded_list:
            logging.debug(f"Upgraded: {upgraded['title']} ({upgraded['year']}) - Versions: {upgraded['versions']}")

        # Clean expired cache entries
        clean_expired_cache()

        return {
            'upgraded': upgraded_list
        }
    except Exception as e:
        logging.error(f"Error in get_recently_upgraded_items: {str(e)}", exc_info=True)
        return {'upgraded': []}
    finally:
        conn.close()