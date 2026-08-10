import asyncio
import aiohttp
import logging
from utilities.settings import get_setting
import time
from typing import Dict, List, Any, Tuple, Optional
import ast
import re
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
from concurrent.futures import ThreadPoolExecutor, TimeoutError

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 50  # Back to original but with small delays between batches
OPTIMAL_PAGE_SIZE = 500  # Reduced from 2500 for gentler scanning
EPISODE_BATCH_SIZE = 15  # Balanced batch size - prevents NAS timeouts while keeping good performance
EPISODE_BATCH_DELAY = 0.5  # Moderate delay between episode batches
CHUNK_SIZE = 10
MAX_RETRIES = 3

# Plex show GUID cache for fast ID-based lookups
# Maps ID -> show data (e.g., 'tt0074050' -> show_object)
_plex_show_cache: Dict[str, Any] = {}
_plex_show_cache_timestamp: Optional[float] = None
_SHOW_CACHE_TTL_HOURS = 6  # Refresh cache every 6 hours

# Plex movie GUID cache for fast ID-based lookups
# Maps ID -> movie data (e.g., 'tt0111161' -> movie_object)
_plex_movie_cache: Dict[str, Any] = {}
_plex_movie_cache_timestamp: Optional[float] = None
_MOVIE_CACHE_TTL_HOURS = 6  # Refresh cache every 6 hours
import threading as _threading
_plex_movie_cache_lock = _threading.Lock()  # Prevents concurrent cache refreshes

def normalize_plex_resolution(resolution: str) -> str:
    """
    Normalize Plex videoResolution to standard format.

    Plex returns values like "4k", "1080", "720", "sd" but we want standardized
    "p" format like "2160p", "1080p", etc.

    Backwards compatible: if already normalized (e.g., "2160p"), returns as-is.

    Args:
        resolution: Raw resolution from Plex videoResolution

    Returns:
        Normalized resolution string (e.g., "2160p", "1080p")
    """
    if not resolution:
        return None

    resolution_lower = resolution.lower().strip()

    # Normalization map for Plex-specific formats
    resolution_map = {
        '4k': '2160p',
        'uhd': '2160p',
        '8k': '4320p',
        '2k': '1440p',
        'qhd': '1440p',
        'fhd': '1080p',
        'hd': '720p',
        'sd': '480p',
        # Map numeric-only to standard "p" format
        '2160': '2160p',
        '1440': '1440p',
        '1080': '1080p',
        '720': '720p',
        '576': '576p',
        '480': '480p',
    }

    # Check if it needs normalization
    if resolution_lower in resolution_map:
        return resolution_map[resolution_lower]

    # Already in correct format (e.g., "2160p", "1080i") - return as-is
    # Supports both progressive (p) and interlaced (i) formats
    if re.match(r'^\d{3,4}[pi]$', resolution_lower):
        return resolution_lower

    # Unknown format - return original value
    logger.debug(f"Unknown resolution format from Plex: {resolution}")
    return resolution
RETRY_DELAY = 1
BATCH_DELAY = 0.2  # Small delay between batch fetches to reduce Plex CPU spikes

# HTTP timeout configuration for Plex API requests
# Prevents hanging when Plex stops responding, works with existing retry logic
PLEX_HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=None,      # No total timeout (allow long-running operations)
    connect=10,      # 10 seconds to establish connection
    sock_read=30     # 30 seconds max per read operation (prevents hanging)
)

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
             # Log a warning for names/IDs that don't match anything
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
    # ------------------------------------------------------------
    # 1. Get the first page (start=0) – this also gives us totalSize
    # ------------------------------------------------------------
    all_metadata: List[Dict[str, Any]] = []
    effective_page_size = max(1, page_size)

    base_url = f"{plex_url}/library/sections/{library_key}/all?includeGuids=1"
    if item_type is not None:
        base_url += f"&type={item_type}"

    type_str = f" (Type={item_type})" if item_type is not None else ""

    first_headers = headers.copy()
    first_headers['X-Plex-Container-Start'] = "0"
    first_headers['X-Plex-Container-Size'] = str(effective_page_size)

    logger.info(f"Fetching FIRST page from library {library_key}{type_str}, size: {effective_page_size}")
    first_page = await fetch_data(session, base_url, first_headers, semaphore)

    if 'MediaContainer' not in first_page or 'Metadata' not in first_page['MediaContainer']:
        logger.error(f"Failed to retrieve valid MediaContainer for first page of library {library_key}{type_str}")
        return []

    all_metadata.extend(first_page['MediaContainer'].get('Metadata', []))

    total_size = first_page['MediaContainer'].get('totalSize')

    # If totalSize is missing, fall back to old serial pagination logic ----------------
    if total_size is None:
        logger.warning(f"totalSize missing in response; falling back to serial pagination for library {library_key}{type_str}")

        start_index = len(all_metadata)
        while True:
            paged_headers = headers.copy()
            paged_headers['X-Plex-Container-Start'] = str(start_index)
            paged_headers['X-Plex-Container-Size'] = str(effective_page_size)

            logger.info(f"(Fallback) Fetching items from library {library_key}{type_str}, start: {start_index}, size: {effective_page_size}")
            page_data = await fetch_data(session, base_url, paged_headers, semaphore)

            if 'MediaContainer' in page_data and 'Metadata' in page_data['MediaContainer']:
                metadata = page_data['MediaContainer']['Metadata']
                if not metadata:
                    break
                all_metadata.extend(metadata)
                if len(metadata) < effective_page_size:
                    break
                start_index += len(metadata)
            else:
                break
        logger.info(f"Retrieved {len(all_metadata)} items in total from library {library_key}{type_str} (serial fallback)")
        return all_metadata

    # ------------------------------------------------------------
    # 2. Build list of remaining offsets and fetch them concurrently
    # ------------------------------------------------------------
    total_size = int(total_size)
    remaining_offsets = list(range(effective_page_size, total_size, effective_page_size))

    logger.info(f"Library {library_key}{type_str}: totalSize={total_size}, remaining pages={len(remaining_offsets)} (page_size={effective_page_size})")

    # Process pages in small batches with delays to reduce Plex CPU load
    if remaining_offsets:
        # Use smaller batch size for episodes to prevent overwhelming Plex on NAS systems
        # Episodes are more expensive than movies and can cause timeouts with high concurrency
        is_episode_library = item_type == 4
        batch_size = EPISODE_BATCH_SIZE if is_episode_library else MAX_CONCURRENT_REQUESTS
        batch_delay = EPISODE_BATCH_DELAY if is_episode_library else BATCH_DELAY

        total_batches = (len(remaining_offsets) + batch_size - 1) // batch_size
        logger.info(f"Fetching {len(remaining_offsets)} additional pages from library {library_key}{type_str} in {total_batches} batches of {batch_size} (episode_mode={is_episode_library})")

        for batch_num, batch_start in enumerate(range(0, len(remaining_offsets), batch_size), start=1):
            batch_offsets = remaining_offsets[batch_start:batch_start + batch_size]
            tasks = []
            for offset in batch_offsets:
                hdr = headers.copy()
                hdr['X-Plex-Container-Start'] = str(offset)
                hdr['X-Plex-Container-Size'] = str(effective_page_size)
                tasks.append(fetch_data(session, base_url, hdr, semaphore))

            results = await asyncio.gather(*tasks)
            for page_idx, page in enumerate(results, start=1):
                meta = page.get('MediaContainer', {}).get('Metadata') or []
                all_metadata.extend(meta)
                logger.debug(f"Batch {batch_num}/{total_batches}, page {page_idx}/{len(tasks)} for library {library_key}{type_str} returned {len(meta)} items")

            # Add delay between batches to let Plex breathe
            if batch_num < total_batches:
                logger.debug(f"Waiting {batch_delay}s before next batch...")
                await asyncio.sleep(batch_delay)

    logger.info(f"Retrieved {len(all_metadata)} items in total from library {library_key}{type_str} (Concurrent Pagination)")
    return all_metadata

async def get_detailed_movie_metadata(session: aiohttp.ClientSession, plex_url: str, movie_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{movie_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    result = data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] and data['MediaContainer']['Metadata'] else {}
    if result:
        has_media = 'Media' in result
        logger.debug(f"[DetailedMeta] Movie {movie_key}: has_media={has_media}, keys={list(result.keys())[:10]}")
    return result

async def get_detailed_show_metadata(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] and data['MediaContainer']['Metadata'] else {}

async def get_detailed_episode_metadata(session: aiohttp.ClientSession, plex_url: str, episode_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """Fetch detailed metadata for a single episode including Media/Part info."""
    url = f"{plex_url}/library/metadata/{episode_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    result = data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] and data['MediaContainer']['Metadata'] else {}
    if result:
        has_media = 'Media' in result
        if has_media and result['Media']:
            first_media = result['Media'][0]
            has_parts = 'Part' in first_media
            logger.debug(f"[DetailedMeta] Episode {episode_key}: has_media={has_media}, has_parts={has_parts}")
    return result

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
    
    # Check Battery for air date if Plex doesn't have it or if it's "Unknown"
    if not base_episode_data['release_date'] or str(base_episode_data['release_date']).lower() == 'unknown':
        # logger.warning(f"Plex missing/invalid originallyAvailableAt for {show_title} S{season_number}E{base_episode_data['episode_number']}. Attempting Battery fallback lookup.")
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
    media_list = episode_meta.get('Media', [])
    # Debug: Log Media count for all episodes
    if media_list:
        logger.debug(f"[process_episode] {show_title} S{season_number}E{base_episode_data.get('episode_number', '?')} has {len(media_list)} Media entries")
    if 'Media' in episode_meta and episode_meta['Media']:
        for media in episode_meta.get('Media', []):
            if 'Part' in media and media['Part']:
                for part in media.get('Part', []):
                    if 'file' in part:
                        episode_entry = base_episode_data.copy()
                        episode_entry['location'] = part['file']
                        # Extract file size and convert to GB
                        if 'size' in part:
                            try:
                                size_bytes = int(part['size'])
                                episode_entry['size_gb'] = round(size_bytes / (1024**3), 2)
                            except (ValueError, TypeError):
                                episode_entry['size_gb'] = None
                        else:
                            episode_entry['size_gb'] = None

                        # Fallback: Get size from filesystem if not available from Plex (symlink mode)
                        if episode_entry['size_gb'] is None and part.get('file'):
                            try:
                                file_management = get_setting('File Management', 'file_collection_management', default='Plex')
                                if file_management in ['Symlink', 'Symlinked/Local'] and os.path.exists(part['file']):
                                    size_bytes = os.path.getsize(part['file'])
                                    episode_entry['size_gb'] = round(size_bytes / (1024**3), 2) if size_bytes else None
                                    logger.debug(f"Got episode size from filesystem: {episode_entry['size_gb']}GB for {part['file']}")
                            except Exception as fs_error:
                                logger.debug(f"Could not get filesystem size for {part['file']}: {fs_error}")

                        # Extract resolution from Plex media
                        if hasattr(media, 'videoResolution') and media.videoResolution:
                            raw_resolution = media.videoResolution
                            normalized_resolution = normalize_plex_resolution(raw_resolution)
                            episode_entry['resolution'] = normalized_resolution
                            logger.debug(f"[ResolutionExtract] Episode S{season_number}E{episode_number}: raw={raw_resolution}, normalized={normalized_resolution}")
                        else:
                            episode_entry['resolution'] = None
                            logger.debug(f"[ResolutionExtract] Episode S{season_number}E{episode_number}: No videoResolution available")

                        episode_entries.append(episode_entry)

    if not episode_entries:
        ep_index_log = episode_meta.get('index', 'Unknown')
        try:
            ep_index_log = f"{int(ep_index_log):02d}" if ep_index_log != 'Unknown' else 'Unknown'
        except (ValueError, TypeError):
            ep_index_log = str(ep_index_log)

        logger.error(f"No file path found for episode: {show_title} - S{season_number:02d}E{ep_index_log} - {base_episode_data['episode_title']}")
    elif len(episode_entries) > 1:
        logger.info(f"[MultiVersion Debug] Episode '{show_title}' S{season_number:02d}E{base_episode_data.get('episode_number', '?')} has {len(episode_entries)} file entries: {[e.get('location') for e in episode_entries]}")

    return episode_entries

async def process_movies_chunk(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, movies: List[Dict[str, Any]], fetch_sizes: bool = False) -> List[Dict[str, Any]]:
    results = []
    detailed_map = {}

    # Only fetch detailed metadata (for size info) when fetch_sizes=True (backfill mode)
    if fetch_sizes:
        detail_tasks = []
        movies_with_keys = []  # Track which movies have keys for proper mapping

        for movie in movies:
            movie_key = movie.get('ratingKey')
            if movie_key:
                detail_tasks.append(get_detailed_movie_metadata(session, plex_url, movie_key, headers, semaphore))
                movies_with_keys.append(movie)

        # Fetch all detailed metadata in parallel
        if detail_tasks:
            detailed_results = await asyncio.gather(*detail_tasks, return_exceptions=True)

            # Create a mapping from ratingKey to detailed metadata
            for i, movie in enumerate(movies_with_keys):
                movie_key = movie.get('ratingKey')
                detailed = detailed_results[i]
                if not isinstance(detailed, Exception) and detailed:
                    detailed_map[movie_key] = detailed
                else:
                    if isinstance(detailed, Exception):
                        logger.warning(f"Failed to fetch detailed metadata for movie {movie.get('title', 'Unknown')}: {detailed}")

    # Process all movies, using detailed metadata when available
    for movie in movies:
        movie_key = movie.get('ratingKey')
        if movie_key and movie_key in detailed_map:
            movie_entries = await process_movie(detailed_map[movie_key])
        else:
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
                        # Extract file size and convert to GB
                        if 'size' in part:
                            try:
                                size_bytes = int(part['size'])
                                movie_entry['size_gb'] = round(size_bytes / (1024**3), 2)
                                logger.debug(f"[SizeExtract] Movie {movie_data['title']}: size_bytes={size_bytes}, size_gb={movie_entry['size_gb']}")
                            except (ValueError, TypeError):
                                movie_entry['size_gb'] = None
                                logger.debug(f"[SizeExtract] Movie {movie_data['title']}: Invalid size value in part")
                        else:
                            movie_entry['size_gb'] = None
                            logger.debug(f"[SizeExtract] Movie {movie_data['title']}: No 'size' key in part. Part keys: {list(part.keys())}")

                        # Fallback: Get size from filesystem if not available from Plex (symlink mode)
                        if movie_entry['size_gb'] is None and part.get('file'):
                            try:
                                file_management = get_setting('File Management', 'file_collection_management', default='Plex')
                                if file_management in ['Symlink', 'Symlinked/Local'] and os.path.exists(part['file']):
                                    size_bytes = os.path.getsize(part['file'])
                                    movie_entry['size_gb'] = round(size_bytes / (1024**3), 2) if size_bytes else None
                                    logger.debug(f"[SizeExtract-Filesystem] Movie {movie_data['title']}: Got size from filesystem: {movie_entry['size_gb']}GB")
                            except Exception as fs_error:
                                logger.debug(f"[SizeExtract-Filesystem] Movie {movie_data['title']}: Could not get filesystem size: {fs_error}")

                        # Extract resolution from Plex media
                        if hasattr(media, 'videoResolution') and media.videoResolution:
                            raw_resolution = media.videoResolution
                            normalized_resolution = normalize_plex_resolution(raw_resolution)
                            movie_entry['resolution'] = normalized_resolution
                            logger.debug(f"[ResolutionExtract] Movie {movie_data['title']}: raw={raw_resolution}, normalized={normalized_resolution}")
                        else:
                            movie_entry['resolution'] = None
                            logger.debug(f"[ResolutionExtract] Movie {movie_data['title']}: No videoResolution available")

                        movie_entries.append(movie_entry)

    if not movie_entries:
        logger.error(f"No filename found for movie: {movie['title']}")
    elif len(movie_entries) > 1:
        logger.info(f"[MultiVersion Debug] Movie '{movie_data['title']}' has {len(movie_entries)} file entries: {[e.get('location') for e in movie_entries]}")

    return movie_entries

async def get_collected_from_plex(request='all', progress_callback=None, bypass=False,
                                page_size: int = OPTIMAL_PAGE_SIZE,
                                max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
                                specific_library_keys: List[str] = None,
                                scan_all_libraries: bool = False,
                                fetch_sizes: bool = False):
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
    shows_processed_count = 0  # Initialize to prevent UnboundLocalError when no show libraries

    async with aiohttp.ClientSession(timeout=PLEX_HTTP_TIMEOUT) as session:
        t_libs_start = time.perf_counter()
        if progress_callback: progress_callback('scanning', 'Retrieving library sections...')
        libraries_url = f"{plex_url}/library/sections"
        logger.debug(f"Fetching library sections from: {libraries_url}")
        libraries_data = await fetch_data(session, libraries_url, headers, semaphore)

        libraries_by_key = {str(library['key']): library['title'] for library in libraries_data['MediaContainer']['Directory']}
        all_libraries = {library['title']: str(library['key']) for library in libraries_data['MediaContainer']['Directory']}
        logger.debug(f"all_libraries dict created: {all_libraries}")
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
             movie_libs_setting = get_setting('Plex', 'movie_libraries', '')
             shows_libs_setting = get_setting('Plex', 'shows_libraries', '')

             # FALLBACK: If settings are empty, scan all libraries instead of finding nothing
             if not movie_libs_setting.strip() and not shows_libs_setting.strip():
                 logger.warning("Both movie_libraries and shows_libraries settings are empty! Falling back to scan all libraries.")
                 logger.warning("Please configure your Plex library settings in Settings -> Plex -> Library Settings")
                 # Use scan all logic as fallback
                 for library in libraries_data['MediaContainer']['Directory']:
                     lib_key = str(library.get('key'))
                     lib_type = library.get('type')
                     lib_title = library.get('title', 'Unknown')
                     if lib_type == 'movie':
                         movie_libraries.append(lib_key)
                         logger.info(f"Auto-detected movie library: {lib_title} (Key: {lib_key})")
                     elif lib_type == 'show':
                         show_libraries.append(lib_key)
                         logger.info(f"Auto-detected show library: {lib_title} (Key: {lib_key})")
             else:
                 movie_libraries = process_library_names(movie_libs_setting, all_libraries, libraries_by_key)
                 show_libraries = process_library_names(shows_libs_setting, all_libraries, libraries_by_key)

        stats["movie_libs"] = len(movie_libraries)
        stats["show_libs"] = len(show_libraries)
        stats["libraries_to_process"] = stats["movie_libs"] + stats["show_libs"]

        logger.info(f"Identified {stats['movie_libs']} movie libraries to process: {movie_libraries}")
        logger.info(f"Identified {stats['show_libs']} show libraries to process: {show_libraries}")

        if progress_callback:
            progress_callback('scanning', f'Pre-processing phase: fetched {len(movie_libraries)} movie libraries. Episode scan will follow.', {
                'total_shows': 'unknown',
                'total_movies': len(movie_libraries),
                'shows_processed': 0,
                'movies_processed': 0
            })

        # We skip an upfront fetch of show-level items (which was previously only used for a count)
        # because we only require episode-level metadata later. This saves an extra pass through the
        # library. We simply initialise the list so existing progress-reporting logic continues to work.
        all_shows = []  # full show objects will be discovered implicitly during the episode fetch.

        if progress_callback: progress_callback('scanning', 'Retrieving movie library contents...')

        # Initial one-time fetch of movie metadata (reuse later for processing)
        all_movies = []
        for lib_idx, library_key in enumerate(movie_libraries):
            movies = await get_library_contents(session, plex_url, library_key, headers, semaphore, page_size=effective_page_size)
            # Tag each item with its source library so collected_items.py can protect
            # primary-library fields (location_on_disk, ms_item_id) from being overwritten
            # by secondary libraries that may share the same physical files.
            for m in movies:
                m['_plex_library_key'] = library_key
                m['_plex_library_primary'] = (lib_idx == 0)
            all_movies.extend(movies)

        if progress_callback:
            progress_callback('scanning', f'Retrieved {len(all_movies)} movies; proceeding to episode libraries...', {
                'total_shows': 'unknown',
                'total_movies': len(all_movies),
                'shows_processed': 0,
                'movies_processed': 0
            })

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
            # We already retrieved movie metadata earlier (all_movies). Re-use it instead of fetching again.
            logger.info(f"Using previously gathered movie metadata from {len(movie_libraries)} movie libraries (items: {len(all_movies)}). Skipping redundant fetch.")

            all_raw_movies = all_movies  # reuse earlier results
            stats["total_raw_movies_fetched"] = len(all_raw_movies)
            # We did not measure time separately for this earlier serial fetch; set to 0 to avoid skewing summary.
            stats.setdefault("time_fetch_movies", 0.0)
            if progress_callback:
                progress_callback('scanning', f'Processing {len(all_raw_movies)} movies...')

            if all_raw_movies:
                if progress_callback: progress_callback('scanning', f'Processing {len(all_raw_movies)} movies...')
                logger.info(f"Starting processing for {len(all_raw_movies)} movies...")
                t_process_mov_start = time.perf_counter()

                movie_processing_tasks = []
                effective_movie_chunk_size = max(1, CHUNK_SIZE)
                for i in range(0, len(all_raw_movies), effective_movie_chunk_size):
                     chunk = all_raw_movies[i:i+effective_movie_chunk_size]
                     movie_processing_tasks.append(process_movies_chunk(session, plex_url, headers, semaphore, chunk, fetch_sizes=fetch_sizes))

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
            for lib_idx, result in enumerate(library_results):
                lib_key = show_libraries[lib_idx]
                for ep in result:
                    ep['_plex_library_key'] = lib_key
                    ep['_plex_library_primary'] = (lib_idx == 0)
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

                # Only fetch detailed episode metadata (for size info) when fetch_sizes=True (backfill mode)
                if fetch_sizes:
                    logger.info(f"Fetching detailed metadata for {len(all_raw_episodes)} episodes to get file size info...")
                    episodes_with_keys = []  # Track episodes with keys for proper mapping

                    for episode_meta in all_raw_episodes:
                        episode_key = episode_meta.get('ratingKey')
                        if episode_key:
                            episodes_with_keys.append(episode_meta)

                    # Process episode detail fetches in batches with delays to reduce Plex CPU load
                    if episodes_with_keys:
                        batch_size = MAX_CONCURRENT_REQUESTS
                        total_batches = (len(episodes_with_keys) + batch_size - 1) // batch_size
                        logger.info(f"Fetching details for {len(episodes_with_keys)} episodes in {total_batches} batches of {batch_size}")

                        all_detailed_episodes = []
                        for batch_num, batch_start in enumerate(range(0, len(episodes_with_keys), batch_size), start=1):
                            batch_episodes = episodes_with_keys[batch_start:batch_start + batch_size]
                            batch_tasks = []
                            for episode_meta in batch_episodes:
                                episode_key = episode_meta.get('ratingKey')
                                batch_tasks.append(get_detailed_episode_metadata(session, plex_url, episode_key, headers, semaphore))

                            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                            all_detailed_episodes.extend(batch_results)

                            if batch_num % 100 == 0:
                                logger.info(f"Episode detail fetch progress: {batch_num}/{total_batches} batches")

                            # Add delay between batches to let Plex breathe
                            if batch_num < total_batches:
                                await asyncio.sleep(BATCH_DELAY)

                        # Merge detailed metadata back into episode_meta using proper mapping
                        for i, episode_meta in enumerate(episodes_with_keys):
                            detailed = all_detailed_episodes[i]
                            if not isinstance(detailed, Exception) and detailed and 'Media' in detailed:
                                episode_meta['Media'] = detailed['Media']
                                # Debug: Log multi-file episodes
                                media_count = len(detailed['Media'])
                                if media_count > 1:
                                    ep_title = episode_meta.get('grandparentTitle', 'Unknown')
                                    ep_season = episode_meta.get('parentIndex', '?')
                                    ep_num = episode_meta.get('index', '?')
                                    logger.info(f"[DetailedMeta] Episode '{ep_title}' S{ep_season}E{ep_num} has {media_count} Media entries from detailed fetch")
                            # If detailed fetch failed, episode_meta won't have Media, which is fine - size will be None

                    logger.info(f"Finished fetching detailed episode metadata. Processing episodes...")
                else:
                    logger.info(f"Skipping detailed episode metadata fetch (fetch_sizes=False). Processing episodes...")

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
    allowed_kwargs = {'page_size', 'max_concurrent_requests', 'specific_library_keys', 'scan_all_libraries', 'fetch_sizes'}
    filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed_kwargs}
    result = await get_collected_from_plex(request, progress_callback, bypass, **filtered_kwargs)
    logger.info("Completed run_get_collected_from_plex")
    return result

def sync_run_get_collected_from_plex(request='all', progress_callback=None, bypass=False, **kwargs):
    logger.info(f"Starting sync_run_get_collected_from_plex with kwargs: {kwargs}")
    allowed_kwargs = {'page_size', 'max_concurrent_requests', 'specific_library_keys', 'scan_all_libraries', 'fetch_sizes'}
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

        # FIX: Cache show metadata to avoid fetching the same show multiple times
        # This prevents fetching the same show 100 times for 100 episodes of that show
        show_metadata_cache: Dict[str, Optional[Dict[str, Any]]] = {}

        async with aiohttp.ClientSession(timeout=PLEX_HTTP_TIMEOUT) as session:
            libraries_url = f"{plex_url}/library/sections"
            libraries_data = await fetch_data(session, libraries_url, headers, semaphore)

            plex_directories = libraries_data.get('MediaContainer', {}).get('Directory', [])
            if not plex_directories:
                logger.warning("No Plex libraries returned (Plex may be unavailable or timing out) — aborting recent scan.")
                return {'movies': [], 'episodes': []}

            libraries_by_key = {str(library['key']): library['title'] for library in plex_directories}
            all_libraries = {library['title']: str(library['key']) for library in plex_directories}

            movie_libraries = []
            show_libraries = []

            if scan_all_libraries:
                 logger.info("Scan All Libraries requested for recent scan. Identifying all Movie and Show libraries.")
                 for library in plex_directories:
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
                recent_url = f"{plex_url}/library/sections/{library_key}/recentlyAdded?X-Plex-Container-Size=200"
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
                                if 'MediaContainer' in metadata and 'Metadata' in metadata['MediaContainer'] and metadata['MediaContainer']['Metadata']:
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
                                # FIX: Use cached show metadata if available
                                if show_key in show_metadata_cache:
                                    show_full_metadata = show_metadata_cache[show_key]
                                    if show_full_metadata is None:
                                        logger.warning(f"Skipping season '{item_title_log}' - show metadata previously failed to fetch")
                                        continue
                                else:
                                    show_metadata_url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
                                    show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                                    if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer'] and show_metadata['MediaContainer']['Metadata']:
                                        show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                        show_metadata_cache[show_key] = show_full_metadata
                                    else:
                                        logger.warning(f"Could not fetch show metadata for recent season: {item_title_log}")
                                        show_metadata_cache[show_key] = None
                                        continue
                                season_episodes = await process_recent_season(item, show_full_metadata, session, plex_url, headers, semaphore)
                                for episode_list in season_episodes:
                                    processed_episodes.extend(episode_list)
                            elif item_type == 'episode':
                                show_key = item.get('grandparentRatingKey')
                                if not show_key:
                                     logger.warning(f"Skipping recent episode '{item_title_log}' due to missing grandparentRatingKey.")
                                     continue
                                # FIX: Use cached show metadata if available
                                if show_key in show_metadata_cache:
                                    show_full_metadata = show_metadata_cache[show_key]
                                    if show_full_metadata is None:
                                        logger.warning(f"Skipping episode '{item_title_log}' - show metadata previously failed to fetch")
                                        continue
                                else:
                                    show_metadata_url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
                                    show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                                    if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer'] and show_metadata['MediaContainer']['Metadata']:
                                        show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                        show_metadata_cache[show_key] = show_full_metadata
                                    else:
                                        logger.warning(f"Could not fetch show metadata for recent episode: {item_title_log}")
                                        show_metadata_cache[show_key] = None
                                        continue
                                show_imdb_id, show_tmdb_id = extract_show_ids(show_full_metadata)
                                episode_data = await process_recent_episode(item, show_full_metadata['title'], item.get('parentIndex'), show_imdb_id, show_tmdb_id, show_full_metadata)
                                processed_episodes.extend(episode_data)
                            else:
                                logger.debug(f"Skipping non-movie/season/episode recent item: {item_title_log} (Type: {item_type})")
                        except Exception as process_err:
                             logger.error(f"Error processing recent item '{item_title_log}' (Type: {item_type}): {process_err}", exc_info=True)

            # Log cache effectiveness
            if show_metadata_cache:
                logger.info(f"[RecentScan] Show metadata cache: {len(show_metadata_cache)} unique shows cached (avoided redundant API calls)")

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
        # Fallback: search Battery (Trakt) by title+year — same source used for file naming
        try:
            from cli_battery.app.direct_api import DirectAPI
            _title = movie_data['title']
            _year = movie_data.get('year')
            _results, _source = DirectAPI.search_media(_title, year=_year, media_type='movie')
            if _results:
                _first = _results[0] if isinstance(_results[0], dict) else None
                if _first:
                    movie_data['imdb_id'] = _first.get('imdb_id')
                    movie_data['tmdb_id'] = str(_first.get('tmdb_id')) if _first.get('tmdb_id') else None
                    if movie_data['imdb_id']:
                        logger.info(f"Resolved IMDb ID {movie_data['imdb_id']} for '{_title}' ({_year}) via Battery title search")
                    else:
                        logger.warning(f"Battery title search returned no IMDb ID for '{_title}' ({_year})")
                else:
                    logger.warning(f"Battery title search returned unexpected result type for '{_title}' ({_year}): {type(_results[0])}")
            else:
                logger.warning(f"No IMDb ID or TMDB ID found for movie: {movie_data['title']}. Battery title search also returned no results.")
                movie_data['release_date'] = None
        except Exception as _e:
            logger.warning(f"No IMDb ID or TMDB ID found for movie: {movie_data['title']}. Battery fallback failed: {_e}")
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
                        # Extract file size and convert to GB
                        if 'size' in part:
                            try:
                                size_bytes = int(part['size'])
                                movie_entry['size_gb'] = round(size_bytes / (1024**3), 2)
                            except (ValueError, TypeError):
                                movie_entry['size_gb'] = None
                        else:
                            movie_entry['size_gb'] = None

                        # Fallback: Get size from filesystem if not available from Plex (symlink mode)
                        if movie_entry['size_gb'] is None and file_path:
                            try:
                                file_management = get_setting('File Management', 'file_collection_management', default='Plex')
                                if file_management in ['Symlink', 'Symlinked/Local'] and os.path.exists(file_path):
                                    size_bytes = os.path.getsize(file_path)
                                    movie_entry['size_gb'] = round(size_bytes / (1024**3), 2) if size_bytes else None
                                    logger.debug(f"Got movie size from filesystem: {movie_entry['size_gb']}GB for {file_path}")
                            except Exception as fs_error:
                                logger.debug(f"Could not get filesystem size for {file_path}: {fs_error}")

                        # Extract resolution from Plex media
                        if 'videoResolution' in media:
                            raw_resolution = media.get('videoResolution')
                            normalized_resolution = normalize_plex_resolution(raw_resolution)
                            movie_entry['resolution'] = normalized_resolution
                            logger.debug(f"[ResolutionExtract-Recent] Movie {movie_data['title']}: raw={raw_resolution}, normalized={normalized_resolution}")
                        else:
                            movie_entry['resolution'] = None
                            logger.debug(f"[ResolutionExtract-Recent] Movie {movie_data['title']}: No videoResolution available")

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
    show_year = show.get('year')
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
        'year': show_year,
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
                        # Extract file size and convert to GB
                        if 'size' in part:
                            try:
                                size_bytes = int(part['size'])
                                episode_entry['size_gb'] = round(size_bytes / (1024**3), 2)
                            except (ValueError, TypeError):
                                episode_entry['size_gb'] = None
                        else:
                            episode_entry['size_gb'] = None

                        # Fallback: Get size from filesystem if not available from Plex (symlink mode)
                        if episode_entry['size_gb'] is None and file_path:
                            try:
                                file_management = get_setting('File Management', 'file_collection_management', default='Plex')
                                if file_management in ['Symlink', 'Symlinked/Local'] and os.path.exists(file_path):
                                    size_bytes = os.path.getsize(file_path)
                                    episode_entry['size_gb'] = round(size_bytes / (1024**3), 2) if size_bytes else None
                                    logger.debug(f"Got episode size from filesystem: {episode_entry['size_gb']}GB for {file_path}")
                            except Exception as fs_error:
                                logger.debug(f"Could not get filesystem size for {file_path}: {fs_error}")

                        # Extract resolution from Plex media
                        if 'videoResolution' in media:
                            raw_resolution = media.get('videoResolution')
                            normalized_resolution = normalize_plex_resolution(raw_resolution)
                            episode_entry['resolution'] = normalized_resolution
                            logger.debug(f"[ResolutionExtract-Recent] Episode S{season_number}E{episode_number}: raw={raw_resolution}, normalized={normalized_resolution}")
                        else:
                            episode_entry['resolution'] = None
                            logger.debug(f"[ResolutionExtract-Recent] Episode S{season_number}E{episode_number}: No videoResolution available")

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


def remove_show_from_plex(show_title: str, imdb_id: str = None, tmdb_id: str = None) -> dict:
    """
    Remove an entire show from Plex library (1 API call instead of per-episode).

    Args:
        show_title: Title of the show to remove
        imdb_id: Optional IMDB ID for more accurate matching
        tmdb_id: Optional TMDB ID for more accurate matching

    Returns:
        dict with 'success', 'show_deleted', 'error'
    """
    result = {
        'success': False,
        'show_deleted': False,
        'error': None
    }

    try:
        # Check for Jellyfin configuration first
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()

        if jellyfin_url and jellyfin_token:
            logger.info(f"Jellyfin configured - show-level deletion not yet implemented for Jellyfin")
            result['error'] = "Jellyfin show deletion not implemented"
            return result

        # Get Plex connection
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            result['error'] = "No Plex configuration found"
            return result

        if not plex_url or not plex_token:
            result['error'] = "Plex URL or token is empty"
            return result

        plex = plexapi.server.PlexServer(plex_url, plex_token, timeout=30)
        sections = plex.library.sections()

        logger.info(f"[PLEX_SHOW_DELETE] Searching for show: {show_title} (imdb={imdb_id}, tmdb={tmdb_id})")

        for section in sections:
            if section.type != 'show':
                continue

            try:
                shows = section.search(title=show_title)

                for show in shows:
                    # Try to match by ID if provided
                    matched = False

                    if imdb_id or tmdb_id:
                        # Check GUIDs for ID match
                        if hasattr(show, 'guids'):
                            for guid in show.guids:
                                guid_str = str(guid.id) if hasattr(guid, 'id') else str(guid)
                                if imdb_id and f'imdb://{imdb_id}' in guid_str:
                                    matched = True
                                    break
                                if tmdb_id and f'tmdb://{tmdb_id}' in guid_str:
                                    matched = True
                                    break

                    # If no IDs provided or matched by ID, also check title match
                    if not (imdb_id or tmdb_id) or matched:
                        if show.title.lower() == show_title.lower() or matched:
                            logger.info(f"[PLEX_SHOW_DELETE] Found show '{show.title}' in section '{section.title}'. Deleting entire show.")
                            try:
                                show.delete()
                                result['success'] = True
                                result['show_deleted'] = True
                                logger.info(f"[PLEX_SHOW_DELETE] Successfully deleted show '{show.title}' from Plex")
                                return result
                            except Exception as del_err:
                                error_str = str(del_err)
                                if '400' in error_str or 'bad_request' in error_str.lower():
                                    logger.warning(f"[PLEX_SHOW_DELETE] 400 Bad Request for show '{show.title}'. "
                                                  f"'Allow media deletion' may be disabled in Plex settings.")
                                    result['error'] = "Plex deletion disabled (400 Bad Request)"
                                else:
                                    logger.error(f"[PLEX_SHOW_DELETE] Error deleting show '{show.title}': {error_str}")
                                    result['error'] = str(del_err)
                                return result

            except Exception as e:
                logger.error(f"[PLEX_SHOW_DELETE] Error searching section '{section.title}': {str(e)}")
                continue

        logger.warning(f"[PLEX_SHOW_DELETE] Show '{show_title}' not found in any Plex library")
        result['error'] = f"Show '{show_title}' not found in Plex"
        return result

    except Exception as e:
        logger.error(f"[PLEX_SHOW_DELETE] General error removing show '{show_title}': {str(e)}")
        result['error'] = str(e)
        return result


def remove_season_from_plex(show_title: str, season_number: int, imdb_id: str = None, tmdb_id: str = None) -> dict:
    """
    Remove an entire season from Plex library (1 API call instead of per-episode).

    Args:
        show_title: Title of the show
        season_number: Season number to remove
        imdb_id: Optional IMDB ID for more accurate matching
        tmdb_id: Optional TMDB ID for more accurate matching

    Returns:
        dict with 'success', 'season_deleted', 'error'
    """
    result = {
        'success': False,
        'season_deleted': False,
        'error': None
    }

    try:
        # Check for Jellyfin configuration first
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()

        if jellyfin_url and jellyfin_token:
            logger.info(f"Jellyfin configured - season-level deletion not yet implemented for Jellyfin")
            result['error'] = "Jellyfin season deletion not implemented"
            return result

        # Get Plex connection
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            result['error'] = "No Plex configuration found"
            return result

        if not plex_url or not plex_token:
            result['error'] = "Plex URL or token is empty"
            return result

        plex = plexapi.server.PlexServer(plex_url, plex_token, timeout=30)
        sections = plex.library.sections()

        logger.info(f"[PLEX_SEASON_DELETE] Searching for show: {show_title} S{season_number:02d} (imdb={imdb_id}, tmdb={tmdb_id})")

        for section in sections:
            if section.type != 'show':
                continue

            try:
                shows = section.search(title=show_title)

                for show in shows:
                    # Try to match by ID if provided
                    matched = False

                    if imdb_id or tmdb_id:
                        # Check GUIDs for ID match
                        if hasattr(show, 'guids'):
                            for guid in show.guids:
                                guid_str = str(guid.id) if hasattr(guid, 'id') else str(guid)
                                if imdb_id and f'imdb://{imdb_id}' in guid_str:
                                    matched = True
                                    break
                                if tmdb_id and f'tmdb://{tmdb_id}' in guid_str:
                                    matched = True
                                    break

                    # If no IDs provided or matched by ID, also check title match
                    if not (imdb_id or tmdb_id) or matched:
                        if show.title.lower() == show_title.lower() or matched:
                            # Find the season
                            for season in show.seasons():
                                if season.seasonNumber == season_number:
                                    logger.info(f"[PLEX_SEASON_DELETE] Found season {season_number} of '{show.title}'. Deleting entire season.")
                                    try:
                                        season.delete()
                                        result['success'] = True
                                        result['season_deleted'] = True
                                        logger.info(f"[PLEX_SEASON_DELETE] Successfully deleted S{season_number:02d} of '{show.title}' from Plex")
                                        return result
                                    except Exception as del_err:
                                        error_str = str(del_err)
                                        if '400' in error_str or 'bad_request' in error_str.lower():
                                            logger.warning(f"[PLEX_SEASON_DELETE] 400 Bad Request for S{season_number:02d} of '{show.title}'. "
                                                          f"'Allow media deletion' may be disabled in Plex settings.")
                                            result['error'] = "Plex deletion disabled (400 Bad Request)"
                                        else:
                                            logger.error(f"[PLEX_SEASON_DELETE] Error deleting season: {error_str}")
                                            result['error'] = str(del_err)
                                        return result

                            logger.warning(f"[PLEX_SEASON_DELETE] Season {season_number} not found in show '{show.title}'")

            except Exception as e:
                logger.error(f"[PLEX_SEASON_DELETE] Error searching section '{section.title}': {str(e)}")
                continue

        logger.warning(f"[PLEX_SEASON_DELETE] Show '{show_title}' S{season_number:02d} not found in any Plex library")
        result['error'] = f"Season {season_number} of '{show_title}' not found in Plex"
        return result

    except Exception as e:
        logger.error(f"[PLEX_SEASON_DELETE] General error removing season: {str(e)}")
        result['error'] = str(e)
        return result


def remove_movie_from_plex(movie_title: str, imdb_id: str = None, tmdb_id: str = None) -> dict:
    """
    Remove an entire movie from Plex library.

    Args:
        movie_title: Title of the movie to remove
        imdb_id: Optional IMDB ID for more accurate matching
        tmdb_id: Optional TMDB ID for more accurate matching

    Returns:
        dict with 'success', 'movie_deleted', 'error'
    """
    result = {
        'success': False,
        'movie_deleted': False,
        'error': None
    }

    try:
        # Check for Jellyfin configuration first
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()

        if jellyfin_url and jellyfin_token:
            logger.info(f"Jellyfin configured - movie-level deletion not yet implemented for Jellyfin")
            result['error'] = "Jellyfin movie deletion not implemented"
            return result

        # Get Plex connection
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            result['error'] = "No Plex configuration found"
            return result

        if not plex_url or not plex_token:
            result['error'] = "Plex URL or token is empty"
            return result

        plex = plexapi.server.PlexServer(plex_url, plex_token, timeout=30)
        sections = plex.library.sections()

        logger.info(f"[PLEX_MOVIE_DELETE] Searching for movie: {movie_title} (imdb={imdb_id}, tmdb={tmdb_id})")

        for section in sections:
            if section.type != 'movie':
                continue

            try:
                movies = section.search(title=movie_title)

                for movie in movies:
                    # Try to match by ID if provided
                    matched = False

                    if imdb_id or tmdb_id:
                        # Check GUIDs for ID match
                        if hasattr(movie, 'guids'):
                            for guid in movie.guids:
                                guid_str = str(guid.id) if hasattr(guid, 'id') else str(guid)
                                if imdb_id and f'imdb://{imdb_id}' in guid_str:
                                    matched = True
                                    break
                                if tmdb_id and f'tmdb://{tmdb_id}' in guid_str:
                                    matched = True
                                    break

                    # If no IDs provided or matched by ID, also check title match
                    if not (imdb_id or tmdb_id) or matched:
                        if movie.title.lower() == movie_title.lower() or matched:
                            logger.info(f"[PLEX_MOVIE_DELETE] Found movie '{movie.title}' in section '{section.title}'. Deleting.")
                            try:
                                movie.delete()
                                result['success'] = True
                                result['movie_deleted'] = True
                                logger.info(f"[PLEX_MOVIE_DELETE] Successfully deleted movie '{movie.title}' from Plex")
                                return result
                            except Exception as del_err:
                                error_str = str(del_err)
                                if '400' in error_str or 'bad_request' in error_str.lower():
                                    logger.warning(f"[PLEX_MOVIE_DELETE] 400 Bad Request for movie '{movie.title}'. "
                                                  f"'Allow media deletion' may be disabled in Plex settings.")
                                    result['error'] = "Plex deletion disabled (400 Bad Request)"
                                else:
                                    logger.error(f"[PLEX_MOVIE_DELETE] Error deleting movie '{movie.title}': {error_str}")
                                    result['error'] = str(del_err)
                                return result

            except Exception as e:
                logger.error(f"[PLEX_MOVIE_DELETE] Error searching section '{section.title}': {str(e)}")
                continue

        logger.warning(f"[PLEX_MOVIE_DELETE] Movie '{movie_title}' not found in any Plex library")
        result['error'] = f"Movie '{movie_title}' not found in Plex"
        return result

    except Exception as e:
        logger.error(f"[PLEX_MOVIE_DELETE] General error removing movie '{movie_title}': {str(e)}")
        result['error'] = str(e)
        return result


def remove_file_from_plex(item_title, item_path, episode_title=None):
    try:
        # Check for Jellyfin configuration first - if available, use Jellyfin instead
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()
        
        if jellyfin_url and jellyfin_token:
            logger.info(f"Jellyfin configured, using Jellyfin for removal: {item_title} ({item_path})")
            try:
                from utilities.emby_functions import remove_file_from_emby
                return remove_file_from_emby(item_title, item_path, episode_title)
            except Exception as e:
                logger.error(f"Error removing file from Jellyfin: {str(e)}. Falling back to Plex.")
                # Continue to Plex removal below
        
        # Proceed with Plex removal (original logic)
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            logger.error("No Plex URL or token found in settings")
            return False
            
        plex = plexapi.server.PlexServer(plex_url, plex_token, timeout=30)

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
                                # If episode_title is known, filter to matching episodes only
                                # (much faster for large shows — avoids fetching all episodes).
                                # Falls back to all episodes if the filtered result is empty,
                                # preserving backward compatibility.
                                if episode_title:
                                    try:
                                        episodes = show.episodes(title=episode_title)
                                        if not episodes:
                                            episodes = show.episodes()
                                    except Exception:
                                        episodes = show.episodes()
                                else:
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
                        error_str = str(e)
                        item_name = getattr(show if section.type == 'show' else (movie if section.type == 'movie' else None), 'title', item_title)

                        # Check for 400 Bad Request - this means Plex doesn't allow deletion
                        # or the file is already gone from Plex's perspective
                        if '400' in error_str or 'bad_request' in error_str.lower():
                            logger.warning(f"[PLEX_REMOVAL] 400 Bad Request for {item_name} in section {section.title}. "
                                          f"This usually means: 1) 'Allow media deletion' is disabled in Plex settings, "
                                          f"2) The file no longer exists in Plex metadata, or "
                                          f"3) Plex needs a library scan to detect the file is missing. "
                                          f"Consider using scan_and_empty_plex_trash() instead.")
                            # Return False but don't keep retrying - this is a permanent failure
                            # that requires user intervention or a different approach
                            return False

                        logger.error(f"Error processing item {item_name} in section {section.title}: {error_str}")
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


def scan_and_empty_plex_trash(paths: list = None, section_type: str = None, empty_trash: bool = True) -> dict:
    """
    Scan specific Plex paths and then empty trash to clean up unavailable items.

    The scan is needed because Plex doesn't move items to trash until it detects
    the files are missing during a library scan.

    Args:
        paths: List of specific folder paths to scan (e.g., deleted symlink parent folders).
               If provided, only these paths will be scanned instead of entire sections.
        section_type: Optional - 'movie' or 'show' to filter sections for trash empty.
                      If None, empties trash for all sections.

    Returns:
        dict with 'success', 'paths_scanned', 'sections_cleaned', 'errors'
    """
    import time

    result = {
        'success': True,
        'paths_scanned': [],
        'sections_cleaned': [],
        'errors': []
    }

    try:
        # Check for Jellyfin configuration first
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()

        if jellyfin_url and jellyfin_token:
            logger.info("Jellyfin configured - skipping Plex scan and trash empty")
            result['sections_cleaned'].append('Jellyfin (skipped)')
            return result

        # Get Plex connection based on file management mode
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            logger.warning("No Plex URL or token found in settings for trash empty")
            result['errors'].append("No Plex configuration found")
            result['success'] = False
            return result

        if not plex_url or not plex_token:
            logger.warning("Plex URL or token is empty")
            result['errors'].append("Plex URL or token is empty")
            result['success'] = False
            return result

        plex = plexapi.server.PlexServer(plex_url, plex_token, timeout=30)
        sections = plex.library.sections()

        # Track which sections were actually scanned
        scanned_sections = set()

        # Step 1: Scan specific paths or sections
        if paths:
            # Scan only the specific paths provided (much faster than full library scan)
            for path in paths:
                try:
                    # Find which section contains this path
                    for section in sections:
                        if not hasattr(section, 'locations') or not section.locations:
                            continue
                        for location in section.locations:
                            if path.startswith(location):
                                logger.info(f"Scanning specific path in Plex: {path} (section: {section.title})")
                                section.update(path=path)
                                result['paths_scanned'].append(path)
                                scanned_sections.add(section.key)  # Track this section
                                logger.info(f"Successfully triggered scan for path: {path}")
                                break
                except Exception as e:
                    error_msg = f"Failed to scan path {path}: {str(e)}"
                    logger.error(error_msg)
                    result['errors'].append(error_msg)
        else:
            # No specific paths - scan entire sections (fallback, slower)
            for section in sections:
                try:
                    if section_type:
                        if section_type == 'movie' and section.type != 'movie':
                            continue
                        if section_type == 'show' and section.type != 'show':
                            continue

                    logger.info(f"Scanning entire Plex section: {section.title} ({section.type})")
                    section.update()
                    result['paths_scanned'].append(f"[section] {section.title}")
                    scanned_sections.add(section.key)  # Track this section
                    logger.info(f"Successfully triggered scan for section: {section.title}")

                except Exception as e:
                    error_msg = f"Failed to scan section {section.title}: {str(e)}"
                    logger.error(error_msg)
                    result['errors'].append(error_msg)

        # Step 2: Wait for scan to process (Plex needs time to detect missing files)
        if result['paths_scanned']:
            logger.info("Waiting for Plex to process scan...")
            time.sleep(2)

        # Step 3: Empty trash ONLY for sections that were actually scanned
        if not empty_trash:
            return result

        # Safety: if specific paths were requested but none of them matched any
        # Plex library section (e.g. a debrid-mount path that doesn't match the
        # section's Plex-visible location), scanned_sections stays empty. Emptying
        # trash in that case would fall through to "every section" below, wiping
        # unrelated content across the whole library instead of the intended item.
        # Fail closed: skip the trash-empty entirely rather than guess.
        if paths and not scanned_sections:
            error_msg = f"No Plex section matched any of the requested paths {paths!r} — skipping trash empty to avoid affecting unrelated sections"
            logger.warning(error_msg)
            result['success'] = False
            result['errors'].append(error_msg)
            return result

        for section in sections:
            try:
                # Skip if this section wasn't scanned
                if scanned_sections and section.key not in scanned_sections:
                    continue

                # Additional filter by section type if specified (backup safety check)
                if section_type:
                    if section_type == 'movie' and section.type != 'movie':
                        continue
                    if section_type == 'show' and section.type != 'show':
                        continue

                logger.info(f"Emptying trash for Plex section: {section.title} ({section.type})")
                section.emptyTrash()
                result['sections_cleaned'].append(section.title)
                logger.info(f"Successfully emptied trash for section: {section.title}")

            except Exception as e:
                error_msg = f"Failed to empty trash for section {section.title}: {str(e)}"
                logger.error(error_msg)
                result['errors'].append(error_msg)

        if result['errors']:
            result['success'] = len(result['sections_cleaned']) > 0

    except Exception as e:
        logger.error(f"Error during Plex scan and trash empty: {str(e)}")
        result['errors'].append(str(e))
        result['success'] = False

    return result


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
    # Get display name for logging (title if available, otherwise path)
    item_display = item.get('title')
    if not item_display:
        item_path = item.get('full_path') or item.get('location_on_disk') or item.get('location')
        item_display = f"[batch scan: {item_path}]" if item_path else "Unknown"

    logger.info(f"Attempting to trigger media server scan for item: {item_display}")
    try:
        # Check for Jellyfin configuration first - if available, use Jellyfin instead
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()

        if jellyfin_url and jellyfin_token:
            logger.info(f"Jellyfin configured, using Jellyfin for update: {item_display}")
            try:
                from utilities.emby_functions import emby_update_item
                return emby_update_item(item)
            except Exception as e:
                logger.error(f"Error updating item in Jellyfin: {str(e)}. Falling back to Plex.")
                # Continue to Plex update below
        
        # Proceed with Plex update (original logic)
        plex_url = get_setting('File Management', 'plex_url_for_symlink', '').rstrip('/')
        plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
        
        if not plex_url or not plex_token:
            logger.warning("Plex URL or token not configured for symlink updates.")
            return False
            
        # Initialise Plex client (timeout on connection itself)
        try:
            plex = PlexServer(plex_url, plex_token, timeout=30)
        except Exception as conn_err:
            logger.error(f"Could not connect to Plex server at '{plex_url}': {conn_err}")
            return False

        # Determine the directory we want Plex to rescan
        file_location = item.get('full_path') or item.get('location_on_disk') or item.get('location')
        if not file_location:
            logger.error(f"Cannot trigger update: No file location found for item: {item_display}")
            return False

        # Handle both file paths and directory paths
        # checking_queue.py may pass directories (already extracted from file paths)
        # to avoid double directory extraction which causes full library scans for movies
        file_extensions = ('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.mpg', '.mpeg', '.wmv', '.flv', '.webm')
        if file_location.lower().endswith(file_extensions):
            # It's a file path - extract the directory
            directory = os.path.dirname(file_location)
            logger.debug(f"Detected file path, extracting directory: {file_location} -> {directory}")
        else:
            # Already a directory path - use as-is
            directory = file_location
            logger.debug(f"Detected directory path, using as-is: {directory}")

        # Normalize path for reliable matching
        directory = os.path.abspath(os.path.normpath(directory))

        # Add small delay for symlink mode to allow filesystem to settle
        # This prevents race condition between symlink creation and Plex scan
        if plex_url:  # We're in symlink mode (plex_url_for_symlink is set)
            time.sleep(0.5)  # 500ms delay
            logger.debug("Added 500ms delay for symlink mode to allow filesystem to settle")

        # Obtain timeout from settings (seconds). Fallback to 60 s.
        try:
            plex_update_timeout = int(get_setting('File Management', 'plex_section_update_timeout', 1))
        except (TypeError, ValueError):
            plex_update_timeout = 60
            logger.warning(f"Invalid plex_section_update_timeout value; defaulting to {plex_update_timeout}s")

        def _update_section_with_timeout(sec, dir_path) -> bool:
            """Run `sec.update(path=dir_path)` in a background thread with a hard timeout."""
            section_name = getattr(sec, 'title', 'Unknown')
            logger.debug(f"Starting Plex section.update for '{section_name}' on '{dir_path}' (timeout {plex_update_timeout}s)")
            try:
                with ThreadPoolExecutor(max_workers=1) as _executor:
                    future = _executor.submit(sec.update, path=dir_path)
                    future.result(timeout=plex_update_timeout)
                logger.debug(f"Completed Plex section.update for '{section_name}' on '{dir_path}'")
                return True
            except TimeoutError:
                logger.error(f"Plex section.update timed out after {plex_update_timeout}s for section '{section_name}' on '{dir_path}'")
            except Exception as ex:
                logger.error(f"Error during Plex section.update for '{section_name}' on '{dir_path}': {ex}")
            return False

        # Get item type for section filtering
        item_type = item.get('type')  # 'movie' or 'episode'

        found_matching_section = False
        matching_sections = []

        # Get configured library filters from settings (support both names and IDs)
        allowed_library_keys = None
        try:
            # Build library dictionaries for filtering
            all_libraries = {}  # {name: key}
            libraries_by_key = {}  # {key: name}
            for section in plex.library.sections():
                # Convert key to string for consistent comparison
                key_str = str(section.key)
                all_libraries[section.title] = key_str
                libraries_by_key[key_str] = section.title

            # Get configured library names/IDs from settings
            if item_type == 'movie':
                movie_libs_setting = get_setting('Plex', 'movie_libraries', '')
                if movie_libs_setting:
                    allowed_library_keys = process_library_names(movie_libs_setting, all_libraries, libraries_by_key)
                    # Translate keys to names for logging
                    lib_names = [libraries_by_key.get(key, key) for key in allowed_library_keys]
                    logger.debug(f"Filtering to configured movie libraries: {lib_names}")
            elif item_type == 'episode':
                shows_libs_setting = get_setting('Plex', 'shows_libraries', '')
                if shows_libs_setting:
                    allowed_library_keys = process_library_names(shows_libs_setting, all_libraries, libraries_by_key)
                    # Translate keys to names for logging
                    lib_names = [libraries_by_key.get(key, key) for key in allowed_library_keys]
                    logger.debug(f"Filtering to configured show libraries: {lib_names}")
        except Exception as filter_err:
            logger.warning(f"Error setting up library filters: {filter_err}. Will scan all matching sections.")
            allowed_library_keys = None

        for section in plex.library.sections():
            try:
                # Type checking: Only check movie sections for movies, show sections for episodes
                if item_type == 'movie' and section.type != 'movie':
                    logger.debug(f"Skipping non-movie section '{section.title}' for movie item")
                    continue
                if item_type == 'episode' and section.type != 'show':
                    logger.debug(f"Skipping non-show section '{section.title}' for episode item")
                    continue

                # Filter by configured library settings (if specified)
                if allowed_library_keys is not None and str(section.key) not in allowed_library_keys:
                    logger.debug(f"Skipping section '{section.title}' (key: {section.key}) - not in configured libraries: {allowed_library_keys}")
                    continue

                # Validate section.locations exists
                if not hasattr(section, 'locations') or not section.locations:
                    logger.debug(f"Section '{section.title}' has no locations, skipping")
                    continue

                # Check each location in the section
                for location in section.locations:
                    # Normalize location path for comparison
                    normalized_location = os.path.abspath(os.path.normpath(location))

                    # Check if directory is under this location
                    if directory.startswith(normalized_location):
                        matching_sections.append((section, normalized_location))
                        logger.debug(f"Directory '{directory}' matches section '{section.title}' location '{normalized_location}'")
                        break  # Found match for this section, move to next section
            except Exception as e:
                logger.error(f"Error checking section '{section.title}': {str(e)}", exc_info=True)
                continue

        # Try to update matching sections
        if matching_sections:
            for section, matched_location in matching_sections:
                try:
                    logger.info(f"Found matching section '{section.title}' (location: {matched_location}), scanning directory: {directory}")
                    if _update_section_with_timeout(section, directory):
                        found_matching_section = True
                        return True  # Exit after successful update
                except Exception as e:
                    logger.error(f"Error updating section '{section.title}': {str(e)}", exc_info=True)
                    continue

        # Fallback: Try to match by content type folder for custom folders
        # This handles cases like /mnt/symlinked/Test/Movies where the exact path doesn't match
        # but we can infer the content type from the folder name
        if not found_matching_section and item_type:
            try:
                from utilities.path_utils import get_content_folder_path

                # Extract the content folder path (e.g., /mnt/symlinked/Test/Movies)
                content_folder_path = get_content_folder_path(directory, media_type='movie' if item_type == 'movie' else 'show')

                if content_folder_path and content_folder_path != directory:
                    logger.info(f"No exact path match found. Trying content folder path: {content_folder_path}")

                    # Find sections of matching type and scan the content folder path
                    for section in plex.library.sections():
                        try:
                            # Filter by configured library settings (if specified)
                            if allowed_library_keys is not None and str(section.key) not in allowed_library_keys:
                                logger.debug(f"Skipping section '{section.title}' (key: {section.key}) in fallback - not in configured libraries")
                                continue

                            # Match by section type
                            if item_type == 'movie' and section.type == 'movie':
                                logger.info(f"Using movie section '{section.title}' for custom folder scan: {content_folder_path}")
                                if _update_section_with_timeout(section, content_folder_path):
                                    found_matching_section = True
                                    return True
                            elif item_type == 'episode' and section.type == 'show':
                                logger.info(f"Using show section '{section.title}' for custom folder scan: {content_folder_path}")
                                if _update_section_with_timeout(section, content_folder_path):
                                    found_matching_section = True
                                    return True
                        except Exception as e:
                            logger.debug(f"Error trying section '{section.title}' for custom folder: {str(e)}")
                            continue
            except Exception as e:
                logger.debug(f"Error in custom folder fallback logic: {str(e)}")

        # No matching section found - log detailed error and return False
        # DO NOT scan all sections as this causes full library scans
        if not found_matching_section:
            available_sections = []
            try:
                available_sections = [f"{s.title} ({s.type})" for s in plex.library.sections()]
            except:
                available_sections = ["<error listing sections>"]

            logger.error(
                f"Could not find matching Plex library section for directory: {directory}\n"
                f"  Item: {item.get('title', 'Unknown')} (Type: {item_type})\n"
                f"  File location: {file_location}\n"
                f"  Available sections: {', '.join(available_sections)}\n"
                f"  Skipping Plex scan to avoid triggering full library scan.\n"
                f"  Please verify your Plex library configuration matches your symlink paths."
            )
            return False

        return False  # Should not be reached, but as final fallback
        
    except Exception as e:
        logger.error(f"Error updating item in Plex via scan: {str(e)}")
        return False


def generate_title_variations(title: str) -> list:
    """
    Generate title variations to handle Plex's inconsistent title storage.

    Tries removing and replacing various special characters that Plex may normalize.
    For titles with multiple special character types, tries each individually first,
    then combinations.

    Args:
        title: Original title from database

    Returns:
        List of title variations to try (duplicates removed, order preserved)
    """
    if not title:
        return []

    variations = [title]  # Always try original first

    # Define special character groups to handle
    # Each tuple is (characters_to_handle, description)
    special_char_groups = [
        ("'''", "apostrophes"),           # Various apostrophe types
        ("-", "hyphens"),
        ("/\\", "slashes"),                # Forward and back slashes
        (":", "colons"),
        ("|", "pipes"),
        ("!", "exclamation marks"),
        ("&", "ampersands"),
        (".", "periods"),                  # Sometimes used in titles like "Alien3"
        ("...", "ellipsis"),
    ]

    # Try removing each special character type individually
    for chars, desc in special_char_groups:
        for char in chars:
            if char in title:
                # Remove the character
                variations.append(title.replace(char, ""))
                # Replace with space
                variations.append(title.replace(char, " "))

    # Try removing multiple consecutive spaces (can happen after replacements)
    import re
    for var in list(variations):
        normalized = re.sub(r'\s+', ' ', var).strip()
        if normalized and normalized not in variations:
            variations.append(normalized)

    # Try removing ALL special characters at once (last resort)
    all_special_chars = "'''\\-/:!|&.…"
    title_no_special = title
    for char in all_special_chars:
        title_no_special = title_no_special.replace(char, "")
    if title_no_special:
        variations.append(title_no_special)
        # Also try with spaces instead of removal
        title_spaces = title
        for char in all_special_chars:
            title_spaces = title_spaces.replace(char, " ")
        title_spaces = re.sub(r'\s+', ' ', title_spaces).strip()
        if title_spaces:
            variations.append(title_spaces)

    # Add case variations for each variation
    # This handles cases where Plex stores "Zombies" but database has "Z-O-M-B-I-E-S"
    case_variations = []
    for var in variations:
        var_stripped = var.strip()
        if var_stripped:
            # Add original case
            case_variations.append(var_stripped)
            # Add title case (first letter caps, rest lowercase)
            case_variations.append(var_stripped.title())
            # Add uppercase
            case_variations.append(var_stripped.upper())
            # Add lowercase
            case_variations.append(var_stripped.lower())

    # Remove duplicates while preserving order
    seen = set()
    unique_variations = []
    for var in case_variations:
        # Use lowercase for duplicate detection to treat "ZOMBIES" and "Zombies" as same
        var_lower = var.lower()
        if var and var_lower not in seen:
            seen.add(var_lower)
            unique_variations.append(var)

    return unique_variations


def refresh_plex_show_cache():
    """
    Refresh the Plex show cache by fetching all shows with GUIDs from Plex.

    This method fetches ALL shows from TV libraries in one API call with includeGuids=1,
    which is much faster and more reliable than using guid__contains (which is broken).

    Returns:
        bool: True if cache was refreshed successfully, False otherwise
    """
    global _plex_show_cache, _plex_show_cache_timestamp

    try:
        # Get Plex connection details
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')

        if not plex_url or not plex_token:
            logger.debug("Plex URL or token not configured")
            return False

        plex_url = plex_url.rstrip('/')

        # Connect to Plex to get TV library sections
        plex = PlexServer(plex_url, plex_token, timeout=10)

        new_cache = {}
        shows_fetched = 0

        # Process all TV show libraries
        for section in plex.library.sections():
            if section.type == 'show':
                try:
                    # Fetch ALL shows with GUIDs in one API call (fast!)
                    section_key = section.key
                    url = f"{plex_url}/library/sections/{section_key}/all?includeGuids=1"
                    headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}

                    logger.info(f"Fetching shows with GUIDs from library: {section.title}")
                    start_time = time.time()

                    response = requests.get(url, headers=headers, timeout=30)
                    response.raise_for_status()
                    data = response.json()

                    shows = data.get('MediaContainer', {}).get('Metadata', [])
                    elapsed = time.time() - start_time

                    logger.info(f"Retrieved {len(shows)} shows with GUIDs from '{section.title}' in {elapsed:.2f}s")

                    # Build cache: extract all GUIDs and map to show data
                    for show in shows:
                        if 'Guid' in show:
                            for guid_obj in show['Guid']:
                                guid_full = guid_obj.get('id', '')
                                # Extract ID from formats like "imdb://tt123", "tmdb://456", "tvdb://789"
                                if '://' in guid_full:
                                    guid_id = guid_full.split('://')[-1]
                                else:
                                    guid_id = guid_full

                                # Store show data by ID (e.g., 'tt0074050', '43966', '36133')
                                if guid_id:
                                    new_cache[guid_id] = {
                                        'title': show.get('title'),
                                        'year': show.get('year'),
                                        'ratingKey': show.get('ratingKey'),
                                        'key': show.get('key'),
                                        'section_key': section_key
                                    }
                        shows_fetched += len(shows)

                except Exception as e:
                    logger.error(f"Error fetching shows from library '{section.title}': {e}", exc_info=True)
                    continue

        # Update global cache
        _plex_show_cache = new_cache
        _plex_show_cache_timestamp = time.time()

        logger.info(f"Plex show cache refreshed: {len(new_cache)} ID mappings from {shows_fetched} shows")
        return True

    except Exception as e:
        logger.error(f"Error refreshing Plex show cache: {e}", exc_info=True)
        return False


def is_plex_show_cache_stale() -> bool:
    """Check if the Plex show cache needs refreshing."""
    if _plex_show_cache_timestamp is None:
        return True

    age_hours = (time.time() - _plex_show_cache_timestamp) / 3600
    return age_hours >= _SHOW_CACHE_TTL_HOURS


def refresh_plex_movie_cache():
    """
    Refresh the Plex movie cache by fetching all movies with GUIDs from Plex.

    This method fetches ALL movies from movie libraries in one API call with includeGuids=1,
    which is much faster and more reliable than using guid__contains (which is broken).

    A threading lock ensures only one refresh runs at a time — concurrent callers
    (e.g. simultaneous Agregarr webhooks) wait for the first to complete and then
    reuse the freshly-populated cache rather than each issuing a full Plex API call
    and DB write, which caused SQLite lock contention.

    Returns:
        bool: True if cache was refreshed successfully, False otherwise
    """
    global _plex_movie_cache, _plex_movie_cache_timestamp

    if not _plex_movie_cache_lock.acquire(blocking=True, timeout=60):
        logger.warning("refresh_plex_movie_cache: could not acquire lock within 60s, skipping")
        return False

    try:
        # Re-check staleness after acquiring lock — a concurrent caller may have
        # already refreshed the cache while we were waiting.
        if not is_plex_movie_cache_stale():
            logger.debug("refresh_plex_movie_cache: cache refreshed by another thread while waiting — skipping")
            return True
    except Exception:
        pass

    try:
        # Get Plex connection details
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')

        if not plex_url or not plex_token:
            logger.debug("Plex URL or token not configured")
            return False

        plex_url = plex_url.rstrip('/')

        # Connect to Plex to get movie library sections
        plex = PlexServer(plex_url, plex_token, timeout=10)

        new_cache = {}
        movies_fetched = 0

        # Process all movie libraries
        for section in plex.library.sections():
            if section.type == 'movie':
                try:
                    # Fetch ALL movies with GUIDs in one API call (fast!)
                    section_key = section.key
                    url = f"{plex_url}/library/sections/{section_key}/all?includeGuids=1"
                    headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}

                    logger.info(f"Fetching movies with GUIDs from library: {section.title}")
                    start_time = time.time()

                    response = requests.get(url, headers=headers, timeout=30)
                    response.raise_for_status()
                    data = response.json()

                    movies = data.get('MediaContainer', {}).get('Metadata', [])
                    elapsed = time.time() - start_time

                    logger.info(f"Retrieved {len(movies)} movies with GUIDs from '{section.title}' in {elapsed:.2f}s")

                    # Build cache: extract all GUIDs and map to movie data
                    for movie in movies:
                        if 'Guid' in movie:
                            for guid_obj in movie['Guid']:
                                guid_full = guid_obj.get('id', '')
                                # Extract ID from formats like "imdb://tt123", "tmdb://456"
                                if '://' in guid_full:
                                    guid_id = guid_full.split('://')[-1]
                                else:
                                    guid_id = guid_full

                                # Store movie data by ID (e.g., 'tt0111161', '278')
                                if guid_id:
                                    new_cache[guid_id] = {
                                        'title': movie.get('title'),
                                        'year': movie.get('year'),
                                        'ratingKey': movie.get('ratingKey'),
                                        'key': movie.get('key'),
                                        'section_key': section_key
                                    }
                        movies_fetched += len(movies)

                except Exception as e:
                    logger.error(f"Error fetching movies from library '{section.title}': {e}", exc_info=True)
                    continue

        # Update global cache
        _plex_movie_cache = new_cache
        _plex_movie_cache_timestamp = time.time()

        logger.info(f"Plex movie cache refreshed: {len(new_cache)} ID mappings from {movies_fetched} movies")
        return True

    except Exception as e:
        logger.error(f"Error refreshing Plex movie cache: {e}", exc_info=True)
        return False
    finally:
        _plex_movie_cache_lock.release()


def is_plex_movie_cache_stale() -> bool:
    """Check if the Plex movie cache needs refreshing."""
    if _plex_movie_cache_timestamp is None:
        return True

    age_hours = (time.time() - _plex_movie_cache_timestamp) / 3600
    return age_hours >= _MOVIE_CACHE_TTL_HOURS


def get_plex_item(item_id: int):
    """
    Get a Plex item object by database ID

    Args:
        item_id: Database ID of the media item (can be int or dict with 'id' key)

    Returns:
        PlexAPI item object if found in Plex, None otherwise
    """
    # Handle both int and dict input
    if isinstance(item_id, dict):
        item_data = item_id
        item_id = item_data.get('id')
    else:
        item_data = get_media_item_by_id(item_id)

    if not item_data:
        logger.debug(f"Item {item_id} not found in database")
        return None

    try:
        # Get Plex connection details
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')

        if not plex_url or not plex_token:
            logger.debug("Plex URL or token not configured")
            return None

        # Connect to Plex
        plex = PlexServer(plex_url, plex_token, timeout=10)

        # Refresh show cache if stale (for episodes/shows only)
        if item_data.get('type') == 'episode' and is_plex_show_cache_stale():
            logger.debug("Plex show cache is stale, refreshing...")
            refresh_plex_show_cache()

        # Refresh movie cache if stale (for movies only)
        if item_data.get('type') == 'movie' and is_plex_movie_cache_stale():
            logger.debug("Plex movie cache is stale, refreshing...")
            refresh_plex_movie_cache()

        # Determine item type
        item_type = item_data.get('type')
        imdb_id = item_data.get('imdb_id')
        tmdb_id = item_data.get('tmdb_id')
        title = item_data.get('title')
        year = item_data.get('year')

        # Search for item
        if item_type == 'movie':
            # Try cache lookup first (fast path using ID-based matching)
            cache_hit = False
            if imdb_id or tmdb_id:
                logger.debug(f"Searching Plex cache for movie '{title}' - IMDb: {imdb_id}, TMDb: {tmdb_id}")

                cached_movie = None

                # Check IMDb match (cache is keyed by GUID IDs)
                if imdb_id and imdb_id in _plex_movie_cache:
                    cached_movie = _plex_movie_cache[imdb_id]
                    logger.info(f"Found movie in cache by IMDb ID ({imdb_id}): {cached_movie.get('title')}")
                    cache_hit = True

                # Check TMDb match
                elif tmdb_id and str(tmdb_id) in _plex_movie_cache:
                    cached_movie = _plex_movie_cache[str(tmdb_id)]
                    logger.info(f"Found movie in cache by TMDb ID ({tmdb_id}): {cached_movie.get('title')}")
                    cache_hit = True

                # If found in cache, fetch the actual Plex movie object
                if cache_hit and cached_movie:
                    try:
                        rating_key = cached_movie.get('ratingKey')
                        # Use ratingKey to construct reliable path instead of using 'key' directly
                        # This prevents issues with Tag objects being returned
                        movie_key = f"/library/metadata/{rating_key}"
                        movie = plex.fetchItem(movie_key)
                        logger.debug(f"Successfully fetched movie from cache using ratingKey: {rating_key}")
                        return movie
                    except Exception as e:
                        logger.warning(f"Failed to fetch movie by ratingKey {rating_key}: {e}")
                        # Fall through to title+year search
                        cache_hit = False

            # If not found in cache, fallback to title+year search
            if not cache_hit:
                logger.debug(f"Movie not found in cache, falling back to title+year search")
                # Search in all movie libraries
                for section in plex.library.sections():
                    if section.type == 'movie':
                        try:
                            # Fallback to title and year
                            if title and year:
                                movies = section.search(title=title, year=year)
                                if movies:
                                    movie = movies[0]
                                    logger.info(f"Found movie by title+year: {movie.title}")
                                    return movie

                            # Final fallback: Try title variations with GUID matching
                            if title and (imdb_id or tmdb_id):
                                logger.debug(f"Movie not found via title+year, trying title variations with GUID matching")
                                title_variations = generate_title_variations(title)

                                for title_var in title_variations:
                                    movies = section.search(title=title_var)
                                    if movies:
                                        for movie in movies:
                                            # Check if any of the movie's GUIDs match our IDs
                                            if hasattr(movie, 'guids'):
                                                for guid in movie.guids:
                                                    if (imdb_id and imdb_id in guid.id) or (tmdb_id and str(tmdb_id) in guid.id):
                                                        logger.info(f"Found movie by title variation ('{title_var}') with GUID match: {movie.title}")
                                                        return movie
                        except Exception as e:
                            logger.debug(f"Error searching section {section.title}: {e}")
                            continue

        elif item_type == 'episode':
            season_number = item_data.get('season_number')
            episode_number = item_data.get('episode_number')

            # Try cache lookup first (fast path using ID-based matching)
            cache_hit = False
            if imdb_id or tmdb_id:
                logger.debug(f"Searching Plex cache for show '{title}' - IMDb: {imdb_id}, TMDb: {tmdb_id}")

                cached_show = None

                # Check IMDb match (cache is keyed by GUID IDs)
                if imdb_id and imdb_id in _plex_show_cache:
                    cached_show = _plex_show_cache[imdb_id]
                    logger.info(f"Found show in cache by IMDb ID ({imdb_id}): {cached_show.get('title')}")
                    cache_hit = True

                # Check TMDb match
                elif tmdb_id and str(tmdb_id) in _plex_show_cache:
                    cached_show = _plex_show_cache[str(tmdb_id)]
                    logger.info(f"Found show in cache by TMDb ID ({tmdb_id}): {cached_show.get('title')}")
                    cache_hit = True

                # If found in cache, fetch the actual Plex show object
                if cache_hit and cached_show:
                    try:
                        rating_key = cached_show.get('ratingKey')
                        # Use ratingKey to construct reliable path instead of using 'key' directly
                        # This prevents issues with Tag objects being returned
                        show_key = f"/library/metadata/{rating_key}"
                        show = plex.fetchItem(show_key)
                        logger.debug(f"Successfully fetched show from cache using ratingKey: {rating_key}")
                        return show
                    except Exception as e:
                        logger.warning(f"Failed to fetch show by ratingKey {rating_key}: {e}")
                        # Fall through to title+year search
                        cache_hit = False

            # If not found in cache, fallback to title+year search
            if not cache_hit:
                logger.debug(f"Show not found in cache, falling back to title+year search")
                # Search in all TV libraries
                for section in plex.library.sections():
                    if section.type == 'show':
                        try:
                            # Fallback to title and year
                            if title and year:
                                shows = section.search(title=title, year=year)
                                if shows:
                                    show = shows[0]
                                    logger.info(f"Found show by title+year for label application: {show.title}")
                                    # For Plex labels, return the show itself (not specific episodes)
                                    return show

                            # Final fallback: Try title variations with GUID matching
                            if title and (imdb_id or tmdb_id):
                                logger.debug(f"Show not found via title+year, trying title variations with GUID matching")
                                title_variations = generate_title_variations(title)

                                for title_var in title_variations:
                                    shows = section.search(title=title_var)
                                    if shows:
                                        for show in shows:
                                            # Check if any of the show's GUIDs match our IDs
                                            if hasattr(show, 'guids'):
                                                for guid in show.guids:
                                                    if (imdb_id and imdb_id in guid.id) or (tmdb_id and str(tmdb_id) in guid.id):
                                                        logger.info(f"Found show by title variation ('{title_var}') with GUID match for label application: {show.title}")
                                                        return show
                        except Exception as e:
                            logger.debug(f"Error searching section {section.title}: {e}")
                            continue

        # Log detailed info about what we searched for when item not found
        logger.info(f"Plex item not found after exhaustive search - "
                   f"Type: {item_type}, Title: '{title}', Year: {year}, "
                   f"IMDb: {imdb_id}, TMDb: {tmdb_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting Plex item for {item_id}: {e}", exc_info=True)
        return None


def get_file_info_from_filesystem(file_path: str) -> dict:
    """
    Get file size from filesystem (instant, no API calls).
    This is much faster than querying Plex API.

    Args:
        file_path: Path to the file on disk

    Returns:
        Dictionary with 'size_gb' key, or empty dict if file not found
    """
    try:
        if not file_path or not os.path.exists(file_path):
            return {}

        size_bytes = os.path.getsize(file_path)
        size_gb = round(size_bytes / (1024**3), 2)
        logger.debug(f"Got size from filesystem for {file_path}: {size_gb}GB")
        return {'size_gb': size_gb}
    except Exception as e:
        logger.debug(f"Could not get filesystem info for {file_path}: {e}")
        return {}


def get_plex_file_info(file_path: str) -> dict:
    """
    Get file size and resolution from Plex for a specific file.

    Args:
        file_path: Path to the file (should match what Plex has in its library)

    Returns:
        Dictionary with 'size_gb' and 'resolution' keys, or empty dict if not found
    """
    try:
        # Get Plex connection details
        file_management = get_setting('File Management', 'file_collection_management', default='Plex')

        if file_management == 'Plex':
            plex_url = get_setting('Plex', 'url')
            plex_token = get_setting('Plex', 'token')
        else:
            plex_url = get_setting('File Management', 'plex_url_for_symlink')
            plex_token = get_setting('File Management', 'plex_token_for_symlink')

        if not plex_url or not plex_token:
            logger.debug("Plex URL or token not configured for get_plex_file_info")
            return {}

        plex = PlexServer(plex_url, plex_token, timeout=30)
        filename = os.path.basename(file_path)

        # Search through all library sections for the file
        for section in plex.library.sections():
            try:
                if section.type == 'movie':
                    # Search movies
                    for movie in section.all():
                        try:
                            for media in movie.media:
                                for part in media.parts:
                                    if os.path.basename(part.file) == filename:
                                        size_bytes = part.size if part.size else 0
                                        size_gb = round(size_bytes / (1024**3), 2) if size_bytes else None
                                        raw_resolution = media.videoResolution if hasattr(media, 'videoResolution') else None
                                        resolution = normalize_plex_resolution(raw_resolution) if raw_resolution else None
                                        logger.debug(f"Found file {filename} in Plex: size={size_gb}GB, resolution={resolution}")
                                        return {'size_gb': size_gb, 'resolution': resolution, 'location': part.file}
                        except Exception:
                            continue

                elif section.type == 'show':
                    # Search episodes - use allEpisodes for efficiency
                    try:
                        for episode in section.searchEpisodes():
                            try:
                                for media in episode.media:
                                    for part in media.parts:
                                        if os.path.basename(part.file) == filename:
                                            size_bytes = part.size if part.size else 0
                                            size_gb = round(size_bytes / (1024**3), 2) if size_bytes else None
                                            raw_resolution = media.videoResolution if hasattr(media, 'videoResolution') else None
                                            resolution = normalize_plex_resolution(raw_resolution) if raw_resolution else None
                                            logger.debug(f"Found file {filename} in Plex: size={size_gb}GB, resolution={resolution}")
                                            return {'size_gb': size_gb, 'resolution': resolution, 'location': part.file}
                            except Exception:
                                continue
                    except Exception as e:
                        logger.debug(f"Error searching episodes in section {section.title}: {e}")
                        continue

            except Exception as e:
                logger.debug(f"Error searching section {section.title}: {e}")
                continue

        logger.info(f"File {filename} not found in any Plex library")

        # Fallback: Get size directly from filesystem (useful for symlink mode)
        logger.info(f"Checking filesystem fallback: file_management={file_management}, file_exists={os.path.exists(file_path)}, path={file_path}")
        if file_management in ['Symlink', 'Symlinked/Local'] and os.path.exists(file_path):
            try:
                size_bytes = os.path.getsize(file_path)
                size_gb = round(size_bytes / (1024**3), 2) if size_bytes else None
                logger.info(f"Got file size from filesystem for {filename}: {size_gb}GB")
                return {'size_gb': size_gb, 'resolution': None, 'location': file_path}
            except Exception as fs_error:
                logger.info(f"Could not get filesystem size for {file_path}: {fs_error}")

        logger.info(f"Filesystem fallback not used, returning empty dict")
        return {}

    except Exception as e:
        logger.error(f"Error getting Plex file info for {file_path}: {e}")

        # Fallback: Try filesystem even on error if in symlink mode
        try:
            file_management = get_setting('File Management', 'file_collection_management', default='Plex')
            if file_management in ['Symlink', 'Symlinked/Local'] and os.path.exists(file_path):
                size_bytes = os.path.getsize(file_path)
                size_gb = round(size_bytes / (1024**3), 2) if size_bytes else None
                logger.debug(f"Got file size from filesystem (fallback) for {file_path}: {size_gb}GB")
                return {'size_gb': size_gb, 'resolution': None, 'location': file_path}
        except:
            pass

        return {}


def update_item_with_plex_info(item_id: int, file_path: str = None, skip_plex_search: bool = False) -> bool:
    """
    Update a database item with size/resolution info.
    OPTIMIZED: Tries filesystem first (instant), only uses Plex API as fallback.

    Call this after an item transitions to Collected state.

    Args:
        item_id: Database ID of the media item
        file_path: Optional file path. If not provided, will try to get from DB item
        skip_plex_search: If True, only use filesystem check (default False)

    Returns:
        True if item was updated, False otherwise
    """
    from database.database_reading import get_media_item_by_id
    from database import get_db_connection

    try:
        # Get item from database if file_path not provided
        if not file_path:
            item = get_media_item_by_id(item_id)
            if not item:
                logger.debug(f"Item {item_id} not found in database")
                return False
            file_path = item.get('filled_by_file') or item.get('location_on_disk')

        if not file_path:
            logger.debug(f"No file path available for item {item_id}")
            return False

        # OPTIMIZATION: Try filesystem first (instant for symlink mode)
        file_management = get_setting('File Management', 'file_collection_management', default='Plex')
        info = {}

        if file_management != 'Plex' or skip_plex_search:
            # For symlink mode, filesystem check is primary
            info = get_file_info_from_filesystem(file_path)
            if info:
                logger.debug(f"Got file info from filesystem for item {item_id} (skipping slow Plex search)")

        # If filesystem didn't work and we're allowed to search Plex, do it
        # NOTE: This searches ALL episodes in Plex library and is VERY SLOW (35-40s per episode)
        # Only do this if absolutely necessary
        if not info and not skip_plex_search:
            logger.debug(f"Filesystem check failed for item {item_id}, falling back to Plex API search")
            info = get_plex_file_info(file_path)

        if not info:
            logger.debug(f"Could not get file info for item {item_id} ({file_path})")
            return False

        # Update database
        conn = get_db_connection()
        try:
            size_gb = info.get('size_gb')
            resolution = info.get('resolution')
            location = info.get('location')

            # Only update if we have new data
            if size_gb is not None or resolution is not None or location:
                conn.execute('''
                    UPDATE media_items
                    SET size = COALESCE(size, ?),
                        resolution = COALESCE(resolution, ?),
                        location_on_disk = COALESCE(location_on_disk, ?)
                    WHERE id = ?
                ''', (size_gb, resolution, location, item_id))
                conn.commit()
                logger.info(f"Updated item {item_id} with file info: size={size_gb}GB, resolution={resolution}")
                return True
        finally:
            conn.close()

        return False

    except Exception as e:
        logger.error(f"Error updating item {item_id} with file info: {e}")
        return False
