import asyncio
import aiohttp
import logging
from settings import get_setting
import time
from typing import Dict, List, Any
import ast
from metadata.metadata import get_metadata, get_release_date
import plexapi.server
import plexapi.exceptions
import os
from cli_battery.app.direct_api import DirectAPI
from plexapi.server import PlexServer
from database.database_reading import get_media_item_by_id
import requests

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 400
CHUNK_SIZE = 10  # Adjust this value to find the optimal balance
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds

def process_library_names(library_names: str, all_libraries: dict, libraries_by_key: dict) -> list:
    """
    Process a comma-separated string of library names/ids and return their corresponding library keys.
    Handles both library names and numeric IDs.
    
    Args:
        library_names: Comma-separated string of library names or IDs
        all_libraries: Dictionary mapping library names to keys
        libraries_by_key: Dictionary mapping library keys to names
        
    Returns:
        List of library keys
    """
    libraries = []
    for name in library_names.split(','):
        name = name.strip()
        if not name:
            continue
        if name in all_libraries:  # If it's a library name
            libraries.append(all_libraries[name])
        elif name in libraries_by_key:  # If it's a library key/ID
            libraries.append(name)
    return libraries

async def fetch_data(session: aiohttp.ClientSession, url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """Fetch data from Plex with retry logic."""
    for attempt in range(MAX_RETRIES):
        try:
            async with semaphore:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        try:
                            return await response.json()
                        except aiohttp.ContentTypeError:
                            # If we can't decode JSON, try to get the text content for error details
                            error_content = await response.text()
                            logger.error(f"Failed to decode JSON from {url}. Content: {error_content[:200]}...")
                            if attempt < MAX_RETRIES - 1:
                                wait_time = RETRY_DELAY * (attempt + 1)
                                logger.info(f"Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                return {'MediaContainer': {'Metadata': []}}
                    elif response.status == 404:
                        logger.warning(f"Resource not found at {url}")
                        return {'MediaContainer': {'Metadata': []}}
                    else:
                        error_content = await response.text()
                        logger.error(f"HTTP {response.status} from {url}. Content: {error_content[:200]}...")
                        
                        if attempt < MAX_RETRIES - 1:
                            wait_time = RETRY_DELAY * (attempt + 1)
                            logger.info(f"Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            return {'MediaContainer': {'Metadata': []}}
                            
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                logger.warning(f"Request failed: {str(e)}. Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"Failed after {MAX_RETRIES} attempts: {str(e)}")
                return {'MediaContainer': {'Metadata': []}}
    
    return {'MediaContainer': {'Metadata': []}}

async def get_library_contents(session: aiohttp.ClientSession, plex_url: str, library_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/sections/{library_key}/all?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    metadata = data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []
    logger.info(f"Retrieved {len(metadata)} items from library {library_key}")
    return metadata

async def get_detailed_movie_metadata(session: aiohttp.ClientSession, plex_url: str, movie_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{movie_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else {}

async def get_show_seasons(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{show_key}/children?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def get_season_episodes(session: aiohttp.ClientSession, plex_url: str, season_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{season_key}/children?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def get_detailed_show_metadata(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else {}

async def process_show(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, show: Dict[str, Any]) -> List[Dict[str, Any]]:
    show_key = show['ratingKey']
    
    # Get detailed show metadata
    detailed_show = await get_detailed_show_metadata(session, plex_url, show_key, headers, semaphore)
    
    show_title = detailed_show['title']
    show_imdb_id = None
    show_tmdb_id = None
    show_year = None

    if 'Guid' in detailed_show:
        for guid in detailed_show['Guid']:
            if guid['id'].startswith('imdb://'):
                show_imdb_id = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                show_tmdb_id = guid['id'].split('://')[1]

    # Get show metadata to find the original year
    if show_imdb_id:
        try:
            metadata_result = DirectAPI.get_show_metadata(show_imdb_id)
            if metadata_result and isinstance(metadata_result, tuple):
                show_metadata, _ = metadata_result
                if show_metadata and isinstance(show_metadata, dict):
                    show_year = show_metadata.get('year')
        except Exception as e:
            logger.error(f"Error retrieving show metadata for {show_title}: {str(e)}")
            # Continue processing without the metadata

    # If we couldn't get the year from metadata, use the show's year as fallback
    if show_year is None:
        show_year = detailed_show.get('year')

    show_genres = []
    if 'Genre' in detailed_show:
        show_genres = [genre['tag'] for genre in detailed_show['Genre'] if 'tag' in genre]
    
    # Filter genres
    filtered_show_genres = filter_genres(show_genres)
    
    seasons = await get_show_seasons(session, plex_url, show_key, headers, semaphore)
    
    all_episodes = []
    for season in seasons:
        season_number = season.get('index')
        if season_number is None:
            logger.error(f"Missing season index for show {show_title} in season with ratingKey {season.get('ratingKey')}")
            continue
            
        season_key = season['ratingKey']
        
        episodes = await get_season_episodes(session, plex_url, season_key, headers, semaphore)
        
        for episode in episodes:
            try:
                episode_entries = await process_episode(episode, show_title, season_number, show_imdb_id, show_tmdb_id, filtered_show_genres, show_year)
                all_episodes.extend(episode_entries)
            except Exception as e:
                logger.error(f"Error processing episode {episode.get('title')} of {show_title} S{season_number}: {str(e)}")
                continue
    
    return all_episodes

async def process_episode(episode: Dict[str, Any], show_title: str, season_number: int, show_imdb_id: str, show_tmdb_id: str, show_genres: List[str], show_year: int = None) -> List[Dict[str, Any]]:
    # Validate season_number
    if season_number is None:
        logger.error(f"Missing season number for episode {episode.get('title')} in show {show_title}")
        return []

    base_episode_data = {
        'title': show_title,
        'episode_title': episode['title'],
        'season_number': season_number,
        'episode_number': episode.get('index'),
        'year': show_year,  # Use show_year instead of episode's year
        'show_year': show_year,  # Add show_year as a separate field
        'addedAt': episode.get('addedAt'),  # Use .get() with None as default
        'guid': episode.get('guid'),
        'ratingKey': episode['ratingKey'],
        'release_date': episode.get('originallyAvailableAt'),
        'imdb_id': show_imdb_id,
        'tmdb_id': show_tmdb_id,
        'episode_imdb_id': None,
        'episode_tmdb_id': None,
        'type': 'episode',
        'genres': show_genres
    }

    #logger.info(f"Processing episode: {base_episode_data['title']}, genres: {base_episode_data['genres']}")
           
    if 'Guid' in episode:
        for guid in episode['Guid']:
            if guid['id'].startswith('imdb://'):
                base_episode_data['episode_imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                base_episode_data['episode_tmdb_id'] = guid['id'].split('://')[1]
    
    # If release_date is missing, try to get it from our metadata service
    if not base_episode_data['release_date']:
        try:
            # First try to get episode-specific metadata if we have an episode IMDb ID
            if base_episode_data['episode_imdb_id']:
                metadata_result = DirectAPI.get_episode_metadata(base_episode_data['episode_imdb_id'])
                if metadata_result and isinstance(metadata_result, tuple):
                    metadata, _ = metadata_result
                    if metadata and isinstance(metadata, dict):
                        episode_data = metadata.get('episode', {})
                        if episode_data and isinstance(episode_data, dict):  # Add type check
                            # Get first_aired from episode data
                            first_aired = episode_data.get('first_aired')
                            if first_aired:
                                if not base_episode_data['release_date']:
                                    base_episode_data['release_date'] = first_aired[:10]
            
            # If we still don't have the data and have a show IMDb ID, try getting show metadata
            if not base_episode_data['release_date'] and show_imdb_id:
                metadata_result = DirectAPI.get_show_metadata(show_imdb_id)
                if metadata_result and isinstance(metadata_result, tuple):
                    show_metadata, _ = metadata_result
                    if show_metadata and isinstance(show_metadata, dict):
                        # Check if we have season/episode data in show metadata
                        seasons = show_metadata.get('seasons', {})
                        if isinstance(seasons, dict):  # Add type check
                            season_data = seasons.get(str(season_number), {})
                            if season_data and isinstance(season_data, dict) and 'episodes' in season_data:  # Add type check
                                episodes = season_data['episodes']
                                if isinstance(episodes, dict):  # Add type check
                                    # Find the matching episode by number
                                    for ep_num, ep_data in episodes.items():
                                        if str(base_episode_data['episode_number']) == ep_num:
                                            first_aired = ep_data.get('first_aired') if isinstance(ep_data, dict) else None  # Add type check
                                            if first_aired and not base_episode_data['release_date']:
                                                base_episode_data['release_date'] = first_aired[:10]
                                            break
                
        except Exception as e:
            logger.error(f"Error retrieving metadata for {show_title} S{season_number}E{base_episode_data['episode_number']}: {str(e)}")
            # Continue processing without the metadata
    
    episode_entries = []
    if 'Media' in episode and episode['Media']:
        for media in episode['Media']:
            if 'Part' in media and media['Part']:
                for part in media['Part']:
                    if 'file' in part:
                        episode_entry = base_episode_data.copy()
                        episode_entry['location'] = part['file']
                        episode_entries.append(episode_entry)
    
    if not episode_entries:
        logger.error(f"No file path found for episode: {show_title} - S{season_number:02d}E{episode.get('index', 'Unknown'):02d} - {episode['title']}")
    
    return episode_entries

async def process_shows_chunk(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks = [process_show(session, plex_url, headers, semaphore, show) for show in shows]
    results = await asyncio.gather(*tasks)
    flattened_results = [episode for show_episodes in results for episode in show_episodes]
    return flattened_results

async def process_movies_chunk(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, movies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for movie in movies:
        #detailed_movie = await get_detailed_movie_metadata(session, plex_url, movie['ratingKey'], headers, semaphore)
        movie_entries = await process_movie(movie)
        results.extend(movie_entries)
    return results

async def process_movie(movie: Dict[str, Any]) -> List[Dict[str, Any]]:
    genres = [genre['tag'] for genre in movie.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(genres)
    logging.info(f"Movie: {movie['title']}")

    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie.get('addedAt', None),
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie',
        'genres': filtered_genres,
        'release_date': movie.get('originallyAvailableAt', None)
    }

    if 'addedAt' not in movie:
        logger.warning(f"'addedAt' field missing for movie: {movie['title']}. Movie data: {movie}")

    if 'Guid' in movie:
        for guid in movie['Guid']:
            if guid['id'].startswith('imdb://'):
                movie_data['imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                movie_data['tmdb_id'] = guid['id'].split('://')[1]

    if not movie_data['imdb_id'] and not movie_data['tmdb_id']:
        logging.warning(f"No IMDb ID or TMDB ID found for movie: {movie_data['title']}. Skipping metadata retrieval.")
        movie_data['release_date'] = None
    '''try:
        metadata = get_metadata(imdb_id=movie_data['imdb_id'], tmdb_id=movie_data['tmdb_id'], item_media_type='movie')
        if metadata:
            movie_data['release_date'] = get_release_date(metadata, movie_data['imdb_id'])
        else:
            movie_data['release_date'] = None
    except ValueError as e:
        logger.error(f"Error retrieving metadata for {movie_data['title']}: {str(e)}")
        return []'''

    movie_entries = []
    if 'Media' in movie and movie['Media']:
        for media in movie['Media']:
            if 'Part' in media and media['Part']:
                for part in media['Part']:
                    if 'file' in part:
                        movie_entry = movie_data.copy()
                        movie_entry['location'] = part['file']
                        movie_entries.append(movie_entry)
    
    if not movie_entries:
        logger.error(f"No file path found for movie: {movie['title']}")
    
    logger.debug(f"Processed {len(movie_entries)} entries for movie: {movie['title']}")
    return movie_entries

async def get_collected_from_plex(request='all', progress_callback=None, bypass=False):
    try:
        start_time = time.time()
        logger.info(f"Starting Plex content collection. Request type: {request}")

        if progress_callback:
            logger.debug("Calling progress callback: Connecting to Plex server...")
            progress_callback('scanning', 'Connecting to Plex server...')

        plex_url = get_setting('Plex', 'url').rstrip('/')
        plex_token = get_setting('Plex', 'token')
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async with aiohttp.ClientSession() as session:
            if progress_callback:
                logger.debug("Calling progress callback: Retrieving library sections...")
                progress_callback('scanning', 'Retrieving library sections...')

            libraries_url = f"{plex_url}/library/sections"
            logger.debug(f"Fetching library sections from: {libraries_url}")
            libraries_data = await fetch_data(session, libraries_url, headers, semaphore)
            
            # Create mappings for library names and keys
            libraries_by_key = {str(library['key']): library['title'] for library in libraries_data['MediaContainer']['Directory']}
            all_libraries = {library['title']: library['key'] for library in libraries_data['MediaContainer']['Directory']}
            
            if bypass:
                # When bypass is true, gather all movie and show libraries
                movie_libraries = []
                show_libraries = []
                for library in libraries_data['MediaContainer']['Directory']:
                    if library['type'] == 'movie':
                        movie_libraries.append(str(library['key']))
                    elif library['type'] == 'show':
                        show_libraries.append(str(library['key']))
            else:
                # Process movie and show libraries from settings
                movie_libraries = process_library_names(get_setting('Plex', 'movie_libraries', ''), all_libraries, libraries_by_key)
                show_libraries = process_library_names(get_setting('Plex', 'shows_libraries', ''), all_libraries, libraries_by_key)
            
            logger.info(f"TV Show libraries to process: {show_libraries}")
            logger.info(f"Movie libraries to process: {movie_libraries}")

            if progress_callback:
                logger.debug("Calling progress callback: Retrieving show library contents...")
                progress_callback('scanning', 'Retrieving show library contents...')

            all_shows = []
            for library_key in show_libraries:
                shows = await get_library_contents(session, plex_url, library_key, headers, semaphore)
                all_shows.extend(shows)
            
            if progress_callback:
                progress_callback('scanning', 'Retrieving movie library contents...')

            all_movies = []
            for library_key in movie_libraries:
                movies = await get_library_contents(session, plex_url, library_key, headers, semaphore)
                all_movies.extend(movies)

            logger.info(f"Total shows found: {len(all_shows)}")
            logger.info(f"Total movies found: {len(all_movies)}")
            
            if progress_callback:
                progress_callback('scanning', f'Processing {len(all_shows)} shows...', {
                    'total_shows': len(all_shows),
                    'total_movies': len(all_movies),
                    'shows_processed': 0,
                    'movies_processed': 0
                })

            all_episodes = []
            for i in range(0, len(all_shows), CHUNK_SIZE):
                chunk = all_shows[i:i+CHUNK_SIZE]
                chunk_episodes = await process_shows_chunk(session, plex_url, headers, semaphore, chunk)
                all_episodes.extend(chunk_episodes)
                
                if progress_callback:
                    shows_processed = min(i + CHUNK_SIZE, len(all_shows))
                    progress_callback('scanning', 
                        f'Processed {shows_processed}/{len(all_shows)} shows ({len(all_episodes)} episodes found)',
                        {
                            'shows_processed': shows_processed,
                            'total_shows': len(all_shows),
                            'total_movies': len(all_movies),
                            'episodes_found': len(all_episodes)
                        }
                    )
            
            if progress_callback:
                progress_callback('scanning', f'Processing {len(all_movies)} movies...')

            all_movies_processed = []
            for i in range(0, len(all_movies), CHUNK_SIZE):
                chunk = all_movies[i:i+CHUNK_SIZE]
                chunk_movies = await process_movies_chunk(session, plex_url, headers, semaphore, chunk)
                all_movies_processed.extend(chunk_movies)
                
                if progress_callback:
                    movies_processed = min(i + CHUNK_SIZE, len(all_movies))
                    progress_callback('scanning', 
                        f'Processed {movies_processed}/{len(all_movies)} movies',
                        {
                            'shows_processed': len(all_shows),
                            'total_shows': len(all_shows),
                            'movies_processed': movies_processed,
                            'total_movies': len(all_movies),
                            'episodes_found': len(all_episodes)
                        }
                    )
           
        end_time = time.time()
        total_time = end_time - start_time
        logger.info(f"Collection complete. Total time: {total_time:.2f} seconds")
        logger.info(f"Collected: {len(all_episodes)} episodes and {len(all_movies_processed)} movies")

        if not all_movies_processed and not all_episodes:
            logger.error("No content retrieved from Plex scan")
            return None

        if progress_callback:
            progress_callback('complete', 'Scan complete', {
                'shows_processed': len(all_shows),
                'total_shows': len(all_shows),
                'movies_processed': len(all_movies),
                'total_movies': len(all_movies),
                'episodes_found': len(all_episodes)
            })

        return {
            'movies': all_movies_processed,
            'episodes': all_episodes
        }
    except Exception as e:
        logger.error(f"Error collecting content from Plex: {str(e)}", exc_info=True)
        if progress_callback:
            progress_callback('error', f'Error: {str(e)}')
        raise

async def run_get_collected_from_plex(request='all', progress_callback=None, bypass=False):
    logger.info("Starting run_get_collected_from_plex")
    result = await get_collected_from_plex(request, progress_callback, bypass)
    logger.info("Completed run_get_collected_from_plex")
    return result

def sync_run_get_collected_from_plex(request='all', progress_callback=None):
    return asyncio.run(run_get_collected_from_plex(request, progress_callback))

async def get_recent_from_plex():
    try:
        start_time = time.time()
        logger.info("Starting Plex recent content collection")

        plex_url = get_setting('Plex', 'url').rstrip('/')
        plex_token = get_setting('Plex', 'token')
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async with aiohttp.ClientSession() as session:
            libraries_url = f"{plex_url}/library/sections"
            libraries_data = await fetch_data(session, libraries_url, headers, semaphore)
            
            all_libraries = {library['title']: library['key'] for library in libraries_data['MediaContainer']['Directory']}
            
            movie_library_names = get_setting('Plex', 'movie_libraries', '').split(',')
            show_library_names = get_setting('Plex', 'shows_libraries', '').split(',')
            
            movie_libraries = process_library_names(get_setting('Plex', 'movie_libraries', ''), all_libraries, {str(library['key']): library['title'] for library in libraries_data['MediaContainer']['Directory']})
            show_libraries = process_library_names(get_setting('Plex', 'shows_libraries', ''), all_libraries, {str(library['key']): library['title'] for library in libraries_data['MediaContainer']['Directory']})
            
            logger.info(f"TV Show libraries to process: {show_libraries}")
            logger.info(f"Movie libraries to process: {movie_libraries}")

            processed_movies = []
            processed_episodes = []
            
            for library_key in movie_libraries + show_libraries:
                recent_url = f"{plex_url}/library/sections/{library_key}/recentlyAdded"
                recent_data = await fetch_data(session, recent_url, headers, semaphore)
                
                if 'MediaContainer' in recent_data and 'Metadata' in recent_data['MediaContainer']:
                    recent_items = recent_data['MediaContainer']['Metadata']
                    logger.info(f"Retrieved {len(recent_items)} recent items from library {library_key}")
                    
                    for item in recent_items:
                        if item['type'] == 'movie':
                            metadata_url = f"{plex_url}{item['key']}?includeGuids=1"
                            metadata = await fetch_data(session, metadata_url, headers, semaphore)
                            
                            if 'MediaContainer' in metadata and 'Metadata' in metadata['MediaContainer']:
                                full_metadata = metadata['MediaContainer']['Metadata'][0]
                                processed_items = await process_recent_movie(full_metadata)
                                processed_movies.extend(processed_items)
                        elif item['type'] == 'season':
                            show_metadata_url = f"{plex_url}/library/metadata/{item['parentRatingKey']}?includeGuids=1"
                            show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                            
                            if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                season_episodes = await process_recent_season(item, show_full_metadata, session, plex_url, headers, semaphore)
                                processed_episodes.extend([episode for sublist in season_episodes for episode in sublist])
                        elif item['type'] == 'episode':
                            show_metadata_url = f"{plex_url}/library/metadata/{item['grandparentRatingKey']}?includeGuids=1"
                            show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                            
                            if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                show_imdb_id, show_tmdb_id = extract_show_ids(show_full_metadata)
                                episode_data = await process_recent_episode(item, show_full_metadata['title'], item['parentIndex'], show_imdb_id, show_tmdb_id, show_full_metadata)
                                processed_episodes.extend(episode_data)
                            else:
                                logger.error(f"Failed to fetch show metadata for episode: {item.get('title', 'Unknown')}")
                        else:
                            logger.warning(f"Skipping item: {item.get('title', 'Unknown')} (Type: {item.get('type', 'Unknown')})")

        end_time = time.time()
        total_time = end_time - start_time

        logger.info(f"Collection complete. Total time: {total_time:.2f} seconds")
        logger.info(f"Collected: {len(processed_episodes)} episodes and {len(processed_movies)} movies")
        
        logger.debug(f"Final episodes list length: {len(processed_episodes)}")
        logger.debug(f"Final movies list length: {len(processed_movies)}")

        if not processed_movies and not processed_episodes:
            logger.error("No content retrieved from Plex recent scan")
            return None

        return {
            'movies': processed_movies,
            'episodes': processed_episodes
        }
    except Exception as e:
        logger.error(f"Error collecting recent content from Plex: {str(e)}", exc_info=True)
        return None

def is_anime(item):
    if 'Genre' in item:
        return any(genre.get('tag').lower() == 'anime' for genre in item['Genre'])
    return False

def filter_genres(genres):
    # If genres is a string representation of a list, convert it to a list
    if isinstance(genres, str):
        try:
            genres = ast.literal_eval(genres)
        except (ValueError, SyntaxError):
            # If it's not a valid list representation, treat it as a single genre
            genres = [genres]
    
    # Ensure genres is a list
    if not isinstance(genres, list):
        genres = [genres]
    
    # Convert all genres to lowercase strings and remove duplicates
    filtered = list(set(str(genre).strip().lower() for genre in genres if genre))
    return filtered

async def process_recent_movie(movie: Dict[str, Any]) -> List[Dict[str, Any]]:
    genres = [genre['tag'] for genre in movie.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(genres)
    logging.info(f"Movie: {movie['title']}")

    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie.get('addedAt', None),
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie',
        'genres': filtered_genres,
        'release_date': movie.get('originallyAvailableAt', None)
    }

    if 'addedAt' not in movie:
        logger.warning(f"'addedAt' field missing for movie: {movie['title']}. Movie data: {movie}")

    if 'Guid' in movie:
        for guid in movie['Guid']:
            if guid['id'].startswith('imdb://'):
                movie_data['imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                movie_data['tmdb_id'] = guid['id'].split('://')[1]

    if not movie_data['imdb_id'] and not movie_data['tmdb_id']:
        logger.warning(f"No IMDb ID or TMDB ID found for movie: {movie_data['title']}. Skipping metadata retrieval.")
        movie_data['release_date'] = None
    '''try:
        metadata = get_metadata(imdb_id=movie_data['imdb_id'], tmdb_id=movie_data['tmdb_id'], item_media_type='movie')
        if metadata:
            movie_data['release_date'] = get_release_date(metadata, movie_data['imdb_id'])
        else:
            movie_data['release_date'] = None
    except ValueError as e:
        logger.error(f"Error retrieving metadata for {movie_data['title']}: {str(e)}")
        movie_data['release_date'] = None'''

    movie_entries = []
    if 'Media' in movie:
        for media in movie['Media']:
            if 'Part' in media:
                for part in media['Part']:
                    file_path = part.get('file')
                    if file_path:
                        movie_entry = movie_data.copy()
                        movie_entry['location'] = file_path
                        movie_entries.append(movie_entry)
    
    if not movie_entries:
        logger.error(f"No filename found for movie: {movie['title']}")
    
    return movie_entries

async def process_recent_season(season: Dict[str, Any], show: Dict[str, Any], session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    show_imdb_id, show_tmdb_id = extract_show_ids(show)
    show_genres = filter_genres([genre['tag'] for genre in show.get('Genre', []) if 'tag' in genre])

    season_episodes_url = f"{plex_url}/library/metadata/{season['ratingKey']}/children?includeGuids=1"
    season_episodes_data = await fetch_data(session, season_episodes_url, headers, semaphore)

    processed_episodes = []
    if 'MediaContainer' in season_episodes_data and 'Metadata' in season_episodes_data['MediaContainer']:
        episodes = season_episodes_data['MediaContainer']['Metadata']
        for episode in episodes:
            processed_episode = await process_recent_episode(episode, show['title'], season['index'], show_imdb_id, show_tmdb_id, show)
            processed_episodes.append(processed_episode)

    return processed_episodes

async def process_recent_episode(episode: Dict[str, Any], show_title: str, season_number: int, show_imdb_id: str, show_tmdb_id: str, show: Dict[str, Any]) -> List[Dict[str, Any]]:
    show_genres = [genre['tag'] for genre in show.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(show_genres)
    logging.info(f"Episode: {show_title} - {episode['title']}")

    episode_data = {
        'title': show_title,
        'episode_title': episode['title'],
        'season_number': season_number,
        'episode_number': episode.get('index'),
        'year': episode.get('year'),
        'addedAt': episode['addedAt'],
        'guid': episode.get('guid'),
        'ratingKey': episode['ratingKey'],
        'release_date': episode.get('originallyAvailableAt'),
        'imdb_id': show_imdb_id,
        'tmdb_id': show_tmdb_id,
        'episode_imdb_id': None,
        'episode_tmdb_id': None,
        'type': 'episode',
        'genres': filter_genres(show_genres)
    }
    

    if 'Guid' in episode:
        for guid in episode['Guid']:
            if guid['id'].startswith('imdb://'):
                episode_data['episode_imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                episode_data['episode_tmdb_id'] = guid['id'].split('://')[1]

    episode_entries = []
    if 'Media' in episode:
        for media in episode['Media']:
            if 'Part' in media:
                for part in media['Part']:
                    file_path = part.get('file')
                    if file_path:
                        episode_entry = episode_data.copy()
                        episode_entry['location'] = file_path
                        episode_entries.append(episode_entry)
    
    if not episode_entries:
        logger.error(f"No filename found for episode: {show_title} - S{season_number:02d}E{episode.get('index', 'Unknown'):02d} - {episode['title']}")
    
    return episode_entries

def extract_show_ids(show_metadata):
    show_imdb_id = None
    show_tmdb_id = None
    if 'Guid' in show_metadata:
        for guid in show_metadata['Guid']:
            if guid['id'].startswith('imdb://'):
                show_imdb_id = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                show_tmdb_id = guid['id'].split('://')[1]
    return show_imdb_id, show_tmdb_id

async def run_get_recent_from_plex():
    logger.info("Starting run_get_recent_from_plex")
    result = await get_recent_from_plex()
    logger.info("Completed run_get_recent_from_plex")
    return result

def sync_run_get_recent_from_plex():
    return asyncio.run(run_get_recent_from_plex())

def remove_file_from_plex(item_title, item_path, episode_title=None):
    try:
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            logger.error("No Plex URL or token found in settings")
            return False
            
        plex = plexapi.server.PlexServer(plex_url, plex_token)
        
        logger.info(f"Searching for item with title: {item_title}, episode title: {episode_title}, and file name: {item_path}")
        
        sections = plex.library.sections()
        file_deleted = False
        max_retries = 3
        retry_delay = 1  # seconds
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"Retry attempt {attempt + 1}/{max_retries} for removing {item_title}")
                    time.sleep(retry_delay)
                
                for section in sections:
                    try:
                        if section.type == 'show':
                            # Extract show title from item_title (assuming format "Show Title_...")
                            show_title = item_title.split('_')[0]
                            
                            # Search for the show
                            shows = section.search(title=show_title)
                            
                            for show in shows:
                                # Get all episodes for the show
                                try:
                                    episodes = show.episodes()
                                except Exception as e:
                                    logger.error(f"Error getting episodes for show {show.title}: {str(e)}")
                                    continue
                                
                                for episode in episodes:
                                    if hasattr(episode, 'media'):
                                        for media in episode.media:
                                            for part in media.parts:
                                                if os.path.basename(part.file) == os.path.basename(item_path):
                                                    logger.info(f"Found matching file in episode: {episode.title}")
                                                    # Refresh media before deletion
                                                    try:
                                                        episode.refresh()
                                                        logger.info(f"Refreshed episode: {episode.title}")
                                                        # Wait for Plex to process the refresh
                                                        time.sleep(1)
                                                        media.delete()
                                                        logger.info(f"Successfully deleted media containing file: {part.file}")
                                                        file_deleted = True
                                                        return True
                                                    except Exception as e:
                                                        logger.error(f"Error during episode refresh/delete: {str(e)}")
                                                        if attempt == max_retries - 1:  # Last attempt
                                                            raise  # Re-raise on final attempt
                                                        continue
                                    else:
                                        logger.warning(f"No media found for episode: {episode.title}")
                        else:
                            # For movies and other types, use the existing search method
                            items = section.search(title=item_title)
                            
                            for item in items:
                                logger.info(f"Checking item: {item.title}")
                                if hasattr(item, 'media'):
                                    for media in item.media:
                                        for part in media.parts:
                                            logger.info(f"Checking file: {part.file}")
                                            if os.path.basename(part.file) == os.path.basename(item_path):
                                                logger.info(f"Found matching file in item: {item.title}")
                                                # Refresh media before deletion
                                                try:
                                                    item.refresh()
                                                    logger.info(f"Refreshed item: {item.title}")
                                                    # Wait for Plex to process the refresh
                                                    time.sleep(1)
                                                    media.delete()
                                                    logger.info(f"Successfully deleted media containing file: {part.file} from item: {item.title}")
                                                    file_deleted = True
                                                    return True
                                                except Exception as e:
                                                    logger.error(f"Error during item refresh/delete: {str(e)}")
                                                    if attempt == max_retries - 1:  # Last attempt
                                                        raise  # Re-raise on final attempt
                                                    continue
                                else:
                                    logger.warning(f"No media found for item: {item.title}")
                    except Exception as e:
                        logger.error(f"Error processing section {section.title}: {str(e)}")
                        if attempt == max_retries - 1:  # Last attempt
                            raise  # Re-raise on final attempt
                        continue
                
                if not file_deleted:
                    logger.warning(f"No matching files found in section {section.title} for title: {item_title}, episode title: {episode_title}")
                    if attempt < max_retries - 1:  # Not the last attempt
                        continue
                    
            except Exception as e:
                logger.error(f"Error during attempt {attempt + 1}: {str(e)}")
                if attempt == max_retries - 1:  # Last attempt
                    raise  # Re-raise on final attempt
                continue
                
        return file_deleted
            
    except Exception as e:
        logger.error(f"Error removing file from Plex: {str(e)}")
        return False

def remove_symlink_from_plex(item_title: str, item_path: str, episode_title: str = None) -> bool:
    """
    Remove a symlinked file from Plex without touching the symlink itself.
    This function assumes the symlink has already been or will be handled elsewhere.
    
    Args:
        item_title (str): The title of the show or movie
        item_path (str): The full path to the symlinked file
        episode_title (str, optional): The episode title for TV shows
        
    Returns:
        bool: True if successfully removed from Plex, False otherwise
    """
    max_retries = 3
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                logger.info(f"Retry attempt {attempt + 1}/{max_retries} for {item_title} - {episode_title}")
                time.sleep(retry_delay)
                
            # Get Plex connection details based on file management type
            if get_setting('File Management', 'file_collection_management') == 'Plex':
                plex_url = get_setting('Plex', 'url').rstrip('/')
                plex_token = get_setting('Plex', 'token')
            elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
                plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
            else:
                logger.error("No Plex URL or token found in settings")
                return False
                
            # Connect to Plex server
            plex = PlexServer(plex_url, plex_token)
            
            # Get all library sections
            sections = plex.library.sections()
            file_deleted = False
            
            for section in sections:
                try:
                    if section.type == 'show' and episode_title:
                        # For TV shows, we need to find the specific episode
                        shows = section.search(title=item_title)
                        
                        for show in shows:
                            episodes = show.episodes()
                            for episode in episodes:
                                if episode.title == episode_title:
                                    logger.info(f"Found matching episode: {episode.title}")
                                    logger.info(f"Episode rating key: {episode.ratingKey}")
                                    
                                    # Step 1: Unmatch the episode
                                    try:
                                        url = f"{plex_url}/library/metadata/{episode.ratingKey}/unmatch"
                                        headers = {'X-Plex-Token': plex_token}
                                        response = requests.put(url, headers=headers)
                                        logger.info(f"Unmatch response: {response.status_code}")
                                    except Exception as e:
                                        logger.error(f"Failed to unmatch episode: {str(e)}")
                                    
                                    # Step 2: Try to delete the media directly
                                    if hasattr(episode, 'media'):
                                        for media in episode.media:
                                            try:
                                                url = f"{plex_url}/library/metadata/{episode.ratingKey}/media/{media.id}"
                                                headers = {'X-Plex-Token': plex_token}
                                                response = requests.delete(url, headers=headers)
                                                logger.info(f"Delete media response: {response.status_code}")
                                            except Exception as e:
                                                logger.error(f"Failed to delete media: {str(e)}")
                                    
                                    # Step 3: Empty trash for the section
                                    try:
                                        url = f"{plex_url}/library/sections/{section.key}/emptyTrash"
                                        headers = {'X-Plex-Token': plex_token}
                                        response = requests.put(url, headers=headers)
                                        logger.info(f"Empty trash response: {response.status_code}")
                                    except Exception as e:
                                        logger.error(f"Failed to empty trash: {str(e)}")
                                    
                                    # Step 4: Analyze the section
                                    try:
                                        section.analyze()
                                        logger.info(f"Triggered analysis on section: {section.title}")
                                    except Exception as e:
                                        logger.error(f"Failed to analyze section: {str(e)}")
                                    
                                    # Step 5: Refresh the show
                                    try:
                                        show.refresh()
                                        logger.info(f"Triggered refresh on show: {show.title}")
                                        
                                        # Step 6: Verify if episode still exists
                                        time.sleep(2)  # Give Plex a moment to process
                                        try:
                                            episode.reload()
                                            # If we can still load the episode, continue to next retry
                                            logger.warning("Episode still exists in Plex, will retry if attempts remain")
                                            continue
                                        except:
                                            # If we can't load the episode, it was successfully removed
                                            logger.info("Successfully removed episode from Plex")
                                            return True
                                            
                                    except Exception as e:
                                        logger.error(f"Failed to refresh show: {str(e)}")
                                        continue
                    elif section.type == 'movie' and not episode_title:
                        # For movies
                        items = section.search(title=item_title)
                        
                        for item in items:
                            logger.info(f"Checking movie: {item.title}")
                            if hasattr(item, 'media'):
                                for media in item.media:
                                    for part in media.parts:
                                        logger.info(f"Checking file: {part.file}")
                                        if os.path.basename(part.file).lower() == os.path.basename(item_path).lower():
                                            logger.info(f"Found matching movie: {item.title}")
                                            logger.info(f"Movie rating key: {item.ratingKey}")
                                            
                                            # Step 1: Unmatch the movie
                                            try:
                                                url = f"{plex_url}/library/metadata/{item.ratingKey}/unmatch"
                                                headers = {'X-Plex-Token': plex_token}
                                                response = requests.put(url, headers=headers)
                                                logger.info(f"Unmatch response: {response.status_code}")
                                            except Exception as e:
                                                logger.error(f"Failed to unmatch movie: {str(e)}")
                                            
                                            # Step 2: Try to delete the media directly
                                            try:
                                                url = f"{plex_url}/library/metadata/{item.ratingKey}/media/{media.id}"
                                                headers = {'X-Plex-Token': plex_token}
                                                response = requests.delete(url, headers=headers)
                                                logger.info(f"Delete media response: {response.status_code}")
                                            except Exception as e:
                                                logger.error(f"Failed to delete media: {str(e)}")
                                            
                                            # Step 3: Empty trash for the section
                                            try:
                                                url = f"{plex_url}/library/sections/{section.key}/emptyTrash"
                                                headers = {'X-Plex-Token': plex_token}
                                                response = requests.put(url, headers=headers)
                                                logger.info(f"Empty trash response: {response.status_code}")
                                            except Exception as e:
                                                logger.error(f"Failed to empty trash: {str(e)}")
                                            
                                            # Step 4: Analyze the section
                                            try:
                                                section.analyze()
                                                logger.info(f"Triggered analysis on section: {section.title}")
                                            except Exception as e:
                                                logger.error(f"Failed to analyze section: {str(e)}")
                                            
                                            # Step 5: Verify if movie still exists
                                            time.sleep(2)  # Give Plex a moment to process
                                            try:
                                                item.reload()
                                                # If we can still load the movie, continue to next retry
                                                logger.warning("Movie still exists in Plex, will retry if attempts remain")
                                                continue
                                            except:
                                                # If we can't load the movie, it was successfully removed
                                                logger.info("Successfully removed movie from Plex")
                                                return True
                                                
                except Exception as e:
                    logger.error(f"Error processing section {section.title}: {str(e)}")
                    continue
            
            if not file_deleted:
                logger.warning(f"No matching {'episode' if episode_title else 'movie'} found in any Plex library section")
                continue  # Try next attempt if available
                
        except plexapi.exceptions.Unauthorized:
            logger.error("Unauthorized: Please check your Plex token")
            return False
        except plexapi.exceptions.NotFound:
            logger.error(f"Plex server not found at {plex_url}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error removing file from Plex: {str(e)}", exc_info=True)
            continue  # Try next attempt if available
    
    logger.error(f"Failed to remove {'episode' if episode_title else 'movie'} after {max_retries} attempts")
    return False

def plex_update_item(item: Dict[str, Any]) -> bool:
    try:
        plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        
        if not plex_url or not plex_token:
            logger.warning("Plex URL or token not configured for symlink")
            return False
            
        plex = PlexServer(plex_url, plex_token)
        
        # Use the full_path directly from the item data passed from verification
        file_location = item.get('full_path')
        if not file_location:
            # Fallback to database lookup if full_path is not in the item
            media_item = get_media_item_by_id(item.get('media_item_id') or item.get('id'))
            if media_item:
                file_location = media_item.get('location_on_disk')
            
        if not file_location:
            logger.error(f"No file location found for item: {item.get('title', 'Unknown')}")
            return False
            
        # Get the directory containing the file
        directory = os.path.dirname(file_location)
        
        # Find which section contains this directory
        for section in plex.library.sections():
            try:
                # Get the locations for this section
                for location in section.locations:
                    # Check if our directory is under this location
                    if directory.startswith(location):
                        logger.info(f"Found matching section {section.title}, scanning directory: {directory}")
                        # Scan the specific directory
                        section.update(path=directory)
                        return True
            except Exception as e:
                logger.error(f"Error checking section {section.title}: {str(e)}")
                continue
                
        logger.warning(f"Could not find matching library section for directory: {directory}")
        return False
        
    except Exception as e:
        logger.error(f"Error updating item in Plex: {str(e)}")
        return False
