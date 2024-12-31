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
        SELECT title, year, type, collected_at, imdb_id, tmdb_id, version, filled_by_title, filled_by_file, state
        FROM media_items
        WHERE type = 'movie' AND collected_at IS NOT NULL AND state = 'Collected'
        GROUP BY title, year
        ORDER BY MAX(collected_at) DESC
        LIMIT ?
        """
        
        # Query for episodes
        episode_query = """
        WITH LatestEpisodes AS (
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
            filled_by_file
        FROM LatestEpisodes
        WHERE rn = 1
        ORDER BY collected_at DESC
        LIMIT ?
        """
        
        cursor.execute(movie_query, (movie_limit,))
        movie_results = cursor.fetchall()
        
        #logging.debug(f"Initial movie results: {len(movie_results)}")
        #for movie in movie_results:
        #    logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Version: {movie['version']} - State: {movie['state']}")
        
        # Fetch more items initially to ensure we get enough unique ones
        cursor.execute(episode_query, (show_limit * 2,))
        episode_results = cursor.fetchall()
        
        #logging.debug(f"Initial episode results: {len(episode_results)}")
        #for episode in episode_results:
        #    logging.debug(f"Episode: {episode['title']} - Season: {episode['season_number']} - Episode: {episode['episode_number']} - Version: {episode['version']}")
        
        consolidated_movies = {}
        shows = {}
        
        async with aiohttp.ClientSession() as session:
            poster_tasks = []
            
            # Process movies
            for row in movie_results:
                item = dict(row)
                key = f"{item['title']}-{item['year']}"
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
                
                media_type = 'movie'
                cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                if cached_url:
                    movie_item['poster_url'] = cached_url
                else:
                    poster_task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                    poster_tasks.append((movie_item, poster_task, media_type))
                consolidated_movies[key] = movie_item
            
            # Process episodes
            for row in episode_results:
                item = dict(row)
                key = item['title']
                
                if key not in shows:
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
                    
                    media_type = 'tv'
                    cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                    if cached_url:
                        show_item['poster_url'] = cached_url
                    else:
                        poster_task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                        poster_tasks.append((show_item, poster_task, media_type))
                    shows[key] = show_item
                    
                    # Break if we have enough unique shows
                    if len(shows) >= show_limit:
                        break
            
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
                    #logging.info(f"get_setting('TMDB', 'api_key'): {get_setting('TMDB', 'api_key')}")
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
        
        #logging.debug("Before limit_and_process:")
        #for movie in movies_list:
        #    logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Versions: {movie['versions']}")
        #for show in shows_list:
        #    logging.debug(f"Show: {show['title']} - Versions: {show['versions']}")
        
        # Final processing and limiting to 5 unique items based on title
        def limit_and_process(items, limit=5):
            unique_items = {}
            for item in items:
                if len(unique_items) >= limit:
                    break
                # Use both title and year as the key for movies
                key = f"{item['title']}-{item['year']}" if 'year' in item else item['title']
                if key not in unique_items:
                    unique_items[key] = item
            return list(unique_items.values())

        movies_list = limit_and_process(movies_list)
        shows_list = limit_and_process(shows_list)

        #logging.debug("After limit_and_process:")
        #for movie in movies_list:
        #    logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Versions: {movie['versions']}")
        #for show in shows_list:
        #    logging.debug(f"Show: {show['title']} - Versions: {show['versions']}")

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
        for item in items_list:
            logging.info(f"  {item['title']} - Last Updated: {item['last_updated']}")
        
        return items_list
    except Exception as e:
        logging.error(f"Error in get_recently_upgraded_items: {str(e)}", exc_info=True)
        return []
    finally:
        conn.close()