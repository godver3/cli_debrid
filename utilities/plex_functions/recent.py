"""Recently-added fetch from Plex."""

import asyncio
import aiohttp
import logging
from utilities.settings import get_setting
import time
from typing import Dict, List, Any, Optional
import os
from utilities import plex_health
from ._common import MAX_CONCURRENT_REQUESTS, PLEX_HTTP_TIMEOUT, logger, normalize_plex_resolution
from .collected import extract_show_ids, fetch_data, filter_genres, process_library_names

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
                plex_health.record_contact_failure()
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

async def process_recent_movie(movie: Dict[str, Any]) -> List[Dict[str, Any]]:
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
