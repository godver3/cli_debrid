"""Cached Plex show/movie indexes and item/file lookups."""

from utilities.settings import get_setting
import time
from typing import Dict, Any, Optional
import os
from plexapi.server import PlexServer
from database.database_reading import get_media_item_by_id
import requests
import threading as _threading
from ._common import logger, normalize_plex_resolution
from .update import generate_title_variations

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

_plex_movie_cache_lock = _threading.Lock()  # Prevents concurrent cache refreshes

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
        plex_url = (get_setting('Plex', 'url') or '').rstrip('/')
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
        plex_url = (get_setting('Plex', 'url') or '').rstrip('/')
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
        plex_url = (get_setting('Plex', 'url') or '').rstrip('/')
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
    from utilities.silo_functions import is_silo_selected
    from utilities.direct_mount import is_direct_mount_jellyfin
    if is_silo_selected() or is_direct_mount_jellyfin():
        return {}  # Silo or Jellyfin/Emby is the media server; callers fall back to the file itself
    try:
        # Get Plex connection details
        file_management = get_setting('File Management', 'file_collection_management', default='Plex')

        if file_management == 'Plex':
            plex_url = (get_setting('Plex', 'url') or '').rstrip('/')
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
                    SET size = COALESCE(?, size),
                        resolution = COALESCE(?, resolution),
                        location_on_disk = COALESCE(?, location_on_disk)
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
