"""
Path utility functions for intelligent path extraction and display.
Handles both Plex and Symlink mode with varying folder depths.
"""
import os
import logging
from utilities.settings import get_setting


def get_content_folder_path(full_path, media_type='movie'):
    """
    Extract path up to and including the content folder (Movies/TV Shows/etc.)
    Works for any depth and both Plex and Symlink modes.

    Args:
        full_path: Full file path (e.g., /mnt/symlinked/Test/Movies/Movie.mkv)
        media_type: 'movie' or 'show' to help identify correct folder

    Returns:
        Path up to and including the content folder

    Examples:
        /debrid/movies/Movie.mkv → /debrid/movies
        /mnt/symlinked/movies/Movie.mkv → /mnt/symlinked/movies
        /mnt/symlinked/Test/Movies/Movie.mkv → /mnt/symlinked/Test/Movies
        /media/plex/TV Shows/Show/S01E01.mkv → /media/plex/TV Shows
    """
    if not full_path:
        return None

    # Build list of possible content folder names (case-insensitive)
    content_folders = set()

    # Standard folders from settings (stored under 'Debug' section)
    movies_folder = get_setting('Debug', 'movies_folder_name', 'Movies')
    tv_folder = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
    content_folders.add(movies_folder.lower())
    content_folders.add(tv_folder.lower())

    # Anime folders if enabled
    if get_setting('Debug', 'enable_separate_anime_folders', False):
        anime_movies = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies')
        anime_tv = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows')
        content_folders.add(anime_movies.lower())
        content_folders.add(anime_tv.lower())

    # Documentary folders if enabled
    if get_setting('Debug', 'enable_separate_documentary_folders', False):
        doc_movies = get_setting('Debug', 'documentary_movies_folder_name', 'Documentary Movies')
        doc_tv = get_setting('Debug', 'documentary_tv_shows_folder_name', 'Documentary TV Shows')
        content_folders.add(doc_movies.lower())
        content_folders.add(doc_tv.lower())

    # Add common variations for robustness
    content_folders.update(['movies', 'tv shows', 'shows', 'tv', 'series', 'television'])

    # Handle custom folders - they typically have /Movies or /TV Shows as subfolders
    # So we need to look for these patterns in the path

    # Split path and search for content folder
    path_parts = full_path.strip('/').split('/')

    # Debug: Log what we're searching for
    logging.debug(f"[PathUtils] Analyzing path: {full_path}")
    logging.debug(f"[PathUtils] Path parts: {path_parts}")
    logging.debug(f"[PathUtils] Content folders to match: {sorted(content_folders)}")

    # Search from right to left (closest to file) to find the deepest match
    # This ensures we get the actual content folder, not an intermediate folder
    # that happens to have the same name
    for i in range(len(path_parts) - 1, -1, -1):
        part_lower = path_parts[i].lower()
        logging.debug(f"[PathUtils] Checking part {i}: '{path_parts[i]}' (lowercase: '{part_lower}')")
        if part_lower in content_folders:
            result = '/' + '/'.join(path_parts[:i+1])
            logging.debug(f"[PathUtils] ✓ Found content folder '{path_parts[i]}' at index {i}, returning: {result}")
            return result

    # Fallback: return first 3 levels if no match found
    # This is better than 2 levels for symlink mode
    fallback = '/' + '/'.join(path_parts[:min(3, len(path_parts))])
    logging.warning(f"[PathUtils] ✗ No content folder found in path! Using fallback (3 levels): {fallback}")
    logging.warning(f"[PathUtils] Path parts were: {path_parts}")
    logging.warning(f"[PathUtils] Expected folders were: {sorted(content_folders)}")
    return fallback


def get_mount_path_for_content(media_type='movie'):
    """
    Get the mount path from settings and ensure it includes the content type folder.
    Handles both __all__ placeholder and paths without it.

    Args:
        media_type: 'movie' or 'show' to determine which subfolder to use

    Returns:
        Mount path with appropriate content folder (e.g., /media/mount/movies)

    Examples:
        Setting: /media/mount/__all__, Type: movie → /media/mount/movies
        Setting: /media/mount, Type: movie → /media/mount/movies
        Setting: /media/mount/movies, Type: movie → /media/mount/movies
    """
    # Get mount root from settings
    mount_root = get_setting('Plex', 'mounted_file_location', default='')

    if not mount_root:
        return None

    # Determine the content folder name (settings stored under 'Debug' section)
    if media_type == 'movie':
        content_folder = get_setting('Debug', 'movies_folder_name', 'movies')
    else:
        content_folder = get_setting('Debug', 'tv_shows_folder_name', 'shows')

    # Case 1: Path contains __all__ placeholder - replace it
    if '__all__' in mount_root:
        result = mount_root.replace('__all__', content_folder.lower())
        logging.debug(f"[PathUtils] Replaced __all__ in mount path: {result}")
        return result

    # Case 2: Path already ends with the content folder - return as is
    if mount_root.rstrip('/').lower().endswith(content_folder.lower()):
        logging.debug(f"[PathUtils] Mount path already contains content folder: {mount_root}")
        return mount_root

    # Case 3: Path already ends with movies/shows/tv - return as is
    path_lower = mount_root.rstrip('/').lower()
    if path_lower.endswith('/movies') or path_lower.endswith('/shows') or path_lower.endswith('/tv'):
        logging.debug(f"[PathUtils] Mount path already ends with content folder: {mount_root}")
        return mount_root

    # Case 4: Append the content folder
    result = os.path.join(mount_root, content_folder.lower())
    logging.debug(f"[PathUtils] Appended content folder to mount path: {result}")
    return result
