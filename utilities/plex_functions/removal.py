"""Remove shows, seasons, movies and files from Plex."""

from utilities.settings import get_setting
import time
import plexapi.server
import os
from ._common import logger

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
        # Silo picked as the symlink media server: a rescan of the path drops the file
        from utilities.silo_functions import is_silo_mode, remove_file_from_silo
        if is_silo_mode():
            return remove_file_from_silo(item_title, item_path, episode_title)

        # Direct Mount run with Jellyfin/Emby: rescan the file's folder through its libraries
        from utilities.jellyfin_direct import is_jellyfin_direct, remove_file as jellyfin_remove_file
        if is_jellyfin_direct():
            return jellyfin_remove_file(item_title, item_path, episode_title)

        from utilities.direct_mount import unconfigured_server
        missing = unconfigured_server()
        if missing:
            logger.warning(f"{missing} is the media server but its URL or API key is not set; "
                           f"{item_title} not removed from it")
            return False

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

def _find_plex_owner_of_file(plex, item_title, item_path, episode_title=None):
    """(movie/episode, media) in Plex whose media part has item_path's file name, or (None, None)."""
    target = os.path.basename(item_path)
    for section in plex.library.sections():
        if section.type == 'movie':
            owners = section.search(title=item_title)
        elif section.type == 'show':
            owners = []
            for show in section.search(title=item_title):
                try:
                    episodes = show.episodes(title=episode_title) if episode_title else []
                    owners.extend(episodes or show.episodes())
                except Exception:
                    owners.extend(show.episodes())
        else:
            continue
        for owner in owners:
            for media in getattr(owner, 'media', []) or []:
                if any(os.path.basename(part.file) == target for part in media.parts):
                    return owner, media
    return None, None
