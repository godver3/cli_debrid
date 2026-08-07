"""
Deletion Manager - Unified deletion logic for all library operations
Used by: Library multi-select, Maintenance page, Show detail page

Handles all 6 deletion layers:
1. Database (DELETE or UPDATE to Blacklisted)
2. Media Server (Plex/Jellyfin)
3. Filesystem (rclone mount or original files)
4. Debrid Provider (optional)
5. Symlinks (if applicable)
6. Content Source Cache

Configuration-aware: Detects file mode and media server type automatically
"""

import os
import shutil
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Set
from datetime import datetime
from utilities.settings import get_setting
from database.core import get_db_connection

# State priority mapping (lower number = higher priority = keep)
STATE_PRIORITY = {
    'Collected': 1,       # Keep - actual collected content
    'Upgrading': 2,       # Keep - in progress
    'Final_Check': 3,     # Keep - almost done
    'Wanted': 4,          # Keep - actively seeking
    'Unreleased': 5,      # Keep - not yet available
    'Error': 6,           # DELETE candidate
    'Blacklisted': 7,     # DELETE candidate
    'ghostlisted': 8,     # DELETE candidate
    'all_blacklisted': 9  # DELETE candidate
}

class DeletionManager:
    """
    Unified deletion manager for all library deletion operations
    Handles configuration detection and all 6 deletion layers
    """

    def __init__(self, debrid_provider=None):
        self.debrid = debrid_provider
        self.deletion_log = []
        self._detect_configuration()

    def _detect_configuration(self):
        """Detect all configuration settings for deletion operations"""
        # File collection management mode from settings
        # "Plex" = no symlinks, "Symlinked/Local" = symlinks enabled
        self.file_collection_mode = get_setting('File Management', 'file_collection_management', 'Plex')

        # Symlink paths from settings (dynamic, user-configurable)
        self.symlink_dir = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
        self.original_files_path = get_setting('File Management', 'original_files_path', '')

        # Symlinks are only available if file_collection_management is "Symlinked/Local"
        self.using_symlinks = False
        if self.file_collection_mode == 'Symlinked/Local':
            # Verify the symlink directory exists and has content
            if self.symlink_dir and os.path.exists(self.symlink_dir):
                try:
                    dir_contents = os.listdir(self.symlink_dir)
                    if dir_contents:
                        self.using_symlinks = True
                        logging.debug(f"[DELETION_MANAGER] Symlink mode detected: {self.symlink_dir} has {len(dir_contents)} items")
                except PermissionError:
                    logging.warning(f"[DELETION_MANAGER] Cannot access symlink directory: {self.symlink_dir}")
            else:
                logging.warning(f"[DELETION_MANAGER] Symlinked/Local mode configured but symlink directory not found: {self.symlink_dir}")
        else:
            logging.debug(f"[DELETION_MANAGER] Plex mode - symlink operations disabled")

        # Media Server Type (stored in File Management section)
        media_server_raw = get_setting('File Management', 'media_server_type', 'plex')
        self.media_server = (media_server_raw or 'plex').lower()
        # Values: "plex" or "jellyfin"

        # Mounted File Location
        self.mounted_location = get_setting('Plex', 'mounted_file_location', '')

        # Media Server Availability
        self.plex_enabled = self._check_plex_available()
        self.jellyfin_enabled = self._check_jellyfin_available()

        # Limited Environment Mode
        self.limited_env = os.environ.get('CLI_DEBRID_ENVIRONMENT_MODE', 'full') != 'full'

        # Database content directory
        self.db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')

        logging.info(f"DeletionManager initialized: file_mode={self.file_collection_mode}, using_symlinks={self.using_symlinks}, "
                    f"symlink_dir={self.symlink_dir}, media_server={self.media_server}, "
                    f"plex_enabled={self.plex_enabled}, jellyfin_enabled={self.jellyfin_enabled}, "
                    f"limited_env={self.limited_env}")

    def _check_plex_available(self) -> bool:
        """
        Check if Plex is configured and available for deletion operations.

        Plex operations are available when:
        1. file_collection_management == 'Plex' (Plex mode), OR
        2. file_collection_management == 'Symlinked/Local' AND media_server_type == 'plex'

        Additionally, Plex must be properly configured (URL, token, not disabled).
        """
        # Check if Plex should be used based on file collection mode
        plex_mode_enabled = False
        if self.file_collection_mode == 'Plex':
            # Plex mode - Plex operations always available
            plex_mode_enabled = True
        elif self.file_collection_mode == 'Symlinked/Local' and self.media_server == 'plex':
            # Symlinked/Local mode with Plex as media server
            plex_mode_enabled = True

        if not plex_mode_enabled:
            return False

        # Check Plex configuration
        plex_url = get_setting('Plex', 'url', '')
        plex_token = get_setting('Plex', 'token', '')
        disable_plex = get_setting('Plex', 'disable_plex_library_checks', False)

        return bool(plex_url and plex_token and not disable_plex)

    def _check_jellyfin_available(self) -> bool:
        """
        Check if Jellyfin/Emby is configured and available for deletion operations.

        Jellyfin/Emby operations are available when:
        - file_collection_management == 'Symlinked/Local' AND media_server_type == 'jellyfin'

        Note: media_server_type 'jellyfin' covers both Jellyfin and Emby servers.

        Additionally, Jellyfin/Emby must be properly configured (URL, token).
        """
        # Jellyfin/Emby only available in Symlinked/Local mode with jellyfin as media server
        # Note: 'jellyfin' setting covers both Jellyfin and Emby
        if self.file_collection_mode != 'Symlinked/Local' or self.media_server != 'jellyfin':
            return False

        # Check Jellyfin/Emby configuration (stored in Debug section)
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', '')
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', '')

        return bool(jellyfin_url and jellyfin_token)

    def _translate_plex_path_to_local(self, plex_path: str) -> str:
        """
        Translate a Plex path to the local filesystem path.

        Plex stores paths as it sees them (e.g., /movies/Title/file.mkv),
        but the local filesystem may have these at a different location
        (e.g., /mnt/symlinked/movies/Title/file.mkv).

        This function attempts to find the local path by checking if the
        plex_path exists, and if not, prepending the symlinked_files_path.

        Args:
            plex_path: The path as stored in location_on_disk (Plex's view)

        Returns:
            The local filesystem path where the symlink actually exists
        """
        if not plex_path:
            return plex_path

        # First, check if the path already exists (it might already be a local path)
        if os.path.exists(plex_path) or os.path.islink(plex_path):
            return plex_path

        # Get the symlinked files path setting (use instance variable)
        symlinked_files_path = self.symlink_dir
        if not symlinked_files_path:
            logging.debug(f"No symlinked_files_path setting found, returning original path: {plex_path}")
            return plex_path

        # Try to construct the local path by prepending symlinked_files_path
        # The plex_path might be something like /movies/Title/file.mkv
        # and we need /mnt/symlinked/movies/Title/file.mkv

        # Remove leading slash from plex_path if present for joining
        relative_path = plex_path.lstrip('/')
        local_path = os.path.join(symlinked_files_path, relative_path)

        if os.path.exists(local_path) or os.path.islink(local_path):
            logging.debug(f"Translated Plex path '{plex_path}' to local path '{local_path}'")
            return local_path

        # PERFORMANCE FIX: Disable expensive filesystem walk
        # If the symlink doesn't exist at the expected locations, it's likely already deleted
        # Don't waste 2+ minutes per item scanning the entire directory tree
        # Just return the original path - deletion code will handle non-existent files gracefully
        logging.debug(f"[PERF] Skipping expensive os.walk() for '{plex_path}' - file not found at expected locations")
        return plex_path

        # DISABLED: This os.walk() takes 2+ minutes with large libraries!
        # If that didn't work, try matching by filename in the symlinked directory
        # This handles cases where folder structure differs between Plex and local
        # filename = os.path.basename(plex_path)
        #
        # # Try to find the file recursively in symlinked_files_path (limited depth)
        # try:
        #     for root, dirs, files in os.walk(symlinked_files_path):
        #         # Limit depth to avoid searching too deep
        #         depth = root.replace(symlinked_files_path, '').count(os.sep)
        #         if depth > 4:  # Max 4 levels deep
        #             dirs[:] = []  # Don't recurse further
        #             continue
        #         if filename in files:
        #             found_path = os.path.join(root, filename)
        #             if os.path.islink(found_path):
        #                 logging.debug(f"Found symlink by filename search: '{found_path}' for Plex path '{plex_path}'")
        #                 return found_path
        # except Exception as e:
        #     logging.debug(f"Error searching for file in symlinked_files_path: {e}")

        # Return original path if no translation found
        logging.debug(f"Could not translate Plex path '{plex_path}', returning as-is")
        return plex_path

    # =======================================================================
    # State Priority Methods
    # =======================================================================

    def get_state_priority(self, state: str) -> int:
        """Get priority number for a state (lower = higher priority)"""
        return STATE_PRIORITY.get(state, 100)

    def is_delete_candidate(self, state: str) -> bool:
        """Check if state is a deletion candidate (priority >= 6)"""
        return self.get_state_priority(state) >= 6

    # =======================================================================
    # File Deletion Methods (Layer 3 & 5)
    # =======================================================================

    def delete_file_from_disk(self, item: dict, force_delete_parent_folder: bool = False) -> dict:
        """
        Delete file(s) based on configuration

        Args:
            item: The media item to delete files for
            force_delete_parent_folder: If True, delete parent folder even if not empty
                                        (used for whole show/movie deletion)

        Returns:
        {
            'success': bool,
            'deleted_files': List[str],
            'deleted_symlinks': List[str],
            'errors': List[str]
        }
        """
        result = {
            'success': True,
            'deleted_files': [],
            'deleted_symlinks': [],
            'errors': []
        }

        if self.limited_env:
            # Skip actual deletion in limited mode
            logging.warning(f"Limited environment mode - skipping file deletion for item {item.get('id')}")
            result['deleted_files'].append(f"[SIMULATED] {item.get('filled_by_file', 'unknown')}")
            return result

        if self.using_symlinks:
            return self._delete_symlinked_files(item, force_delete_parent_folder=force_delete_parent_folder)
        else:
            return self._delete_plex_mode_files(item)

    def _delete_symlinked_files(self, item: dict, force_delete_parent_folder: bool = False) -> dict:
        """
        Symlinked/Local Mode:
        1. Delete symlink at location_on_disk
        2. Delete original at original_path_for_symlink

        Args:
            item: The media item to delete files for
            force_delete_parent_folder: If True, delete parent folder even if not empty
                                        (used for whole show/movie deletion)
        """
        result = {
            'success': True,
            'deleted_files': [],
            'deleted_symlinks': [],
            'errors': []
        }

        # Use symlink directory from settings (dynamic, user-configurable)
        symlink_base = self.symlink_dir
        if not symlink_base or not os.path.exists(symlink_base):
            logging.warning(f"[SYMLINK_DELETION] Symlink directory does not exist: {symlink_base}")
            result['errors'].append(f"Symlink directory not found: {symlink_base}")
            result['success'] = False
            return result

        # Normalize symlink_base for consistent path comparisons
        symlink_base = os.path.normpath(symlink_base)

        # Check if directory has any content
        try:
            dir_contents = os.listdir(symlink_base)
            if not dir_contents:
                logging.warning(f"[SYMLINK_DELETION] Symlink directory is empty: {symlink_base}")
                result['errors'].append(f"Symlink directory is empty: {symlink_base}")
                result['success'] = False
                return result
            logging.debug(f"[SYMLINK_DELETION] Symlink directory verified: {symlink_base} ({len(dir_contents)} items)")
        except PermissionError as e:
            logging.error(f"[SYMLINK_DELETION] Permission denied accessing symlink directory: {symlink_base}")
            result['errors'].append(f"Permission denied: {symlink_base}")
            result['success'] = False
            return result

        def is_safe_to_delete(folder_path):
            """
            Check if a folder is safe to delete using depth-based protection.

            Depth is calculated from symlink_base (from settings):
            - Depth 0: symlink_base itself - NEVER DELETE
            - Depth 1: symlink_base/<library> (Movies, TV Shows, etc.) - NEVER DELETE
            - Depth 2: symlink_base/<library>/<movie_or_show_name>/ - SAFE TO DELETE
            - Depth 3: symlink_base/<library>/<show_name>/<season>/ - SAFE TO DELETE

            This approach works regardless of library folder names (Movies, Films, Custom, etc.)
            because it protects based on structural depth, not folder names.

            From files perspective:
            - Movie files: library is 1 level up from movie folder (depth 2 = safe)
            - Show files: library is 2 levels up from file (season=depth 3, show=depth 2 = both safe)
            - Season folders: library is 1 level up (show folder at depth 2 = safe)
            """
            if not folder_path:
                return False

            # Normalize path for comparison
            folder_path = os.path.normpath(folder_path)

            # Must be under symlink_base
            if not folder_path.startswith(symlink_base + '/'):
                logging.warning(f"[SYMLINK_DELETION] BLOCKED deletion of folder outside symlink base: {folder_path}")
                return False

            # Calculate depth from symlink_base
            relative_path = folder_path[len(symlink_base):].strip('/')
            depth = len(relative_path.split('/')) if relative_path else 0

            # Only allow deletion of folders at depth >= 2 (movie/show folders and below)
            if depth < 2:
                logging.warning(f"[SYMLINK_DELETION] BLOCKED deletion of protected folder at depth {depth}: {folder_path}")
                return False

            return True

        # Delete symlink and clean up parent folders
        symlink_path = item.get('location_on_disk')
        if symlink_path and os.path.islink(symlink_path):
            try:
                # Get parent folder (movie name or season folder)
                parent_folder = os.path.dirname(symlink_path)
                # Get grandparent folder (for shows: show name folder)
                grandparent_folder = os.path.dirname(parent_folder) if parent_folder else None

                os.unlink(symlink_path)
                result['deleted_symlinks'].append(symlink_path)
                logging.info(f"Deleted symlink: {symlink_path}")

                # Delete subtitle sidecar files left by Bazarr (if setting enabled)
                subtitle_extensions = {'.srt', '.ass', '.sub', '.ssa', '.vtt', '.idx', '.sup'}
                delete_subtitles = get_setting('Bazarr Integration', 'delete_subtitles_on_removal', fallback=True)
                if delete_subtitles and parent_folder and os.path.exists(parent_folder):
                    try:
                        for fname in os.listdir(parent_folder):
                            if os.path.splitext(fname)[1].lower() in subtitle_extensions:
                                sub_path = os.path.join(parent_folder, fname)
                                os.remove(sub_path)
                                logging.info(f"[SYMLINK_DELETION] Deleted subtitle sidecar: {sub_path}")
                    except Exception as e:
                        logging.warning(f"[SYMLINK_DELETION] Error cleaning up subtitle files in {parent_folder}: {e}")

                # Handle parent folder cleanup (with safety checks)
                if parent_folder and os.path.exists(parent_folder) and is_safe_to_delete(parent_folder):
                    try:
                        if force_delete_parent_folder:
                            # Force delete: remove folder even if not empty (whole show/movie delete)
                            shutil.rmtree(parent_folder)
                            logging.info(f"Force deleted folder: {parent_folder}")

                            # For shows: also force remove show folder (if safe)
                            if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete(grandparent_folder):
                                shutil.rmtree(grandparent_folder)
                                logging.info(f"Force deleted show folder: {grandparent_folder}")
                        else:
                            # Normal: only remove if empty (preserves other episodes/seasons)
                            if not os.listdir(parent_folder):
                                os.rmdir(parent_folder)
                                logging.info(f"Deleted empty folder: {parent_folder}")

                                # For shows: also remove show folder if now empty (if safe)
                                if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete(grandparent_folder):
                                    if not os.listdir(grandparent_folder):
                                        os.rmdir(grandparent_folder)
                                        logging.info(f"Deleted empty show folder: {grandparent_folder}")
                    except OSError as e:
                        logging.debug(f"Could not remove folder {parent_folder}: {e}")

            except Exception as e:
                result['errors'].append(f"Failed to delete symlink {symlink_path}: {e}")
                result['success'] = False

        # Delete original file and clean up parent folders
        original_path = item.get('original_path_for_symlink')
        if original_path and os.path.exists(original_path):
            try:
                # Get parent folder (movie name or season folder)
                parent_folder = os.path.dirname(original_path)
                # Get grandparent folder (for shows: show name folder)
                grandparent_folder = os.path.dirname(parent_folder) if parent_folder else None

                os.remove(original_path)
                result['deleted_files'].append(original_path)
                logging.info(f"Deleted original file: {original_path}")

                # Get original files base path from settings for protection
                original_base = self.original_files_path
                if original_base:
                    original_base = os.path.normpath(original_base)

                def is_safe_to_delete_original(folder_path):
                    """Check if folder is safe to delete (not the original_files_path root or library folders)"""
                    if not folder_path or not original_base:
                        return False
                    folder_path = os.path.normpath(folder_path)
                    if not folder_path.startswith(original_base + '/'):
                        logging.warning(f"[ORIGINAL_DELETION] BLOCKED deletion of folder outside original base: {folder_path}")
                        return False
                    # Calculate depth - same logic as symlinks
                    relative_path = folder_path[len(original_base):].strip('/')
                    depth = len(relative_path.split('/')) if relative_path else 0
                    if depth < 2:
                        logging.warning(f"[ORIGINAL_DELETION] BLOCKED deletion of protected folder at depth {depth}: {folder_path}")
                        return False
                    return True

                # Handle parent folder cleanup (with depth-based protection)
                if parent_folder and os.path.exists(parent_folder):
                    try:
                        if force_delete_parent_folder and is_safe_to_delete_original(parent_folder):
                            # Force delete: remove folder even if not empty (whole show/movie delete)
                            shutil.rmtree(parent_folder)
                            logging.info(f"Force deleted folder: {parent_folder}")

                            # For shows: also force remove show folder (if safe)
                            if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete_original(grandparent_folder):
                                shutil.rmtree(grandparent_folder)
                                logging.info(f"Force deleted show folder: {grandparent_folder}")
                        elif not force_delete_parent_folder and is_safe_to_delete_original(parent_folder):
                            # Normal: only remove if empty (preserves other episodes/seasons)
                            if not os.listdir(parent_folder):
                                os.rmdir(parent_folder)
                                logging.info(f"Deleted empty folder: {parent_folder}")

                                # For shows: also remove show folder if now empty (if safe)
                                if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete_original(grandparent_folder):
                                    if not os.listdir(grandparent_folder):
                                        os.rmdir(grandparent_folder)
                                        logging.info(f"Deleted empty show folder: {grandparent_folder}")
                    except OSError as e:
                        logging.debug(f"Could not remove folder {parent_folder}: {e}")

            except Exception as e:
                result['errors'].append(f"Failed to delete original {original_path}: {e}")
                result['success'] = False

        return result

    def _verify_content_in_plex(self, plex_deletion_type: str, content_title: str,
                                imdb_id: str = None, tmdb_id: str = None,
                                season_number: int = None) -> bool:
        """
        Verify if content (show/season/movie) still exists in Plex.
        Used to safely determine if non-empty folders can be force deleted.

        Args:
            plex_deletion_type: 'show', 'season', or 'movie'
            content_title: Title to search for
            imdb_id: Optional IMDB ID for matching
            tmdb_id: Optional TMDB ID for matching
            season_number: Season number (required when type='season')

        Returns:
            True if content exists in Plex, False if not found or error
        """
        if not plex_deletion_type or not content_title:
            logging.warning("[VERIFY_PLEX] Missing deletion type or title - assuming content exists")
            return True  # Conservative: assume it exists if we can't check

        try:
            from utilities.settings import get_setting
            import plexapi.server

            # Get Plex connection
            if get_setting('File Management', 'file_collection_management') == 'Plex':
                plex_url = get_setting('Plex', 'url').rstrip('/')
                plex_token = get_setting('Plex', 'token')
            elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
                plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
            else:
                logging.debug("[VERIFY_PLEX] No Plex configuration - assuming content doesn't exist")
                return False

            if not plex_url or not plex_token:
                logging.debug("[VERIFY_PLEX] Missing Plex URL/token - assuming content doesn't exist")
                return False

            plex = plexapi.server.PlexServer(plex_url, plex_token)
            sections = plex.library.sections()

            logging.info(f"[VERIFY_PLEX] Checking if {plex_deletion_type} '{content_title}' still exists in Plex")

            # Search appropriate library sections
            if plex_deletion_type in ['show', 'season']:
                # Search TV show sections
                for section in sections:
                    if section.type != 'show':
                        continue

                    try:
                        shows = section.search(title=content_title)

                        for show in shows:
                            # Try to match by ID if provided
                            matched = False

                            if imdb_id or tmdb_id:
                                if hasattr(show, 'guids'):
                                    for guid in show.guids:
                                        guid_str = str(guid.id) if hasattr(guid, 'id') else str(guid)
                                        if imdb_id and f'imdb://{imdb_id}' in guid_str:
                                            matched = True
                                            break
                                        if tmdb_id and f'tmdb://{tmdb_id}' in guid_str:
                                            matched = True
                                            break

                            # Check title match
                            if not (imdb_id or tmdb_id) or matched:
                                if show.title.lower() == content_title.lower() or matched:
                                    # Show found
                                    if plex_deletion_type == 'show':
                                        logging.info(f"[VERIFY_PLEX] Show '{content_title}' FOUND in Plex")
                                        return True

                                    # For season, check if specific season exists
                                    if plex_deletion_type == 'season' and season_number is not None:
                                        for season in show.seasons():
                                            if season.seasonNumber == season_number:
                                                logging.info(f"[VERIFY_PLEX] Season {season_number} of '{content_title}' FOUND in Plex")
                                                return True

                                        logging.info(f"[VERIFY_PLEX] Season {season_number} of '{content_title}' NOT FOUND in Plex")
                                        return False

                    except Exception as e:
                        logging.error(f"[VERIFY_PLEX] Error searching section '{section.title}': {e}")

            elif plex_deletion_type == 'movie':
                # Search movie sections
                for section in sections:
                    if section.type != 'movie':
                        continue

                    try:
                        movies = section.search(title=content_title)

                        for movie in movies:
                            # Try to match by ID if provided
                            matched = False

                            if imdb_id or tmdb_id:
                                if hasattr(movie, 'guids'):
                                    for guid in movie.guids:
                                        guid_str = str(guid.id) if hasattr(guid, 'id') else str(guid)
                                        if imdb_id and f'imdb://{imdb_id}' in guid_str:
                                            matched = True
                                            break
                                        if tmdb_id and f'tmdb://{tmdb_id}' in guid_str:
                                            matched = True
                                            break

                            # Check title match
                            if not (imdb_id or tmdb_id) or matched:
                                if movie.title.lower() == content_title.lower() or matched:
                                    logging.info(f"[VERIFY_PLEX] Movie '{content_title}' FOUND in Plex")
                                    return True

                    except Exception as e:
                        logging.error(f"[VERIFY_PLEX] Error searching section '{section.title}': {e}")

            # Content not found in any section
            logging.info(f"[VERIFY_PLEX] Content '{content_title}' NOT FOUND in Plex")
            return False

        except Exception as e:
            logging.error(f"[VERIFY_PLEX] Error connecting to Plex: {e}")
            # Conservative: assume content exists if we can't verify
            return True

    def _batch_delete_symlinks(self, items: List[dict], force_delete_parent_folder: bool = False,
                              plex_deletion_type: str = None, plex_content_title: str = None,
                              plex_imdb_id: str = None, plex_tmdb_id: str = None,
                              plex_season_number: int = None) -> dict:
        """
        Batch delete symlinks for multiple items (optimized for performance)

        Instead of deleting symlinks one-by-one with folder checks for each,
        this method:
        1. Collects all symlink paths upfront
        2. Deletes all symlinks in one pass
        3. Deduplicates parent folders
        4. Cleans up empty folders once at the end
        5. Optionally force-deletes non-empty folders after Plex verification

        This is 5-10x faster than sequential deletion for large batches.

        Args:
            items: List of item dicts with location_on_disk
            force_delete_parent_folder: If True, delete parent folders even if not empty (after Plex verification)
            plex_deletion_type: Type of deletion ('season', 'show', 'movie') for verification
            plex_content_title: Content title for Plex verification
            plex_imdb_id: IMDB ID for Plex verification
            plex_tmdb_id: TMDB ID for Plex verification
            plex_season_number: Season number (required when plex_deletion_type='season')

        Returns:
            dict with deleted_symlinks, folders_removed, errors
        """
        from utilities.settings import get_setting

        result = {
            'success': True,
            'deleted_symlinks': [],
            'folders_removed': [],
            'errors': []
        }

        if not items:
            return result

        symlink_base = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')

        # PHASE 1: Collect all symlink paths (fast, no I/O)
        symlink_paths = []
        for item in items:
            symlink_path = item.get('location_on_disk')
            # Translate Plex-view paths to local paths, same as delete_single_item,
            # so items whose location_on_disk isn't already the local symlink path
            # aren't silently dropped from the batch.
            if symlink_path:
                symlink_path = self._translate_plex_path_to_local(symlink_path)
            if symlink_path and symlink_path.startswith(symlink_base):
                symlink_paths.append(symlink_path)

        if not symlink_paths:
            logging.info(f"[BATCH_DELETE_SYMLINKS] No symlinks to delete")
            return result

        logging.info(f"[BATCH_DELETE_SYMLINKS] Deleting {len(symlink_paths)} symlinks")

        # PHASE 2: Delete all symlinks (batch operation)
        for symlink_path in symlink_paths:
            try:
                # os.path.islink() alone (not gated on os.path.exists()) is required
                # to catch broken symlinks: exists() follows the link and returns
                # False when the target is missing, which is the defining trait of
                # a broken symlink — the exact case this deletion needs to handle.
                if os.path.islink(symlink_path):
                    os.unlink(symlink_path)
                    result['deleted_symlinks'].append(symlink_path)
                elif os.path.exists(symlink_path):
                    logging.warning(f"[BATCH_DELETE_SYMLINKS] Path exists but is not a symlink: {symlink_path}")
                else:
                    logging.info(f"[BATCH_DELETE_SYMLINKS] Symlink {symlink_path} already removed (not found)")
            except Exception as e:
                logging.error(f"[BATCH_DELETE_SYMLINKS] Failed to delete symlink {symlink_path}: {e}")
                result['errors'].append(f"Symlink deletion failed: {str(e)}")

        logging.info(f"[BATCH_DELETE_SYMLINKS] Deleted {len(result['deleted_symlinks'])} symlinks")

        # PHASE 3: Collect unique parent folders (deduplicate)
        parent_folders = set()
        grandparent_folders = set()

        for symlink_path in result['deleted_symlinks']:
            parent = os.path.dirname(symlink_path)
            if parent:
                parent_folders.add(parent)
                grandparent = os.path.dirname(parent)
                if grandparent:
                    grandparent_folders.add(grandparent)

        # Safety check function (same logic as original)
        def is_safe_to_delete_symlink(folder_path):
            """Check if folder is safe to delete using depth-based protection"""
            if not folder_path or not symlink_base:
                return False

            # Must be inside symlink base
            if not folder_path.startswith(symlink_base):
                return False

            # Calculate depth relative to base
            relative_path = os.path.relpath(folder_path, symlink_base)
            depth = len([p for p in relative_path.split(os.sep) if p and p != '.'])

            # Allow deletion for folders at least 2 levels deep
            return depth >= 2

        # PHASE 4: Clean up parent folders (empty or force delete if verified)
        logging.info(f"[BATCH_DELETE_SYMLINKS] Checking {len(parent_folders)} parent folders (force_delete={force_delete_parent_folder})")

        # Sort by depth (deepest first) to clean up correctly
        sorted_parents = sorted(parent_folders, key=lambda p: p.count(os.sep), reverse=True)

        for parent_folder in sorted_parents:
            try:
                if os.path.exists(parent_folder) and is_safe_to_delete_symlink(parent_folder):
                    is_empty = not os.listdir(parent_folder)

                    if is_empty:
                        # Always remove empty folders
                        os.rmdir(parent_folder)
                        result['folders_removed'].append(parent_folder)
                        logging.debug(f"[BATCH_DELETE_SYMLINKS] Removed empty folder: {parent_folder}")
                    elif force_delete_parent_folder and plex_deletion_type:
                        # Non-empty folder with force delete enabled - verify content no longer in Plex
                        logging.info(f"[BATCH_DELETE_SYMLINKS] Non-empty folder detected: {parent_folder}")

                        # Verify content no longer exists in Plex before force deleting
                        content_still_exists = self._verify_content_in_plex(
                            plex_deletion_type=plex_deletion_type,
                            content_title=plex_content_title,
                            imdb_id=plex_imdb_id,
                            tmdb_id=plex_tmdb_id,
                            season_number=plex_season_number
                        )

                        if not content_still_exists:
                            # Safe to force delete - content confirmed removed from Plex
                            import shutil
                            shutil.rmtree(parent_folder)
                            result['folders_removed'].append(parent_folder)
                            logging.info(f"[BATCH_DELETE_SYMLINKS] Force deleted non-empty folder (content not in Plex): {parent_folder}")
                        else:
                            logging.warning(f"[BATCH_DELETE_SYMLINKS] Content still exists in Plex - skipping force delete of: {parent_folder}")
            except OSError as e:
                logging.debug(f"[BATCH_DELETE_SYMLINKS] Could not remove folder {parent_folder}: {e}")
            except Exception as e:
                logging.error(f"[BATCH_DELETE_SYMLINKS] Error processing folder {parent_folder}: {e}")
                result['errors'].append(f"Folder cleanup error: {str(e)}")

        # PHASE 5: Clean up grandparent folders (empty or force delete if verified)
        sorted_grandparents = sorted(grandparent_folders, key=lambda p: p.count(os.sep), reverse=True)

        for grandparent_folder in sorted_grandparents:
            try:
                if os.path.exists(grandparent_folder) and is_safe_to_delete_symlink(grandparent_folder):
                    is_empty = not os.listdir(grandparent_folder)

                    if is_empty:
                        # Always remove empty folders
                        os.rmdir(grandparent_folder)
                        result['folders_removed'].append(grandparent_folder)
                        logging.debug(f"[BATCH_DELETE_SYMLINKS] Removed empty grandparent folder: {grandparent_folder}")
                    elif force_delete_parent_folder and plex_deletion_type == 'show':
                        # For show deletion, also check grandparent (show folder)
                        logging.info(f"[BATCH_DELETE_SYMLINKS] Non-empty show folder detected: {grandparent_folder}")

                        # Verify show no longer exists in Plex
                        content_still_exists = self._verify_content_in_plex(
                            plex_deletion_type='show',
                            content_title=plex_content_title,
                            imdb_id=plex_imdb_id,
                            tmdb_id=plex_tmdb_id
                        )

                        if not content_still_exists:
                            import shutil
                            shutil.rmtree(grandparent_folder)
                            result['folders_removed'].append(grandparent_folder)
                            logging.info(f"[BATCH_DELETE_SYMLINKS] Force deleted non-empty show folder (show not in Plex): {grandparent_folder}")
                        else:
                            logging.warning(f"[BATCH_DELETE_SYMLINKS] Show still exists in Plex - skipping force delete of: {grandparent_folder}")
            except OSError as e:
                logging.debug(f"[BATCH_DELETE_SYMLINKS] Could not remove grandparent {grandparent_folder}: {e}")
            except Exception as e:
                logging.error(f"[BATCH_DELETE_SYMLINKS] Error processing grandparent {grandparent_folder}: {e}")
                result['errors'].append(f"Grandparent cleanup error: {str(e)}")

        logging.info(f"[BATCH_DELETE_SYMLINKS] Complete: {len(result['deleted_symlinks'])} symlinks, {len(result['folders_removed'])} folders removed")

        # Set final success state based on whether errors occurred
        result['success'] = len(result['errors']) == 0

        return result

    def _check_symlinks(self, item: dict) -> dict:
        """
        Check if symlinks exist for an item before deletion.
        Used by library_routes.py to show impact preview.

        Returns:
            dict with 'found', 'symlink_path', 'original_path'
        """
        result = {
            'found': False,
            'symlink_path': None,
            'original_path': None
        }

        if not self.using_symlinks:
            return result

        symlink_path = item.get('location_on_disk')
        original_path = item.get('original_path_for_symlink')

        if symlink_path and os.path.islink(symlink_path):
            result['found'] = True
            result['symlink_path'] = symlink_path

        if original_path and os.path.exists(original_path):
            result['found'] = True
            result['original_path'] = original_path

        return result

    def _validate_symlink_mode_deletion(self, symlink_path: str, item: dict) -> dict:
        """
        Validate that symlink mode deletion was successful.

        In symlink mode, database removal should trigger automatic cleanup:
        - Symlink file should be gone (or will be cleaned up)
        - Plex should detect the missing file on next scan

        Args:
            symlink_path: Path to the symlink that should have been removed
            item: The media item that was deleted

        Returns:
            dict: {
                'symlink_removed': bool,  # True if symlink no longer exists
                'symlink_path': str,      # The path that was checked
                'plex_removed': bool,     # True if item not found in Plex (may be pending scan)
                'validation_time': str,   # When validation occurred
                'message': str            # Human-readable status
            }
        """
        from datetime import datetime

        result = {
            'symlink_removed': False,
            'symlink_path': symlink_path,
            'plex_removed': False,
            'validation_time': datetime.now().isoformat(),
            'message': ''
        }

        # Check if symlink still exists
        if symlink_path:
            symlink_exists = os.path.islink(symlink_path) or os.path.exists(symlink_path)
            result['symlink_removed'] = not symlink_exists

            if symlink_exists:
                logging.info(f"[SYMLINK_VALIDATION] Symlink still exists: {symlink_path}")
            else:
                logging.info(f"[SYMLINK_VALIDATION] Symlink successfully removed: {symlink_path}")
        else:
            # No symlink path to check
            result['symlink_removed'] = True
            logging.debug(f"[SYMLINK_VALIDATION] No symlink path provided for validation")

        # Check Plex status (non-blocking, just for reporting)
        # In symlink mode, Plex removal happens automatically on next library scan
        # We don't actively call Plex API here - just report that removal is pending
        if self.plex_enabled:
            try:
                from utilities.plex_functions import check_item_in_plex
                item_title = item.get('title', '')
                item_path = symlink_path

                # Quick check if item is still in Plex (this is informational only)
                # Note: Plex may not have scanned yet, so item might still appear
                in_plex = check_item_in_plex(item_title, item_path) if item_title else None

                if in_plex is False:
                    result['plex_removed'] = True
                    logging.info(f"[SYMLINK_VALIDATION] Item confirmed removed from Plex: {item_title}")
                elif in_plex is True:
                    result['plex_removed'] = False
                    logging.info(f"[SYMLINK_VALIDATION] Item still in Plex (pending scan): {item_title}")
                else:
                    # Could not determine - assume pending
                    result['plex_removed'] = False
                    logging.debug(f"[SYMLINK_VALIDATION] Could not determine Plex status for: {item_title}")
            except ImportError:
                # check_item_in_plex function doesn't exist yet - that's OK
                result['plex_removed'] = False
                logging.debug(f"[SYMLINK_VALIDATION] Plex validation function not available")
            except Exception as e:
                result['plex_removed'] = False
                logging.debug(f"[SYMLINK_VALIDATION] Error checking Plex status: {e}")
        else:
            # No Plex configured
            result['plex_removed'] = True

        # Build summary message
        if result['symlink_removed'] and result['plex_removed']:
            result['message'] = 'Deletion validated: symlink removed, Plex updated'
        elif result['symlink_removed']:
            result['message'] = 'Symlink removed, Plex update pending (next library scan)'
        else:
            result['message'] = 'Symlink still exists - cleanup may be pending'

        return result

    def _delete_plex_mode_files(self, item: dict) -> dict:
        """
        Plex Mode:
        Delete file at location_on_disk (on rclone mount)
        """
        result = {
            'success': True,
            'deleted_files': [],
            'deleted_symlinks': [],
            'paths_for_plex_scan': [],  # Paths that need Plex scan even if file not found
            'errors': []
        }

        file_path = item.get('location_on_disk') or item.get('filled_by_file')
        if file_path:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    result['deleted_files'].append(file_path)
                    logging.info(f"Deleted file: {file_path}")
                except Exception as e:
                    result['errors'].append(f"Failed to delete file {file_path}: {e}")
                    result['success'] = False
            else:
                # File not found locally - may have been removed by debrid deletion
                # Still track path for Plex scan so it can detect the missing file
                result['paths_for_plex_scan'].append(file_path)
                logging.warning(f"File not found for deletion: {file_path} (will still scan Plex)")

        return result

    # =======================================================================
    # Media Server Removal (Layer 2)
    # =======================================================================

    def remove_from_media_server(self, item: dict) -> dict:
        """
        Remove from Plex, Jellyfin, or Emby

        Returns:
        {
            'success': bool,
            'server_type': str,  # 'plex', 'jellyfin', or 'none'
            'error': Optional[str]
        }
        """
        if self.plex_enabled:
            return self._remove_from_plex(item)
        elif self.jellyfin_enabled:
            return self._remove_from_jellyfin(item)
        else:
            return {'success': True, 'server_type': 'none'}

    def _remove_from_plex(self, item: dict) -> dict:
        """Remove item from Plex using existing function"""
        try:
            from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash

            item_title = item.get('title')
            # Use location_on_disk first, fall back to filled_by_file (same logic as _delete_plex_mode_files)
            item_path = item.get('location_on_disk') or item.get('filled_by_file')
            episode_title = item.get('episode_title') if item.get('type') == 'episode' else None

            # Skip if no file path (blacklisted items, missing files, etc.)
            if not item_path:
                logging.info(f"[PLEX_REMOVAL] Skipping Plex removal for '{item_title}' - no file path available (item may be blacklisted or file missing)")
                return {
                    'success': True,
                    'server_type': 'plex',
                    'error': None
                }

            success = remove_file_from_plex(item_title, item_path, episode_title)

            # If direct removal failed (possibly 400 error), try scan & empty trash as fallback
            if not success:
                logging.warning(f"[PLEX_REMOVAL] Direct Plex removal failed for '{item_title}'. Trying scan & empty trash...")
                try:
                    # Determine section type based on item type
                    section_type = None
                    item_type = item.get('type')
                    if item_type == 'movie':
                        section_type = 'movie'
                    elif item_type in ('episode', 'show'):
                        section_type = 'show'

                    # Use parent folder path for scanning (more efficient than full section scan)
                    import os
                    scan_paths = [os.path.dirname(item_path)] if item_path else None

                    scan_and_empty_plex_trash(paths=scan_paths, section_type=section_type)
                    logging.info(f"[PLEX_REMOVAL] Triggered library scan and trash empty for '{item_title}' (section_type={section_type}).")
                    success = True  # Consider it successful after fallback
                except Exception as scan_err:
                    logging.warning(f"[PLEX_REMOVAL] Scan & empty trash also failed for '{item_title}': {scan_err}")

            return {
                'success': success,
                'server_type': 'plex',
                'error': None if success else 'Plex removal failed'
            }
        except Exception as e:
            logging.error(f"Error removing from Plex: {e}")
            return {
                'success': False,
                'server_type': 'plex',
                'error': str(e)
            }

    def _remove_from_jellyfin(self, item: dict) -> dict:
        """Remove item from Jellyfin/Emby - PLACEHOLDER"""
        # TODO: Implement Jellyfin API client for item deletion
        logging.warning("Jellyfin removal not yet implemented")
        return {
            'success': False,
            'server_type': 'jellyfin',
            'error': 'Jellyfin removal not implemented'
        }

    def scan_and_empty_media_server_trash(self, deleted_paths: list = None, item_type: str = None) -> dict:
        """
        Scan specific paths in media server and empty trash to clean up unavailable items.

        The scan is needed because Plex doesn't move items to trash until it detects
        the files are missing during a library scan.

        Args:
            deleted_paths: List of paths that were deleted (symlink parent folders).
                           If provided, only these specific paths are scanned (fast).
                           If None, falls back to scanning entire sections (slow).
            item_type: Optional - 'movie' or 'episode'/'show' to filter which sections to process.
                       If None, processes all sections.

        Returns:
            dict with 'success', 'paths_scanned', 'sections_cleaned', 'errors'
        """
        if self.plex_enabled:
            try:
                from utilities.plex_functions import scan_and_empty_plex_trash

                # Map item type to section type
                section_type = None
                if item_type == 'movie':
                    section_type = 'movie'
                elif item_type in ('episode', 'show'):
                    section_type = 'show'

                result = scan_and_empty_plex_trash(paths=deleted_paths, section_type=section_type)
                logging.info(f"[PLEX_CLEANUP] Scan and empty trash result: {result}")
                return result

            except Exception as e:
                logging.error(f"Error during Plex scan and trash empty: {e}")
                return {
                    'success': False,
                    'paths_scanned': [],
                    'sections_cleaned': [],
                    'errors': [str(e)]
                }
        elif self.jellyfin_enabled:
            # TODO: Implement Jellyfin scan and trash empty if applicable
            logging.info("Jellyfin scan and trash empty not implemented")
            return {
                'success': True,
                'paths_scanned': [],
                'sections_cleaned': [],
                'errors': []
            }
        else:
            return {
                'success': True,
                'paths_scanned': [],
                'sections_cleaned': [],
                'errors': []
            }

    # =======================================================================
    # Content Source Management (Layer 6)
    # =======================================================================

    def remove_from_content_source(self, item: dict) -> dict:
        """
        Remove item from all enabled content sources to prevent re-addition

        Instead of relying on the content_source field (which may be unreliable),
        this checks which content sources are enabled in settings and attempts
        removal from all applicable sources.

        Args:
            item: Media item dict with imdb_id, tmdb_id, type fields

        Returns:
            dict: {
                'sources_attempted': List[str],  # Which sources were attempted
                'sources_succeeded': List[str],  # Which sources succeeded
                'sources_failed': List[str],     # Which sources failed
                'success': bool,                 # True if at least one removal succeeded
                'message': str,
                'details': dict                  # Detailed results per source
            }
        """
        result = {
            'sources_attempted': [],
            'sources_succeeded': [],
            'sources_failed': [],
            'sources_not_found': [],  # Sources that were checked but item wasn't found (not an error)
            'success': False,
            'message': '',
            'details': {}
        }

        tmdb_id = item.get('tmdb_id')
        imdb_id = item.get('imdb_id')
        media_type = item.get('type')
        item_title = item.get('title', 'Unknown')

        logging.info(f"[CONTENT_SOURCE_REMOVAL] Starting removal for: {item_title} (IMDB: {imdb_id}, TMDB: {tmdb_id}, Type: {media_type})")

        # OPTIMIZATION: Parse item's content sources for smart filtering
        # Only attempt removal from sources that actually added this item
        item_content_sources = []
        if item.get('content_source'):
            item_content_sources.append(item['content_source'])
        if item.get('content_sources'):
            try:
                import json
                sources_list = json.loads(item['content_sources']) if isinstance(item['content_sources'], str) else item['content_sources']
                if sources_list:
                    for s in sources_list:
                        if isinstance(s, dict):
                            # content_sources stores dicts like {"source": "Overseerr_1", ...}
                            source_name = s.get('source', '')
                            if source_name:
                                item_content_sources.append(source_name)
                        elif isinstance(s, str):
                            item_content_sources.append(s)
            except Exception as e:
                logging.warning(f"[CONTENT_SOURCE_REMOVAL] Could not parse content_sources: {e}")

        # Extract source types (e.g., "Overseerr_1" -> "Overseerr", "Trakt Collection_1" -> "Trakt")
        item_source_types = set()
        for source in item_content_sources:
            # Extract type (everything before first underscore or number)
            if '_' in source:
                source_type = source.split('_')[0]
            elif ' ' in source:
                source_type = source.split(' ')[0]
            else:
                source_type = source
            item_source_types.add(source_type)

        if item_content_sources:
            logging.info(f"[CONTENT_SOURCE_REMOVAL] Item content sources: {item_content_sources}")
            logging.info(f"[CONTENT_SOURCE_REMOVAL] Filtered source types: {item_source_types}")
            logging.info(f"[CONTENT_SOURCE_REMOVAL] Will only check relevant sources (skipping others for performance)")
        else:
            logging.warning(f"[CONTENT_SOURCE_REMOVAL] No content_source field found - will check all enabled sources (slower)")

        try:
            # Check Trakt Watchlist - SKIP if item not from Trakt
            if item_source_types and 'Trakt' not in item_source_types:
                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⏭️  Skipping Trakt Watchlist - item not from Trakt source")
            else:
                trakt_watchlist_enabled = get_setting('Trakt', 'trakt_get_watchlist', False)
                logging.info(f"[CONTENT_SOURCE_REMOVAL] Trakt Watchlist enabled: {trakt_watchlist_enabled}")
                if trakt_watchlist_enabled:
                    result['sources_attempted'].append('Trakt_Watchlist')
                    try:
                        from content_checkers.trakt import remove_from_trakt_watchlist
                        removal_result = remove_from_trakt_watchlist([item])
                        result['details']['Trakt_Watchlist'] = removal_result
                        if removal_result.get('success'):
                            result['sources_succeeded'].append('Trakt_Watchlist')
                        else:
                            result['sources_failed'].append('Trakt_Watchlist')
                    except Exception as e:
                        logging.error(f"Error removing from Trakt Watchlist: {e}")
                        result['sources_failed'].append('Trakt_Watchlist')
                        result['details']['Trakt_Watchlist'] = {'success': False, 'message': str(e)}

            # Check Trakt Personal Lists from Content Sources
            content_sources = get_setting('Content Sources', default={})
            trakt_list_sources = []

            # Find all enabled Trakt Lists content sources
            for source_key, source_config in content_sources.items():
                if source_config.get('type') == 'Trakt Lists' and source_config.get('enabled', False):
                    trakt_list_sources.append({
                        'key': source_key,
                        'url': source_config.get('trakt_lists', ''),
                        'display_name': source_config.get('display_name', source_key),
                        'media_type': source_config.get('media_type', 'All')
                    })

            logging.info(f"[CONTENT_SOURCE_REMOVAL] Found {len(trakt_list_sources)} enabled Trakt List(s) in Content Sources")

            # Get authenticated user's Trakt username for permission checking
            # Fetch from Trakt API since it's not stored in settings
            authenticated_username = None
            try:
                import requests
                from content_checkers.trakt import get_trakt_headers

                headers = get_trakt_headers()
                if headers:
                    response = requests.get('https://api.trakt.tv/users/settings', headers=headers)
                    if response.status_code == 200:
                        user_data = response.json()
                        authenticated_username = user_data.get('user', {}).get('username', '')
                        logging.info(f"[CONTENT_SOURCE_REMOVAL] Fetched authenticated Trakt username from API: '{authenticated_username}'")
                    else:
                        logging.warning(f"[CONTENT_SOURCE_REMOVAL] Failed to fetch Trakt username from API: {response.status_code}")
                else:
                    logging.warning(f"[CONTENT_SOURCE_REMOVAL] No Trakt headers available (not authenticated)")
            except Exception as e:
                logging.error(f"[CONTENT_SOURCE_REMOVAL] Error fetching Trakt username: {e}")

            if not authenticated_username:
                logging.warning(f"[CONTENT_SOURCE_REMOVAL] Could not determine authenticated Trakt username - will skip permission checks for all lists")

            for list_source in trakt_list_sources:
                # Extract slug and username from URL
                # Format: https://trakt.tv/users/mash2k3/lists/all-time-favs
                trakt_url = list_source['url']
                list_slug = None
                trakt_username = None

                if '/users/' in trakt_url and '/lists/' in trakt_url:
                    # Extract username (between /users/ and /lists/)
                    parts = trakt_url.split('/users/')[-1]
                    if '/lists/' in parts:
                        trakt_username = parts.split('/lists/')[0]
                        list_slug = parts.split('/lists/')[-1].strip('/')

                list_name = list_source['display_name']

                # Debug: Log extracted username
                logging.info(f"[CONTENT_SOURCE_REMOVAL] Processing list '{list_name}': extracted_username='{trakt_username}', list_slug='{list_slug}', url='{trakt_url}'")

                # PERMISSION CHECK: Only attempt removal from lists owned by authenticated user
                # Skip if we couldn't extract username from URL
                if not trakt_username:
                    logging.warning(f"[CONTENT_SOURCE_REMOVAL] Skipping list '{list_name}' - could not extract username from URL: {trakt_url}")
                    continue

                # Skip if no authenticated username configured
                if not authenticated_username:
                    logging.warning(f"[CONTENT_SOURCE_REMOVAL] Skipping list '{list_name}' - no authenticated Trakt username configured in settings")
                    continue

                # Skip if list is not owned by authenticated user
                logging.info(f"[CONTENT_SOURCE_REMOVAL] Comparing usernames: '{trakt_username.lower()}' vs '{authenticated_username.lower()}'")
                if trakt_username.lower() != authenticated_username.lower():
                    logging.info(f"[CONTENT_SOURCE_REMOVAL] Skipping list '{list_name}' - not owned by authenticated user (list owner: {trakt_username}, authenticated: {authenticated_username})")
                    continue

                # Skip if media type doesn't match (e.g., Movies list for a Show item)
                if list_source['media_type'] != 'All':
                    if media_type == 'movie' and list_source['media_type'] != 'Movies':
                        logging.debug(f"[CONTENT_SOURCE_REMOVAL] Skipping list '{list_name}' - media type mismatch (list is for {list_source['media_type']}, item is {media_type})")
                        continue
                    if media_type == 'show' and list_source['media_type'] != 'Shows':
                        logging.debug(f"[CONTENT_SOURCE_REMOVAL] Skipping list '{list_name}' - media type mismatch (list is for {list_source['media_type']}, item is {media_type})")
                        continue

                if list_slug and trakt_username:
                    source_name = f'Trakt_List_{list_slug}'
                    result['sources_attempted'].append(source_name)
                    logging.info(f"[CONTENT_SOURCE_REMOVAL] Attempting removal from Trakt list '{list_name}' (user: {trakt_username}, slug: {list_slug})")
                    try:
                        from content_checkers.trakt import remove_from_trakt_list
                        removal_result = remove_from_trakt_list(list_slug, [item], username=trakt_username)
                        result['details'][source_name] = removal_result
                        if removal_result.get('success'):
                            logging.info(f"[CONTENT_SOURCE_REMOVAL] ✓ Successfully removed from Trakt list '{list_name}': {removal_result.get('message')}")
                            result['sources_succeeded'].append(source_name)
                        else:
                            logging.warning(f"[CONTENT_SOURCE_REMOVAL] ✗ Failed to remove from Trakt list '{list_name}': {removal_result.get('message')}")
                            result['sources_failed'].append(source_name)
                    except Exception as e:
                        logging.error(f"[CONTENT_SOURCE_REMOVAL] Exception removing from Trakt list '{list_name}': {e}")
                        result['sources_failed'].append(source_name)
                        result['details'][source_name] = {'success': False, 'message': str(e)}

            # Check Trakt Collection from Content Sources
            trakt_collection_sources = []
            logging.debug(f"[CONTENT_SOURCE_REMOVAL] Checking content sources for Trakt Collection...")
            for source_key, source_config in content_sources.items():
                source_type = source_config.get('type', '')
                source_enabled = source_config.get('enabled', False)
                logging.debug(f"[CONTENT_SOURCE_REMOVAL] Source '{source_key}': type='{source_type}', enabled={source_enabled}")

                if source_type == 'Trakt Collection' and source_enabled:
                    trakt_collection_sources.append({
                        'key': source_key,
                        'display_name': source_config.get('display_name', source_key),
                        'media_type': source_config.get('media_type', 'All')
                    })
                    logging.info(f"[CONTENT_SOURCE_REMOVAL] Added Trakt Collection source: '{source_key}' (media_type={source_config.get('media_type', 'All')})")

            logging.info(f"[CONTENT_SOURCE_REMOVAL] Found {len(trakt_collection_sources)} enabled Trakt Collection(s) in Content Sources")

            for collection_source in trakt_collection_sources:
                collection_key = collection_source['key']
                collection_name = collection_source['display_name']
                collection_media_type = collection_source['media_type']

                logging.info(f"[CONTENT_SOURCE_REMOVAL] Processing Trakt Collection '{collection_name}' (media_type={collection_media_type}, item_type={media_type})")

                # Skip if media type doesn't match
                if collection_media_type != 'All':
                    if media_type == 'movie' and collection_media_type != 'Movies':
                        logging.warning(f"[CONTENT_SOURCE_REMOVAL] Skipping collection '{collection_name}' - media type mismatch (collection={collection_media_type}, item={media_type})")
                        continue
                    if media_type == 'show' and collection_media_type != 'Shows':
                        logging.warning(f"[CONTENT_SOURCE_REMOVAL] Skipping collection '{collection_name}' - media type mismatch (collection={collection_media_type}, item={media_type})")
                        continue

                # Use actual source key (e.g., 'Trakt Collection_1') with consistent format
                # Replace spaces with underscores for consistency with other sources
                source_name = collection_key.replace(' ', '_')
                result['sources_attempted'].append(source_name)
                logging.info(f"[CONTENT_SOURCE_REMOVAL] Attempting removal from Trakt Collection '{collection_name}' (source_name={source_name})")
                try:
                    from content_checkers.trakt import remove_from_trakt_collection
                    removal_result = remove_from_trakt_collection([item])
                    result['details'][source_name] = removal_result
                    if removal_result.get('success'):
                        logging.info(f"[CONTENT_SOURCE_REMOVAL] ✓ Successfully removed from Trakt Collection: {removal_result.get('message')}")
                        result['sources_succeeded'].append(source_name)
                    else:
                        logging.warning(f"[CONTENT_SOURCE_REMOVAL] ✗ Failed to remove from Trakt Collection: {removal_result.get('message')}")
                        result['sources_failed'].append(source_name)
                except Exception as e:
                    logging.error(f"[CONTENT_SOURCE_REMOVAL] Exception removing from Trakt Collection: {e}")
                    result['sources_failed'].append(source_name)
                    result['details'][source_name] = {'success': False, 'message': str(e)}

            # Check Scrob Lists/Collection — SKIP if item not from a Scrob source
            # (item_source_types values are the literal source_type strings, e.g.
            # "Scrob Lists_1".split('_')[0] == "Scrob Lists" — check by prefix,
            # not exact match, same as the pre-existing "'Trakt' not in ..." gates above).
            item_is_from_scrob = any(t.startswith('Scrob') for t in item_source_types)
            if item_source_types and not item_is_from_scrob:
                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⏭️  Skipping Scrob - item not from a Scrob source")
            else:
                from content_checkers.scrob import is_deletion_sync_configured
                if not is_deletion_sync_configured():
                    logging.debug("[CONTENT_SOURCE_REMOVAL] Scrob username/password not configured — skipping Scrob deletion sync")
                else:
                    # Scrob Collection
                    scrob_collection_sources = [
                        source_key for source_key, source_config in content_sources.items()
                        if source_config.get('type') == 'Scrob Collection' and source_config.get('enabled', False)
                    ]
                    if scrob_collection_sources:
                        source_name = 'Scrob_Collection'
                        result['sources_attempted'].append(source_name)
                        try:
                            from content_checkers.scrob import remove_from_scrob_collection
                            removal_result = remove_from_scrob_collection([item])
                            result['details'][source_name] = removal_result
                            if removal_result.get('success'):
                                logging.info(f"[CONTENT_SOURCE_REMOVAL] ✓ Successfully removed from Scrob Collection: {removal_result.get('message')}")
                                result['sources_succeeded'].append(source_name)
                            else:
                                logging.warning(f"[CONTENT_SOURCE_REMOVAL] ✗ Failed to remove from Scrob Collection: {removal_result.get('message')}")
                                result['sources_failed'].append(source_name)
                        except Exception as e:
                            logging.error(f"[CONTENT_SOURCE_REMOVAL] Exception removing from Scrob Collection: {e}")
                            result['sources_failed'].append(source_name)
                            result['details'][source_name] = {'success': False, 'message': str(e)}

                    # Scrob Lists — one removal attempt per configured list ID, across all enabled Scrob Lists sources
                    scrob_list_ids = set()
                    for source_key, source_config in content_sources.items():
                        if source_config.get('type') == 'Scrob Lists' and source_config.get('enabled', False):
                            source_media_type = source_config.get('media_type', 'All')
                            if source_media_type != 'All':
                                if media_type == 'movie' and source_media_type != 'Movies':
                                    continue
                                if media_type == 'show' and source_media_type != 'Shows':
                                    continue
                            for lid in (source_config.get('scrob_list_ids', '') or '').split(','):
                                lid = lid.strip()
                                if lid:
                                    scrob_list_ids.add(lid)

                    for list_id in scrob_list_ids:
                        source_name = f'Scrob_List_{list_id}'
                        result['sources_attempted'].append(source_name)
                        try:
                            from content_checkers.scrob import remove_from_scrob_list
                            removal_result = remove_from_scrob_list(list_id, [item])
                            result['details'][source_name] = removal_result
                            if removal_result.get('success'):
                                logging.info(f"[CONTENT_SOURCE_REMOVAL] ✓ Successfully removed from Scrob list {list_id}: {removal_result.get('message')}")
                                result['sources_succeeded'].append(source_name)
                            else:
                                logging.warning(f"[CONTENT_SOURCE_REMOVAL] ✗ Failed to remove from Scrob list {list_id}: {removal_result.get('message')}")
                                result['sources_failed'].append(source_name)
                        except Exception as e:
                            logging.error(f"[CONTENT_SOURCE_REMOVAL] Exception removing from Scrob list {list_id}: {e}")
                            result['sources_failed'].append(source_name)
                            result['details'][source_name] = {'success': False, 'message': str(e)}

            # Check Plex Watchlist - SKIP if item not from Plex Watchlist
            if item_source_types and 'Plex' not in item_source_types and 'My' not in item_source_types:
                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⏭️  Skipping Plex Watchlist - item not from Plex source")
            else:
                for source_key, source_config in content_sources.items():
                    if source_config.get('type') == 'My Plex Watchlist' and source_config.get('enabled', False):
                        result['sources_attempted'].append('Plex_Watchlist')
                        try:
                            from content_checkers.plex_watchlist import remove_from_plex_watchlist_by_item
                            removal_result = remove_from_plex_watchlist_by_item(item)
                            result['details']['Plex_Watchlist'] = removal_result
                            if removal_result.get('success'):
                                result['sources_succeeded'].append('Plex_Watchlist')
                            elif removal_result.get('not_found'):
                                # Item wasn't in watchlist - not an error, just checked
                                result['sources_not_found'].append('Plex_Watchlist')
                                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⊘ Plex Watchlist checked (item not found in source)")
                            else:
                                result['sources_failed'].append('Plex_Watchlist')
                        except Exception as e:
                            logging.error(f"Error removing from Plex Watchlist: {e}")
                            result['sources_failed'].append('Plex_Watchlist')
                            result['details']['Plex_Watchlist'] = {'success': False, 'message': str(e)}

            # Check Overseerr from Content Sources - SKIP if item not from Overseerr
            if item_source_types and 'Overseerr' not in item_source_types:
                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⏭️  Skipping Overseerr - item not from Overseerr source")
            else:
                overseerr_sources = []
                for source_key, source_config in content_sources.items():
                    if source_config.get('type') == 'Overseerr' and source_config.get('enabled', False):
                        overseerr_sources.append({
                            'key': source_key,
                            'display_name': source_config.get('display_name', source_key),
                            'media_type': source_config.get('media_type', 'All'),
                            'url': source_config.get('url', ''),
                            'api_key': source_config.get('api_key', '')
                        })

                logging.info(f"[CONTENT_SOURCE_REMOVAL] Found {len(overseerr_sources)} enabled Overseerr source(s) in Content Sources")

                for overseerr_source in overseerr_sources:
                    overseerr_name = overseerr_source['display_name']

                    # Skip if media type doesn't match
                    if overseerr_source['media_type'] != 'All':
                        if media_type == 'movie' and overseerr_source['media_type'] != 'Movies':
                            logging.debug(f"[CONTENT_SOURCE_REMOVAL] Skipping Overseerr '{overseerr_name}' - media type mismatch")
                            continue
                        if media_type == 'show' and overseerr_source['media_type'] != 'Shows':
                            logging.debug(f"[CONTENT_SOURCE_REMOVAL] Skipping Overseerr '{overseerr_name}' - media type mismatch")
                            continue

                    if tmdb_id or imdb_id:
                        result['sources_attempted'].append('Overseerr')
                        logging.info(f"[CONTENT_SOURCE_REMOVAL] Attempting removal from Overseerr '{overseerr_name}'")
                        try:
                            from content_checkers.overseerr import remove_from_overseerr_by_tmdb_id
                            removal_result = remove_from_overseerr_by_tmdb_id(
                                tmdb_id=tmdb_id,
                                media_type=media_type,
                                imdb_id=imdb_id,  # Fallback if TMDB ID is None
                                overseerr_url=overseerr_source['url'],
                                api_key=overseerr_source['api_key']
                            )
                            result['details']['Overseerr'] = removal_result
                            if removal_result.get('success'):
                                logging.info(f"[CONTENT_SOURCE_REMOVAL] ✓ Successfully removed from Overseerr: {removal_result.get('message')}")
                                result['sources_succeeded'].append('Overseerr')
                            elif removal_result.get('not_found'):
                                # Item wasn't in Overseerr - not an error, just checked
                                result['sources_not_found'].append('Overseerr')
                                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⊘ Overseerr checked (item not found in source)")
                            else:
                                logging.warning(f"[CONTENT_SOURCE_REMOVAL] ✗ Failed to remove from Overseerr: {removal_result.get('message')}")
                                result['sources_failed'].append('Overseerr')
                        except Exception as e:
                            logging.error(f"[CONTENT_SOURCE_REMOVAL] Exception removing from Overseerr: {e}")
                            result['sources_failed'].append('Overseerr')
                            result['details']['Overseerr'] = {'success': False, 'message': str(e)}

            # Check MDBList Personal Lists from Content Sources - DISABLED (removal not supported via MDBList API)
            # NOTE: MDBList removal is not supported, so this entire section is commented out
            if False:  # Disabled - MDBList removal not supported
                pass
            elif item_source_types and 'MDBList' not in item_source_types:
                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⏭️  Skipping MDBList - item not from MDBList source")
            elif False:  # Disabled block below
                mdblist_api_key = get_setting('Content Sources', 'mdblist_api_key', '') or get_setting('MDBList', 'api_key', '')
                mdblist_sources = []

                # Find all enabled MDBList content sources
                for source_key, source_config in content_sources.items():
                    if source_config.get('type') == 'MDBList' and source_config.get('enabled', False):
                        mdblist_sources.append({
                            'key': source_key,
                            'url': source_config.get('urls', ''),
                            'display_name': source_config.get('display_name', source_key),
                            'media_type': source_config.get('media_type', 'All')
                        })

                logging.info(f"[CONTENT_SOURCE_REMOVAL] Found {len(mdblist_sources)} enabled MDBList source(s) in Content Sources")

                if mdblist_api_key and imdb_id:
                    for mdblist_source in mdblist_sources:
                        # Extract list ID from URL
                        mdblist_url = mdblist_source['url']
                        from content_checkers.mdb_list import extract_list_id_from_url
                        list_id = extract_list_id_from_url(mdblist_url)
                        list_name = mdblist_source['display_name']

                        # Skip if media type doesn't match
                        if mdblist_source['media_type'] != 'All':
                            if media_type == 'movie' and mdblist_source['media_type'] != 'Movies':
                                logging.debug(f"[CONTENT_SOURCE_REMOVAL] Skipping MDBList '{list_name}' - media type mismatch")
                                continue
                            if media_type == 'show' and mdblist_source['media_type'] != 'Shows':
                                logging.debug(f"[CONTENT_SOURCE_REMOVAL] Skipping MDBList '{list_name}' - media type mismatch")
                                continue

                        # Only attempt removal for personal lists (have numeric list ID)
                        if list_id:
                            source_label = f'MDBList_{list_id}'
                            result['sources_attempted'].append(source_label)
                            logging.info(f"[CONTENT_SOURCE_REMOVAL] Attempting removal from MDBList '{list_name}' (ID: {list_id})")
                            try:
                                from content_checkers.mdb_list import remove_from_mdblist
                                removal_result = remove_from_mdblist(
                                    api_key=mdblist_api_key,
                                    list_id=list_id,
                                    items=[item]
                                )
                                result['details'][source_label] = removal_result
                                if removal_result.get('success'):
                                    logging.info(f"[CONTENT_SOURCE_REMOVAL] ✓ Successfully removed from MDBList '{list_name}': {removal_result.get('message')}")
                                    result['sources_succeeded'].append(source_label)
                                else:
                                    logging.warning(f"[CONTENT_SOURCE_REMOVAL] ✗ Failed to remove from MDBList '{list_name}': {removal_result.get('message')}")
                                    result['sources_failed'].append(source_label)
                            except Exception as e:
                                logging.error(f"[CONTENT_SOURCE_REMOVAL] Exception removing from MDBList '{list_name}': {e}")
                                result['sources_failed'].append(source_label)
                                result['details'][source_label] = {'success': False, 'message': str(e)}
                elif not mdblist_api_key and mdblist_sources:
                    logging.warning(f"[CONTENT_SOURCE_REMOVAL] MDBList sources configured but API key is missing")

            # Build summary message
            if not result['sources_attempted']:
                result['message'] = 'No content sources with removal capability are enabled'
            elif result['sources_succeeded'] or result['sources_not_found']:
                # Success if we removed from at least one source, or checked sources but item wasn't found
                result['success'] = True
                message_parts = []
                if result['sources_succeeded']:
                    succeeded_list = ', '.join(result['sources_succeeded'])
                    message_parts.append(f'Removed from: {succeeded_list}')
                if result['sources_not_found']:
                    not_found_list = ', '.join(result['sources_not_found'])
                    message_parts.append(f'Checked (not found): {not_found_list}')
                if result['sources_failed']:
                    failed_list = ', '.join(result['sources_failed'])
                    message_parts.append(f'Failed: {failed_list}')
                result['message'] = ' | '.join(message_parts)
            else:
                result['message'] = f'Failed to remove from all attempted sources: {", ".join(result["sources_attempted"])}'

        except Exception as e:
            logging.error(f"Error in remove_from_content_source: {e}", exc_info=True)
            result['message'] = f'Unexpected error: {str(e)}'
            result['success'] = False

        return result

    def check_content_sources(self, item: dict) -> dict:
        """
        Identify which content sources added this item

        Returns:
        {
            'sources': ['Trakt_Watchlist_1', 'Overseerr'],
            'requires_manual_removal': True,
            'cache_files': ['/user/db_content/content_source_*.pkl']
        }
        """
        sources = []

        # Check content_source field
        if item.get('content_source'):
            sources.append(item['content_source'])

        # Check content_sources (comma-separated)
        if item.get('content_sources'):
            sources.extend([s.strip() for s in item['content_sources'].split(',')])

        # Build cache file paths
        cache_files = []
        for source in sources:
            cache_file = os.path.join(self.db_content_dir, f"content_source_{source}_cache.pkl")
            if os.path.exists(cache_file):
                cache_files.append(cache_file)

        return {
            'sources': list(set(sources)),  # Remove duplicates
            'requires_manual_removal': len(sources) > 0,
            'cache_files': cache_files
        }

    def clear_content_source_cache(self, item: dict, cache_files: List[str]) -> bool:
        """
        Clear item from content source caches
        Prevents immediate re-addition (delays ~12 hours)
        """
        import pickle

        try:
            for cache_file in cache_files:
                # Load cache
                with open(cache_file, 'rb') as f:
                    cache = pickle.load(f)

                # Create cache key (simplified - may need adjustment based on actual cache structure)
                imdb_id = item.get('imdb_id', '')
                tmdb_id = item.get('tmdb_id', '')
                media_type = item.get('type', 'movie')

                # Try multiple cache key formats
                possible_keys = [
                    f"imdb_{imdb_id}_{media_type}",
                    f"tmdb_{tmdb_id}_{media_type}",
                    f"{imdb_id}_{media_type}",
                    f"{tmdb_id}_{media_type}"
                ]

                removed_any = False
                for cache_key in possible_keys:
                    if cache_key in cache:
                        del cache[cache_key]
                        removed_any = True
                        logging.info(f"Removed {cache_key} from cache {cache_file}")

                # Save updated cache if we removed anything
                if removed_any:
                    with open(cache_file, 'wb') as f:
                        pickle.dump(cache, f)

            return True
        except Exception as e:
            logging.error(f"Error clearing content source cache: {e}")
            return False

    def _batch_clear_cache(self, items: List[dict]) -> dict:
        """
        Batch clear multiple items from content source caches
        Much faster than per-item clearing - opens each cache file once

        Args:
            items: List of item dicts with imdb_id, tmdb_id, type

        Returns:
            {
                'success': bool,
                'items_cleared': int,
                'cache_files_processed': int,
                'errors': List[str]
            }
        """
        import pickle
        from collections import defaultdict

        result = {
            'success': True,
            'items_cleared': 0,
            'cache_files_processed': 0,
            'errors': []
        }

        try:
            # Group items by cache file to minimize file I/O
            cache_file_to_items = defaultdict(list)

            for item in items:
                # Get source info to find cache files
                source_info = self.check_content_sources(item)

                for cache_file in source_info['cache_files']:
                    cache_file_to_items[cache_file].append(item)

            if not cache_file_to_items:
                logging.info(f"[BATCH_CACHE] No cache files found for {len(items)} items")
                return result

            logging.info(f"[BATCH_CACHE] Clearing {len(items)} items from {len(cache_file_to_items)} cache file(s)")

            # Process each cache file once
            for cache_file, items_in_cache in cache_file_to_items.items():
                try:
                    # Load cache once
                    with open(cache_file, 'rb') as f:
                        cache = pickle.load(f)

                    initial_size = len(cache)
                    removed_count = 0

                    # Remove all items from this cache
                    for item in items_in_cache:
                        imdb_id = item.get('imdb_id', '')
                        tmdb_id = item.get('tmdb_id', '')
                        media_type = item.get('type', 'movie')

                        # Try multiple cache key formats
                        possible_keys = [
                            f"imdb_{imdb_id}_{media_type}",
                            f"tmdb_{tmdb_id}_{media_type}",
                            f"{imdb_id}_{media_type}",
                            f"{tmdb_id}_{media_type}"
                        ]

                        for cache_key in possible_keys:
                            if cache_key in cache:
                                del cache[cache_key]
                                removed_count += 1

                    # Save cache once if we removed anything
                    if removed_count > 0:
                        with open(cache_file, 'wb') as f:
                            pickle.dump(cache, f)

                        result['items_cleared'] += removed_count
                        result['cache_files_processed'] += 1

                        logging.info(f"[BATCH_CACHE] Removed {removed_count} items from {cache_file} (was {initial_size} entries)")

                except FileNotFoundError:
                    logging.debug(f"[BATCH_CACHE] Cache file not found: {cache_file}")
                except Exception as e:
                    error_msg = f"Error processing cache file {cache_file}: {e}"
                    result['errors'].append(error_msg)
                    logging.error(f"[BATCH_CACHE] {error_msg}")
                    result['success'] = False

            logging.info(f"[BATCH_CACHE] Complete: {result['items_cleared']} items cleared from {result['cache_files_processed']} cache file(s)")

        except Exception as e:
            result['success'] = False
            result['errors'].append(f"Batch cache clearing failed: {str(e)}")
            logging.error(f"[BATCH_CACHE] Fatal error: {e}")

        return result

    def get_blacklist_recommendation(self, item: dict, source_info: dict) -> dict:
        """
        Recommend whether to blacklist based on content source

        Returns:
        {
            'should_blacklist': True,
            'reason': 'Item from Trakt - will be re-added',
            'manual_removal_required': True,
            'manual_removal_instructions': 'Remove from Trakt at https://...'
        }
        """
        sources = source_info['sources']

        if not sources:
            return {
                'should_blacklist': False,
                'reason': 'No content source - safe to delete permanently'
            }

        # Map sources to user-friendly instructions
        source_instructions = {
            'Trakt_Watchlist': ('Trakt Watchlist', 'https://trakt.tv/users/me/watchlist'),
            'Overseerr': ('Overseerr', 'Delete request in Overseerr'),
            'Plex_Watchlist': ('Plex Watchlist', 'Remove from Plex watchlist'),
        }

        source_name = sources[0].split('_')[0]  # Get base source name
        instructions = source_instructions.get(sources[0], ('content source', ''))

        return {
            'should_blacklist': True,
            'reason': f'Item from {source_name} - will be re-added when cache expires (~12 hours)',
            'manual_removal_required': True,
            'manual_removal_instructions': instructions[1] if len(instructions) > 1 else 'Remove from source'
        }

    # =======================================================================
    # Main Deletion Methods
    # =======================================================================

    def delete_single_item(self, item_id: int, blacklist: bool = False,
                          clear_cache: bool = True, delete_from_debrid: bool = False,
                          delete_from_media_server: bool = True, delete_files: bool = True,
                          delete_symlinks: bool = True, remove_from_content_source: bool = False,
                          skip_database: bool = False, force_delete_parent_folder: bool = False,
                          skip_filesystem_wait: bool = False) -> dict:
        """
        Delete single item with configurable layers

        Args:
            item_id: Database ID
            blacklist: If True, set state to 'Blacklisted' instead of DELETE
            clear_cache: If True, clear from content source caches
            delete_from_debrid: If True, remove torrent from debrid provider
            delete_from_media_server: If True, remove from Plex/Jellyfin
            delete_files: If True, delete physical files
            delete_symlinks: If True, remove symlinks
            remove_from_content_source: If True, remove from original source (Trakt, Overseerr, etc.)
            skip_database: If True, skip database operations (for ghostlist where DB already updated)
            force_delete_parent_folder: If True, delete parent folder even if not empty (whole show/movie delete)
            skip_filesystem_wait: If True, skip the 1-second filesystem wait (used in batch operations where wait happens once for entire batch)

        Returns:
        {
            'success': bool,
            'item_id': int,
            'title': str,
            'deleted_files': List[str],
            'media_server_removed': bool,
            'cache_cleared': bool,
            'blacklisted': bool,
            'content_source_removal': dict,  # Result of content source removal
            'errors': List[str]
        }
        """
        from database.database_reading import get_item_by_id
        from database.database_writing import update_item_state, remove_from_media_items

        result = {
            'success': False,
            'item_id': item_id,
            'title': '',
            'deleted_files': [],
            'deleted_symlinks': [],
            'media_server_removed': False,
            'media_server_type': 'none',
            'debrid_removed': False,
            'cache_cleared': False,
            'blacklisted': blacklist,
            'content_source_removal': None,
            'errors': []
        }

        try:
            # Get item from database
            item = get_item_by_id(item_id)
            if not item:
                result['errors'].append(f"Item {item_id} not found")
                result['success'] = False  # Explicit for clarity (already False from init)
                return result

            result['title'] = item.get('title', 'Unknown')

            # Check if item is already blacklisted - skip physical operations if so
            item_state = item.get('state', '')
            skip_physical_operations = (item_state == 'Blacklisted')

            if skip_physical_operations:
                logging.info(f"[DELETE_ITEM] Item {item_id} ({result['title']}) has state '{item_state}' - skipping physical operations (Plex/filesystem/debrid/symlinks) as item has no files")

            # Check content sources
            source_info = self.check_content_sources(item)

            # SYMLINK MODE: Follow same deletion order as database_routes.py
            # Order: 1. Symlink -> 2. Original File -> 3. (sleep) -> 4. Plex -> 5. Database
            if self.using_symlinks and not skip_physical_operations:
                logging.info(f"[DELETE_ITEM] Symlink mode - performing full cleanup for item {item_id}")

                # PERFORMANCE TIMING: Track deletion phases
                phase_start = time.time()
                logging.info(f"[DELETE_TIMING] Starting deletion for item {item_id} at {phase_start}")

                symlink_path_from_db = item.get('location_on_disk')
                original_file_path = item.get('original_path_for_symlink')

                logging.info(f"[DELETE_TIMING] Phase 0 (setup) took {time.time() - phase_start:.3f}s")

                # Translate Plex path to local path for symlink deletion
                symlink_path_to_remove = self._translate_plex_path_to_local(symlink_path_from_db) if symlink_path_from_db else None

                if symlink_path_to_remove and symlink_path_to_remove != symlink_path_from_db:
                    logging.info(f"[DELETE_ITEM] Translated path '{symlink_path_from_db}' to local path '{symlink_path_to_remove}'")

                # Keep original Plex path for API call
                path_for_plex_api = symlink_path_from_db

                # Step 1: Delete symlink
                phase_start = time.time()
                if delete_symlinks and symlink_path_to_remove:
                    try:
                        if os.path.exists(symlink_path_to_remove) and os.path.islink(symlink_path_to_remove):
                            os.unlink(symlink_path_to_remove)
                            result['deleted_symlinks'].append(symlink_path_to_remove)
                            logging.info(f"[DELETE_ITEM] Removed symlink {symlink_path_to_remove}")
                        elif os.path.islink(symlink_path_to_remove):
                            # Broken symlink - still remove it
                            os.unlink(symlink_path_to_remove)
                            result['deleted_symlinks'].append(symlink_path_to_remove)
                            logging.info(f"[DELETE_ITEM] Removed broken symlink {symlink_path_to_remove}")
                        else:
                            # Path doesn't exist - symlink already gone, still count as removed for accurate reporting
                            if not os.path.exists(symlink_path_to_remove) and not os.path.islink(symlink_path_to_remove):
                                result['deleted_symlinks'].append(symlink_path_to_remove)
                                logging.info(f"[DELETE_ITEM] Symlink {symlink_path_to_remove} already removed (not found)")
                            else:
                                logging.warning(f"[DELETE_ITEM] Path {symlink_path_to_remove} is not a symlink. Skipping symlink removal.")
                    except Exception as e:
                        logging.error(f"[DELETE_ITEM] Error removing symlink at {symlink_path_to_remove}: {str(e)}")
                        result['errors'].append(f"Symlink removal failed: {str(e)}")
                logging.info(f"[DELETE_TIMING] Phase 1 (symlink deletion) took {time.time() - phase_start:.3f}s")

                # Step 2: Delete original file (if not in limited environment)
                phase_start = time.time()
                if delete_files and original_file_path:
                    if not path_for_plex_api:
                        path_for_plex_api = original_file_path

                    # Check if original_file_path is actually on the debrid mount (can't be deleted)
                    mount_location = get_setting('Plex', 'mounted_file_location', '')
                    is_on_mount = mount_location and original_file_path.startswith(mount_location)

                    if is_on_mount:
                        logging.debug(f"[DELETE_ITEM] Skipping deletion of {original_file_path} - file is on debrid mount (removed via debrid API)")
                    elif not self.limited_env:
                        try:
                            if os.path.exists(original_file_path):
                                os.remove(original_file_path)
                                result['deleted_files'].append(original_file_path)
                                logging.info(f"[DELETE_ITEM] Removed original file {original_file_path}")
                        except Exception as e:
                            logging.error(f"[DELETE_ITEM] Error deleting original file at {original_file_path}: {str(e)}")
                            result['errors'].append(f"Original file deletion failed: {str(e)}")
                    else:
                        logging.info(f"[DELETE_ITEM] Skipped original file deletion for {original_file_path} due to limited environment mode")
                logging.info(f"[DELETE_TIMING] Phase 2 (original file deletion) took {time.time() - phase_start:.3f}s")

                # Step 2.5: Clean up parent folders
                phase_start = time.time()
                if delete_symlinks and symlink_path_to_remove:
                    symlink_base = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')

                    def is_safe_to_delete_symlink(folder_path):
                        """Check if folder is safe to delete using depth-based protection"""
                        if not folder_path or not symlink_base:
                            return False
                        folder_path = os.path.normpath(folder_path)
                        if not folder_path.startswith(symlink_base + '/'):
                            return False
                        relative_path = folder_path[len(symlink_base):].strip('/')
                        depth = len(relative_path.split('/')) if relative_path else 0
                        if depth < 2:
                            return False
                        return True

                    # Get parent and grandparent folders
                    parent_folder = os.path.dirname(symlink_path_to_remove)
                    grandparent_folder = os.path.dirname(parent_folder) if parent_folder else None

                    # Clean up symlink parent folders
                    if parent_folder and os.path.exists(parent_folder) and is_safe_to_delete_symlink(parent_folder):
                        try:
                            if force_delete_parent_folder:
                                # Force delete: remove folder even if not empty (whole show/movie delete)
                                shutil.rmtree(parent_folder)
                                logging.info(f"[DELETE_ITEM] Force deleted symlink folder: {parent_folder}")

                                # For shows: also force remove show folder (if safe)
                                if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete_symlink(grandparent_folder):
                                    shutil.rmtree(grandparent_folder)
                                    logging.info(f"[DELETE_ITEM] Force deleted symlink show folder: {grandparent_folder}")
                            else:
                                # Normal: only remove if empty (preserves other episodes/seasons)
                                if not os.listdir(parent_folder):
                                    os.rmdir(parent_folder)
                                    logging.info(f"[DELETE_ITEM] Deleted empty symlink folder: {parent_folder}")

                                    # For shows: also remove show folder if now empty (if safe)
                                    if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete_symlink(grandparent_folder):
                                        if not os.listdir(grandparent_folder):
                                            os.rmdir(grandparent_folder)
                                            logging.info(f"[DELETE_ITEM] Deleted empty symlink show folder: {grandparent_folder}")
                        except OSError as e:
                            logging.debug(f"[DELETE_ITEM] Could not remove symlink folder {parent_folder}: {e}")

                # Clean up original file folders
                if delete_files and original_file_path:
                    mount_location = get_setting('Plex', 'mounted_file_location', '')
                    is_on_mount = mount_location and original_file_path.startswith(mount_location)

                    # Only clean up folders if original file is NOT on the mount
                    if not is_on_mount:
                        original_base = get_setting('File Management', 'original_files_path', '')

                        def is_safe_to_delete_original(folder_path):
                            """Check if folder is safe to delete using depth-based protection"""
                            if not folder_path or not original_base:
                                return False
                            folder_path = os.path.normpath(folder_path)
                            if not folder_path.startswith(original_base + '/'):
                                return False
                            relative_path = folder_path[len(original_base):].strip('/')
                            depth = len(relative_path.split('/')) if relative_path else 0
                            if depth < 2:
                                return False
                            return True

                        # Get parent and grandparent folders
                        parent_folder = os.path.dirname(original_file_path)
                        grandparent_folder = os.path.dirname(parent_folder) if parent_folder else None

                        # Clean up original file parent folders
                        if parent_folder and os.path.exists(parent_folder):
                            try:
                                if force_delete_parent_folder and is_safe_to_delete_original(parent_folder):
                                    # Force delete: remove folder even if not empty (whole show/movie delete)
                                    shutil.rmtree(parent_folder)
                                    logging.info(f"[DELETE_ITEM] Force deleted original folder: {parent_folder}")

                                    # For shows: also force remove show folder (if safe)
                                    if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete_original(grandparent_folder):
                                        shutil.rmtree(grandparent_folder)
                                        logging.info(f"[DELETE_ITEM] Force deleted original show folder: {grandparent_folder}")
                                elif not force_delete_parent_folder and is_safe_to_delete_original(parent_folder):
                                    # Normal: only remove if empty (preserves other episodes/seasons)
                                    if not os.listdir(parent_folder):
                                        os.rmdir(parent_folder)
                                        logging.info(f"[DELETE_ITEM] Deleted empty original folder: {parent_folder}")

                                        # For shows: also remove show folder if now empty (if safe)
                                        if grandparent_folder and os.path.exists(grandparent_folder) and is_safe_to_delete_original(grandparent_folder):
                                            if not os.listdir(grandparent_folder):
                                                os.rmdir(grandparent_folder)
                                                logging.info(f"[DELETE_ITEM] Deleted empty original show folder: {grandparent_folder}")
                            except OSError as e:
                                logging.debug(f"[DELETE_ITEM] Could not remove original folder {parent_folder}: {e}")
                logging.info(f"[DELETE_TIMING] Phase 2.5 (folder cleanup) took {time.time() - phase_start:.3f}s")

                # Step 3: Wait for filesystem operations to complete
                # Only wait if filesystem operations were performed AND not skipped for batch operations
                phase_start = time.time()
                if not skip_filesystem_wait and (delete_files or delete_symlinks):
                    time.sleep(1)
                    logging.info(f"[DELETE_TIMING] Phase 3 (filesystem wait) took {time.time() - phase_start:.3f}s")
                else:
                    logging.info(f"[DELETE_TIMING] Phase 3 (filesystem wait) skipped: {time.time() - phase_start:.3f}s")

                # Step 4: Remove from Plex
                phase_start = time.time()
                if delete_from_media_server and path_for_plex_api:
                    try:
                        from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
                        logging.info(f"[DELETE_ITEM] Attempting Plex removal for {item.get('title')} using path {path_for_plex_api}")
                        plex_result = remove_file_from_plex(item.get('title', 'Unknown'), path_for_plex_api, item.get('episode_title'))
                        if plex_result:
                            result['media_server_removed'] = True
                            result['media_server_type'] = 'plex'
                            logging.info(f"[DELETE_ITEM] Successfully removed {item.get('title')} from Plex")
                        else:
                            # Direct removal failed - try scan & empty trash as fallback
                            logging.warning(f"[DELETE_ITEM] Direct Plex removal failed for {item.get('title')}. Trying scan & empty trash...")
                            try:
                                # Determine section type based on item type
                                section_type = None
                                if item.get('type') == 'movie':
                                    section_type = 'movie'
                                elif item.get('type') in ('episode', 'show'):
                                    section_type = 'show'

                                # Use parent folder path for scanning
                                import os
                                scan_paths = [os.path.dirname(path_for_plex_api)] if path_for_plex_api else None

                                scan_and_empty_plex_trash(paths=scan_paths, section_type=section_type)
                                logging.info(f"[DELETE_ITEM] Triggered library scan and trash empty for {item.get('title')} (section_type={section_type})")
                            except Exception as scan_err:
                                logging.warning(f"[DELETE_ITEM] Scan & empty trash also failed: {scan_err}")
                    except Exception as e:
                        logging.error(f"[DELETE_ITEM] Error during Plex removal for {item.get('title')}: {str(e)}")
                        result['errors'].append(f"Plex removal failed: {str(e)}")
                elif delete_from_media_server and not path_for_plex_api:
                    logging.warning(f"[DELETE_ITEM] No suitable path found for Plex removal for item {item_id}")
                logging.info(f"[DELETE_TIMING] Phase 4 (Plex removal) took {time.time() - phase_start:.3f}s")

            elif not self.using_symlinks and not skip_physical_operations:
                # PLEX MODE: Plex API removes the item, debrid handles file cleanup
                # Order: 1. Plex API removal -> 2. Debrid removal (done later in this function)
                # No local file deletion needed - files are on rclone mount which reflects debrid state

                # Step 1: Remove from Plex FIRST (Plex uses its own paths like /debrid/shows/...)
                if delete_from_media_server:
                    server_result = self.remove_from_media_server(item)
                    result['media_server_removed'] = server_result['success']
                    result['media_server_type'] = server_result.get('server_type', 'none')
                    if server_result.get('error'):
                        result['errors'].append(server_result['error'])

                    # Track the Plex path for batch scanning in Phase 3 (for delete_multiple)
                    plex_path = item.get('location_on_disk') or item.get('filled_by_file')
                    if plex_path:
                        result['paths_for_plex_scan'] = [plex_path]

                # Note: Debrid removal happens after this block (Layer 4)
                # No local file deletion - rclone mount auto-updates when debrid removes the file

            # Layer 4: Debrid / Usenet removal (if requested)
            if delete_from_debrid and not skip_physical_operations:
                torrent_id = item.get('filled_by_torrent_id')
                item_title = item.get('title', 'Unknown')
                is_nzb = str(torrent_id or '').startswith('nzb:')
                logging.info(f"[DEBRID_REMOVAL] Removal requested. is_nzb={is_nzb}, torrent_id={torrent_id}")
                # Sibling guard: never remove the provider entry while another
                # media_items row (e.g. a different version sharing this job id due
                # to a since-fixed dedup bug) still references it — that would delete
                # the file out from under the surviving row.
                _has_sibling = False
                if torrent_id:
                    try:
                        from database import get_db_connection as _gdb_single_del
                        _conn_sd = _gdb_single_del()
                        try:
                            _sib_row = _conn_sd.execute(
                                "SELECT COUNT(*) FROM media_items "
                                "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                (torrent_id, item_id)
                            ).fetchone()
                            _has_sibling = (_sib_row[0] if _sib_row else 0) > 0
                        finally:
                            _conn_sd.close()
                    except Exception as _sib_err:
                        logging.warning(f"[DEBRID_REMOVAL] Sibling check failed for {torrent_id!r}, skipping removal to be safe: {_sib_err}")
                        _has_sibling = True
                if _has_sibling:
                    logging.info(f"[DEBRID_REMOVAL] Skipping removal of {torrent_id!r} — still referenced by another item")
                elif is_nzb:
                    # NZB item — delete from usenet provider (cli_mount or NzbDAV)
                    try:
                        from usenet import get_usenet_client, reset_usenet_client
                        reset_usenet_client()
                        client = get_usenet_client()
                        info_hash = torrent_id[4:]  # strip 'nzb:'
                        # Pass location_basename (the scene release name) as the
                        # fallback match key: it equals the provider's history entry
                        # name and carries quality/codec, so a stale id on a migrated
                        # item resolves to the right entry. filled_by_file is often an
                        # opaque blob name; title is too loose.
                        nzb_match_name = item.get('location_basename') or item.get('filled_by_file') or item_title
                        removed = client.remove_nzb(info_hash, nzb_match_name)
                        result['debrid_removed'] = removed
                        result['debrid_nzb_removed'] = 1 if removed else 0
                        if removed:
                            logging.info(f"[DEBRID_REMOVAL] Successfully removed NZB {info_hash} from usenet provider for '{item_title}'")
                        else:
                            logging.warning(f"[DEBRID_REMOVAL] Usenet provider removal returned False for {info_hash}")
                    except Exception as e:
                        logging.error(f"[DEBRID_REMOVAL] Failed to remove NZB {torrent_id} from usenet provider: {e}")
                        result['errors'].append(f"Usenet provider removal failed: {str(e)}")
                elif self.debrid and torrent_id:
                    logging.info(f"[DEBRID_REMOVAL] Attempting to remove torrent {torrent_id} for '{item_title}'")
                    try:
                        self.debrid.remove_torrent(torrent_id, removal_reason=f"Library deletion: {item_title}")
                        result['debrid_removed'] = True
                        logging.info(f"[DEBRID_REMOVAL] Successfully removed torrent {torrent_id} from debrid provider")
                    except Exception as e:
                        error_msg = str(e)
                        if '404' in error_msg or 'Not Found' in error_msg:
                            logging.info(f"[DEBRID_REMOVAL] Torrent {torrent_id} already removed from debrid provider (expected after Plex deletion)")
                            result['debrid_removed'] = True
                        else:
                            logging.error(f"[DEBRID_REMOVAL] Failed to remove torrent {torrent_id}: {e}")
                            result['errors'].append(f"Debrid removal failed: {str(e)}")
                elif not self.debrid and not is_nzb:
                    logging.warning(f"[DEBRID_REMOVAL] Skipped - debrid provider not initialized")
                elif not torrent_id:
                    logging.warning(f"[DEBRID_REMOVAL] Skipped - item has no filled_by_torrent_id (title: {item_title})")

            # Layer 6: Clear content source cache (if requested)
            if clear_cache and source_info['cache_files']:
                cache_cleared = self.clear_content_source_cache(item, source_info['cache_files'])
                result['cache_cleared'] = cache_cleared

            # Content Source Removal (before database deletion so we can use item metadata)
            phase_start = time.time()
            if remove_from_content_source:
                logging.info(f"[DELETE_TIMING] Starting content source removal for item {item_id}")
                source_removal_result = self.remove_from_content_source(item)
                result['content_source_removal'] = source_removal_result

                # Log the result
                if source_removal_result.get('success'):
                    logging.info(f"Successfully removed item from content source: {source_removal_result.get('message')}")
                else:
                    logging.warning(f"Content source removal failed: {source_removal_result.get('message')}")
            logging.info(f"[DELETE_TIMING] Phase 6 (content source removal) took {time.time() - phase_start:.3f}s")

            # Layer 1: Database - Blacklist or Delete (LAST - after all other operations)
            phase_start = time.time()
            if skip_database:
                # Skip database operations (already handled by ghostlist)
                result['success'] = True
                logging.info(f"[DELETE_ITEM] Skipped database operation for item {item_id} (already ghostlisted)")
            elif blacklist:
                if update_item_state(item_id, 'Blacklisted'):
                    result['success'] = True
                else:
                    result['errors'].append("Failed to update state to Blacklisted")
            else:
                if remove_from_media_items(item_id):
                    result['success'] = True
                else:
                    result['errors'].append("Failed to delete from database")
            logging.info(f"[DELETE_TIMING] Phase 7 (database deletion) took {time.time() - phase_start:.3f}s")

            # SYMLINK MODE VALIDATION: After database removal, validate that cleanup happened
            phase_start = time.time()
            if self.using_symlinks and result['success'] and not skip_physical_operations:
                symlink_path = result.get('symlink_path_for_validation')
                if symlink_path:
                    validation_result = self._validate_symlink_mode_deletion(symlink_path, item)
                    result['symlink_validation'] = validation_result
                    if not validation_result.get('symlink_removed'):
                        logging.warning(f"[DELETE_ITEM] Symlink still exists after DB removal: {symlink_path}")
                    if not validation_result.get('plex_removed'):
                        logging.info(f"[DELETE_ITEM] Plex removal pending - will happen on next library scan")
            logging.info(f"[DELETE_TIMING] Phase 8 (validation) took {time.time() - phase_start:.3f}s")
            logging.info(f"[DELETE_TIMING] ===== Total deletion for item {item_id} complete =====")

            return result

        except Exception as e:
            logging.error(f"Error deleting item {item_id}: {e}", exc_info=True)
            result['errors'].append(str(e))
            result['success'] = False  # Explicit for clarity (already False from init)
            return result

    def _delete_show_folder(self, item: dict, delete_files: bool = True,
                            delete_symlinks: bool = True) -> dict:
        """
        Delete an entire show folder (and all its contents).
        Used when deleting a whole show to avoid per-episode race conditions.
        ONLY runs in Symlinked/Local mode (not Plex mode).

        Args:
            item: Item dict (any episode from the show)
            delete_files: If True, delete original files folder
            delete_symlinks: If True, delete symlink folder

        Returns:
            dict with 'success', 'deleted_folders', 'errors'
        """
        result = {
            'success': True,
            'deleted_folders': [],
            'errors': []
        }

        location = item.get('location_on_disk')
        if not location:
            result['errors'].append("No location_on_disk found in item")
            result['success'] = False
            return result

        # Get settings paths needed for both operations
        # Note: These settings are stored under 'File Management' section
        symlink_base = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
        original_base = get_setting('File Management', 'original_files_path', '')

        # Extract show folder path (grandparent of file)
        # Structure: /symlink_base/library/ShowName/Season/episode.mkv
        # parent_folder = Season folder (e.g., /symlink_base/library/ShowName/Season)
        # show_folder = Show folder (e.g., /symlink_base/library/ShowName)
        parent_folder = os.path.dirname(location)
        show_folder = os.path.dirname(parent_folder)

        if not show_folder:
            result['errors'].append(f"Could not determine show folder from location: {location}")
            result['success'] = False
            return result

        logging.info(f"[DELETE_SHOW_FOLDER] Extracted show folder: {show_folder}")
        logging.info(f"[DELETE_SHOW_FOLDER] Symlink base: {symlink_base}, Original base: {original_base}")

        # Track what we were asked to delete vs what we actually deleted
        requested_deletions = 0
        successful_deletions = 0

        # Handle symlink deletion
        if delete_symlinks:
            requested_deletions += 1
            if not symlink_base:
                error_msg = "Symlink deletion requested but symlink_location not configured"
                result['errors'].append(error_msg)
                logging.warning(f"[DELETE_SHOW_FOLDER] {error_msg}")
                result['success'] = False
            elif not show_folder.startswith(symlink_base + '/'):
                error_msg = f"Show folder {show_folder} is not under symlink base {symlink_base}"
                result['errors'].append(error_msg)
                logging.warning(f"[DELETE_SHOW_FOLDER] {error_msg}")
                result['success'] = False
            else:
                def is_safe_to_delete_symlink(folder_path):
                    """Check if folder is safe to delete using depth-based protection"""
                    if not folder_path:
                        return False
                    folder_path = os.path.normpath(folder_path)
                    if not folder_path.startswith(symlink_base + '/'):
                        return False
                    relative_path = folder_path[len(symlink_base):].strip('/')
                    depth = len(relative_path.split('/')) if relative_path else 0
                    if depth < 2:
                        logging.warning(f"[SYMLINK_DELETION] BLOCKED: protected folder at depth {depth}: {folder_path}")
                        return False
                    return True

                if not os.path.exists(show_folder):
                    logging.info(f"[DELETE_SHOW_FOLDER] Symlink show folder already gone: {show_folder}")
                    successful_deletions += 1  # Count as success if already deleted
                elif is_safe_to_delete_symlink(show_folder):
                    try:
                        logging.info(f"[DELETE_SHOW_FOLDER] Attempting to delete symlink folder: {show_folder}")
                        shutil.rmtree(show_folder)
                        result['deleted_folders'].append(show_folder)
                        successful_deletions += 1
                        logging.info(f"[DELETE_SHOW_FOLDER] ✓ Deleted symlink show folder: {show_folder}")
                    except Exception as e:
                        error_msg = f"Failed to delete symlink show folder {show_folder}: {e}"
                        result['errors'].append(error_msg)
                        logging.error(f"[DELETE_SHOW_FOLDER] {error_msg}")
                        result['success'] = False
                else:
                    error_msg = f"Safety check failed for symlink show folder: {show_folder}"
                    result['errors'].append(error_msg)
                    logging.warning(f"[DELETE_SHOW_FOLDER] {error_msg}")
                    result['success'] = False

        # Handle original file deletion
        if delete_files:
            requested_deletions += 1
            if not original_base:
                logging.info(f"[DELETE_SHOW_FOLDER] Original files deletion skipped: original_files_path not configured")
                successful_deletions += 1  # Not an error if not configured
            elif not symlink_base:
                error_msg = "Cannot determine original path: symlink_location not configured"
                result['errors'].append(error_msg)
                logging.warning(f"[DELETE_SHOW_FOLDER] {error_msg}")
                result['success'] = False
            elif not show_folder.startswith(symlink_base + '/'):
                error_msg = f"Cannot derive original path: show folder {show_folder} not under symlink base"
                result['errors'].append(error_msg)
                logging.warning(f"[DELETE_SHOW_FOLDER] {error_msg}")
                result['success'] = False
            else:
                # Derive original path by replacing symlink base with original base
                original_show_folder = show_folder.replace(symlink_base, original_base, 1)
                logging.info(f"[DELETE_SHOW_FOLDER] Derived original show folder: {original_show_folder}")

                def is_safe_to_delete_original(folder_path):
                    """Check if folder is safe to delete using depth-based protection"""
                    if not folder_path:
                        return False
                    folder_path = os.path.normpath(folder_path)
                    if not folder_path.startswith(original_base + '/'):
                        return False
                    relative_path = folder_path[len(original_base):].strip('/')
                    depth = len(relative_path.split('/')) if relative_path else 0
                    if depth < 2:
                        logging.warning(f"[ORIGINAL_DELETION] BLOCKED: protected folder at depth {depth}: {folder_path}")
                        return False
                    return True

                if not os.path.exists(original_show_folder):
                    logging.info(f"[DELETE_SHOW_FOLDER] Original show folder already gone: {original_show_folder}")
                    successful_deletions += 1  # Count as success if already deleted
                elif is_safe_to_delete_original(original_show_folder):
                    try:
                        logging.info(f"[DELETE_SHOW_FOLDER] Attempting to delete original folder: {original_show_folder}")
                        shutil.rmtree(original_show_folder)
                        result['deleted_folders'].append(original_show_folder)
                        successful_deletions += 1
                        logging.info(f"[DELETE_SHOW_FOLDER] ✓ Deleted original show folder: {original_show_folder}")
                    except Exception as e:
                        error_msg = f"Failed to delete original show folder {original_show_folder}: {e}"
                        result['errors'].append(error_msg)
                        logging.error(f"[DELETE_SHOW_FOLDER] {error_msg}")
                        result['success'] = False
                else:
                    error_msg = f"Safety check failed for original show folder: {original_show_folder}"
                    result['errors'].append(error_msg)
                    logging.warning(f"[DELETE_SHOW_FOLDER] {error_msg}")
                    result['success'] = False

        # Final success check: we must have successfully handled all requested deletions
        if result['success'] and successful_deletions < requested_deletions:
            result['success'] = False
            result['errors'].append(f"Only {successful_deletions}/{requested_deletions} deletions completed")

        if result['deleted_folders']:
            logging.info(f"[DELETE_SHOW_FOLDER] ✓ Successfully deleted {len(result['deleted_folders'])} folder(s): {result['deleted_folders']}")
        elif result['success']:
            logging.info(f"[DELETE_SHOW_FOLDER] ✓ No folders needed deletion (already gone)")
        else:
            logging.error(f"[DELETE_SHOW_FOLDER] ✗ Deletion failed with errors: {result['errors']}")

        return result

    def delete_multiple_items(self, item_ids: List[int], blacklist: bool = False,
                             blacklist_sources: bool = False,
                             delete_from_media_server: bool = True,
                             delete_files: bool = True,
                             delete_from_debrid: bool = False,
                             delete_symlinks: bool = True,
                             clear_cache: bool = True,
                             remove_from_content_source: bool = False,
                             skip_database: bool = False,
                             force_delete_parent_folder: bool = False,
                             plex_deletion_type: str = None,
                             plex_content_title: str = None,
                             plex_imdb_id: str = None,
                             plex_tmdb_id: str = None,
                             plex_season_number: int = None) -> dict:
        """
        Delete multiple items at once

        Args:
            item_ids: List of database IDs to delete
            blacklist: If True, blacklist the items
            blacklist_sources: If True, blacklist the content sources
            delete_from_media_server: If True, remove from Plex/Jellyfin
            delete_files: If True, delete physical files
            delete_from_debrid: If True, remove from debrid provider
            delete_symlinks: If True, remove symlinks
            clear_cache: If True, clear from content source caches
            remove_from_content_source: If True, remove from original source (Trakt, Overseerr, etc.)
            skip_database: If True, skip database operations (for ghostlist where DB already updated)
            force_delete_parent_folder: If True, delete parent folder even if not empty (whole show/movie delete)
            plex_deletion_type: Type of Plex deletion - 'show', 'season', 'movie', or None for episode-level
            plex_content_title: Title of show/movie for Plex lookup
            plex_imdb_id: IMDB ID for accurate Plex matching
            plex_tmdb_id: TMDB ID for accurate Plex matching
            plex_season_number: Season number (required when plex_deletion_type='season')

        Returns:
        {
            'success': bool,
            'deleted_count': int,
            'failed_count': int,
            'results': List[dict],  # Individual results
            'errors': List[str],
            'database_locked': bool  # True if database lock detected
        }
        """
        aggregate_result = {
            'success': True,
            'deleted_count': 0,
            'failed_count': 0,
            'results': [],
            'errors': [],
            'database_locked': False,
            'plex_deleted': False,
            'debrid_removed': False,
            'debrid_torrents_removed': 0
        }

        # Content-level Plex deletion (show/season/movie) FIRST - applies to BOTH modes
        # This avoids per-episode API calls and removes from Plex before files disappear
        # Works for: Plex mode (debrid mount) AND Symlink mode (local symlinks + debrid)
        plex_content_deleted = False
        if delete_from_media_server and plex_deletion_type and plex_content_title:
            logging.info(f"[DELETE_MULTIPLE] Content-level Plex deletion: {plex_deletion_type}-level for '{plex_content_title}'")

            try:
                if plex_deletion_type == 'show':
                    from utilities.plex_functions import remove_show_from_plex
                    plex_result = remove_show_from_plex(
                        show_title=plex_content_title,
                        imdb_id=plex_imdb_id,
                        tmdb_id=plex_tmdb_id
                    )
                    if plex_result['success']:
                        plex_content_deleted = True
                        aggregate_result['plex_deleted'] = True
                        logging.info(f"[DELETE_MULTIPLE] Plex show '{plex_content_title}' deleted successfully")
                    else:
                        logging.warning(f"[DELETE_MULTIPLE] Plex show deletion failed: {plex_result.get('error')}")
                        aggregate_result['errors'].append(f"Plex show deletion failed: {plex_result.get('error')}")

                elif plex_deletion_type == 'season' and plex_season_number is not None:
                    from utilities.plex_functions import remove_season_from_plex
                    plex_result = remove_season_from_plex(
                        show_title=plex_content_title,
                        season_number=plex_season_number,
                        imdb_id=plex_imdb_id,
                        tmdb_id=plex_tmdb_id
                    )
                    if plex_result['success']:
                        plex_content_deleted = True
                        aggregate_result['plex_deleted'] = True
                        logging.info(f"[DELETE_MULTIPLE] Plex season {plex_season_number} of '{plex_content_title}' deleted successfully")
                    else:
                        logging.warning(f"[DELETE_MULTIPLE] Plex season deletion failed: {plex_result.get('error')}")
                        aggregate_result['errors'].append(f"Plex season deletion failed: {plex_result.get('error')}")

                elif plex_deletion_type == 'movie':
                    from utilities.plex_functions import remove_movie_from_plex
                    plex_result = remove_movie_from_plex(
                        movie_title=plex_content_title,
                        imdb_id=plex_imdb_id,
                        tmdb_id=plex_tmdb_id
                    )
                    if plex_result['success']:
                        plex_content_deleted = True
                        aggregate_result['plex_deleted'] = True
                        logging.info(f"[DELETE_MULTIPLE] Plex movie '{plex_content_title}' deleted successfully")
                    else:
                        logging.warning(f"[DELETE_MULTIPLE] Plex movie deletion failed: {plex_result.get('error')}")
                        aggregate_result['errors'].append(f"Plex movie deletion failed: {plex_result.get('error')}")

            except Exception as e:
                logging.error(f"[DELETE_MULTIPLE] Error during Plex content-level deletion: {e}")
                aggregate_result['errors'].append(f"Plex deletion error: {str(e)}")

        # CRITICAL CHECK: If Plex deletion was requested but failed, stop here
        # This prevents orphaned Plex entries (items in Plex with no files)
        # Exception: "not found" errors are OK - nothing to delete
        # Exception: "400 Bad Request" errors indicate media deletion is disabled - fallback to path-based deletion
        if delete_from_media_server and plex_deletion_type and not plex_content_deleted and self.plex_enabled:
            # Check if the error is "not found" - this is OK, nothing to delete
            is_not_found_error = any('not found in Plex' in str(err) for err in aggregate_result['errors'])

            # Check if the error is "400 Bad Request" (media deletion disabled in Plex settings)
            # In this case, we can fallback to path-based deletion which doesn't require that setting
            is_deletion_disabled = any('Plex deletion disabled' in str(err) or '400 Bad Request' in str(err)
                                      for err in aggregate_result['errors'])

            if not is_not_found_error and not is_deletion_disabled:
                # Real error occurred - abort to prevent orphaned Plex entries
                error_msg = f"Plex deletion failed for {plex_deletion_type} '{plex_content_title}'. Aborting to prevent orphaned Plex entries."
                logging.error(f"[DELETE_MULTIPLE] {error_msg}")
                aggregate_result['success'] = False
                aggregate_result['errors'].append(error_msg)
                aggregate_result['deleted_count'] = 0  # Use existing key, not undefined 'items_deleted'
                return aggregate_result
            elif is_deletion_disabled:
                # Media deletion disabled in Plex - fallback to path-based deletion
                logging.warning(f"[DELETE_MULTIPLE] Content-level Plex deletion disabled (400 Bad Request) - will fallback to path-based deletion")
                # Clear the 400 error from the list since we're handling it with a fallback
                aggregate_result['errors'] = [err for err in aggregate_result['errors']
                                             if 'Plex deletion disabled' not in str(err) and '400 Bad Request' not in str(err)]
            else:
                # Not found error — signal the caller to prompt the user before continuing
                logging.info(f"[DELETE_MULTIPLE] Plex content not found (already deleted or never added) - returning plex_not_found for user confirmation")
                aggregate_result['success'] = False
                aggregate_result['plex_not_found'] = True
                return aggregate_result

        # PHASE 0: Deduplicated debrid/usenet removal - collect unique IDs and remove each ONCE
        # This prevents removing the same torrent/NZB 10+ times when episodes share a pack
        debrid_removed_ids = set()
        if delete_from_debrid:
            from database.database_reading import get_item_by_id

            # Collect unique torrent/NZB IDs from all items, split by type
            unique_torrent_ids = {}   # torrent_id -> (title, infohash, debrid_folder_name)
            unique_nzb_ids = {}       # info_hash -> (filled_by_file, title) (cli_mount)
            for item_id in item_ids:
                try:
                    item = get_item_by_id(item_id)
                    if item and item.get('filled_by_torrent_id'):
                        tid = item['filled_by_torrent_id']
                        title = item.get('title', 'Unknown')
                        if str(tid).startswith('nzb:'):
                            if tid not in unique_nzb_ids:
                                # location_basename = scene release name = the provider's
                                # history entry name (the exact-name fallback key).
                                match_name = item.get('location_basename') or item.get('filled_by_file') or title
                                unique_nzb_ids[tid] = (match_name, title)
                        elif tid not in unique_torrent_ids:
                            # Store infohash (from magnet) and folder name for cli_mount deletion
                            magnet = item.get('filled_by_magnet') or ''
                            import re as _re
                            _m = _re.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re.IGNORECASE)
                            infohash = _m.group(1).lower() if _m else ''
                            folder_name = item.get('debrid_folder_name') or ''
                            unique_torrent_ids[tid] = (title, infohash, folder_name)
                except Exception as e:
                    logging.warning(f"[DELETE_MULTIPLE] Could not get item {item_id} for dedup: {e}")

            # Sibling guard: never remove a torrent/NZB from the provider while a
            # media_items row OUTSIDE this delete batch (e.g. a different version
            # sharing the same job id due to a since-fixed dedup bug) still references
            # it — that would delete the file out from under the surviving row,
            # leaving it with a broken symlink. Mirrors the guard already used for
            # single-item deletion in routes/database_routes.py.
            _batch_id_set = set(item_ids)

            def _has_external_sibling(_torrent_or_nzb_id):
                # Select rows by torrent id (one small query, no per-batch-item
                # placeholder) and compare returned ids against the batch in
                # Python — avoids SQLite's bound-parameter limit on large batches
                # (e.g. deleting an entire show with thousands of episodes).
                try:
                    from database import get_db_connection as _gdb_dm
                    _conn_dm = _gdb_dm()
                    try:
                        _rows = _conn_dm.execute(
                            "SELECT id FROM media_items "
                            "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking')",
                            (_torrent_or_nzb_id,)
                        ).fetchall()
                        return any(r[0] not in _batch_id_set for r in _rows)
                    finally:
                        _conn_dm.close()
                except Exception as _sib_err:
                    logging.warning(f"[DELETE_MULTIPLE] Sibling check failed for {_torrent_or_nzb_id!r}, skipping removal to be safe: {_sib_err}")
                    return True

            # Remove NZB items from usenet provider (cli_mount or NzbDAV)
            if unique_nzb_ids:
                logging.info(f"[DELETE_MULTIPLE] Phase 0: Removing {len(unique_nzb_ids)} NZB(s) from usenet provider")
                try:
                    from usenet import get_usenet_client, reset_usenet_client
                    reset_usenet_client()
                    dcy_client = get_usenet_client()
                    for tid, (file_name, title) in unique_nzb_ids.items():
                        info_hash = tid[4:]
                        if _has_external_sibling(tid):
                            logging.info(f"[DELETE_MULTIPLE] Skipping usenet provider removal for {info_hash} — still referenced by an item outside this batch")
                            continue
                        try:
                            removed = dcy_client.remove_nzb(info_hash, file_name or title)
                            if removed:
                                debrid_removed_ids.add(tid)
                                logging.info(f"[DELETE_MULTIPLE] Removed NZB {info_hash} from usenet provider")
                            else:
                                logging.warning(f"[DELETE_MULTIPLE] Usenet provider remove returned False for {info_hash}")
                        except Exception as e:
                            logging.error(f"[DELETE_MULTIPLE] Failed to remove NZB {info_hash}: {e}")
                            aggregate_result['errors'].append(f"Usenet provider removal failed for {tid}: {str(e)}")
                except Exception as e:
                    logging.error(f"[DELETE_MULTIPLE] Usenet provider client error: {e}")

            # Remove torrent items from debrid provider
            if unique_torrent_ids and self.debrid:
                logging.info(f"[DELETE_MULTIPLE] Phase 0: Removing {len(unique_torrent_ids)} unique torrent(s) from debrid (from {len(item_ids)} items) - PARALLEL MODE (5 concurrent)")

                def remove_single_torrent(torrent_id_and_info):
                    torrent_id, (first_title, infohash, folder_name) = torrent_id_and_info
                    if _has_external_sibling(torrent_id):
                        logging.info(f"[DELETE_MULTIPLE] Skipping debrid removal for {torrent_id} — still referenced by an item outside this batch")
                        return torrent_id, False, None
                    try:
                        self.debrid.remove_torrent(torrent_id, removal_reason=f"Library deletion: {first_title}", skip_hash_tracking=True)
                        logging.info(f"[DELETE_MULTIPLE] Removed torrent {torrent_id} from debrid")
                    except Exception as e:
                        error_msg = str(e)
                        if '404' in error_msg or 'Not Found' in error_msg:
                            logging.info(f"[DELETE_MULTIPLE] Torrent {torrent_id} already removed from debrid")
                        else:
                            logging.error(f"[DELETE_MULTIPLE] Failed to remove torrent {torrent_id}: {e}")
                            return torrent_id, False, str(e)
                    # Also remove from cli_mount — it keeps its own entry separate from RD
                    if infohash:
                        try:
                            from usenet.debrid_repair_engine import _delete_from_climount
                            _delete_from_climount(infohash, folder_name)
                            logging.info(f"[DELETE_MULTIPLE] Removed torrent {infohash} from cli_mount")
                        except Exception as dcy_err:
                            logging.warning(f"[DELETE_MULTIPLE] cli_mount removal failed for {infohash}: {dcy_err}")
                    return torrent_id, True, None

                with ThreadPoolExecutor(max_workers=5) as executor:
                    future_to_torrent = {
                        executor.submit(remove_single_torrent, (tid, info)): tid
                        for tid, info in unique_torrent_ids.items()
                    }
                    for future in as_completed(future_to_torrent):
                        torrent_id, success, error = future.result()
                        if success:
                            debrid_removed_ids.add(torrent_id)
                        elif error:
                            aggregate_result['errors'].append(f"Debrid removal failed for {torrent_id}: {error}")

            total = len(unique_torrent_ids) + len(unique_nzb_ids)
            if total:
                logging.info(f"[DELETE_MULTIPLE] Phase 0 complete: {len(debrid_removed_ids)}/{total} items removed")
                aggregate_result['debrid_removed'] = len(debrid_removed_ids) > 0
                aggregate_result['debrid_torrents_removed'] = sum(1 for tid in debrid_removed_ids if not str(tid).startswith('nzb:'))
                aggregate_result['debrid_nzb_removed'] = sum(1 for tid in debrid_removed_ids if str(tid).startswith('nzb:'))
            else:
                logging.info(f"[DELETE_MULTIPLE] Phase 0: No torrent/NZB IDs found for removal")

        # PHASE 0.5: Batch cache clearing (if enabled) - BEFORE per-item loop
        # Opens each cache file once instead of once per item
        batch_cache_cleared = False
        if clear_cache:
            from database.database_reading import get_items_by_ids

            items_for_cache = get_items_by_ids(item_ids)

            if items_for_cache:
                logging.info(f"[DELETE_MULTIPLE] Phase 0.5: Batch cache clearing for {len(items_for_cache)} items")
                batch_cache_result = self._batch_clear_cache(items_for_cache)

                if batch_cache_result['success']:
                    batch_cache_cleared = True
                    logging.info(f"[DELETE_MULTIPLE] Batch cache clearing successful: {batch_cache_result['items_cleared']} items cleared")
                else:
                    logging.warning(f"[DELETE_MULTIPLE] Batch cache clearing had errors: {batch_cache_result['errors']}")
                    aggregate_result['errors'].extend(batch_cache_result['errors'])
            else:
                logging.warning(f"[DELETE_MULTIPLE] Could not get items for batch cache clearing")

        # PHASE 1: Physical cleanup (symlinks, cache) - debrid already handled in Phase 0
        # Skip per-item Plex calls if we already did content-level deletion above
        # Only force delete folders if Plex deletion succeeded (prevents orphaned Plex entries)
        logging.info(f"[DELETE_MULTIPLE] Phase 1: Physical cleanup for {len(item_ids)} items")
        if force_delete_parent_folder and not plex_content_deleted:
            logging.warning(f"[DELETE_MULTIPLE] Force folder deletion disabled: Plex deletion did not succeed. Will only delete empty folders.")

        # OPTIMIZATION: Batch symlink deletion if using symlinks and delete_symlinks enabled
        batch_result = None
        if delete_symlinks and self.using_symlinks:
            from database.database_reading import get_items_by_ids

            # Get all items at once for batch processing
            items_for_batch = get_items_by_ids(item_ids)

            if items_for_batch:
                logging.info(f"[DELETE_MULTIPLE] Using BATCH symlink deletion for {len(items_for_batch)} items")

                # Batch delete all symlinks at once
                # Only force delete if Plex content-level deletion succeeded (prevents orphaned entries)
                batch_result = self._batch_delete_symlinks(
                    items_for_batch,
                    force_delete_parent_folder=force_delete_parent_folder and plex_content_deleted,
                    plex_deletion_type=plex_deletion_type,
                    plex_content_title=plex_content_title,
                    plex_imdb_id=plex_imdb_id,
                    plex_tmdb_id=plex_tmdb_id,
                    plex_season_number=plex_season_number
                )

                # Single sleep after batch operation (instead of 1 second per item)
                time.sleep(1)

                logging.info(f"[DELETE_MULTIPLE] Batch symlink deletion complete: {len(batch_result['deleted_symlinks'])} symlinks, {len(batch_result['folders_removed'])} folders")

                # Now process each item for cache/content-source cleanup only
                # Skip symlink/file operations since we already did them in batch
                for item_id in item_ids:
                    result = self.delete_single_item(
                        item_id,
                        blacklist=blacklist,
                        clear_cache=False if batch_cache_cleared else clear_cache,  # Skip if already done in batch
                        delete_from_debrid=False,  # Already handled in Phase 0
                        delete_from_media_server=delete_from_media_server and not plex_content_deleted,  # Skip if content-level deletion succeeded, otherwise fall back to individual
                        delete_files=False,  # Already handled in batch above
                        delete_symlinks=False,  # IMPORTANT: Already done in batch above
                        remove_from_content_source=remove_from_content_source,
                        skip_database=True,  # Always skip DB in Phase 1
                        force_delete_parent_folder=False,  # Already handled in batch
                        skip_filesystem_wait=True  # Skip per-item wait, batch-level wait at line 2312
                    )

                    # Merge batch results into individual result for tracking
                    if batch_result['deleted_symlinks']:
                        result['deleted_symlinks'] = batch_result['deleted_symlinks']
                    if batch_result['errors']:
                        result['errors'].extend(batch_result['errors'])

                    aggregate_result['results'].append(result)

                    if not result['success']:
                        aggregate_result['failed_count'] += 1
                        aggregate_result['errors'].extend(result['errors'])
            else:
                logging.warning(f"[DELETE_MULTIPLE] Could not get items for batch deletion, falling back to sequential")
                # Fall back to original sequential processing
                for item_id in item_ids:
                    result = self.delete_single_item(
                        item_id,
                        blacklist=blacklist,
                        clear_cache=False if batch_cache_cleared else clear_cache,  # Skip if already done in batch
                        delete_from_debrid=False,
                        delete_from_media_server=delete_from_media_server and not plex_content_deleted,  # Skip if content-level deletion succeeded, otherwise fall back to individual
                        delete_files=delete_files,
                        delete_symlinks=delete_symlinks,
                        remove_from_content_source=remove_from_content_source,
                        skip_database=True,
                        force_delete_parent_folder=force_delete_parent_folder and plex_content_deleted,
                        skip_filesystem_wait=False  # Keep per-item wait in fallback mode since no batch-level wait
                    )
                    aggregate_result['results'].append(result)

                    if not result['success']:
                        aggregate_result['failed_count'] += 1
                        aggregate_result['errors'].extend(result['errors'])
        else:
            # Non-symlink mode or symlink deletion disabled - use original sequential processing
            logging.info(f"[DELETE_MULTIPLE] Using sequential deletion (symlinks={'enabled' if delete_symlinks else 'disabled'}, mode={'symlink' if self.using_symlinks else 'plex'})")
            for item_id in item_ids:
                result = self.delete_single_item(
                    item_id,
                    blacklist=blacklist,
                    clear_cache=False if batch_cache_cleared else clear_cache,  # Skip if already done in batch
                    delete_from_debrid=False,
                    delete_from_media_server=delete_from_media_server and not plex_content_deleted,  # Skip if content-level deletion succeeded, otherwise fall back to individual
                    delete_files=delete_files,
                    delete_symlinks=delete_symlinks,
                    remove_from_content_source=remove_from_content_source,
                    skip_database=True,
                    force_delete_parent_folder=force_delete_parent_folder and plex_content_deleted
                )
                aggregate_result['results'].append(result)

                if not result['success']:
                    aggregate_result['failed_count'] += 1
                    aggregate_result['errors'].extend(result['errors'])

        # PHASE 2: Database deletion - ALL items in ONE transaction
        if not skip_database:
            logging.info(f"[DELETE_MULTIPLE] Phase 2: Batch database deletion for {len(item_ids)} items (blacklist={blacklist})")
            from database.database_writing import delete_items_batch

            db_result = delete_items_batch(
                item_ids=item_ids,
                blacklist=blacklist
            )

            if db_result['success']:
                aggregate_result['deleted_count'] = db_result['deleted_count']
                logging.info(f"[DELETE_MULTIPLE] Database deletion successful: {db_result['deleted_count']} items")
            else:
                # Database deletion failed
                aggregate_result['failed_count'] += len(item_ids)
                aggregate_result['errors'].append(db_result['error'])
                aggregate_result['success'] = False

                # Check for database lock
                if db_result.get('database_locked'):
                    aggregate_result['database_locked'] = True
                    logging.error("[DELETE_MULTIPLE] Database lock detected during batch deletion")

                logging.error(f"[DELETE_MULTIPLE] Database deletion failed: {db_result['error']}")
        else:
            # Database skipped (handled externally, e.g., show delete with ghostlist)
            aggregate_result['deleted_count'] = len(item_ids)
            logging.info(f"[DELETE_MULTIPLE] Database operations skipped (skip_database=True)")

        # PHASE 3: Scan and empty media server trash to clean up unavailable items
        # Skip if we already did content-level Plex deletion (show/season/movie level)
        if plex_content_deleted:
            logging.info(f"[DELETE_MULTIPLE] Phase 3: Skipped - content-level Plex deletion already completed")
        else:
            # Collect all paths that need Plex scanning (deleted files, symlinks, or paths where files were already gone)
            deleted_paths_for_scan = []
            for r in aggregate_result['results']:
                # Check for deleted symlinks (Symlink/Local mode)
                for symlink_path in r.get('deleted_symlinks', []):
                    parent_folder = os.path.dirname(symlink_path)
                    if parent_folder and parent_folder not in deleted_paths_for_scan:
                        deleted_paths_for_scan.append(parent_folder)
                # Check for deleted files (Plex mode)
                for file_path in r.get('deleted_files', []):
                    parent_folder = os.path.dirname(file_path)
                    if parent_folder and parent_folder not in deleted_paths_for_scan:
                        deleted_paths_for_scan.append(parent_folder)
                # Check for paths where file was already gone (e.g., debrid removal happened first)
                for file_path in r.get('paths_for_plex_scan', []):
                    parent_folder = os.path.dirname(file_path)
                    if parent_folder and parent_folder not in deleted_paths_for_scan:
                        deleted_paths_for_scan.append(parent_folder)

            if deleted_paths_for_scan and delete_from_media_server:
                logging.info(f"[DELETE_MULTIPLE] Phase 3: Scanning {len(deleted_paths_for_scan)} specific path(s) and emptying trash")
                trash_result = self.scan_and_empty_media_server_trash(deleted_paths=deleted_paths_for_scan)
                if trash_result.get('paths_scanned'):
                    logging.info(f"[DELETE_MULTIPLE] Scanned paths: {trash_result['paths_scanned']}")
                if trash_result.get('sections_cleaned'):
                    logging.info(f"[DELETE_MULTIPLE] Emptied trash for sections: {trash_result['sections_cleaned']}")
                if trash_result.get('errors'):
                    logging.warning(f"[DELETE_MULTIPLE] Scan/trash errors: {trash_result['errors']}")

        # Handle blacklist_sources if requested
        if blacklist_sources and blacklist:
            # TODO: Implement source blacklisting
            logging.warning("blacklist_sources parameter not yet implemented")

        # Overall success only if no failures
        aggregate_result['success'] = (aggregate_result['failed_count'] == 0 and not aggregate_result['database_locked'])

        return aggregate_result

    # =======================================================================
    # Duplicate Detection Methods (for Maintenance Page)
    # =======================================================================

    def find_state_duplicates(self) -> dict:
        """
        Find items with same media ID but different states

        Returns:
        {
            'groups': [
                {
                    'group_key': 'tt10620868_movie',
                    'title': '#Alive',
                    'year': 2020,
                    'type': 'movie',
                    'items': [...]
                }
            ],
            'total_duplicates': int,
            'recommended_deletions': int
        }
        """
        conn = get_db_connection()
        try:
            # Find duplicates by counting items with same media ID
            # Use NULLIF to convert empty strings to NULL for proper COALESCE behavior
            query = """
                SELECT
                    COALESCE(NULLIF(tmdb_id, ''), NULLIF(imdb_id, '')) as media_id,
                    type,
                    title,
                    year,
                    COUNT(*) as count
                FROM media_items
                WHERE type IN ('movie', 'show')
                GROUP BY COALESCE(NULLIF(tmdb_id, ''), NULLIF(imdb_id, '')), type
                HAVING COUNT(*) > 1
            """

            cursor = conn.execute(query)
            duplicate_groups = []
            total_recommended = 0

            for row in cursor:
                media_id = row['media_id']
                media_type = row['type']

                # Get all items for this media ID
                # Use NULLIF to convert empty strings to NULL for proper COALESCE behavior
                items_query = """
                    SELECT * FROM media_items
                    WHERE COALESCE(NULLIF(tmdb_id, ''), NULLIF(imdb_id, '')) = ?
                    AND type = ?
                    ORDER BY
                        CASE state
                            WHEN 'Collected' THEN 1
                            WHEN 'Upgrading' THEN 2
                            WHEN 'Final_Check' THEN 3
                            WHEN 'Wanted' THEN 4
                            WHEN 'Unreleased' THEN 5
                            WHEN 'Error' THEN 6
                            WHEN 'Blacklisted' THEN 7
                            WHEN 'ghostlisted' THEN 8
                            WHEN 'all_blacklisted' THEN 9
                            ELSE 10
                        END
                """
                items_cursor = conn.execute(items_query, (media_id, media_type))
                items = [dict(item_row) for item_row in items_cursor]

                # Analyze group
                analysis = self.analyze_duplicate_group(items, 'state')

                group = {
                    'group_key': f"{media_id}_{media_type}",
                    'title': row['title'],
                    'year': row['year'],
                    'type': media_type,
                    'media_id': media_id,
                    'items': items,
                    'analysis': analysis
                }

                duplicate_groups.append(group)
                total_recommended += sum(1 for item in items if analysis.get('recommended_delete', {}).get(str(item['id'])))

            return {
                'groups': duplicate_groups,
                'total_duplicates': len(duplicate_groups),
                'recommended_deletions': total_recommended
            }

        finally:
            conn.close()

    def analyze_duplicate_group(self, items: List[dict], criteria: str = 'state') -> dict:
        """
        Analyze a group of duplicate items and recommend which to delete

        Returns:
        {
            'best_item_id': int,
            'recommended_delete': {item_id: True/False},
            'reason': str
        }
        """
        if not items:
            return {}

        # Find best item (highest priority state)
        best_item = min(items, key=lambda x: self.get_state_priority(x.get('state', '')))

        recommended_delete = {}
        for item in items:
            # Recommend deletion if not the best item
            recommended_delete[str(item['id'])] = (item['id'] != best_item['id'])

        return {
            'best_item_id': best_item['id'],
            'recommended_delete': recommended_delete,
            'reason': f"Keep {best_item.get('state')} (highest priority), delete others"
        }
