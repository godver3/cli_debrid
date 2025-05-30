import asyncio
import aiohttp
import logging
from utilities.settings import get_setting
import time
from typing import Dict, List, Any, Tuple, Optional
import ast
import plexapi.server
import plexapi.exceptions
import plexapi.library
import os
from pathlib import Path
from cli_battery.app.direct_api import DirectAPI
from plexapi.server import PlexServer
from plexapi.library import LibrarySection
from database.database_reading import get_media_item_by_id
import requests
from plexapi.exceptions import NotFound

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 50
OPTIMAL_PAGE_SIZE = 2500
CHUNK_SIZE = 10
MAX_RETRIES = 3
RETRY_DELAY = 1

def process_library_names(library_names: str, all_libraries: dict, libraries_by_key: dict) -> list:
    """
    Process a comma-separated string of library names/ids and return their corresponding library keys.
    Handles both library names and numeric IDs, performing case-insensitive matching for names.
    
    Args:
        library_names: Comma-separated string of library names or IDs
        all_libraries: Dictionary mapping library names (case-sensitive) to keys
        libraries_by_key: Dictionary mapping library keys to names
        
    Returns:
        List of library keys
    """
    processed_keys = set() # Use a set to avoid duplicate keys if names overlap case-insensitively
    
    # Create a lower-case mapping for efficient case-insensitive lookup
    all_libraries_lower = {name.lower(): key for name, key in all_libraries.items()}
    
    settings_names = [name.strip() for name in library_names.split(',') if name.strip()]

    for name_or_id in settings_names:
        name_lower = name_or_id.lower()
        
        # Check case-insensitively against Plex library names
        if name_lower in all_libraries_lower:
            processed_keys.add(all_libraries_lower[name_lower])
        # Check if it's a direct library key/ID match
        elif name_or_id in libraries_by_key:
            processed_keys.add(name_or_id)
        else:
             # Optionally log a warning for names/IDs that don't match anything
             logger.warning(f"Library name or ID '{name_or_id}' from settings not found in Plex libraries.")

    return list(processed_keys)

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

async def get_library_contents(session: aiohttp.ClientSession, plex_url: str, library_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, page_size: int = OPTIMAL_PAGE_SIZE, item_type: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Fetches all metadata from a library section using pagination.
    Optionally filters by item type (e.g., 4 for episodes).
    Uses more robust pagination checks inspired by the test script.
    """
    all_metadata = []
    start_index = 0
    effective_page_size = max(1, page_size)

    while True:
        url = f"{plex_url}/library/sections/{library_key}/all?includeGuids=1"
        if item_type is not None:
            url += f"&type={item_type}"

        request_headers = headers.copy()
        request_headers['X-Plex-Container-Start'] = str(start_index)
        request_headers['X-Plex-Container-Size'] = str(effective_page_size)

        type_str = f" (Type={item_type})" if item_type is not None else ""
        logger.info(f"Fetching items from library {library_key}{type_str}, start: {start_index}, size: {effective_page_size}")
        data = await fetch_data(session, url, request_headers, semaphore)
        
        if 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
            metadata = data['MediaContainer']['Metadata']
            if metadata:
                all_metadata.extend(metadata)
                start_index += len(metadata)
                
                total_size = data['MediaContainer'].get('totalSize')
                size_attr = data['MediaContainer'].get('size')

                if total_size is not None and start_index >= int(total_size):
                    logger.info(f"Reached totalSize {total_size} for library {library_key}{type_str}")
                    break
                if len(metadata) < effective_page_size:
                    logger.info(f"Fetched last page (size {len(metadata)} < {effective_page_size}) for library {library_key}{type_str}")
                    break
                if size_attr == 0 and start_index > 0:
                     logger.info(f"Response size attribute is 0, assuming end of library {library_key}{type_str}")
                     break

            else:
                logger.info(f"No more metadata found for library {library_key}{type_str} at start index {start_index}")
                break
        else:
            logger.error(f"Failed to retrieve valid MediaContainer from library {library_key}{type_str} at start index {start_index}")
            break
            
    logger.info(f"Retrieved {len(all_metadata)} items in total from library {library_key}{type_str} (PageSize={effective_page_size})")
    return all_metadata

async def get_detailed_movie_metadata(session: aiohttp.ClientSession, plex_url: str, movie_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{movie_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else {}

async def get_detailed_show_metadata(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else {}

async def process_episode(episode_meta: Dict[str, Any], show_details: Dict[str, Any], fallback_show_metadata_cache: Dict[str, Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Processes a single episode's metadata using cached show details.
    Includes fallback logic for release date.
    """
    from cli_battery.app.direct_api import DirectAPI 

    show_title = show_details.get('title', 'Unknown Show')
    season_number = episode_meta.get('parentIndex')
    episode_number = episode_meta.get('index')
    show_year = show_details.get('year')
    show_imdb_id, show_tmdb_id = extract_show_ids(show_details)
    show_genres = filter_genres([genre.get('tag') for genre in show_details.get('Genre', []) if genre.get('tag')])

    if season_number is None:
        logger.error(f"Missing season number for episode {episode_meta.get('title')} in show {show_title}")
        return []

    release_date = episode_meta.get('originallyAvailableAt')

    base_episode_data = {
        'title': show_title,
        'episode_title': episode_meta.get('title', f'Episode {episode_number}'),
        'season_number': season_number,
        'episode_number': episode_number,
        'year': show_year,
        'show_year': show_year, 
        'addedAt': episode_meta.get('addedAt'),
        'guid': episode_meta.get('guid'),
        'ratingKey': episode_meta.get('ratingKey'),
        'grandparentRatingKey': episode_meta.get('grandparentRatingKey'),
        'release_date': release_date,
        'imdb_id': show_imdb_id,
        'tmdb_id': show_tmdb_id,
        'episode_imdb_id': None,
        'episode_tmdb_id': None,
        'type': 'episode',
        'genres': show_genres
    }
           
    if 'Guid' in episode_meta:
        for guid in episode_meta.get('Guid', []):
            if guid['id'].startswith('imdb://'):
                base_episode_data['episode_imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                base_episode_data['episode_tmdb_id'] = guid['id'].split('://')[1]
    
    if not base_episode_data['release_date']:
        # logger.warning(f"Plex missing originallyAvailableAt for {show_title} S{season_number}E{base_episode_data['episode_number']}. Attempting fallback lookup.")
        fallback_date_found = False
        retrieved_show_metadata = None

        try:
            if not fallback_date_found and show_imdb_id:
                if fallback_show_metadata_cache is not None and show_imdb_id in fallback_show_metadata_cache:
                    retrieved_show_metadata = fallback_show_metadata_cache[show_imdb_id]
                    # logger.debug(f"Using cached show metadata for {show_imdb_id} for fallback.")
                elif fallback_show_metadata_cache is not None:
                    # logger.debug(f"Fetching and caching show metadata for {show_imdb_id} for fallback.")
                    try:
                        metadata_result = DirectAPI.get_show_metadata(show_imdb_id)
                        if metadata_result and isinstance(metadata_result, tuple):
                            show_metadata_fetched = metadata_result[0]
                            if isinstance(show_metadata_fetched, dict):
                                retrieved_show_metadata = show_metadata_fetched
                            else:
                                logger.warning(f"DirectAPI metadata for {show_imdb_id} was not a dict: {type(show_metadata_fetched)}")
                                retrieved_show_metadata = None 
                        else:
                             logger.warning(f"DirectAPI.get_show_metadata returned unexpected result for {show_imdb_id}")
                             retrieved_show_metadata = None
                        fallback_show_metadata_cache[show_imdb_id] = retrieved_show_metadata
                    except Exception as fetch_err:
                         logger.error(f"Error fetching DirectAPI metadata for {show_imdb_id} during fallback: {str(fetch_err)}")
                         fallback_show_metadata_cache[show_imdb_id] = None
                         retrieved_show_metadata = None
                else:
                    logger.error("fallback_show_metadata_cache is None, cannot perform cached lookup.")
                    retrieved_show_metadata = None

                if retrieved_show_metadata:
                    seasons = retrieved_show_metadata.get('seasons', {})
                    if isinstance(seasons, dict):
                        season_data = seasons.get(str(season_number), {})
                        if season_data and isinstance(season_data, dict) and 'episodes' in season_data:
                            episodes_meta = season_data['episodes']
                            if isinstance(episodes_meta, dict):
                                for ep_num, ep_data in episodes_meta.items():
                                    if str(base_episode_data['episode_number']) == str(ep_num):
                                        first_aired = ep_data.get('first_aired') if isinstance(ep_data, dict) else None
                                        if first_aired:
                                            base_episode_data['release_date'] = first_aired[:10]
                                            fallback_date_found = True
                                            logger.info(f"Fallback successful using cached/fetched show metadata: set release_date to {base_episode_data['release_date']}")
                                        break
                    if not fallback_date_found:
                        # logger.warning(f"Failed to find episode S{season_number}E{base_episode_data['episode_number']} in cached/fetched show metadata for {show_imdb_id}.")
                        pass
                elif show_imdb_id:
                    logger.warning(f"No valid show metadata available (cache or fetch failed) for {show_imdb_id} to use for fallback.")

        except Exception as e:
            logger.error(f"Error during fallback release date retrieval for {show_title} S{season_number}E{base_episode_data['episode_number']}: {str(e)}")
        
        if not fallback_date_found:
             base_episode_data['release_date'] = None
             # logger.warning(f"All fallbacks failed, release_date remains None for {show_title} S{season_number}E{base_episode_data['episode_number']}")

    episode_entries = []
    if 'Media' in episode_meta and episode_meta['Media']:
        for media in episode_meta.get('Media', []):
            if 'Part' in media and media['Part']:
                for part in media.get('Part', []):
                    if 'file' in part:
                        episode_entry = base_episode_data.copy()
                        episode_entry['location'] = part['file']
                        episode_entries.append(episode_entry)
    
    if not episode_entries:
        ep_index_log = episode_meta.get('index', 'Unknown')
        try:
            ep_index_log = f"{int(ep_index_log):02d}" if ep_index_log != 'Unknown' else 'Unknown'
        except (ValueError, TypeError):
            ep_index_log = str(ep_index_log) 

        logger.error(f"No file path found for episode: {show_title} - S{season_number:02d}E{ep_index_log} - {base_episode_data['episode_title']}")
    
    return episode_entries

async def process_movies_chunk(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, movies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for movie in movies:
        movie_entries = await process_movie(movie)
        results.extend(movie_entries)
    return results

async def process_movie(movie: Dict[str, Any]) -> List[Dict[str, Any]]:
    from metadata.metadata import get_metadata, get_release_date
    genres = [genre['tag'] for genre in movie.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(genres)
    # logging.info(f"Movie: {movie['title']}")

    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie.get('addedAt'),
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie',
        'genres': filtered_genres,
        'release_date': movie.get('originallyAvailableAt')
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
        logger.error(f"No filename found for movie: {movie['title']}")
    
    # logger.debug(f"Processed {len(movie_entries)} entries for movie: {movie['title']}")
    return movie_entries

async def get_collected_from_plex(request='all', progress_callback=None, bypass=False,
                                page_size: int = OPTIMAL_PAGE_SIZE,
                                max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
                                specific_library_keys: List[str] = None,
                                scan_all_libraries: bool = False):
    start_time_total = time.perf_counter()
    logger.info(f"Starting Plex content collection. Request: {request}, PageSize: {page_size}, Concurrency: {max_concurrent_requests}, Libs: {specific_library_keys or ('All' if scan_all_libraries else 'From Settings')}")

    stats = {
        "libraries_to_process": 0,
        "movie_libs": 0,
        "show_libs": 0,
        "total_raw_episodes_fetched": 0,
        "total_raw_movies_fetched": 0,
        "unique_shows_found": 0,
        "show_detail_fetches_attempted": 0,
        "show_detail_fetches_succeeded": 0,
        "episodes_processed_count": 0,
        "episodes_skipped_missing_show_key": 0,
        "episodes_skipped_missing_show_details": 0,
        "movies_processed_count": 0,
        "movies_skipped": 0,
        "file_entries_generated_episodes": 0,
        "file_entries_generated_movies": 0,
        "time_connect_libs": 0.0,
        "time_fetch_movies": 0.0,
        "time_fetch_episodes": 0.0,
        "time_fetch_show_details": 0.0,
        "time_process_episodes": 0.0,
        "time_process_movies": 0.0,
        "time_total": 0.0,
    }

    if progress_callback: progress_callback('scanning', 'Connecting to Plex server...')

    try:
        plex_url = get_setting('Plex', 'url').rstrip('/')
        plex_token = get_setting('Plex', 'token')
    except Exception as e:
        logger.error(f"Failed to get Plex settings: {e}")
        if progress_callback: progress_callback('error', f'Failed to get Plex settings: {e}')
        return None

    headers = {
        'X-Plex-Token': plex_token,
        'Accept': 'application/json'
    }

    effective_concurrency = max(1, max_concurrent_requests)
    effective_page_size = max(1, page_size)
    semaphore = asyncio.Semaphore(effective_concurrency)

    all_processed_movies = []
    all_processed_episodes = []
    direct_api_show_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    async with aiohttp.ClientSession() as session:
        t_libs_start = time.perf_counter()
        if progress_callback: progress_callback('scanning', 'Retrieving library sections...')
        libraries_url = f"{plex_url}/library/sections"
        logger.debug(f"Fetching library sections from: {libraries_url}")
        libraries_data = await fetch_data(session, libraries_url, headers, semaphore)
        
        libraries_by_key = {str(library['key']): library['title'] for library in libraries_data['MediaContainer']['Directory']}
        all_libraries = {library['title']: str(library['key']) for library in libraries_data['MediaContainer']['Directory']}
        t_libs_end = time.perf_counter()
        stats["time_connect_libs"] = t_libs_end - t_libs_start

        movie_libraries = []
        show_libraries = []

        if scan_all_libraries:
             logger.info("Scan All Libraries requested. Identifying all Movie and Show libraries.")
             for library in libraries_data['MediaContainer']['Directory']:
                 lib_key = str(library.get('key'))
                 lib_type = library.get('type')
                 lib_title = library.get('title', 'Unknown')
                 if lib_type == 'movie':
                     movie_libraries.append(lib_key)
                     logger.debug(f"Including all-scan movie library: {lib_title} (Key: {lib_key})")
                 elif lib_type == 'show':
                     show_libraries.append(lib_key)
                     logger.debug(f"Including all-scan show library: {lib_title} (Key: {lib_key})")
        elif specific_library_keys:
             logger.info(f"Specific library keys provided: {specific_library_keys}. Overriding settings.")
             # Assume specific_library_keys contains only valid keys for movie/show libs for now
             # Or add logic here to check their type if needed
             # This part needs refinement based on how specific_library_keys is intended to be used with types
             # For now, assign all to both and let the content fetch handle it.
             # A better approach would be to fetch section details for each key.
             logger.warning("Specific library keys provided, assuming they are movie/show types. Type filtering during fetch will apply.")
             # Fetch section details to determine type
             all_sections_details = libraries_data['MediaContainer']['Directory']
             for key in specific_library_keys:
                 found = False
                 for section_detail in all_sections_details:
                     if str(section_detail.get('key')) == key:
                         if section_detail.get('type') == 'movie':
                             movie_libraries.append(key)
                             found = True
                             break
                         elif section_detail.get('type') == 'show':
                             show_libraries.append(key)
                             found = True
                             break
                 if not found:
                     logger.warning(f"Specific library key {key} not found or is not a movie/show library.")

        else:
             logger.info("Using libraries specified in settings.")
             movie_libraries = process_library_names(get_setting('Plex', 'movie_libraries', ''), all_libraries, libraries_by_key)
             show_libraries = process_library_names(get_setting('Plex', 'shows_libraries', ''), all_libraries, libraries_by_key)

        stats["movie_libs"] = len(movie_libraries)
        stats["show_libs"] = len(show_libraries)
        stats["libraries_to_process"] = stats["movie_libs"] + stats["show_libs"]

        logger.info(f"Identified {stats['movie_libs']} movie libraries to process: {movie_libraries}")
        logger.info(f"Identified {stats['show_libs']} show libraries to process: {show_libraries}")

        if progress_callback: progress_callback('scanning', 'Retrieving show library contents...')

        all_shows = []
        for library_key in show_libraries:
            shows = await get_library_contents(session, plex_url, library_key, headers, semaphore, page_size=effective_page_size)
            all_shows.extend(shows)
        
        if progress_callback: progress_callback('scanning', 'Retrieving movie library contents...')

        all_movies = []
        for library_key in movie_libraries:
            movies = await get_library_contents(session, plex_url, library_key, headers, semaphore, page_size=effective_page_size)
            all_movies.extend(movies)

        logger.info(f"Total shows found: {len(all_shows)}")
        logger.info(f"Total movies found: {len(all_movies)}")

        logger.info("Preparing to process shows...")

        if progress_callback:
            progress_callback('scanning', f'Processing {len(all_shows)} shows...', {
                'total_shows': len(all_shows),
                'total_movies': len(all_movies),
                'shows_processed': 0,
                'movies_processed': len(all_movies)
            })

        if movie_libraries:
            if progress_callback: progress_callback('scanning', f'Retrieving content from {len(movie_libraries)} movie libraries...')
            logger.info(f"Starting movie fetch from {len(movie_libraries)} libraries...")
            t_fetch_mov_start = time.perf_counter()
            all_raw_movies = []
            fetch_movie_tasks = [get_library_contents(session, plex_url, key, headers, semaphore, page_size=effective_page_size) for key in movie_libraries]
            library_movie_results = await asyncio.gather(*fetch_movie_tasks)
            for result in library_movie_results:
                all_raw_movies.extend(result)
            t_fetch_mov_end = time.perf_counter()
            stats["time_fetch_movies"] = t_fetch_mov_end - t_fetch_mov_start
            stats["total_raw_movies_fetched"] = len(all_raw_movies)
            logger.info(f"Fetched {stats['total_raw_movies_fetched']} raw movie metadata objects in {stats['time_fetch_movies']:.2f}s.")

            if all_raw_movies:
                if progress_callback: progress_callback('scanning', f'Processing {len(all_raw_movies)} movies...')
                logger.info(f"Starting processing for {len(all_raw_movies)} movies...")
                t_process_mov_start = time.perf_counter()

                movie_processing_tasks = []
                effective_movie_chunk_size = max(1, CHUNK_SIZE)
                for i in range(0, len(all_raw_movies), effective_movie_chunk_size):
                     chunk = all_raw_movies[i:i+effective_movie_chunk_size]
                     movie_processing_tasks.append(process_movies_chunk(session, plex_url, headers, semaphore, chunk))

                processed_movie_results = await asyncio.gather(*movie_processing_tasks)
                for result_list in processed_movie_results:
                     all_processed_movies.extend(result_list)

                t_process_mov_end = time.perf_counter()
                stats["time_process_movies"] = t_process_mov_end - t_process_mov_start
                stats["movies_processed_count"] = len(all_raw_movies)
                stats["file_entries_generated_movies"] = len(all_processed_movies)
                logger.info(f"Movie processing phase took {stats['time_process_movies']:.2f}s.")
            else:
                 logger.info("No movies found in specified libraries to process.")

        if show_libraries:
            if progress_callback: progress_callback('scanning', f'Retrieving episodes from {len(show_libraries)} show libraries...')
            logger.info(f"Starting episode fetch from {len(show_libraries)} libraries...")
            t_fetch_ep_start = time.perf_counter()
            all_raw_episodes = []
            fetch_tasks = [get_library_contents(session, plex_url, key, headers, semaphore, page_size=effective_page_size, item_type=4) for key in show_libraries]
            library_results = await asyncio.gather(*fetch_tasks)
            for result in library_results:
                all_raw_episodes.extend(result)
            t_fetch_ep_end = time.perf_counter()
            stats["time_fetch_episodes"] = t_fetch_ep_end - t_fetch_ep_start
            stats["total_raw_episodes_fetched"] = len(all_raw_episodes)
            logger.info(f"Fetched {stats['total_raw_episodes_fetched']} raw episode metadata objects in {stats['time_fetch_episodes']:.2f}s.")

            if not all_raw_episodes:
                logger.warning("No episodes found in the specified show libraries.")
            else:
                logger.info("Identifying unique shows from episodes...")
                t_fetch_show_start = time.perf_counter()
                unique_show_keys_to_fetch = set()
                show_details_cache: Dict[str, Optional[Dict[str, Any]]] = {}
                shows_processed_count = 0

                for episode_meta in all_raw_episodes:
                    show_key = episode_meta.get('grandparentRatingKey')
                    if show_key and show_key not in show_details_cache:
                         unique_show_keys_to_fetch.add(show_key)
                         show_details_cache[show_key] = None

                stats["unique_shows_found"] = len(unique_show_keys_to_fetch)
                stats["show_detail_fetches_attempted"] = len(unique_show_keys_to_fetch)
                logger.info(f"Found {stats['unique_shows_found']} unique shows requiring detail fetch.")

                fetch_detail_tasks = []
                if unique_show_keys_to_fetch:
                    logger.info(f"Creating tasks to fetch details for {len(unique_show_keys_to_fetch)} shows...")
                    for show_key in unique_show_keys_to_fetch:
                        fetch_detail_tasks.append(get_detailed_show_metadata(session, plex_url, show_key, headers, semaphore))

                    if fetch_detail_tasks:
                        logger.info(f"Fetching details for {len(fetch_detail_tasks)} shows concurrently...")
                        show_detail_results = await asyncio.gather(*fetch_detail_tasks)
                        logger.info("Finished fetching show details.")

                        if progress_callback:
                            progress_callback('scanning', f'Processing details for {len(fetch_detail_tasks)} shows...', {
                                'shows_processed': shows_processed_count,
                                'total_shows': stats["unique_shows_found"],
                                'total_movies': stats["total_raw_movies_fetched"],
                                'movies_processed': stats["movies_processed_count"]
                            })

                        successful_fetches = 0
                        for show_detail in show_detail_results:
                            if show_detail and 'ratingKey' in show_detail:
                                show_details_cache[show_detail['ratingKey']] = show_detail
                                successful_fetches += 1
                                shows_processed_count += 1

                        stats["show_detail_fetches_succeeded"] = successful_fetches
                        logger.info(f"Successfully fetched details for {successful_fetches}/{len(unique_show_keys_to_fetch)} shows. Processed count: {shows_processed_count}")
                else:
                     logger.info("No new show details needed.")
                t_fetch_show_end = time.perf_counter()
                stats["time_fetch_show_details"] = t_fetch_show_end - t_fetch_show_start
                logger.info(f"Show detail fetching phase took {stats['time_fetch_show_details']:.2f}s.")

                if progress_callback:
                    progress_callback('scanning', f'Processing {len(all_raw_episodes)} episodes...', {
                        'shows_processed': shows_processed_count,
                        'total_shows': stats["unique_shows_found"],
                        'total_movies': stats["total_raw_movies_fetched"],
                        'movies_processed': stats["movies_processed_count"]
                    })
                logger.info(f"Starting processing for {len(all_raw_episodes)} episodes...")
                t_process_ep_start = time.perf_counter()
                processing_tasks = []

                for episode_meta in all_raw_episodes:
                    show_key = episode_meta.get('grandparentRatingKey')
                    if not show_key:
                         logger.warning(f"Episode missing grandparentRatingKey: {episode_meta.get('title')} ratingKey {episode_meta.get('ratingKey')}")
                         stats["episodes_skipped_missing_show_key"] += 1
                         continue

                    cached_show_detail = show_details_cache.get(show_key)

                    if cached_show_detail:
                        processing_tasks.append(process_episode(episode_meta, cached_show_detail, direct_api_show_cache))
                    else:
                        logger.error(f"Missing show details in cache for show key {show_key} (Episode: {episode_meta.get('title')}) because fetch failed. Skipping episode.")
                        stats["episodes_skipped_missing_show_details"] += 1

                if processing_tasks:
                     logger.info(f"Processing {len(processing_tasks)} episodes concurrently...")
                     processed_episode_results = await asyncio.gather(*processing_tasks)
                     logger.info("Finished processing episodes.")

                     for result_list in processed_episode_results:
                         all_processed_episodes.extend(result_list)
                         if result_list:
                             stats["episodes_processed_count"] += 1

                     stats["file_entries_generated_episodes"] = len(all_processed_episodes)
                     stats["episodes_processed_count"] = len(processing_tasks)

                t_process_ep_end = time.perf_counter()
                stats["time_process_episodes"] = t_process_ep_end - t_process_ep_start
                logger.info(f"Episode processing phase took {stats['time_process_episodes']:.2f}s.")

        end_time_total = time.perf_counter()
        stats["time_total"] = end_time_total - start_time_total

        logger.info("--- Plex Collection Summary ---")
        logger.info(f"Libraries Processed:     {stats['libraries_to_process']} ({stats['movie_libs']} movie, {stats['show_libs']} show)")
        logger.info(f"Raw Movies Fetched:      {stats['total_raw_movies_fetched']}")
        logger.info(f"Raw Episodes Fetched:    {stats['total_raw_episodes_fetched']}")
        logger.info(f"Unique Shows Found:      {stats['unique_shows_found']}")
        logger.info(f"Show Detail Fetches:     {stats['show_detail_fetches_succeeded']} succeeded / {stats['show_detail_fetches_attempted']} attempted")
        logger.info(f"Movies Processed:        {stats['movies_processed_count']} (generating {stats['file_entries_generated_movies']} file entries)")
        logger.info(f"Episodes Processed:      {stats['episodes_processed_count']} (generating {stats['file_entries_generated_episodes']} file entries)")
        logger.info(f"Episodes Skipped:        {stats['episodes_skipped_missing_show_key']} (no show key) + {stats['episodes_skipped_missing_show_details']} (show details fetch failed)")
        logger.info("-" * 40)
        logger.info(f"Time - Connect & Libs:   {stats['time_connect_libs']:.2f}s")
        logger.info(f"Time - Fetch Movies:     {stats['time_fetch_movies']:.2f}s")
        logger.info(f"Time - Process Movies:   {stats['time_process_movies']:.2f}s")
        logger.info(f"Time - Fetch Episodes:   {stats['time_fetch_episodes']:.2f}s")
        logger.info(f"Time - Fetch Show Details:{stats['time_fetch_show_details']:.2f}s")
        logger.info(f"Time - Process Episodes: {stats['time_process_episodes']:.2f}s")
        logger.info(f"Time - Total Execution:  {stats['time_total']:.2f}s")
        logger.info("-" * 40)

        if not all_processed_movies and not all_processed_episodes:
            logger.warning("No content successfully processed from Plex scan.")
            if progress_callback: progress_callback('complete', 'Scan complete, no items found/processed.')
            return {'movies': [], 'episodes': []}

        if progress_callback:
            progress_callback('complete', 'Scan complete', {
                'total_movies': stats["total_raw_movies_fetched"],
                'movies_processed': stats["movies_processed_count"],
                'total_shows': stats["unique_shows_found"],
                'shows_processed': shows_processed_count,
                'total_episodes': stats["total_raw_episodes_fetched"],
                'episodes_processed': stats["episodes_processed_count"],
                'movies_found': stats["file_entries_generated_movies"],
                'episodes_found': stats["file_entries_generated_episodes"]
            })

        return {
            'movies': all_processed_movies,
            'episodes': all_processed_episodes
        }

async def run_get_collected_from_plex(request='all', progress_callback=None, bypass=False, **kwargs):
    logger.info(f"Starting run_get_collected_from_plex with kwargs: {kwargs}")
    allowed_kwargs = {'page_size', 'max_concurrent_requests', 'specific_library_keys', 'scan_all_libraries'}
    filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed_kwargs}
    result = await get_collected_from_plex(request, progress_callback, bypass, **filtered_kwargs)
    logger.info("Completed run_get_collected_from_plex")
    return result

def sync_run_get_collected_from_plex(request='all', progress_callback=None, bypass=False, **kwargs):
    logger.info(f"Starting sync_run_get_collected_from_plex with kwargs: {kwargs}")
    allowed_kwargs = {'page_size', 'max_concurrent_requests', 'specific_library_keys', 'scan_all_libraries'}
    filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed_kwargs}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(run_get_collected_from_plex(request, progress_callback, bypass, **filtered_kwargs))
    else:
        return loop.run_until_complete(run_get_collected_from_plex(request, progress_callback, bypass, **filtered_kwargs))

async def get_recent_from_plex(scan_all_libraries: bool = False):
    try:
        start_time = time.time()
        logger.info(f"Starting Plex recent content collection ({'All Libraries' if scan_all_libraries else 'Libraries from Settings'})")

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
            
            libraries_by_key = {str(library['key']): library['title'] for library in libraries_data['MediaContainer']['Directory']}
            all_libraries = {library['title']: str(library['key']) for library in libraries_data['MediaContainer']['Directory']}

            movie_libraries = []
            show_libraries = []

            if scan_all_libraries:
                 logger.info("Scan All Libraries requested for recent scan. Identifying all Movie and Show libraries.")
                 for library in libraries_data['MediaContainer']['Directory']:
                     lib_key = str(library.get('key'))
                     lib_type = library.get('type')
                     lib_title = library.get('title', 'Unknown')
                     if lib_type == 'movie':
                         movie_libraries.append(lib_key)
                         logger.debug(f"Including all-scan movie library: {lib_title} (Key: {lib_key})")
                     elif lib_type == 'show':
                         show_libraries.append(lib_key)
                         logger.debug(f"Including all-scan show library: {lib_title} (Key: {lib_key})")
            else:
                 logger.info("Using libraries specified in settings for recent scan.")
                 movie_libraries = process_library_names(get_setting('Plex', 'movie_libraries', ''), all_libraries, libraries_by_key)
                 show_libraries = process_library_names(get_setting('Plex', 'shows_libraries', ''), all_libraries, libraries_by_key)

            logger.info(f"Identified {len(movie_libraries)} movie libraries for recent scan: {movie_libraries}")
            logger.info(f"Identified {len(show_libraries)} show libraries for recent scan: {show_libraries}")

            processed_movies = []
            processed_episodes = []

            libraries_to_scan = movie_libraries + show_libraries
            if not libraries_to_scan:
                 logger.warning("No libraries identified to scan for recent items.")
                 return {'movies': [], 'episodes': []}

            for library_key in libraries_to_scan:
                library_title = libraries_by_key.get(library_key, f"Unknown Library (Key: {library_key})")
                logger.debug(f"Fetching recent items from library: {library_title} ({library_key})")
                recent_url = f"{plex_url}/library/sections/{library_key}/recentlyAdded"
                recent_data = await fetch_data(session, recent_url, headers, semaphore)

                if 'MediaContainer' in recent_data and 'Metadata' in recent_data['MediaContainer']:
                    recent_items = recent_data['MediaContainer']['Metadata']
                    logger.info(f"Retrieved {len(recent_items)} recent items from library {library_title}")

                    for item in recent_items:
                        item_type = item.get('type')
                        item_title_log = item.get('title', 'Unknown Item')
                        try:
                            if item_type == 'movie':
                                metadata_url = f"{plex_url}{item.get('key')}?includeGuids=1"
                                metadata = await fetch_data(session, metadata_url, headers, semaphore)
                                if 'MediaContainer' in metadata and 'Metadata' in metadata['MediaContainer']:
                                    full_metadata = metadata['MediaContainer']['Metadata'][0]
                                    processed_items = await process_recent_movie(full_metadata)
                                    processed_movies.extend(processed_items)
                                else:
                                    logger.warning(f"Could not fetch full metadata for recent movie: {item_title_log}")
                            elif item_type == 'season':
                                show_key = item.get('parentRatingKey')
                                if not show_key:
                                     logger.warning(f"Skipping recent season '{item_title_log}' due to missing parentRatingKey.")
                                     continue
                                show_metadata_url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
                                show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                                if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                    show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                    season_episodes = await process_recent_season(item, show_full_metadata, session, plex_url, headers, semaphore)
                                    for episode_list in season_episodes:
                                        processed_episodes.extend(episode_list)
                                else:
                                    logger.warning(f"Could not fetch show metadata for recent season: {item_title_log}")
                            elif item_type == 'episode':
                                show_key = item.get('grandparentRatingKey')
                                if not show_key:
                                     logger.warning(f"Skipping recent episode '{item_title_log}' due to missing grandparentRatingKey.")
                                     continue
                                show_metadata_url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
                                show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                                if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                    show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                    show_imdb_id, show_tmdb_id = extract_show_ids(show_full_metadata)
                                    episode_data = await process_recent_episode(item, show_full_metadata['title'], item.get('parentIndex'), show_imdb_id, show_tmdb_id, show_full_metadata)
                                    processed_episodes.extend(episode_data)
                                else:
                                    logger.warning(f"Could not fetch show metadata for recent episode: {item_title_log}")
                            else:
                                logger.debug(f"Skipping non-movie/season/episode recent item: {item_title_log} (Type: {item_type})")
                        except Exception as process_err:
                             logger.error(f"Error processing recent item '{item_title_log}' (Type: {item_type}): {process_err}", exc_info=True)

        end_time = time.time()
        total_time = end_time - start_time

        logger.info(f"Recent content collection complete. Total time: {total_time:.2f} seconds")
        logger.info(f"Collected: {len(processed_episodes)} episodes and {len(processed_movies)} movies")

        logger.debug(f"Final episodes list length: {len(processed_episodes)}")
        logger.debug(f"Final movies list length: {len(processed_movies)}")

        if not processed_movies and not processed_episodes:
            logger.warning("No content retrieved from Plex recent scan")
            return {'movies': [], 'episodes': []}

        return {
            'movies': processed_movies,
            'episodes': processed_episodes
        }
    except Exception as e:
        logger.error(f"Error collecting recent content from Plex: {str(e)}", exc_info=True)
        return None

def is_anime(item):
    if 'Genre' in item:
        return any(genre.get('tag').lower() == 'anime' for genre in item['Genre'] if isinstance(genre, dict))
    return False

def filter_genres(genres):
    if not isinstance(genres, list):
        genres = [genres] if genres else []
    filtered = list(set(str(genre).strip().lower() for genre in genres if genre))
    return filtered

async def process_recent_movie(movie: Dict[str, Any]) -> List[Dict[str, Any]]:
    from metadata.metadata import get_metadata, get_release_date
    genres = [genre['tag'] for genre in movie.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(genres)
    logging.info(f"Movie: {movie['title']}")

    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie.get('addedAt'),
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie',
        'genres': filtered_genres,
        'release_date': movie.get('originallyAvailableAt')
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

    movie_entries = []
    if 'Media' in movie and movie['Media']:
        for media in movie['Media']:
            if 'Part' in media and media['Part']:
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
    episode_number = episode.get('index')
    try:
        log_episode_number = f"{int(episode_number):02d}" if episode_number is not None else "Unknown"
    except (ValueError, TypeError):
        log_episode_number = "Invalid"
    try:
        log_season_number = f"{int(season_number):02d}" if season_number is not None else "Unknown"
    except (ValueError, TypeError):
        log_season_number = "Invalid"

    logging.info(f"Episode: {show_title} - S{log_season_number}E{log_episode_number} - {episode['title']}")

    episode_data = {
        'title': show_title,
        'episode_title': episode['title'],
        'season_number': season_number,
        'episode_number': episode.get('index'),
        'year': show.get('year'),
        'addedAt': episode['addedAt'],
        'guid': episode.get('guid'),
        'ratingKey': episode['ratingKey'],
        'grandparentRatingKey': episode.get('grandparentRatingKey'),
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

async def run_get_recent_from_plex(scan_all_libraries: bool = False):
    logger.info(f"Starting run_get_recent_from_plex (scan_all_libraries={scan_all_libraries})")
    result = await get_recent_from_plex(scan_all_libraries=scan_all_libraries)
    logger.info("Completed run_get_recent_from_plex")
    return result

def sync_run_get_recent_from_plex(scan_all_libraries: bool = False):
    logger.info(f"Starting sync_run_get_recent_from_plex (scan_all_libraries={scan_all_libraries})")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(run_get_recent_from_plex(scan_all_libraries=scan_all_libraries))
    else:
        return loop.run_until_complete(run_get_recent_from_plex(scan_all_libraries=scan_all_libraries))

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
        max_retries = 1
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"Retry attempt {attempt + 1}/{max_retries} for removing {item_title}")
                    time.sleep(retry_delay)
                
                for section in sections:
                    try:
                        if section.type == 'show':
                            shows = section.search(title=item_title)
                            
                            for show in shows:
                                episodes = show.episodes()
                                
                                for episode in episodes:
                                    if hasattr(episode, 'media'):
                                        for media in episode.media:
                                            for part in media.parts:
                                                if os.path.basename(part.file) == os.path.basename(item_path):
                                                    logger.info(f"Found matching file in episode: {episode.title}. Deleting media item.")
                                                    media.delete()
                                                    file_deleted = True
                                                    return True

                        elif section.type == 'movie':
                            movies = section.search(title=item_title)
                            for movie in movies:
                                if hasattr(movie, 'media'):
                                    for media in movie.media:
                                        for part in media.parts:
                                            if os.path.basename(part.file) == os.path.basename(item_path):
                                                logger.info(f"Found matching file in movie: {movie.title}. Deleting media item.")
                                                media.delete()
                                                file_deleted = True
                                                return True

                    except Exception as e:
                        item_name = getattr(show if section.type == 'show' else (movie if section.type == 'movie' else None), 'title', item_title)
                        logger.error(f"Error processing item {item_name} in section {section.title}: {str(e)}")
                        continue
                
                if file_deleted:
                     return True

                if attempt == max_retries - 1 and not file_deleted:
                    logger.warning(f"No matching file found after checking all relevant sections for title: {item_title}, file: {os.path.basename(item_path)}")

            except Exception as e:
                logger.error(f"Error during attempt {attempt + 1} for item {item_title}: {str(e)}")
                if attempt == max_retries - 1:
                     logger.error(f"Failed to remove file {os.path.basename(item_path)} for {item_title} after {max_retries} attempts.")
                     return False

        return file_deleted
            
    except Exception as e:
        logger.error(f"General error removing file from Plex for '{item_title}': {str(e)}")
        return False


def get_section_type(section: LibrarySection) -> Optional[str]:
    """Return the type of the Plex library section."""
    try:
        return section.type
    except Exception as e:
        logger.error(f"Error getting section type for section '{getattr(section, 'title', 'Unknown')}': {e}")
        return None

def find_plex_library_and_section(plex: PlexServer, item_path: str) -> Tuple[Optional[plexapi.library.Library], Optional[LibrarySection]]:
    """Find the Plex library and section containing the given item path."""
    try:
        # Ensure item_path is absolute and symlinks are resolved
        resolved_item_path = Path(item_path).resolve()
        sections = plex.library.sections()
        # Keep track of the best match (longest common path)
        best_match_section = None
        max_common_len = -1

        for section in sections:
            # Ensure section.locations exists and is iterable
            if not hasattr(section, 'locations') or not section.locations:
                 logger.debug(f"Section '{section.title}' has no locations defined. Skipping.")
                 continue

            for location in section.locations:
                try:
                    # Ensure location is absolute and symlinks are resolved
                    resolved_location = Path(location).resolve()

                    # Check if the item path is inside the location path
                    # Use Path.is_relative_to() for robust containment check
                    if resolved_item_path.is_relative_to(resolved_location):
                         # Calculate how much of the path matches (number of parts)
                         common_len = len(resolved_location.parts)
                         # If this is a more specific match (longer path) than previous ones, update
                         if common_len > max_common_len:
                             max_common_len = common_len
                             best_match_section = section
                             logger.debug(f"Potential match: Path '{item_path}' (resolved: {resolved_item_path}) is relative to location '{location}' (resolved: {resolved_location}) in section '{section.title}'")
                except ValueError as ve:
                    # is_relative_to raises ValueError if paths aren't comparable (e.g., different drives on Windows)
                    logger.debug(f"ValueError comparing paths: '{resolved_item_path}' and '{location}' (resolved: {resolved_location}). Skipping location. Error: {ve}")
                    continue
                except Exception as path_err:
                    logger.error(f"Error processing location '{location}' for section '{section.title}': {path_err}", exc_info=True)
                    continue # Skip this problematic location

        if best_match_section:
             logger.info(f"Found best match: Path '{item_path}' belongs to section '{best_match_section.title}'")
             return plex.library, best_match_section
        else:
            # This warning now correctly indicates no library root contained the item path
            logger.warning(f"Could not find Plex section containing path: {item_path} (Resolved: {resolved_item_path})")
            return None, None

    except NotFound:
         logger.error("Plex server connection issue: Library sections not found. Ensure Plex is running and connection details are correct.")
         return None, None
    except Exception as e:
        logger.error(f"Error finding Plex section for path '{item_path}': {e}", exc_info=True)
        return None, None

def plex_update_item(item: Dict[str, Any]) -> bool:
    logger.info(f"Attempting to trigger Plex scan for item: {item.get('title', 'Unknown')}")
    try:
        plex_url = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
        
        if not plex_url or not plex_token:
            logger.warning("Plex URL or token not configured for symlink updates.")
            return False
            
        plex = PlexServer(plex_url, plex_token, timeout=30)
        
        file_location = item.get('full_path') or item.get('location_on_disk') or item.get('location')
        if not file_location:
            logger.error(f"Cannot trigger update: No file location found for item: {item.get('title', 'Unknown')}")
            return False
            
        directory = os.path.dirname(file_location)
        
        found_matching_section = False
        for section in plex.library.sections():
            try:
                for location in section.locations:
                    if directory.startswith(location):
                        logger.info(f"Found matching section {section.title}, scanning directory: {directory}")
                        section.update(path=directory)
                        found_matching_section = True
                        return True  # Exit after finding and updating the first matching section
            except Exception as e:
                logger.error(f"Error checking section {section.title}: {str(e)}")
                continue
        
        if not found_matching_section:
            logger.warning(f"Could not find matching library section for directory: {directory}. Attempting to update all sections.")
            any_section_updated = False
            for section in plex.library.sections():
                try:
                    logger.info(f"Attempting update on section {section.title} for directory: {directory}")
                    section.update(path=directory)
                    any_section_updated = True # If any update call succeeds, mark as true.
                                               # We don't return immediately to try all sections.
                except Exception as e:
                    logger.error(f"Error updating section {section.title} with path {directory}: {str(e)}")
                    continue
            if any_section_updated:
                logger.info(f"Finished attempting update on all sections for directory: {directory}.")
                return True # Return true if at least one section update was attempted without specific error.
            else:
                logger.warning(f"No sections could be updated for directory: {directory}")
                return False
        
        return False # Should not be reached if logic is correct, but as a fallback.
        
    except Exception as e:
        logger.error(f"Error updating item in Plex via scan: {str(e)}")
        return False
