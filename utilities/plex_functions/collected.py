"""Full collected-content fetch from Plex libraries."""

import asyncio
import aiohttp
import logging
from utilities.settings import get_setting
import time
from typing import Dict, List, Any, Optional
import os
from ._common import BATCH_DELAY, CHUNK_SIZE, EPISODE_BATCH_DELAY, EPISODE_BATCH_SIZE, MAX_CONCURRENT_REQUESTS, MAX_RETRIES, OPTIMAL_PAGE_SIZE, PLEX_HTTP_TIMEOUT, RETRY_DELAY, logger, normalize_plex_resolution

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

    if 'MediaContainer' not in first_page:
        logger.error(f"Failed to retrieve valid MediaContainer for first page of library {library_key}{type_str}")
        return []
    if 'Metadata' not in first_page['MediaContainer']:
        # Plex leaves Metadata out for an empty library (failed fetches come back with an empty list).
        logger.info(f"Library {library_key}{type_str} is empty")
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

def is_anime(item):
    if 'Genre' in item:
        return any(genre.get('tag').lower() == 'anime' for genre in item['Genre'] if isinstance(genre, dict))
    return False

def filter_genres(genres):
    if not isinstance(genres, list):
        genres = [genres] if genres else []
    filtered = list(set(str(genre).strip().lower() for genre in genres if genre))
    return filtered

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
