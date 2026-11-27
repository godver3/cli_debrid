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
from typing import List, Optional
from utilities.settings import get_setting

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

_PLEX_WATCHLIST_SOURCE_TYPES = {'My Plex Watchlist', 'Other Plex Watchlist', 'My', 'Other', 'Plex'}


def _item_from_plex_watchlist(item_source_types) -> bool:
    """True if any recorded source type is a My/Other Plex Watchlist (or its first-word form)."""
    return any(t in _PLEX_WATCHLIST_SOURCE_TYPES for t in item_source_types)


SIDECAR_SUBTITLE_EXTS = ('.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.smi')

def _accepts_kwarg(func, name: str) -> bool:
    """True if `func` takes keyword `name` (or **kwargs)."""
    import inspect
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


# FUSE mount types of debrid/usenet mounts (built-in engine, rclone/zurg,
# cli_mount/decypharr). A file under one is removed through its provider,
# never with os.remove.
_DEBRID_FUSE_TYPES = frozenset({'fuse', 'fuse.clid', 'fuse.rclone', 'fuse.decypharr', 'fuse.zurg'})


def remove_sidecar_subtitles(video_path, check_show_nfo: bool = True):
    """Delete only the Clyde-written subtitle files that belong to a library
    file being removed (recorded in subtitle_history, regular files only).
    Subtitles that came with a release — symlinked or regular files — are
    never deleted. Returns the paths removed."""
    removed = []
    folder, name = os.path.split(video_path or '')
    stem = os.path.splitext(name)[0]
    if not folder or not stem:
        return removed
    try:
        names = os.listdir(folder)
    except OSError:
        return removed
    try:
        from database import subtitle_history
    except Exception:
        subtitle_history = None
    # Another video still here whose name starts with this one's
    # ('Movie.2020.1080p.REMUX.mkv' next to 'Movie.2020.1080p.mkv') owns
    # the subtitles named after it.
    longer = [s for s in (os.path.splitext(f)[0] for f in names if f.lower().endswith(VIDEO_EXTS))
              if s != stem and s.startswith(stem + '.')]
    for f in names:
        if not f.startswith(stem + '.') or not f.lower().endswith(SIDECAR_SUBTITLE_EXTS):
            continue
        if any(f.startswith(s + '.') for s in longer):
            continue
        path = os.path.join(folder, f)
        # Indexed lookup per candidate file (not the whole table per video:
        # a 1,200-episode delete calls this 1,200 times)
        if subtitle_history is None or not subtitle_history.is_clyde_path(path):
            continue
        try:
            if os.path.isfile(path) and not os.path.islink(path):
                os.remove(path)
                removed.append(path)
        except OSError as e:
            logging.warning(f"Could not remove subtitle {path}: {e}")
    if removed:
        logging.info(f"Removed {len(removed)} subtitle file(s) of {name}")
    # cli_debrid's NFO files of this episode (and tvshow.nfo once the show's
    # folder has no video left), so the folder can empty
    try:
        from utilities.nfo_writer import remove_for_video
        removed += remove_for_video(video_path, check_show_nfo=check_show_nfo)
    except Exception as e:
        logging.debug(f"NFO cleanup for {video_path} failed: {e}")
    return removed


VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.m4v', '.ts', '.m2ts', '.mov', '.wmv', '.webm', '.mpg', '.mpeg', '.iso')


def find_orphan_subtitles(library_root):
    """Every Clyde-written subtitle file in the symlink library that matches
    no video in its folder (recorded in subtitle_history, regular files only:
    never symlinks, never release subtitles, never tracks inside a video).
    For the Subtitle Manager's opt-in 'Remove orphaned subtitles' tool."""
    found = []
    if not library_root or not os.path.isdir(library_root):
        return found
    try:
        from database import subtitle_history
        clyde_paths = subtitle_history.clyde_sidecar_paths()
    except Exception:
        return found
    if not clyde_paths:
        return found
    for folder, _dirs, files in os.walk(library_root):
        stems = [os.path.splitext(f)[0] for f in files if f.lower().endswith(VIDEO_EXTS)]
        if not stems:
            continue        # no video here at all: not ours to judge
        for f in files:
            if f.lower().endswith(SIDECAR_SUBTITLE_EXTS) and not any(f.startswith(s + '.') for s in stems):
                path = os.path.join(folder, f)
                if os.path.normpath(path) in clyde_paths and os.path.isfile(path) and not os.path.islink(path):
                    found.append(path)
    return found


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
            logging.debug(f"[DELETION_MANAGER] Direct Mount - symlink operations disabled")

        # Media Server Type (stored in File Management section)
        media_server_raw = get_setting('File Management', 'media_server_type', 'plex')
        self.media_server = (media_server_raw or 'plex').lower()
        # Values: "plex" or "jellyfin"

        # Mounted File Location
        self.mounted_location = get_setting('Plex', 'mounted_file_location', '')

        # Media Server Availability
        from utilities.silo_functions import is_silo_selected, is_silo_mode
        # Silo picked as the media server (either mode): no Plex calls, even
        # with a Plex URL left over from before
        self.silo_enabled = is_silo_mode()
        # Direct Mount run with Jellyfin/Emby: removals are rescans through its
        # libraries, never Plex calls
        from utilities.direct_mount import is_direct_mount_jellyfin
        from utilities.jellyfin_direct import is_jellyfin_direct
        self.jellyfin_direct_enabled = is_jellyfin_direct()
        self.plex_enabled = (not is_silo_selected() and not is_direct_mount_jellyfin()
                             and self._check_plex_available())
        self.jellyfin_enabled = self._check_jellyfin_available()

        # Limited Environment Mode
        self.limited_env = os.environ.get('CLI_DEBRID_ENVIRONMENT_MODE', 'full') != 'full'

        # Database content directory
        self.db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')

        logging.info(f"DeletionManager initialized: file_mode={self.file_collection_mode}, using_symlinks={self.using_symlinks}, "
                    f"symlink_dir={self.symlink_dir}, media_server={self.media_server}, "
                    f"plex_enabled={self.plex_enabled}, jellyfin_enabled={self.jellyfin_enabled}, silo_enabled={self.silo_enabled}, "
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


    # =======================================================================
    # File Deletion Methods (Layer 3 & 5)
    # =======================================================================


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
        if self.silo_enabled or self.jellyfin_direct_enabled:
            return False  # Silo or Jellyfin/Emby is the media server: there is no Plex content to check

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
        result = {
            'success': True,
            'deleted_symlinks': [],
            'deleted_plex_paths': [],
            'already_missing_local_paths': [],
            'already_missing_plex_paths': [],
            'folders_removed': [],
            'errors': []
        }

        if not items:
            return result

        symlink_base = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')

        # PHASE 1: Collect all symlink paths (fast, no I/O)
        symlink_entries = []  # list of (local_path, plex_path)
        for item in items:
            if item.get('state', '') == 'Blacklisted':
                continue
            plex_path = item.get('location_on_disk')
            # Translate Plex-view paths to local paths, same as delete_single_item,
            # so items whose location_on_disk isn't already the local symlink path
            # aren't silently dropped from the batch.
            local_path = self._translate_plex_path_to_local(plex_path) if plex_path else None
            if local_path and local_path.startswith(symlink_base):
                symlink_entries.append((local_path, plex_path or local_path))

        if not symlink_entries:
            logging.info(f"[BATCH_DELETE_SYMLINKS] No symlinks to delete")
            return result

        logging.info(f"[BATCH_DELETE_SYMLINKS] Deleting {len(symlink_entries)} symlinks")

        # PHASE 2: Unlink all symlinks first, then clean up sidecar subtitles and episode NFOs,
        # and finally check tvshow.nfo ONCE per unique show folder (avoids per-episode os.walk).
        for local_path, plex_path in symlink_entries:
            try:
                # os.path.islink() alone (not gated on os.path.exists()) is required
                # to catch broken symlinks: exists() follows the link and returns
                # False when the target is missing, which is the defining trait of
                # a broken symlink — the exact case this deletion needs to handle.
                if os.path.islink(local_path):
                    os.unlink(local_path)
                    result['deleted_symlinks'].append(local_path)
                    result['deleted_plex_paths'].append(plex_path)
                elif os.path.exists(local_path):
                    logging.warning(f"[BATCH_DELETE_SYMLINKS] Path exists but is not a symlink: {local_path}")
                else:
                    logging.info(f"[BATCH_DELETE_SYMLINKS] Symlink {local_path} already removed (not found)")
                    result['already_missing_local_paths'].append(local_path)
                    result['already_missing_plex_paths'].append(plex_path)
            except Exception as e:
                logging.error(f"[BATCH_DELETE_SYMLINKS] Failed to delete symlink {local_path}: {e}")
                result['errors'].append(f"Symlink deletion failed: {str(e)}")

        # Clean up sidecar subtitles and episode NFOs after unlinks so same-stem /
        # longer-stem checks accurately see which videos remain in each folder.
        for symlink_path in result['deleted_symlinks']:
            remove_sidecar_subtitles(symlink_path, check_show_nfo=False)

        # Check tvshow.nfo ONCE per unique show folder after all unlinks in the batch
        if result['deleted_symlinks']:
            try:
                from utilities.nfo_writer import show_folder as _nfo_show_folder, remove_show_nfo_if_empty
                unique_show_folders = {
                    sf for sf in (_nfo_show_folder(p) for p in result['deleted_symlinks']) if sf
                }
                for sf in unique_show_folders:
                    remove_show_nfo_if_empty(sf)
            except Exception as e:
                logging.debug(f"[BATCH_DELETE_SYMLINKS] Show NFO cleanup failed: {e}")

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

        # Cache Plex verification result per (deletion_type, season_number) within this batch
        # so we don't open a new PlexServer connection for every non-empty folder.
        _plex_exists_cache = {}

        def _cached_content_in_plex(del_type, season_num=None):
            key = (del_type, season_num)
            if key not in _plex_exists_cache:
                _plex_exists_cache[key] = self._verify_content_in_plex(
                    plex_deletion_type=del_type,
                    content_title=plex_content_title,
                    imdb_id=plex_imdb_id,
                    tmdb_id=plex_tmdb_id,
                    season_number=season_num
                )
            return _plex_exists_cache[key]

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
                        content_still_exists = _cached_content_in_plex(plex_deletion_type, plex_season_number)

                        if not content_still_exists:
                            # Safe to force delete - content confirmed removed from Plex
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
                        content_still_exists = _cached_content_in_plex('show')

                        if not content_still_exists:
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
            result['plex_removed'] = False
            logging.debug(f"[SYMLINK_VALIDATION] Plex removal pending next library scan: {item.get('title', '')}")
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
        if self.silo_enabled:
            return self._remove_from_silo(item)
        if self.jellyfin_direct_enabled:
            return self._remove_from_jellyfin_direct(item)
        if self.plex_enabled:
            return self._remove_from_plex(item)
        elif self.jellyfin_enabled:
            return self._remove_from_jellyfin(item)
        else:
            return {'success': True, 'server_type': 'none'}

    def _remove_from_jellyfin_direct(self, item: dict) -> dict:
        """Direct Mount run with Jellyfin/Emby: rescan the file's folder once it is gone."""
        from utilities.jellyfin_direct import remove_file
        item_path = item.get('location_on_disk') or item.get('filled_by_file')
        if not item_path:
            return {'success': True, 'server_type': 'jellyfin', 'error': None}
        episode_title = item.get('episode_title') if item.get('type') == 'episode' else None
        ok = remove_file(item.get('title') or '', item_path, episode_title)
        return {'success': ok, 'server_type': 'jellyfin', 'error': None if ok else 'Jellyfin/Emby did not take the rescan'}

    def _remove_from_silo(self, item: dict) -> dict:
        """Silo has no delete call: rescan the file's path once it is gone."""
        from utilities.silo_functions import remove_file_from_silo
        item_path = item.get('location_on_disk') or item.get('filled_by_file')
        if not item_path:
            return {'success': True, 'server_type': 'silo', 'error': None}
        episode_title = item.get('episode_title') if item.get('type') == 'episode' else None
        ok = remove_file_from_silo(item.get('title') or '', item_path, episode_title)
        return {'success': ok, 'server_type': 'silo', 'error': None if ok else 'Silo did not take the rescan'}

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
        if self.silo_enabled:
            from utilities.silo_functions import scan_path
            scanned = [p for p in (deleted_paths or []) if scan_path(p, item_type or '')]
            return {'success': len(scanned) == len(deleted_paths or []), 'paths_scanned': scanned,
                    'sections_cleaned': [], 'errors': []}
        if self.jellyfin_direct_enabled:
            from utilities.jellyfin_direct import scan_path as jellyfin_scan_path
            jf_type = 'movie' if item_type == 'movie' else 'episode'
            scanned = [p for p in (deleted_paths or []) if jellyfin_scan_path(p, jf_type, removed=True)]
            return {'success': len(scanned) == len(deleted_paths or []), 'paths_scanned': scanned,
                    'sections_cleaned': [], 'errors': []}
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

    def remove_from_content_source(self, item: dict, queue_retry: bool = True) -> dict:
        """
        Remove item from all enabled content sources to prevent re-addition

        Instead of relying on the content_source field (which may be unreliable),
        this checks which content sources are enabled in settings and attempts
        removal from all applicable sources.

        Args:
            item: Media item dict with imdb_id, tmdb_id, type fields
            queue_retry: if a source fails, queue the item for
                task_retry_content_source_removals (off when called by that task)

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
                    response = requests.get('https://api.trakt.tv/users/settings', headers=headers, timeout=15)
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

            # Check the user's own Trakt Watchlist (not Friends' watchlists, which we can't edit).
            # item_source_types holds full type names ("Trakt Watchlist_1" -> "Trakt Watchlist").
            # Like the other sources, a source limited to Movies or Shows only covers that type.
            def _watchlist_covers(cfg):
                source_media_type = cfg.get('media_type', 'All')
                if source_media_type == 'Movies':
                    return media_type == 'movie'
                if source_media_type == 'Shows':
                    return media_type in ('show', 'episode')
                return True
            watchlist_enabled = any(
                cfg.get('type') == 'Trakt Watchlist' and cfg.get('enabled', False) and _watchlist_covers(cfg)
                for cfg in content_sources.values()
            )
            if watchlist_enabled and item_source_types and 'Trakt Watchlist' not in item_source_types:
                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⏭️  Skipping Trakt Watchlist - item not from it")
            elif watchlist_enabled:
                result['sources_attempted'].append('Trakt_Watchlist')
                try:
                    from content_checkers.trakt import remove_from_trakt_watchlist
                    removal_result = remove_from_trakt_watchlist([item])
                    result['details']['Trakt_Watchlist'] = removal_result
                    if removal_result.get('success'):
                        logging.info(f"[CONTENT_SOURCE_REMOVAL] ✓ Removed from Trakt Watchlist: {removal_result.get('message')}")
                        result['sources_succeeded'].append('Trakt_Watchlist')
                    else:
                        logging.warning(f"[CONTENT_SOURCE_REMOVAL] ✗ Failed to remove from Trakt Watchlist: {removal_result.get('message')}")
                        result['sources_failed'].append('Trakt_Watchlist')
                except Exception as e:
                    logging.error(f"[CONTENT_SOURCE_REMOVAL] Exception removing from Trakt Watchlist: {e}")
                    result['sources_failed'].append('Trakt_Watchlist')
                    result['details']['Trakt_Watchlist'] = {'success': False, 'message': str(e)}

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

            # Check Plex Watchlists (My + Other) - SKIP if item not from a Plex watchlist source.
            # item_source_types holds the literal source types ("My Plex Watchlist"), or just the
            # first word ("My", "Other") for ids without an underscore, so match both forms.
            if item_source_types and not _item_from_plex_watchlist(item_source_types):
                logging.info(f"[CONTENT_SOURCE_REMOVAL] ⏭️  Skipping Plex Watchlist - item not from Plex source")
            else:
                for source_key, source_config in content_sources.items():
                    source_type = source_config.get('type')
                    if source_type not in ('My Plex Watchlist', 'Other Plex Watchlist') or not source_config.get('enabled', False):
                        continue
                    if source_type == 'Other Plex Watchlist':
                        # Each Other source is a different person's watchlist: only touch the one(s)
                        # that added this item (all of them when the item has no recorded source).
                        if item_content_sources and source_key not in item_content_sources:
                            continue
                        token = source_config.get('token', '')
                        if not token:
                            logging.warning(f"[CONTENT_SOURCE_REMOVAL] No token for {source_key}, cannot remove from that watchlist")
                            continue
                    detail_key = 'Plex_Watchlist' if source_type == 'My Plex Watchlist' else f'Other_Plex_Watchlist_{source_key}'
                    result['sources_attempted'].append(detail_key)
                    try:
                        if source_type == 'My Plex Watchlist':
                            from content_checkers.plex_watchlist import remove_from_plex_watchlist_by_item
                            removal_result = remove_from_plex_watchlist_by_item(item)
                        else:
                            from content_checkers.plex_watchlist import remove_from_other_plex_watchlist_by_item
                            removal_result = remove_from_other_plex_watchlist_by_item(item, token)
                        result['details'][detail_key] = removal_result
                        if removal_result.get('success'):
                            result['sources_succeeded'].append(detail_key)
                        elif removal_result.get('not_found'):
                            # Item wasn't in watchlist - not an error, just checked
                            result['sources_not_found'].append(detail_key)
                            logging.info(f"[CONTENT_SOURCE_REMOVAL] ⊘ {source_type} checked (item not found in source)")
                        else:
                            result['sources_failed'].append(detail_key)
                    except Exception as e:
                        logging.error(f"Error removing from {source_type}: {e}")
                        result['sources_failed'].append(detail_key)
                        result['details'][detail_key] = {'success': False, 'message': str(e)}

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

            # MDBList lists aren't touched: its API has no way to remove an item from a list.

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

        if queue_retry and result['sources_failed']:
            self._queue_content_source_retry(item, result['message'])

        return result

    @staticmethod
    def _queue_content_source_retry(item: dict, error: str):
        """Queue a failed removal so a timed-out source doesn't re-add the item later."""
        from database.content_source_retry import add_to_retry_queue
        # Only what removal reads, and only JSON-safe values (rows carry datetimes)
        keys = ('id', 'imdb_id', 'tmdb_id', 'type', 'title', 'year', 'season_number',
                'episode_number', 'version', 'content_source', 'content_sources')
        item_data = {k: item[k] for k in keys
                     if k in item and isinstance(item[k], (str, int, float, bool, type(None)))}
        media_id = item.get('imdb_id') or (f"tmdb:{item['tmdb_id']}" if item.get('tmdb_id') else None)
        if not media_id:
            logging.warning(f"[CONTENT_SOURCE_REMOVAL] Not queuing retry for {item.get('title')}: no imdb/tmdb id")
            return
        add_to_retry_queue(media_id, item.get('title', ''), item.get('type', ''), item_data, error=error)

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


    # =======================================================================
    # Main Deletion Methods
    # =======================================================================

    def delete_single_item(self, item_id: int, blacklist: bool = False,
                          clear_cache: bool = True, delete_from_debrid: bool = False,
                          delete_from_media_server: bool = True, delete_files: bool = True,
                          delete_symlinks: bool = True, remove_from_content_source: bool = False,
                          skip_database: bool = False, force_delete_parent_folder: bool = False,
                          skip_filesystem_wait: bool = False, item: Optional[dict] = None) -> dict:
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
            item: Use this snapshot instead of reading item_id's row (files the row no longer points to)

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
            if item is None:
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
                            remove_sidecar_subtitles(symlink_path_to_remove)
                        elif os.path.islink(symlink_path_to_remove):
                            # Broken symlink - still remove it
                            os.unlink(symlink_path_to_remove)
                            result['deleted_symlinks'].append(symlink_path_to_remove)
                            logging.info(f"[DELETE_ITEM] Removed broken symlink {symlink_path_to_remove}")
                            remove_sidecar_subtitles(symlink_path_to_remove)
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
                            result['media_server_type'] = ('silo' if self.media_server == 'silo' else 'jellyfin' if self.jellyfin_direct_enabled else 'plex')
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
                        from debrid import provider_for_item
                        provider_for_item(item, self.debrid).remove_torrent(
                            torrent_id, removal_reason=f"Library deletion: {item_title}")
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


    def _mount_roots(self, mount_location: str) -> List[str]:
        """Every root a debrid/usenet mount can sit under: the configured mount
        settings (any of them can be stale) plus every FUSE mount point."""
        roots = getattr(self, '_mount_roots_cache', None)
        if roots is not None:
            return roots
        # Not File Management.original_files_path: in Local mode that is the
        # user's own download folder, whose files a delete does remove.
        roots = [mount_location,
                 get_setting('Plex', 'mounted_file_location', '')]
        try:
            from utilities import builtin_engine
            roots.append(builtin_engine.mount_path(get_setting('Built-in Mount') or {}))
        except Exception:
            pass
        try:
            with open('/proc/mounts') as f:
                for line in f:
                    parts = line.split()
                    # Debrid/usenet mounts only: fuseblk (NTFS/exFAT) and mergerfs
                    # pools are real disks a Local-mode user's downloads live on.
                    if len(parts) >= 3 and parts[2] in _DEBRID_FUSE_TYPES:
                        roots.append(parts[1].replace('\\040', ' '))
        except OSError:
            pass
        roots = [os.path.normpath(r) for r in roots if r and str(r).strip()]
        self._mount_roots_cache = roots
        return roots

    def _on_debrid_mount(self, path: str, mount_location: str) -> bool:
        p = os.path.normpath(path)
        for root in self._mount_roots(mount_location):
            if root in ('/', '.'):
                continue
            if p == root or p.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False

    def _remove_original_file_for_batch(self, item: dict, mount_location: str):
        """Cheap local removal of an item's file, as the single-item delete route did.
        Returns (removed_path_or_None, error_or_None). Never touches a media server."""
        if item.get('state') not in ('Collected', 'Upgrading') or self.limited_env:
            return None, None
        if self.using_symlinks:
            path = item.get('original_path_for_symlink')
        else:
            path = item.get('location_on_disk') if mount_location else None
        if not path:
            return None, None
        if self.using_symlinks and self._on_debrid_mount(path, mount_location):
            return None, None  # on the debrid mount: removed through the provider
        try:
            if os.path.exists(path):
                os.remove(path)
                return path, None
        except Exception as e:
            logging.error(f"[DELETE_MULTIPLE] Error deleting file {path}: {e}")
            return None, f"File deletion failed: {str(e)}"
        return None, None

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
                             plex_season_number: int = None,
                             remove_original_files: bool = False,
                             remove_climount_entries: bool = False) -> dict:
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
            remove_original_files: If True (and delete_files), remove each Collected/Upgrading item's
                file from disk like the single-item route (Direct Mount: location_on_disk; Symlinked:
                original_path_for_symlink), skipping files on the debrid mount and limited environments
            remove_climount_entries: If True, remove each unique cli_mount entry (NZB hash / btih) once,
                unless an item outside this batch still references it

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
        plex_result = {}
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

        # Fetch all items ONCE for all batch phases (avoids N individual DB queries)
        from database.database_reading import get_items_by_ids
        all_items = get_items_by_ids(item_ids) if item_ids else []
        items_by_id = {it['id']: it for it in all_items if it and 'id' in it}

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

        # PHASE 0: Deduplicated debrid/usenet removal - collect unique IDs and remove each ONCE
        # This prevents removing the same torrent/NZB 10+ times when episodes share a pack
        debrid_removed_ids = set()
        if delete_from_debrid:
            # Collect unique torrent/NZB IDs from all items, split by type
            unique_torrent_ids = {}   # torrent_id -> (title, infohash, debrid_folder_name)
            torrent_provider_names = {}  # torrent_id -> media_items.debrid_provider
            unique_nzb_ids = {}       # info_hash -> (filled_by_file, title) (cli_mount)
            for item in all_items:
                try:
                    if item and item.get('filled_by_torrent_id') and item.get('state', '') != 'Blacklisted':
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
                            torrent_provider_names[tid] = item.get('debrid_provider')
                except Exception as e:
                    logging.warning(f"[DELETE_MULTIPLE] Could not process item {item.get('id') if item else None} for dedup: {e}")

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
                        from debrid import provider_for_item
                        _provider = provider_for_item(torrent_provider_names.get(torrent_id), self.debrid)
                        _remove_kwargs = {'removal_reason': f"Library deletion: {first_title}"}
                        # Only Real-Debrid takes skip_hash_tracking; passing it to TorBox,
                        # AllDebrid, Premiumize... raised TypeError, so a library delete
                        # never removed their torrents.
                        if _accepts_kwarg(_provider.remove_torrent, 'skip_hash_tracking'):
                            _remove_kwargs['skip_hash_tracking'] = True
                        _provider.remove_torrent(torrent_id, **_remove_kwargs)
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

        # PHASE 0.1: cli_mount entry removal (what the single-item delete route did per item):
        # one remove_nzb per unique NZB hash / btih, only when no row outside this batch uses it.
        if remove_climount_entries and not delete_from_debrid:
            import re as _re_cm
            unique_cm = {}
            for item in all_items:
                tid = item.get('filled_by_torrent_id') or ''
                if not tid or item.get('state') not in ('Collected', 'Upgrading', 'Checking'):
                    continue
                if str(tid).startswith('nzb:'):
                    key = tid[4:]
                else:
                    _m = _re_cm.search(r'urn:btih:([0-9a-fA-F]{40})', item.get('filled_by_magnet') or '', _re_cm.IGNORECASE)
                    key = _m.group(1).lower() if _m else ''
                if key and tid not in unique_cm:
                    unique_cm[tid] = key
            if unique_cm:
                try:
                    from usenet import get_usenet_client
                    cm_client = get_usenet_client()
                    if cm_client and cm_client.is_enabled():
                        for tid, key in unique_cm.items():
                            if _has_external_sibling(tid):
                                logging.info(f"[DELETE_MULTIPLE] Skipping cli_mount removal for {tid!r} - still referenced outside this batch")
                                continue
                            try:
                                cm_client.remove_nzb(key)
                                logging.info(f"[DELETE_MULTIPLE] Removed {key!r} from cli_mount")
                            except Exception as cm_err:
                                logging.warning(f"[DELETE_MULTIPLE] cli_mount removal failed for {key!r}: {cm_err}")
                except Exception as cm_err:
                    logging.warning(f"[DELETE_MULTIPLE] cli_mount removal skipped: {cm_err}")

        # PHASE 0.3: Content source removal (Trakt/Overseerr/etc.) - grouped, not per-item.
        # For TV, a request/media record in a content source is tracked per SHOW, not per
        # episode — so this must only fire when this batch covers every remaining kept
        # episode of the show (no sibling outside the batch). Without this guard, deleting
        # even a single episode or season through the generic multi-select path would
        # remove the content source's request/media record for the ENTIRE show, wiping its
        # availability status even though most of it is still collected. Movies don't have
        # this ambiguity (one item = one removal target) and are handled per-item as before.
        # Mirrors the existing show-level call already used by the dedicated /delete_show
        # route, generalized here so every caller gets the same safety automatically.
        if remove_from_content_source:
            content_source_results = {}
            items_for_cs = all_items
            _batch_id_set_cs = set(item_ids)

            for movie_item in (it for it in items_for_cs if it.get('type') == 'movie'):
                try:
                    r = self.remove_from_content_source(movie_item)
                    content_source_results[f"movie_{movie_item['id']}"] = r
                except Exception as e:
                    logging.error(f"[DELETE_MULTIPLE] Content source removal failed for movie {movie_item.get('id')}: {e}")

            # Group episode items by show (imdb_id) - one removal attempt per show, not per episode
            shows_seen = {}
            for ep in (it for it in items_for_cs if it.get('type') == 'episode'):
                imdb_id = ep.get('imdb_id')
                if imdb_id and imdb_id not in shows_seen:
                    shows_seen[imdb_id] = ep

            for imdb_id, rep_episode in shows_seen.items():
                try:
                    from database import get_db_connection as _gdb_cs
                    _conn_cs = _gdb_cs()
                    try:
                        _rows = _conn_cs.execute(
                            "SELECT id FROM media_items WHERE imdb_id = ? AND type = 'episode' "
                            "AND state IN ('Collected','Upgrading','Checking')",
                            (imdb_id,)
                        ).fetchall()
                    finally:
                        _conn_cs.close()
                    has_external_sibling = any(r[0] not in _batch_id_set_cs for r in _rows)
                except Exception as e:
                    logging.warning(f"[DELETE_MULTIPLE] Sibling check failed for content source removal of show {imdb_id}, skipping to be safe: {e}")
                    has_external_sibling = True

                if has_external_sibling:
                    logging.info(f"[DELETE_MULTIPLE] Skipping content source removal for show {imdb_id} — other episodes remain outside this batch (partial deletion)")
                    continue

                show_item = rep_episode.copy()
                show_item['type'] = 'show'
                try:
                    r = self.remove_from_content_source(show_item)
                    content_source_results[f"show_{imdb_id}"] = r
                    logging.info(f"[DELETE_MULTIPLE] Removed show {imdb_id} from content sources (batch covers every remaining episode): {r.get('message')}")
                except Exception as e:
                    logging.error(f"[DELETE_MULTIPLE] Content source removal failed for show {imdb_id}: {e}")

            aggregate_result['content_source_removal'] = content_source_results

            # Already handled above, centrally and per-show/per-movie - never let the
            # per-item loop below redundantly (and unsafely, for episodes) repeat this.
            remove_from_content_source = False

        # PHASE 0.5: Batch cache clearing (if enabled) - BEFORE physical cleanup
        # Opens each cache file once instead of once per item
        batch_cache_cleared = False
        if clear_cache:
            if all_items:
                logging.info(f"[DELETE_MULTIPLE] Phase 0.5: Batch cache clearing for {len(all_items)} items")
                batch_cache_result = self._batch_clear_cache(all_items)

                if batch_cache_result['success']:
                    batch_cache_cleared = True
                    logging.info(f"[DELETE_MULTIPLE] Batch cache clearing successful: {batch_cache_result['items_cleared']} items cleared")
                else:
                    logging.warning(f"[DELETE_MULTIPLE] Batch cache clearing had errors: {batch_cache_result['errors']}")
                    aggregate_result['errors'].extend(batch_cache_result['errors'])
                    # The per-item delete used to clear each item's cache itself;
                    # retry that way so one bad cache file doesn't leave the rest.
                    _per_item_ok = True
                    for _it in all_items:
                        try:
                            _files = self.check_content_sources(_it).get('cache_files') or []
                            if _files and not self.clear_content_source_cache(_it, _files):
                                _per_item_ok = False
                        except Exception as _ce:
                            _per_item_ok = False
                            logging.warning(f"[DELETE_MULTIPLE] Per-item cache clear failed for {_it.get('id')}: {_ce}")
                    batch_cache_cleared = _per_item_ok
            else:
                logging.warning(f"[DELETE_MULTIPLE] Could not get items for batch cache clearing")

        # PHASE 1: Physical cleanup (symlinks/files) - debrid already handled in Phase 0,
        # media server folder scan + emptyTrash handled once in Phase 3 (no per-episode Plex/Silo/Jellyfin round trips).
        # Only force delete folders if Plex deletion succeeded (prevents orphaned Plex entries)
        logging.info(f"[DELETE_MULTIPLE] Phase 1: Physical cleanup for {len(item_ids)} items")
        if force_delete_parent_folder and not plex_content_deleted:
            logging.warning(f"[DELETE_MULTIPLE] Force folder deletion disabled: Plex deletion did not succeed. Will only delete empty folders.")

        batch_result = None
        scan_folders_seen = {}  # ordered set of unique parent folders for Phase 3 media server scan
        use_local_scan_paths = self.silo_enabled or self.jellyfin_direct_enabled

        if delete_symlinks and self.using_symlinks and all_items:
            logging.info(f"[DELETE_MULTIPLE] Using BATCH symlink deletion for {len(all_items)} items")

            # Batch delete all symlinks, sidecars, episode NFOs, and show NFOs (once per show folder)
            # Only force delete if Plex content-level deletion succeeded (prevents orphaned entries)
            batch_result = self._batch_delete_symlinks(
                all_items,
                force_delete_parent_folder=force_delete_parent_folder and plex_content_deleted,
                plex_deletion_type=plex_deletion_type,
                plex_content_title=plex_content_title,
                plex_imdb_id=plex_imdb_id,
                plex_tmdb_id=plex_tmdb_id,
                plex_season_number=plex_season_number
            )

            logging.info(f"[DELETE_MULTIPLE] Batch symlink deletion complete: {len(batch_result['deleted_symlinks'])} symlinks, {len(batch_result['folders_removed'])} folders")

            if batch_result['errors']:
                aggregate_result['errors'].extend(batch_result['errors'])

            # Collect unique parent folders for Phase 3 scan
            scan_source_paths = (
                batch_result['deleted_symlinks'] + batch_result['already_missing_local_paths']
                if use_local_scan_paths
                else batch_result['deleted_plex_paths'] + batch_result['already_missing_plex_paths']
            )
            for p in scan_source_paths:
                parent_folder = os.path.dirname(p)
                if parent_folder:
                    scan_folders_seen[parent_folder] = None

            # Record per-item results without calling delete_single_item (avoids per-episode Plex calls & DB reads)
            deleted_symlink_set = set(batch_result['deleted_symlinks'])
            sym_mount_location = self.mounted_location or get_setting('Plex', 'mounted_file_location', '')
            for item_id in item_ids:
                item = items_by_id.get(item_id)
                if not item:
                    aggregate_result['failed_count'] += 1
                    err = f"Item {item_id} not found"
                    aggregate_result['errors'].append(err)
                    aggregate_result['results'].append({
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
                        'errors': [err]
                    })
                    continue

                item_plex_path = item.get('location_on_disk')
                item_local_path = self._translate_plex_path_to_local(item_plex_path) if item_plex_path else None
                item_deleted_symlinks = [item_local_path] if item_local_path in deleted_symlink_set else []
                item_deleted_files, item_errors = [], []
                if remove_original_files and delete_files:
                    removed, file_err = self._remove_original_file_for_batch(item, sym_mount_location)
                    if removed:
                        item_deleted_files.append(removed)
                    if file_err:
                        item_errors.append(file_err)
                        aggregate_result['failed_count'] += 1
                        aggregate_result['errors'].append(file_err)
                aggregate_result['results'].append({
                    'success': not item_errors,
                    'item_id': item_id,
                    'title': item.get('title', 'Unknown'),
                    'deleted_files': item_deleted_files,
                    'deleted_symlinks': item_deleted_symlinks,
                    'media_server_removed': plex_content_deleted,
                    'media_server_type': ('silo' if self.silo_enabled else 'jellyfin' if self.jellyfin_direct_enabled else 'plex' if self.plex_enabled else 'none'),
                    'debrid_removed': bool(item.get('filled_by_torrent_id') in debrid_removed_ids),
                    'cache_cleared': batch_cache_cleared,
                    'blacklisted': blacklist,
                    'content_source_removal': None,
                    'errors': item_errors
                })
        else:
            # Direct Mount mode (Plex / Jellyfin / Silo) or symlink deletion disabled:
            # Collect unique folders for a single batched media-server scan in Phase 3
            # instead of per-episode remove_file_from_plex / Silo / Jellyfin calls.
            logging.info(f"[DELETE_MULTIPLE] Using batched folder cleanup (symlinks={'enabled' if delete_symlinks else 'disabled'}, mode={'symlink' if self.using_symlinks else 'direct_mount'})")
            mount_location = self.mounted_location or get_setting('Plex', 'mounted_file_location', '')
            for item_id in item_ids:
                item = items_by_id.get(item_id)
                if not item:
                    aggregate_result['failed_count'] += 1
                    err = f"Item {item_id} not found"
                    aggregate_result['errors'].append(err)
                    aggregate_result['results'].append({
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
                        'errors': [err]
                    })
                    continue

                item_deleted_files = []
                item_errors = []
                item_scan_paths = []
                plex_removed = False
                skip_physical = (item.get('state', '') == 'Blacklisted')

                if not skip_physical:
                    # In Symlinked/Local mode with delete_symlinks=False and delete_files=True,
                    # remove non-mount original files if not in limited_env.
                    if self.using_symlinks and delete_files:
                        orig_path = item.get('original_path_for_symlink')
                        # Same debrid-mount check as _remove_original_file_for_batch
                        # (built-in engine path, FUSE mounts), not just a prefix match
                        # on a possibly stale mounted_file_location.
                        is_on_mount = bool(orig_path and self._on_debrid_mount(orig_path, mount_location))
                        if orig_path and not is_on_mount and not self.limited_env:
                            try:
                                if os.path.exists(orig_path):
                                    os.remove(orig_path)
                                    item_deleted_files.append(orig_path)
                            except Exception as e:
                                err = f"Original file deletion failed: {str(e)}"
                                logging.error(f"[DELETE_MULTIPLE] Error deleting original file {orig_path}: {e}")
                                item_errors.append(err)

                    if not self.using_symlinks and remove_original_files and delete_files:
                        removed, file_err = self._remove_original_file_for_batch(item, mount_location)
                        if removed:
                            item_deleted_files.append(removed)
                        if file_err:
                            item_errors.append(file_err)

                    media_path = item.get('location_on_disk') or item.get('filled_by_file')
                    # Direct Mount + Plex with no show/season/movie delete: the file is
                    # often still on the mount (debrid not removed, or the mount hasn't
                    # refreshed), and a folder scan keeps anything whose file it still
                    # sees. Remove the item through the Plex API as dev did; only fall
                    # back to the batched folder scan when that fails.
                    if (delete_from_media_server and media_path and not self.using_symlinks
                            and not plex_content_deleted and self.plex_enabled
                            and not self.silo_enabled and not self.jellyfin_direct_enabled):
                        try:
                            from utilities.plex_functions import remove_file_from_plex
                            plex_removed = bool(remove_file_from_plex(
                                item.get('title', 'Unknown'), media_path,
                                item.get('episode_title') if item.get('type') == 'episode' else None))
                        except Exception as e:
                            logging.warning(f"[DELETE_MULTIPLE] Plex removal failed for item {item_id}: {e}")
                    if media_path and not plex_removed:
                        item_scan_paths.append(media_path)
                        parent_folder = os.path.dirname(media_path)
                        if parent_folder:
                            scan_folders_seen[parent_folder] = None

                item_ok = len(item_errors) == 0
                if not item_ok:
                    aggregate_result['failed_count'] += 1
                    aggregate_result['errors'].extend(item_errors)

                aggregate_result['results'].append({
                    'success': item_ok,
                    'item_id': item_id,
                    'title': item.get('title', 'Unknown'),
                    'deleted_files': item_deleted_files,
                    'deleted_symlinks': [],
                    'paths_for_plex_scan': item_scan_paths,
                    'media_server_removed': plex_content_deleted or plex_removed,
                    'media_server_type': ('silo' if self.silo_enabled else 'jellyfin' if self.jellyfin_direct_enabled else 'plex' if self.plex_enabled else 'none'),
                    'debrid_removed': bool(item.get('filled_by_torrent_id') in debrid_removed_ids),
                    'cache_cleared': batch_cache_cleared,
                    'blacklisted': blacklist,
                    'content_source_removal': None,
                    'errors': item_errors
                })

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

        # PHASE 3: Scan and empty media server trash ONCE across all unique affected folders.
        # Also after a content-level Plex delete: Plex answers it with success but
        # keeps the items when it can't remove the files itself (symlinks it may
        # not write), and with "Empty trash automatically after every scan" off
        # nothing removes them later (seen live: show 122280 kept all 12 episodes).
        # The content-level delete above runs while the symlinks still exist; Plex
        # can't remove them (no write access), answers 200 and keeps the item.
        # Now that the files are gone, re-send the delete if the item is still
        # there and only fall back to a folder scan (for a removed show folder,
        # a scan of the whole section) when Plex still keeps it.
        plex_content_confirmed = False
        if plex_content_deleted and plex_result.get('rating_key'):
            from utilities.plex_functions import finish_plex_content_delete
            plex_content_confirmed = finish_plex_content_delete(plex_result['rating_key'])
            logging.info(f"[DELETE_MULTIPLE] Phase 3: Plex {plex_deletion_type} "
                         f"{'removed' if plex_content_confirmed else 'still present, scanning folders'}")
        if delete_from_media_server and scan_folders_seen and not plex_content_confirmed:
            deleted_paths_for_scan = list(scan_folders_seen.keys())
            item_types = {it.get('type') for it in all_items if it.get('state', '') != 'Blacklisted' and it.get('type')}
            batch_item_type = next(iter(item_types)) if len(item_types) == 1 else None
            logging.info(f"[DELETE_MULTIPLE] Phase 3: Scanning {len(deleted_paths_for_scan)} unique folder(s) and emptying trash")
            trash_result = self.scan_and_empty_media_server_trash(
                deleted_paths=deleted_paths_for_scan,
                item_type=batch_item_type
            )
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


