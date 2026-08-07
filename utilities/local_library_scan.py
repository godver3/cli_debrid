import logging
from typing import List, Dict, Any, Optional, Callable
import os
import sys  # Import sys module
from utilities.settings import get_setting
import shutil
from pathlib import Path
import re
from datetime import datetime
import time
from utilities.anidb_functions import format_filename_with_anidb
from database.database_writing import update_media_item_state, update_media_item
from utilities.post_processing import handle_state_change
from database.symlink_verification import add_symlinked_file_for_verification, add_path_for_removal_verification, remove_verification_by_media_item_id
from database.database_reading import get_all_media_items, get_media_item_by_id, get_season_year
from scraper.functions.ptt_parser import parse_with_ptt
import json # Ensure json is imported
from concurrent.futures import ThreadPoolExecutor, TimeoutError

# Check MediaInfo availability at module load (optional dependency)
_MEDIAINFO_AVAILABLE = False
_MEDIAINFO_CALL_COUNT = 0  # DEBUG: Track how many times MediaInfo is called
_MEDIAINFO_SUCCESS_COUNT = 0  # DEBUG: Track successful extractions
_MEDIAINFO_FAIL_COUNT = 0  # DEBUG: Track failed extractions

try:
    logging.info("[MEDIAINFO_DEBUG] Attempting to import pymediainfo...")
    from pymediainfo import MediaInfo
    _MEDIAINFO_AVAILABLE = True
    logging.info("[MEDIAINFO_DEBUG] ✓ pymediainfo imported successfully")
    logging.info("[MEDIAINFO] MediaInfo available - will use for accurate resolution extraction")

    # Test MediaInfo works
    try:
        test_result = MediaInfo.can_parse()
        logging.info(f"[MEDIAINFO_DEBUG] MediaInfo.can_parse() = {test_result}")
    except Exception as test_e:
        logging.warning(f"[MEDIAINFO_DEBUG] MediaInfo test failed: {test_e}")
        _MEDIAINFO_AVAILABLE = False

except ImportError as e:
    logging.info(f"[MEDIAINFO_DEBUG] ✗ pymediainfo import failed: {e}")
    logging.info(f"[MEDIAINFO] MediaInfo not available ({e}) - will parse resolution from filenames only")

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be safe for symlinks."""
    # Get replacement character from settings, default to underscore
    from utilities.settings import get_setting
    replacement_char_setting = get_setting('Debug', 'sanitizer_replacement_character', '_')
    
    # Determine the actual replacement character to use
    actual_replacement_char = '_' # Default fallback

    if replacement_char_setting == '':
        actual_replacement_char = ''
        logging.debug("Sanitizer replacement is blank; offending characters will be deleted.")
    elif len(replacement_char_setting) == 1 and re.match(r'[a-zA-Z0-9\-\.~_\[\]]', replacement_char_setting):
        # Use the valid single character provided by the user
        actual_replacement_char = replacement_char_setting
    elif replacement_char_setting != '_': # If setting is not blank, not a valid single char, and not already our default
        logging.warning(f"Invalid sanitizer replacement character ('{replacement_char_setting}') configured. Using default '_' instead.")
        # actual_replacement_char remains '_' (the default fallback)
    
    # Convert Unicode characters to their ASCII equivalents where possible
    import unicodedata
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    
    # Replace problematic characters with the determined actual_replacement_char
    filename = re.sub(r'[<>|?*:"\'\&/\\]', actual_replacement_char, filename)  # Added slashes and backslashes
    return filename.strip()  # Just trim whitespace, don't mess with dots


def extract_resolution_from_filename(filename: str) -> Optional[str]:
    """
    Extract resolution from filename using regex patterns.
    Fast method (~0.001ms per file) with ~95% accuracy for well-named files.
    Normalizes all formats (4K, UHD, HD, etc.) to standard "p" format (2160p, 1080p, etc.).

    Args:
        filename: File name to parse (e.g., "Movie.2160p.mkv" or "Movie.4K.mkv")

    Returns:
        Resolution string like "2160p", "1080p", etc., or None if not found
    """
    # Remove file extension for cleaner matching
    name_without_ext = os.path.splitext(filename)[0]

    # Try multiple patterns in order of reliability
    # Separators include: . space - _ ( ) [ ]
    # Supports both "p" (progressive) and "i" (interlaced) formats
    patterns = [
        # Pattern 1: Resolution with separator before (flexible after - optional separator or word boundary)
        # Matches: .1080p. or .1080i. or (1080p) or [1080i] or .1080p AMZN or S04E05.1080i.BluRay
        r'[\.\s\-_\(\)\[\]](\d{3,4}[pi])(?:[\.\s\-_\(\)\[\]]|$|\b)',
        # Pattern 2: Resolution at the end (before extension)
        r'[\.\s\-_\(\)\[\]](\d{3,4}[pi])$',
        # Pattern 3: Resolution after year
        r'\d{4}[\.\s\-_\(\)\[\]](\d{3,4}[pi])',
        # Pattern 4: Resolution at start (after path)
        r'^(\d{3,4}[pi])[\.\s\-_\(\)\[\]]',
        # Pattern 5: Alternative formats (4K, 8K, 2K, UHD, HD, FHD)
        r'[\.\s\-_\(\)\[\]](8K|4K|UHD|2K|QHD|FHD|FULLHD|FULL\.HD)(?:[\.\s\-_\(\)\[\]]|$|\b)',
    ]

    for pattern in patterns:
        match = re.search(pattern, name_without_ext, re.IGNORECASE)
        if match:
            resolution = match.group(1).lower()

            # Normalize alternative formats to standard "p" format
            resolution_map = {
                '8k': '4320p',
                '4k': '2160p',
                'uhd': '2160p',
                '2k': '1440p',
                'qhd': '1440p',
                'fhd': '1080p',
                'fullhd': '1080p',
                'full.hd': '1080p',
            }

            # Check if it needs normalization
            if resolution in resolution_map:
                return resolution_map[resolution]

            # Validate it's a real resolution (supports both "p" and "i" formats)
            valid_resolutions = [
                '4320p', '4320i', '2160p', '2160i', '1440p', '1440i',
                '1080p', '1080i', '720p', '720i', '576p', '576i',
                '480p', '480i', '360p', '360i', '240p', '240i'
            ]
            if resolution in valid_resolutions:
                return resolution

    return None


def extract_resolution_with_ffprobe(file_path: str) -> Optional[str]:
    """
    Extract resolution using ffprobe (faster alternative to MediaInfo).
    ffprobe is designed for rapid container inspection.

    Args:
        file_path: Full path to video file

    Returns:
        Resolution string like "2160p", "1080p", etc., or None if failed
    """
    try:
        import subprocess
        import json
        import time

        filename = os.path.basename(file_path)
        start_time = time.time()

        # Run ffprobe to get video height (fast, targeted query)
        # Timeout after 3 seconds to prevent slow files from holding up the scan
        cmd = [
            'ffprobe',
            '-v', 'quiet',              # Suppress output
            '-print_format', 'json',    # JSON output
            '-show_streams',            # Show stream info
            '-select_streams', 'v:0',   # First video stream only
            file_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)

        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)

        if 'streams' in data and len(data['streams']) > 0:
            height = data['streams'][0].get('height')

            if height:
                height = int(height)
                elapsed = (time.time() - start_time) * 1000

                # Convert pixel height to resolution
                if height >= 2160:
                    resolution = "2160p"
                elif height >= 1440:
                    resolution = "1440p"
                elif height >= 1080:
                    resolution = "1080p"
                elif height >= 720:
                    resolution = "720p"
                elif height >= 576:
                    resolution = "576p"
                elif height >= 480:
                    resolution = "480p"
                elif height >= 360:
                    resolution = "360p"
                elif height >= 240:
                    resolution = "240p"
                else:
                    resolution = "sd"

                logging.info(f"[FFPROBE_DEBUG] ✓ SUCCESS: {filename} → {resolution} ({elapsed:.2f}ms)")
                return resolution

        return None

    except subprocess.TimeoutExpired:
        logging.warning(f"[FFPROBE_DEBUG] ⏱️ TIMEOUT: {filename} - exceeded 3 second limit, skipping")
        return None
    except Exception as e:
        logging.error(f"[FFPROBE_DEBUG] ✗ EXCEPTION: {filename} - {type(e).__name__}: {e}")
        return None


def extract_resolution_hybrid(file_path: str) -> Optional[str]:
    """
    Hybrid approach: Try path-based regex first (covers filename + parent folder), then ffprobe.
    Achieves ~100% coverage with minimal ffprobe usage.

    Args:
        file_path: Full path to video file

    Returns:
        Resolution string or None
    """
    # STEP 1: Single regex pass on last 2 path components (parent folder + filename)
    # This catches resolution in both filename AND parent folder in one operation
    # Example: /mount/shows/Show S01 1080p/S01E01.mkv → "Show S01 1080p/S01E01.mkv"
    path_parts = file_path.split('/')
    if len(path_parts) >= 2:
        # Get last 2 parts: parent_folder/filename.ext
        relevant_path = '/'.join(path_parts[-2:])
    else:
        # Fallback to just filename if path is weird
        relevant_path = os.path.basename(file_path)

    resolution = extract_resolution_from_filename(relevant_path)
    if resolution:
        logging.debug(f"[RESOLUTION] ✓ Extracted from path (regex): {resolution}")
        return resolution

    # STEP 2: Try ffprobe as fallback (for files with no resolution in filename/folder)
    filename = os.path.basename(file_path)
    logging.debug(f"[RESOLUTION] Regex failed, trying ffprobe for: {filename}")
    resolution = extract_resolution_with_ffprobe(file_path)
    if resolution:
        logging.info(f"[RESOLUTION] ✓ Extracted via ffprobe fallback: {filename} → {resolution}")
        return resolution

    # STEP 3: All filesystem methods failed - will use Plex fallback (if enabled)
    logging.debug(f"[RESOLUTION] ✗ Regex and ffprobe failed for: {relevant_path}")
    return None


def remap_plex_paths_to_mount(items: List[Dict[str, Any]], mount_path: str) -> List[Dict[str, Any]]:
    """
    Remap Plex library paths to CLI Debrid mount paths for filesystem checking.

    Strategy: Auto-detect Plex mount point from database paths, then replace with CLI mount

    Examples:
        Plex:  /debrid/movies/Title.2020.mkv → CLI: /media/mount/movies/Title.2020.mkv
        Plex:  /zurg/shows/Show.S01E01.mkv → CLI: /media/mount/shows/Show.S01E01.mkv
        Plex:  /mnt/data/zurg/ufc/UFC.300.mkv → CLI: /media/mount/ufc/UFC.300.mkv

    Works with ANY custom folder structure (movies, shows, ufc, default, anime, etc.)

    Args:
        items: List of Collected items with Plex paths in location_on_disk
        mount_path: CLI mount root (e.g., "/media/mount")

    Returns:
        List of items with location_on_disk pointing to CLI mount paths
    """
    from collections import Counter

    remapped_items = []
    success_count = 0
    failed_count = 0
    skipped_count = 0

    # Auto-detect Plex mount point from first 100 paths (already in memory, ~1ms)
    sample_size = min(100, len(items))
    first_level_dirs = []

    for item in items[:sample_size]:
        path = item.get('location_on_disk', '')
        if path and path.startswith('/'):
            parts = path.split('/')
            # Extract first directory: /debrid/... → debrid
            if len(parts) >= 2 and parts[1]:
                first_level_dirs.append('/' + parts[1])

    if not first_level_dirs:
        logging.warning(f"[PATH_REMAP] Could not detect Plex mount point from paths. No valid paths found.")
        # Mark all as failed
        for item in items:
            item_copy = item.copy()
            item_copy['_remap_failed'] = True
            remapped_items.append(item_copy)
        return remapped_items

    # DEBUG: Show sample paths for debugging
    sample_paths = [item.get('location_on_disk', '') for item in items[:5] if item.get('location_on_disk')]
    if sample_paths:
        logging.info(f"[PATH_REMAP_DEBUG] Sample Plex paths (first 5):")
        for p in sample_paths:
            logging.info(f"[PATH_REMAP_DEBUG]   {p}")

    # Process EACH item individually - support mixed paths (some /debrid, some /media/mount)
    logging.info(f"[PATH_REMAP] Processing {len(items)} items individually (mixed paths support)")

    # Detect most common mount point for items that need remapping
    plex_mount_point = Counter(first_level_dirs).most_common(1)[0][0] if first_level_dirs else None
    if plex_mount_point:
        logging.info(f"[PATH_REMAP] Detected source mount: {plex_mount_point} (will remap to {mount_path})")

    # Track statistics
    reason_counts = {'no_path': 0, 'already_correct': 0, 'remapped': 0, 'wrong_mount': 0, 'not_exists': 0}
    already_correct_count = 0

    for item in items:
        item_copy = item.copy()
        plex_path = item.get('location_on_disk', '')

        if not plex_path:
            item_copy['_remap_failed'] = True
            remapped_items.append(item_copy)
            failed_count += 1
            reason_counts['no_path'] += 1
            continue

        # CHECK 1: Does this item's path already point to target mount?
        if plex_path.startswith(mount_path + '/'):
            # Path already correct - use as-is
            if os.path.exists(plex_path):
                item_copy['_scan_location'] = plex_path
                success_count += 1
                already_correct_count += 1
                reason_counts['already_correct'] += 1
                if already_correct_count <= 3:
                    logging.debug(f"[PATH_REMAP] ✓ Already correct: {os.path.basename(plex_path)}")
            else:
                item_copy['_remap_failed'] = True
                failed_count += 1
                reason_counts['not_exists'] += 1
            remapped_items.append(item_copy)
            continue

        # CHECK 2: Item needs remapping - check if it matches detected source mount
        if not plex_mount_point or not plex_path.startswith(plex_mount_point + '/'):
            if skipped_count < 5:
                logging.debug(f"[PATH_REMAP_DEBUG] Skipping - path doesn't match source mount: {plex_path}")
            item_copy['_remap_failed'] = True
            remapped_items.append(item_copy)
            skipped_count += 1
            reason_counts['wrong_mount'] += 1
            continue

        # Remap: Replace source mount with target mount
        # Example: /debrid/movies/... → /media/mount/movies/...
        cli_path = plex_path.replace(plex_mount_point + '/', mount_path + '/', 1)

        # DEBUG: Log first few remapping attempts
        if reason_counts['remapped'] < 3:
            logging.debug(f"[PATH_REMAP_DEBUG] Remapping:")
            logging.debug(f"[PATH_REMAP_DEBUG]   From: {plex_path}")
            logging.debug(f"[PATH_REMAP_DEBUG]   To:   {cli_path}")

        # Verify file exists at remapped path
        if os.path.exists(cli_path):
            # Store remapped path in TEMPORARY field for scanning only
            # DO NOT modify location_on_disk - that's for Plex library path
            item_copy['_scan_location'] = cli_path
            success_count += 1
            reason_counts['remapped'] += 1
            if reason_counts['remapped'] <= 3:
                logging.info(f"[PATH_REMAP_DEBUG] ✓ Remapped #{reason_counts['remapped']}: {os.path.basename(plex_path)}")
        else:
            if failed_count < 3:
                logging.debug(f"[PATH_REMAP_DEBUG] ✗ File not found: {cli_path}")
            item_copy['_remap_failed'] = True
            failed_count += 1
            reason_counts['not_exists'] += 1

        remapped_items.append(item_copy)

    logging.info(f"[PATH_REMAP] Results: {already_correct_count} already correct, {reason_counts['remapped']} remapped, {failed_count} failed, {skipped_count} skipped")
    logging.info(f"[PATH_REMAP_DEBUG] Failure reasons: no_path={reason_counts['no_path']}, wrong_mount={reason_counts['wrong_mount']}, not_exists={reason_counts['not_exists']}")

    if success_count == 0 and len(items) > 0:
        logging.warning(f"[PATH_REMAP] No paths remapped successfully! Check mount path and structure.")

    return remapped_items


def _clean_separators_in_string(s: str) -> str:
    """Clean up orphaned separators after removing template components."""
    s = re.sub(r'\s*-\s*\(\s*\)', '', s)
    s = re.sub(r'\(\s*\)', '', s)
    s = re.sub(r'\s*-\s*\[\s*\]', '', s)
    s = re.sub(r'\[\s*\]', '', s)
    s = re.sub(r'\s*-\s*-\s*', ' - ', s)
    s = re.sub(r'^\s*-\s*', '', s)
    s = re.sub(r'\s*-\s*$', '', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip()


def truncate_path_components(
    template: str,
    template_vars: Dict[str, Any],
    base_path: str,
    directory_parts: List[str],
    extension: str,
    max_path_length: int = 255
) -> str:
    """
    Truncate filename components to fit within max_path_length.

    Removal priority (lowest first): original_filename, content_source, tmdb_id, resolution, version
    Then truncates: episode_title, title (aggressively if needed)
    Never removed: imdb_id, season_number, episode_number, year, season_year
    """
    removal_priority = ['original_filename', 'content_source', 'tmdb_id', 'resolution', 'version']
    truncatable_components = ['episode_title', 'title']
    working_vars = dict(template_vars)

    def calculate_full_path(filename_part: str) -> str:
        dir_path = os.path.join(base_path, *directory_parts) if directory_parts else base_path
        return os.path.join(dir_path, filename_part)

    def format_and_sanitize() -> str:
        try:
            formatted = template.format(**working_vars)
        except KeyError as e:
            logging.warning(f"[TruncatePath] Missing template variable: {e}")
            formatted = template
        formatted = _clean_separators_in_string(formatted)
        sanitized = sanitize_filename(formatted)
        if not sanitized.endswith(extension):
            sanitized += extension
        return sanitized

    def get_current_length() -> int:
        return len(calculate_full_path(format_and_sanitize()))

    if get_current_length() <= max_path_length:
        return format_and_sanitize()

    logging.debug(f"[TruncatePath] Path too long ({get_current_length()} > {max_path_length}), truncating...")

    # Remove components in priority order
    for component in removal_priority:
        if component in working_vars and working_vars[component]:
            original_value = working_vars[component]
            working_vars[component] = ''
            current_length = get_current_length()
            if current_length <= max_path_length:
                logging.info(f"[TruncatePath] Removed '{component}'. Path now valid ({current_length} chars).")
                return format_and_sanitize()
            logging.debug(f"[TruncatePath] Removed '{component}', still too long ({current_length} chars).")

    # Truncate episode_title and title (preserve imdb_id for Plex)
    for component in truncatable_components:
        if component in working_vars and working_vars[component]:
            original_value = str(working_vars[component])
            if len(original_value) <= 4:
                continue

            chars_to_remove = get_current_length() - max_path_length + 3
            if len(original_value) > chars_to_remove:
                working_vars[component] = original_value[:-(chars_to_remove)] + '...'
                if get_current_length() <= max_path_length:
                    logging.info(f"[TruncatePath] Truncated '{component}'. Path now valid.")
                    return format_and_sanitize()

                # Aggressive truncation
                while len(working_vars[component]) > 13:
                    working_vars[component] = working_vars[component][:-4] + '...'
                    if get_current_length() <= max_path_length:
                        logging.info(f"[TruncatePath] Truncated '{component}' to '{working_vars[component]}'.")
                        return format_and_sanitize()

    # Legacy fallback (imdb_id is never removed)
    sanitized = format_and_sanitize()
    full_path = calculate_full_path(sanitized)
    if len(full_path) > max_path_length:
        excess = len(full_path) - max_path_length
        filename_without_ext = os.path.splitext(sanitized)[0]
        if len(filename_without_ext) > excess + 3:
            sanitized = filename_without_ext[:-(excess + 3)] + "..." + extension
            logging.warning(f"[TruncatePath] Legacy fallback used: '{sanitized}'")
        else:
            logging.error(f"[TruncatePath] Cannot truncate sufficiently: {full_path}")

    return sanitized


def get_symlink_path(item: Dict[str, Any], original_file: str, skip_jikan_lookup: bool = False) -> str:
    """Get the full path for the symlink based on settings and metadata."""
    import json
    try:
        # --- BEGIN Enhanced Logging ---
        item_title_log = item.get('title', '[Unknown Title]')
        item_type_log = item.get('type', '[Unknown Type]')
        item_season_log = item.get('season_number', '[Unknown Season]')
        item_episode_log = item.get('episode_number', '[Unknown Episode]')
        item_version_log = item.get('version', '[Unknown Version]')
        logging.info(f"[SymlinkPath] Generating path for: Title='{item_title_log}', Type={item_type_log}, S={item_season_log}, E={item_episode_log}, Version='{item_version_log}'")
        logging.debug(f"[SymlinkPath] Original file received: {original_file}")
        # --- END Enhanced Logging ---
        
        logging.debug(f"get_symlink_path received item with filename_real_path: {item.get('filename_real_path')}")
        logging.debug(f"Input item: type={item.get('type')}, genres={item.get('genres')}, content_source={item.get('content_source')}")

        # Get the base symlink path from general File Management settings
        symlinked_path_base = get_setting('File Management', 'symlinked_files_path')
        
        # Check for content source specific custom subfolder
        final_symlinked_path_root = symlinked_path_base # Start with the base path
        item_content_source_id = item.get('content_source')

        if item_content_source_id:
            from utilities.settings import get_all_settings # Moved import here to avoid circular dependency if called at top level
            all_content_sources_config = get_all_settings().get('Content Sources', {})
            source_specific_config = all_content_sources_config.get(item_content_source_id)
            
            if source_specific_config:
                custom_subfolder_name = source_specific_config.get('custom_symlink_subfolder', '').strip()
                if custom_subfolder_name:
                    sanitized_custom_folder = sanitize_filename(custom_subfolder_name) # Sanitize to be safe
                    if sanitized_custom_folder: # Ensure not empty after sanitization
                        final_symlinked_path_root = os.path.join(symlinked_path_base, sanitized_custom_folder)
                        logging.info(f"[SymlinkPath] Using custom subfolder '{sanitized_custom_folder}' for content source '{item_content_source_id}'. New root: '{final_symlinked_path_root}'")
                    else:
                        logging.warning(f"[SymlinkPath] Custom subfolder for source '{item_content_source_id}' ('{custom_subfolder_name}') sanitized to empty. Using default base path.")
                else:
                    logging.debug(f"[SymlinkPath] No custom subfolder specified for content source '{item_content_source_id}'. Using default base path.")
            else:
                logging.debug(f"[SymlinkPath] No specific configuration found for content source '{item_content_source_id}'. Using default base path.")
        else:
            logging.debug("[SymlinkPath] No content_source ID found in item. Using default base symlink path.")

        # Check for user-selected custom folder (from web interface dropdown)
        # This overrides content source custom folder if user manually selected a different one
        selected_folder = item.get('selected_folder')
        selected_folder_is_custom = item.get('selected_folder_is_custom', False)

        if selected_folder and selected_folder_is_custom:
            # User selected a custom folder - update the root path
            sanitized_selected_folder = sanitize_filename(selected_folder)
            if sanitized_selected_folder:
                final_symlinked_path_root = os.path.join(symlinked_path_base, sanitized_selected_folder)
                logging.info(f"[SymlinkPath] User selected custom folder '{sanitized_selected_folder}'. New root: '{final_symlinked_path_root}'")
            else:
                logging.warning(f"[SymlinkPath] User-selected custom folder '{selected_folder}' sanitized to empty. Using current root.")

        # The rest of the function will use 'final_symlinked_path_root' as the starting point
        # instead of 'symlinked_path' which was previously 'symlinked_path_base'.
        # I will rename 'symlinked_path' to 'final_symlinked_path_root' in the following lines where it's used as the base.

        organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        organize_by_version = get_setting('File Management', 'symlink_organize_by_version', False)
        folder_order_str = get_setting('File Management', 'symlink_folder_order', "type,version,resolution")

        # Settings for type folder name determination
        enable_separate_anime_folders = get_setting('Debug', 'enable_separate_anime_folders', False)
        anime_movies_folder_name_setting = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies')
        anime_tv_shows_folder_name_setting = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows')
        movies_folder_name_setting = get_setting('Debug', 'movies_folder_name', 'Movies')
        tv_shows_folder_name_setting = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
        # Documentary folder settings
        enable_separate_documentary_folders = get_setting('Debug', 'enable_separate_documentary_folders', False)
        documentary_movies_folder_name_setting = get_setting('Debug', 'documentary_movies_folder_name', 'Documentary Movies')
        documentary_tv_shows_folder_name_setting = get_setting('Debug', 'documentary_tv_shows_folder_name', 'Documentary TV Shows')

        logging.debug(f"[SymlinkPath] Settings: final_symlinked_path_root='{final_symlinked_path_root}', "
                      f"organize_by_type={organize_by_type}, "
                      f"organize_by_version={organize_by_version}, "
                      f"organize_by_resolution={organize_by_resolution}, "
                      f"folder_order='{folder_order_str}', "
                      f"enable_separate_anime_folders={enable_separate_anime_folders}")
        
        # Get the original extension
        _, extension = os.path.splitext(original_file)
        
        # This list will store the ordered prefix (version, resolution, type folders)
        ordered_prefix_parts = []
        media_type = item.get('type', 'movie') # 'movie' or 'episode'

        folder_order_list = [comp.strip().lower() for comp in folder_order_str.split(',') if comp.strip()]
        logging.debug(f"[SymlinkPath] Parsed folder order: {folder_order_list}")

        for component in folder_order_list:
            if component == "version" and organize_by_version:
                version_str = item.get('version', '').strip('*') # Strip asterisks for folder name
                if version_str:
                    sanitized_version_folder = sanitize_filename(version_str)
                    if sanitized_version_folder: # Ensure not empty after sanitization
                        ordered_prefix_parts.append(sanitized_version_folder)
                        logging.debug(f"[SymlinkPath] Added version component to path: '{sanitized_version_folder}'")
            
            elif component == "resolution" and organize_by_resolution:
                resolution_folder_name = "Unknown" # Default to Unknown
                filled_by_file = item.get('filled_by_file')
                parsed_from_source = None # To track what was parsed

                if filled_by_file:
                    try:
                        parsed_data = parse_with_ptt(filled_by_file)
                        if parsed_data and not parsed_data.get('parsing_error'):
                            parsed_resolution = parsed_data.get('resolution')
                            if parsed_resolution and parsed_resolution.strip():
                                resolution_folder_name = parsed_resolution.strip()
                                parsed_from_source = "filled_by_file"
                                logging.debug(f"[SymlinkPath] Parsed resolution from filled_by_file '{filled_by_file}': '{resolution_folder_name}'")
                            else:
                                logging.debug(f"[SymlinkPath] No resolution found in parsed data for filled_by_file '{filled_by_file}'.")
                        else:
                            logging.warning(f"[SymlinkPath] PTT parsing error or no data for filled_by_file '{filled_by_file}'.")
                    except Exception as e:
                        logging.error(f"[SymlinkPath] Error parsing filled_by_file for resolution: {str(e)}")
                else:
                    logging.warning("[SymlinkPath] 'filled_by_file' not found in item. Will attempt filled_by_title.")

                # Fallback to filled_by_title if resolution is still "Unknown" or not parsed from filled_by_file
                if resolution_folder_name == "Unknown" or not parsed_from_source:
                    filled_by_title = item.get('filled_by_title')
                    if filled_by_title:
                        logging.debug(f"[SymlinkPath] Attempting to parse resolution from filled_by_title: '{filled_by_title}'")
                        try:
                            parsed_data_title = parse_with_ptt(filled_by_title)
                            if parsed_data_title and not parsed_data_title.get('parsing_error'):
                                parsed_resolution_title = parsed_data_title.get('resolution')
                                if parsed_resolution_title and parsed_resolution_title.strip():
                                    resolution_folder_name = parsed_resolution_title.strip()
                                    parsed_from_source = "filled_by_title"
                                    logging.info(f"[SymlinkPath] Parsed resolution from filled_by_title '{filled_by_title}': '{resolution_folder_name}'")
                                else:
                                    logging.debug(f"[SymlinkPath] No resolution found in parsed data for filled_by_title '{filled_by_title}'. Using 'Unknown'.")
                            else:
                                logging.warning(f"[SymlinkPath] PTT parsing error or no data for filled_by_title '{filled_by_title}'. Using 'Unknown'.")
                        except Exception as e:
                            logging.error(f"[SymlinkPath] Error parsing filled_by_title for resolution: {str(e)}. Using 'Unknown'.")
                    else:
                        logging.warning("[SymlinkPath] 'filled_by_title' not found in item. Using 'Unknown' for resolution.")
                
                if not parsed_from_source and resolution_folder_name == "Unknown":
                    logging.warning("[SymlinkPath] Resolution could not be determined from filled_by_file or filled_by_title. Using 'Unknown'.")

                if resolution_folder_name: # Ensure not empty, even if it's "Unknown"
                    ordered_prefix_parts.append(resolution_folder_name) 
                    logging.debug(f"[SymlinkPath] Added resolution component to path: '{resolution_folder_name}' (parsed from: {parsed_from_source or 'default/none'})")
            
            elif component == "type" and organize_by_type:
                # Initial genre value from the item
                item_genres_value = item.get('genres')

                # Check if genres are missing or effectively empty
                needs_genre_fetch = False
                if item_genres_value is None:
                    needs_genre_fetch = True
                elif isinstance(item_genres_value, str):
                    if not item_genres_value.strip() or item_genres_value.strip() == "[]" or item_genres_value.strip() == "[\"anime\"]":
                        needs_genre_fetch = True
                elif isinstance(item_genres_value, list) and not item_genres_value:
                    needs_genre_fetch = True
                
                if needs_genre_fetch:
                    logging.info(f"[SymlinkPath] Genres for item {item.get('imdb_id', 'N/A')} are missing or empty. Attempting to fetch from DirectAPI.")
                    
                    # --- Start: Fetch with Timeout ---
                    fetched_metadata = None
                    source = None
                    
                    def fetch_genres_task():
                        # This function will be executed in a separate thread
                        try:
                            from cli_battery.app.direct_api import DirectAPI
                            api_instance = DirectAPI()
                            item_imdb_id = item.get('imdb_id')
                            
                            if not item_imdb_id:
                                logging.warning("[SymlinkPath] Cannot fetch genres: IMDb ID is missing for item.")
                                return None, None
                            
                            if item.get('type') == 'movie':
                                return api_instance.get_movie_metadata(item_imdb_id)
                            else: # 'show' or 'episode'
                                return api_instance.get_show_metadata(item_imdb_id)
                        except ImportError:
                            logging.error("[SymlinkPath] Could not import DirectAPI. Genre fetching skipped.")
                            return None, None
                        except Exception as e:
                            logging.error(f"[SymlinkPath] Exception in genre fetch thread for {item.get('imdb_id', 'N/A')}: {e}")
                            return None, None

                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(fetch_genres_task)
                        try:
                            # Get timeout value from settings, default to 15 seconds
                            timeout_seconds = get_setting('Debug', 'direct_api_timeout', 15)
                            fetched_metadata, source = future.result(timeout=timeout_seconds)
                        except TimeoutError:
                            logging.error(f"[SymlinkPath] Timed out after {timeout_seconds}s while fetching genres for {item.get('imdb_id', 'N/A')}.")
                        except Exception as e_fetch:
                             logging.error(f"[SymlinkPath] Error fetching genres via DirectAPI for {item.get('imdb_id', 'N/A')}: {e_fetch}")
                    
                    if fetched_metadata and fetched_metadata.get('genres'):
                        item_genres_value = fetched_metadata.get('genres')
                        logging.info(f"[SymlinkPath] Successfully fetched genres for {item.get('imdb_id')} from {source}: {item_genres_value}")
                    elif item.get('imdb_id'): # Only log warning if we attempted a fetch
                        logging.warning(f"[SymlinkPath] Failed to fetch genres or genres were empty from DirectAPI for {item.get('imdb_id')}. Source: {source}")
                    # --- End: Fetch with Timeout ---

                # Process item_genres_value (which might have been updated by the fetch)
                parsed_genres_list = []
                if isinstance(item_genres_value, list):
                    # Ensure all elements are strings for '.lower()' later
                    parsed_genres_list = [str(g) for g in item_genres_value if g is not None]
                elif isinstance(item_genres_value, str) and item_genres_value.strip():
                    try:
                        # Try parsing as JSON first (e.g., '["Action", "Drama"]')
                        potential_list = json.loads(item_genres_value)
                        if isinstance(potential_list, list):
                            parsed_genres_list = [str(g) for g in potential_list if g is not None]
                        else: # Parsed to something other than a list
                            parsed_genres_list = [str(item_genres_value)]
                    except json.JSONDecodeError:
                        # If not JSON, assume comma-separated (e.g., "Action, Drama") or single genre
                        parsed_genres_list = [g.strip() for g in item_genres_value.split(',') if g.strip()]
                
                if not parsed_genres_list and item_genres_value: # If parsing failed but there was some input
                    logging.warning(f"[SymlinkPath] Could not parse genres '{str(item_genres_value)[:100]}' into a list for {item.get('imdb_id', 'N/A')}. Treating as no genres.")

                # Check if user manually selected a folder (from web interface dropdown)
                # Note: Custom folders are handled separately above (they change the root path)
                # Here we only handle standard type folders (Movies, Documentary Movies, etc.)
                selected_folder = item.get('selected_folder')
                selected_folder_is_custom = item.get('selected_folder_is_custom', False)

                logging.info(f"[SymlinkPath] ========== TYPE FOLDER SELECTION DEBUG ==========")
                logging.info(f"[SymlinkPath] selected_folder: {selected_folder}")
                logging.info(f"[SymlinkPath] selected_folder_is_custom: {selected_folder_is_custom}")
                logging.info(f"[SymlinkPath] Root path: {final_symlinked_path_root}")
                logging.info(f"[SymlinkPath] =============================================")

                if selected_folder and not selected_folder_is_custom:
                    # User selected a standard type folder (not a custom folder)
                    folder_name_for_type = selected_folder
                    logging.info(f"[SymlinkPath] Using user-selected type folder: '{folder_name_for_type}' (manual selection overrides genre-based auto-detection)")
                else:
                    # Determine content type based on genres (auto-detection)
                    is_anime = any('anime' in genre.lower() for genre in parsed_genres_list)
                    is_documentary = any('documentary' in genre.lower() for genre in parsed_genres_list)

                    folder_name_for_type = ""
                    if is_anime and enable_separate_anime_folders:
                        folder_name_for_type = anime_movies_folder_name_setting if media_type == 'movie' else anime_tv_shows_folder_name_setting
                        logging.debug(f"[SymlinkPath] Item classified as Anime. Folder type: '{folder_name_for_type}'")
                    elif is_documentary and enable_separate_documentary_folders:
                        folder_name_for_type = documentary_movies_folder_name_setting if media_type == 'movie' else documentary_tv_shows_folder_name_setting
                        logging.debug(f"[SymlinkPath] Item classified as Documentary. Folder type: '{folder_name_for_type}'")
                    else:
                        folder_name_for_type = movies_folder_name_setting if media_type == 'movie' else tv_shows_folder_name_setting
                        logging.debug(f"[SymlinkPath] Item classified as Standard Movie/Show. Folder type: '{folder_name_for_type}'")
                
                folder_name_for_type = folder_name_for_type.strip()
                if not folder_name_for_type:
                    logging.error("[SymlinkPath] Invalid type folder name: folder name is empty. Skipping type component.")
                else:
                    ordered_prefix_parts.append(folder_name_for_type)
                    logging.debug(f"[SymlinkPath] Added type component to path: '{folder_name_for_type}'")

        logging.debug(f"[SymlinkPath] Constructed ordered prefix parts: {ordered_prefix_parts}")
        
        # 'parts' will now start with the ordered_prefix_parts, and then template parts will be added to it.
        parts = list(ordered_prefix_parts) 
        
        # Prepare common template variables
        # Check if IMDb ID is the dummy value and set to empty if so
        imdb_id = item.get('imdb_id', '')
        if imdb_id == 'tt0000000':
            imdb_id = ''
            
        template_vars = {
            'title': item.get('title', 'Unknown'),
            'year': item.get('year', ''),
            'imdb_id': imdb_id,
            'tmdb_id': item.get('tmdb_id', ''),
            'version': item.get('version', '').strip('*'),  # Remove all asterisks for template placeholder use
            'original_filename': os.path.splitext(item.get('filled_by_file', ''))[0],
            'content_source': item.get('content_source', ''),
            'resolution': item.get('resolution', '')
        }

        if item.get('filename_real_path'):
            logging.debug(f"Using filename_real_path for original_filename: {item.get('filename_real_path')}")
            template_vars['original_filename'] = os.path.splitext(item.get('filename_real_path'))[0]
        
        if media_type == 'movie':
            template = get_setting('Debug', 'symlink_movie_template',
                                '{title} ({year})/{title} ({year}) - {imdb_id} - {version} - ({original_filename})')
        else: # episode
            s_num_val = item.get('season_number')
            e_num_val = item.get('episode_number')

            # If DB has season=0/episode=0 (e.g. misnamed season pack grabbed as S01E01),
            # try to recover the real S##E## from the actual filename.
            if (not s_num_val or int(s_num_val) == 0) and (not e_num_val or int(e_num_val) == 0):
                _orig = template_vars.get('original_filename', '')
                _m = re.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', _orig)
                if _m:
                    s_num_val = int(_m.group(1))
                    e_num_val = int(_m.group(2))
                    logging.debug(f'[SymlinkPath] Recovered S{s_num_val:02d}E{e_num_val:02d} from original_filename {_orig!r}')

            episode_vars = {
                'season_number': int(s_num_val if s_num_val is not None else 0),
                'episode_number': int(e_num_val if e_num_val is not None else 0),
                'episode_title': item.get('episode_title', '')
            }
            
            # Add season_year by querying database for earliest episode release date in the season
            season_year = None
            if s_num_val is not None:
                try:
                    season_year = get_season_year(
                        imdb_id=item.get('imdb_id'),
                        tmdb_id=item.get('tmdb_id'),
                        season_number=int(s_num_val)
                    )
                    if season_year:
                        episode_vars['season_year'] = season_year
                        logging.debug(f"[SymlinkPath] Found season year for S{s_num_val}: {season_year}")
                    else:
                        episode_vars['season_year'] = item.get('year', '')  # Fallback to show year
                        logging.debug(f"[SymlinkPath] No season year found for S{s_num_val}, using show year: {item.get('year', '')}")
                except Exception as e:
                    logging.warning(f"[SymlinkPath] Error getting season year for S{s_num_val}: {e}")
                    episode_vars['season_year'] = item.get('year', '')  # Fallback to show year
            else:
                episode_vars['season_year'] = item.get('year', '')  # Fallback to show year
            
            genres_for_anime_check = item.get('genres', '') or ''
            if isinstance(genres_for_anime_check, str):
                try:
                    import json
                    genres_for_anime_check = json.loads(genres_for_anime_check)
                except json.JSONDecodeError:
                    genres_for_anime_check = [g.strip() for g in genres_for_anime_check.split(',') if g.strip()]
            if not isinstance(genres_for_anime_check, list):
                genres_for_anime_check = [str(genres_for_anime_check)]
            is_anime_for_rename = any('anime' in genre.lower() for genre in genres_for_anime_check)

            # anidb_metadata_used = False
            # if get_setting('Debug', 'anime_renaming_using_anidb', False) and is_anime_for_rename and not skip_jikan_lookup:
            #     logging.info(f"[SymlinkPath] Anime detected and AniDB renaming enabled. Attempting to get AniDB metadata for '{item.get('title')} S{episode_vars.get('season_number')}E{episode_vars.get('episode_number')}")
            #     from utilities.anidb_functions import get_anidb_metadata_for_item # Ensure this import is correct
            #     anime_metadata = get_anidb_metadata_for_item(item)
            #     if anime_metadata:
            #         logging.info(f"[SymlinkPath] Successfully got AniDB metadata: {anime_metadata}")
            #         anidb_metadata_used = True
            #         episode_vars.update({
            #             'season_number': int(anime_metadata.get('season_number', episode_vars['season_number'])),
            #             'episode_number': int(anime_metadata.get('episode_number', episode_vars['episode_number'])),
            #             'episode_title': anime_metadata.get('episode_title', episode_vars['episode_title'])
            #         })
            #         if anime_metadata.get('title'): template_vars['title'] = anime_metadata['title']
            #         if anime_metadata.get('year'): template_vars['year'] = anime_metadata['year']
            #     else:
            #         logging.warning(f"[SymlinkPath] Failed to get AniDB metadata for '{item.get('title')}'. Using original item data.")
            # else:
            #     logging.debug(f"[SymlinkPath] AniDB renaming not used. Is Anime: {is_anime_for_rename}, Setting Enabled: {get_setting('Debug', 'anime_renaming_using_anidb', False)}")
            
            # Temporarily disabled AniDB/Jikan API calls
            anidb_metadata_used = False
            logging.debug(f"[SymlinkPath] AniDB renaming temporarily disabled. Is Anime: {is_anime_for_rename}")
            
            template_vars.update(episode_vars)
            
            # Handle multi-episode format in template
            base_template = get_setting('Debug', 'symlink_episode_template',
                                '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})')
            
            # If episode_number is a string (multi-episode format like "E17-E18"), replace the format specifier
            if isinstance(episode_vars.get('episode_number'), str) and 'E' in str(episode_vars.get('episode_number')):
                # Replace {episode_number:02d} with {episode_number} to avoid format error
                template = base_template.replace('{episode_number:02d}', '{episode_number}')
            else:
                template = base_template
        
        path_parts_from_template = template.split('/')
        logging.debug(f"[SymlinkPath] Using template: '{template}'")
        logging.debug(f"[SymlinkPath] Template variables: {template_vars}")
        
        final_filename = "" 

        for i, part_template_segment in enumerate(path_parts_from_template):
            formatted_part = part_template_segment.format(**template_vars)
            sanitized_template_part = sanitize_filename(formatted_part)
            
            if i == len(path_parts_from_template) - 1: # This is the filename part
                # Use the new component-based truncation strategy
                # This will intelligently remove/truncate components in priority order
                max_path_length = 255
                final_filename = truncate_path_components(
                    template=part_template_segment,
                    template_vars=template_vars,
                    base_path=final_symlinked_path_root,
                    directory_parts=parts,
                    extension=extension,
                    max_path_length=max_path_length
                )
            else: # This is a directory part from the template
                if sanitized_template_part: # Ensure not empty
                    parts.append(sanitized_template_part)
        
        # 'parts' now contains: ordered_prefix_parts + directory_parts_from_template
        dir_path = os.path.join(final_symlinked_path_root, *parts)
        
        try:
            os.makedirs(dir_path, exist_ok=True)
            logging.debug(f"Ensured directory path exists: {dir_path}")
        except Exception as e:
            logging.error(f"Failed to create directory path {dir_path}: {str(e)}")
            return None
        
        full_path = os.path.join(dir_path, final_filename)
        
        logging.info(f"[SymlinkPath] Generated path: {full_path}")
        
        if os.path.exists(full_path):
            logging.info(f"Symlink path already exists: {full_path}")
            
        return full_path
        
    except Exception as e:
        logging.error(f"[SymlinkPath] Error generating symlink path for item {item.get('id', '')}: {str(e)}", exc_info=True)
        return None

def create_symlink(source_path: str, dest_path: str, media_item_id: int = None, skip_verification: bool = False) -> bool:
    """Creates a symlink from source_path to dest_path."""
    
    # Normalize paths for better compatibility
    source_path = os.path.abspath(source_path)
    dest_path = os.path.abspath(dest_path)
    
    # Basic checks
    if not source_path or not dest_path:
        logging.error("Source or destination path is empty.")
        return False
    
    if not os.path.exists(source_path):
        logging.error(f"Source path does not exist: {source_path}")
        return False
        
    symlink_ok_or_created = False

    # If destination exists and is a symlink, check if it points to the correct source
    if os.path.islink(dest_path):
        try:
            current_target = os.path.realpath(dest_path)
            if current_target == source_path:
                logging.info(f"Symlink already exists and points to the correct target: {dest_path}")
                symlink_ok_or_created = True # Symlink is already correct
            else:
                logging.warning(f"Symlink exists but points to wrong target ('{current_target}' vs expected '{source_path}'). Removing and recreating.")
                os.unlink(dest_path)
                # Will fall through to create it below
        except Exception as e:
            logging.error(f"Error checking existing symlink {dest_path}: {e}. Attempting to remove and recreate.")
            try:
                os.unlink(dest_path) # Attempt to remove even on error
            except Exception as unlink_err_on_check_error:
                # If removal fails after check error, this is problematic.
                if not isinstance(unlink_err_on_check_error, FileNotFoundError): # Don't error if it was already gone
                    logging.error(f"Failed to remove potentially problematic symlink {dest_path} after check error: {unlink_err_on_check_error}")
                    return False # Cannot proceed safely
            # Will fall through to create it below, assuming problematic link is gone or never existed

    elif os.path.exists(dest_path):
        # If destination exists but is not a symlink (e.g., a regular file), log an error.
        logging.error(f"Destination path exists but is not a symlink: {dest_path}. Cannot create symlink.")
        return False
        
    # Ensure the directory for the destination path exists
    # This needs to happen before attempting to create the symlink if it doesn't exist yet,
    # or if it was unlinked above.
    if not symlink_ok_or_created or (os.path.islink(dest_path) and not os.path.exists(os.path.dirname(dest_path))): # Second condition for recreated links
        dest_dir = os.path.dirname(dest_path)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            logging.error(f"Failed to create directory for symlink {dest_path}: {e}")
            return False

    # If symlink wasn't already OK (i.e., it's a new path or an incorrect/broken one was unlinked), attempt to create it
    if not symlink_ok_or_created:
        try:
            os.symlink(source_path, dest_path)
            logging.info(f"Created symlink: {dest_path} -> {source_path}")
            symlink_ok_or_created = True # Symlink successfully created
        except Exception as e:
            logging.error(f"Failed to create symlink from {source_path} to {dest_path}: {e}")
            return False # Symlink creation failed, cannot proceed

    # If the symlink is now considered OK (either pre-existing correctly or created/recreated successfully)
    # then attempt to add/update it in the verification queue.
    if symlink_ok_or_created:
        if media_item_id is not None and not skip_verification:
            try:
                # add_symlinked_file_for_verification will handle if it's new or needs reset
                add_symlinked_file_for_verification(media_item_id, dest_path)
                # Use a consistent logging message whether it was pre-existing or new
                logging.info(f"Ensured file is in/updated in verification queue: {dest_path} (Media ID: {media_item_id})")
            except Exception as e:
                logging.error(f"Failed to add/update file in verification queue {dest_path} (Media ID: {media_item_id}): {e}", exc_info=True)
                # Continue even if adding to verification fails for now, as symlink itself is OK.
    
    return symlink_ok_or_created

def _get_video_extensions() -> set:
    """Return a set of common video file extensions."""
    return {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.ts', '.m2ts'}


def _find_all_video_files_in_folder(folder_path: str, primary_file: str) -> List[str]:
    """
    Find all video files in the same folder as the primary file.
    Returns a list of full paths to all video files found.

    Args:
        folder_path: The folder containing the primary file
        primary_file: The primary file that was already found (will be included in results)

    Returns:
        List of full paths to all video files in the folder
    """
    video_extensions = _get_video_extensions()
    video_files = []

    try:
        if not os.path.isdir(folder_path):
            logging.debug(f"[MultiFile] Folder path is not a directory: {folder_path}")
            return [primary_file] if primary_file else []

        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path):
                _, ext = os.path.splitext(filename)
                if ext.lower() in video_extensions:
                    video_files.append(file_path)

        if video_files:
            logging.info(f"[MultiFile] Found {len(video_files)} video file(s) in folder {folder_path}: {[os.path.basename(f) for f in video_files]}")
        else:
            # If no video files found but we have the primary file, return it
            if primary_file:
                video_files = [primary_file]

    except Exception as e:
        logging.error(f"[MultiFile] Error scanning folder {folder_path}: {e}")
        if primary_file:
            return [primary_file]
        return []

    return video_files


def _apply_nzb_naming(source_file: str, item: Dict[str, Any]) -> str:
    """
    For NZB items in Plex mode with enable_nzb_naming enabled:
    Move the downloaded file to a path mirroring the symlink template structure,
    rooted at original_files_path instead of symlinked_files_path.

    Returns the new source_file path (moved), or original source_file if skipped.
    """
    try:
        # Only NZB items
        torrent_id = item.get('filled_by_torrent_id', '') or ''
        if not str(torrent_id).startswith('nzb:'):
            return source_file

        # Guard: Plex mode + setting enabled
        if get_setting('File Management', 'file_collection_management', 'Plex') != 'Plex':
            return source_file
        if not get_setting('Usenet Provider', 'enable_nzb_naming', False):
            return source_file

        original_path = get_setting('File Management', 'original_files_path', '')
        symlinked_path = get_setting('File Management', 'symlinked_files_path', '')
        if not original_path or not symlinked_path:
            return source_file

        # Get the structured path that get_symlink_path would produce
        structured = get_symlink_path(item, source_file, skip_jikan_lookup=False)
        if not structured:
            return source_file

        # Strip the symlinked_files_path prefix to get the relative organised path
        symlinked_path_norm = os.path.normpath(symlinked_path)
        structured_norm = os.path.normpath(structured)
        if not structured_norm.startswith(symlinked_path_norm + os.sep):
            logging.warning(f'[NZBNaming] structured path {structured!r} not under symlinked_path {symlinked_path!r} — skipping rename')
            return source_file

        rel_path = structured_norm[len(symlinked_path_norm) + 1:]
        new_path = os.path.join(original_path, rel_path)

        # Already in place
        if os.path.normpath(source_file) == os.path.normpath(new_path):
            return source_file

        # Create parent dirs and move
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if not os.path.exists(new_path):
            os.rename(source_file, new_path)
            logging.info(f'[NZBNaming] Moved {os.path.basename(source_file)!r} → {rel_path!r}')
        else:
            logging.debug(f'[NZBNaming] Target already exists: {new_path!r} — skipping move')

        # Update item fields so DB and Plex scan use the new path
        item['filled_by_file'] = os.path.basename(new_path)
        item['filled_by_title'] = os.path.basename(os.path.dirname(new_path))
        item['debrid_folder_name'] = os.path.basename(os.path.dirname(new_path))
        return new_path

    except Exception as _e:
        logging.warning(f'[NZBNaming] Could not apply NZB naming to {source_file!r}: {_e}')
        return source_file


def _cleanup_old_symlink(item: Dict[str, Any], item_identifier: str, source_file: str, old_filename: str) -> None:
    """Remove the old symlink (and notify the media server) after a successful upgrade.

    Only called when Scraping.enable_upgrading_cleanup is enabled — the old torrent/file
    has already been removed by the caller at this point, this just cleans up the stale
    symlink that pointed at it.
    """
    old_base_path = os.path.dirname(source_file) if source_file else None

    if not (old_base_path and old_filename):
        logging.warning("[UPGRADE] Could not determine old source path components for symlink removal.")
        return

    # Construct the hypothetical source path for the old file
    old_source_for_symlink_path = os.path.join(old_base_path, old_filename)

    # Temporarily modify a copy of the item to represent the OLD file state for get_symlink_path
    item_for_old_path = item.copy()
    item_for_old_path['filled_by_file'] = old_filename
    # Explicitly set the version to the one we are upgrading FROM
    old_version_str = item.get('upgrading_from_version')
    if old_version_str:
        item_for_old_path['version'] = old_version_str
        logging.info(f"[UPGRADE] Using old version '{old_version_str}' for old symlink path calculation.")
    else:
        logging.warning("[UPGRADE] 'upgrading_from_version' not found in item dict. Old symlink path might be incorrect if version changed.")
        # Keep the current version as a fallback if the old one isn't stored

    old_dest = get_symlink_path(item_for_old_path, old_source_for_symlink_path, skip_jikan_lookup=False)

    if not (old_dest and os.path.lexists(old_dest)):
        logging.debug(f"[UPGRADE] No old symlink found at {old_dest} (or path couldn't be determined).")
        return

    try:
        os.unlink(old_dest)
        logging.info(f"[UPGRADE] Removed old symlink during upgrade: {old_dest}")

        try:
            removed_count = remove_verification_by_media_item_id(item['id'])
            if removed_count > 0:
                logging.info(f"[UPGRADE] Removed {removed_count} old verification record(s) for media item ID {item['id']}")
            else:
                logging.debug(f"[UPGRADE] No existing verification record found to remove for media item ID {item['id']}")
        except Exception as db_remove_err:
            logging.error(f"[UPGRADE] Failed to remove old verification record for media item ID {item['id']}: {db_remove_err}")

        # Add the path to the removal verification queue with titles
        episode_title_for_removal = item.get('episode_title') if item.get('type') == 'episode' else None
        add_path_for_removal_verification(old_dest, item['title'], episode_title_for_removal)
        # Wait for media server to detect the removed symlink
        time.sleep(1)

        # Remove the old file from Plex or Emby/Jellyfin
        media_server_type = 'none'
        if get_setting('Debug', 'emby_jellyfin_url', default=False):
            media_server_type = 'emby_jellyfin'
        elif get_setting('File Management', 'plex_url_for_symlink', default=False):
            media_server_type = 'plex'

        if media_server_type != 'none':
            try:
                episode_title = item.get('episode_title') if item.get('type') == 'episode' else None
                if media_server_type == 'emby_jellyfin':
                    from utilities.emby_functions import remove_file_from_emby
                    remove_file_from_emby(item['title'], old_dest, episode_title)
                elif media_server_type == 'plex':
                    from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
                    success = remove_file_from_plex(item['title'], old_dest, episode_title)
                    # If direct removal failed (possibly 400 error), try scan & empty trash as fallback
                    if not success:
                        logging.warning(f"[UPGRADE] Direct Plex removal failed for '{item['title']}'. Trying scan & empty trash...")
                        try:
                            # Determine section type based on item type
                            section_type = 'movie' if item.get('type') == 'movie' else 'show'
                            scan_paths = [os.path.dirname(old_dest)] if old_dest else None
                            scan_and_empty_plex_trash(paths=scan_paths, section_type=section_type)
                            logging.info(f"[UPGRADE] Triggered library scan and trash empty for '{item['title']}' (section_type={section_type}).")
                        except Exception as scan_err:
                            logging.warning(f"[UPGRADE] Scan & empty trash also failed for '{item['title']}': {scan_err}")
            except Exception as media_server_remove_err:
                 logging.error(f"[UPGRADE] Failed removing old file {old_dest} from {media_server_type}: {media_server_remove_err}")

    except Exception as e:
        logging.error(f"[UPGRADE] Failed to remove old symlink {old_dest}: {str(e)}")


def check_local_file_for_item(item: Dict[str, Any], is_webhook: bool = False, extended_search: bool = False, on_success_callback: Optional[Callable[[str], None]] = None, skip_multifile_scan: bool = False) -> bool:
    """
    Check if the local file for the item exists and create symlink if needed.
    When called from webhook endpoint, will retry up to 5 times with 1 second delay.
    Calls on_success_callback(relative_path) upon successful processing.

    For movies, this function will also scan for additional video files in the same folder
    and create separate database entries for each file (similar to Plex mode behavior).

    Args:
        item: Dictionary containing item details
        is_webhook: If True, enables retry mechanism for webhook calls
        extended_search: If True, will perform an extended search for the file
        on_success_callback: Optional function to call with the relative path upon success.
        skip_multifile_scan: If True, skips scanning for additional files in the folder (prevents recursive scanning)

    Returns:
        True if successful, False otherwise.
    """
    max_retries = 10 if is_webhook else 1
    retry_delay = 3  # second

    for attempt in range(max_retries):
        try:
            if not item.get('filled_by_file'):
                return False

            original_path = get_setting('File Management', 'original_files_path')

            # --- Get potential folder names ---
            filled_by_title = item.get('filled_by_title', '')
            original_torrent_title = item.get('original_scraped_torrent_title', '')
            real_debrid_original_title = item.get('real_debrid_original_title', '')
            debrid_folder_name = item.get('debrid_folder_name', '')
            current_filename = item['filled_by_file'] # The actual file we are looking for

            found_file = False
            source_file = None # Initialize source_file
            source_folder = None # Track the folder where the file was found

            # --- Check Order: Exact provider folder -> Original Torrent Title -> Filled By Title ---

            # 0. Check exact provider folder name persisted from the debrid API.
            if debrid_folder_name and not found_file:
                potential_folder = os.path.join(original_path, debrid_folder_name)
                potential_path = os.path.join(potential_folder, current_filename)
                logging.debug(f"Attempt 0: Checking path using debrid_folder_name: {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    source_folder = potential_folder
                    found_file = True
                    logging.info(f"Found file using debrid_folder_name: {source_file}")

            # 1. Check original_scraped_torrent_title (raw)
            if original_torrent_title:
                potential_folder = os.path.join(original_path, original_torrent_title)
                potential_path = os.path.join(potential_folder, current_filename)
                logging.debug(f"Attempt 1: Checking path using original_scraped_torrent_title: {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    source_folder = potential_folder
                    found_file = True
                    logging.info(f"Found file using original_scraped_torrent_title (raw): {source_file}")

            # 2. Check original_scraped_torrent_title (trimmed)
            if not found_file and original_torrent_title:
                original_torrent_title_trimmed = os.path.splitext(original_torrent_title)[0]
                if original_torrent_title_trimmed != original_torrent_title: # Only check if trimming actually changed the name
                    potential_folder = os.path.join(original_path, original_torrent_title_trimmed)
                    potential_path = os.path.join(potential_folder, current_filename)
                    logging.debug(f"Attempt 2: Checking path using trimmed original_scraped_torrent_title: {potential_path}")
                    if os.path.exists(potential_path):
                        source_file = potential_path
                        source_folder = potential_folder
                        found_file = True
                        logging.info(f"Found file using original_scraped_torrent_title (trimmed): {source_file}")

            # 3. Check real_debrid_original_title (raw) (NEW)
            if not found_file and real_debrid_original_title:
                potential_folder = os.path.join(original_path, real_debrid_original_title)
                potential_path = os.path.join(potential_folder, current_filename)
                logging.debug(f"Attempt 3 (New): Checking path using real_debrid_original_title: {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    source_folder = potential_folder
                    found_file = True
                    logging.info(f"Found file using real_debrid_original_title (raw): {source_file}")

            # 4. Check real_debrid_original_title (trimmed) (NEW)
            if not found_file and real_debrid_original_title:
                real_debrid_original_title_trimmed = os.path.splitext(real_debrid_original_title)[0]
                if real_debrid_original_title_trimmed != real_debrid_original_title:
                    potential_folder = os.path.join(original_path, real_debrid_original_title_trimmed)
                    potential_path = os.path.join(potential_folder, current_filename)
                    logging.debug(f"Attempt 4 (New): Checking path using trimmed real_debrid_original_title: {potential_path}")
                    if os.path.exists(potential_path):
                        source_file = potential_path
                        source_folder = potential_folder
                        found_file = True
                        logging.info(f"Found file using real_debrid_original_title (trimmed): {source_file}")

            # 5. Check filled_by_title (raw)
            if not found_file and filled_by_title:
                potential_folder = os.path.join(original_path, filled_by_title)
                potential_path = os.path.join(potential_folder, current_filename)
                logging.debug(f"Attempt 5: Checking path using filled_by_title: {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    source_folder = potential_folder
                    found_file = True
                    logging.info(f"Found file using filled_by_title (raw): {source_file}")

            # 6. Check filled_by_title (trimmed)
            if not found_file and filled_by_title:
                filled_by_title_trimmed = os.path.splitext(filled_by_title)[0]
                if filled_by_title_trimmed != filled_by_title: # Only check if trimming actually changed the name
                    potential_folder = os.path.join(original_path, filled_by_title_trimmed)
                    potential_path = os.path.join(potential_folder, current_filename)
                    logging.debug(f"Attempt 6: Checking path using trimmed filled_by_title: {potential_path}")
                    if os.path.exists(potential_path):
                        source_file = potential_path
                        source_folder = potential_folder
                        found_file = True
                        logging.info(f"Found file using filled_by_title (trimmed): {source_file}")

            # 7. Check direct path (less common, added for completeness)
            if not found_file:
                 potential_path = os.path.join(original_path, current_filename)
                 logging.debug(f"Attempt 7: Checking direct path under original_files_path: {potential_path}")
                 if os.path.exists(potential_path):
                     source_file = potential_path
                     source_folder = original_path  # The folder is the original_path itself
                     found_file = True
                     logging.info(f"Found file directly under original_files_path: {source_file}")

            # 8. Check filename-as-folder pattern (single-file torrent on Real-Debrid).
            # RD presents single-file torrents as: original_path/{file.mkv}/{file.mkv}
            # None of the title-based attempts above catch this because the folder name
            # includes the file extension, while stored titles typically do not.
            if not found_file:
                potential_folder = os.path.join(original_path, current_filename)
                potential_path = os.path.join(potential_folder, current_filename)
                logging.debug(f"Attempt 8: Checking filename-as-folder (RD single-file pattern): {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    source_folder = potential_folder
                    found_file = True
                    logging.info(f"Found file using filename-as-folder pattern: {source_file}")

            # 9. Extended search: scan original_path subdirectories for the file.
            # Only runs when extended_search=True (activated after 900s in checking queue)
            # and all named-folder attempts have failed.
            if not found_file and extended_search:
                logging.info(f"Extended search: scanning '{original_path}' for '{current_filename}'")
                try:
                    for folder_name in os.listdir(original_path):
                        candidate_folder = os.path.join(original_path, folder_name)
                        if not os.path.isdir(candidate_folder):
                            continue
                        candidate_path = os.path.join(candidate_folder, current_filename)
                        if os.path.exists(candidate_path):
                            source_file = candidate_path
                            source_folder = candidate_folder
                            found_file = True
                            logging.info(f"Extended search found file in folder '{folder_name}': {source_file}")
                            break
                except Exception as ext_err:
                    logging.warning(f"Extended search failed: {ext_err}")

            # --- Handling not found after all checks ---
            if not found_file:
                if is_webhook and attempt < max_retries - 1:
                    logging.info(f"File '{current_filename}' not found using any title variation, attempt {attempt + 1}/{max_retries}. Retrying in {retry_delay} second...")
                    time.sleep(retry_delay)
                    continue
                logging.warning(f"File '{current_filename}' not found in any checked location")
                return False
            
            # For NZB items in Plex mode with NZB naming enabled: move file to organised structure
            source_file = _apply_nzb_naming(source_file, item)

            # Get destination path based on settings (using the found source_file)
            dest_file = get_symlink_path(item, source_file, skip_jikan_lookup=False)
            if not dest_file:
                return False
            
            success = False
            
            # Create item identifier first
            item_identifier = f"{item.get('title')} ({item.get('year', '')})"
            if item.get('type') == 'episode':
                item_identifier += f" S{item.get('season_number', '00'):02d}E{item.get('episode_number', '00'):02d}"
            
            # Check if this is a potential upgrade based on release date
            if str(item.get('release_date', '')).lower() in ['unknown', 'none', '']:
                # Treat unknown release dates as very recent (0 days since release)
                logging.debug(f"[UPGRADE] Unknown release date for {item_identifier} - treating as new content")
                days_since_release = 0
            else:
                try:
                    release_date = datetime.strptime(item.get('release_date', '1970-01-01'), '%Y-%m-%d').date()
                    days_since_release = (datetime.now().date() - release_date).days
                except ValueError:
                    # Handle invalid but non-empty release dates by treating them as new
                    logging.debug(f"[UPGRADE] Invalid release date format: {item.get('release_date')} - treating as new content")
                    days_since_release = 0
            
            # Add check for content_source to prevent manual assignments from triggering upgrades
            is_manually_assigned = item.get('content_source') == 'Magnet_Assigner'
            is_upgrade_candidate = (days_since_release <= 7 and 
                                    get_setting("Scraping", "enable_upgrading", default=False) and
                                    not is_manually_assigned) # Check if NOT manually assigned
            
            # Log upgrade status
            logging.debug(f"[UPGRADE] Processing item: {item_identifier}")
            logging.debug(f"[UPGRADE] Days since release: {days_since_release}")
            logging.debug(f"[UPGRADE] Is manually assigned (Magnet_Assigner): {is_manually_assigned}")
            logging.debug(f"[UPGRADE] Is upgrade candidate: {is_upgrade_candidate}")
            logging.debug(f"[UPGRADE] Current file: {item.get('filled_by_file')}")
            logging.debug(f"[UPGRADE] Upgrading from: {item.get('upgrading_from')}")
            logging.debug(f"[UPGRADE] Torrent ID: {item.get('filled_by_torrent_id')}")

            # Only handle cleanup if we have a confirmed upgrade (upgrading_from is set)
            if item.get('upgrading_from'):
                item_title = item.get('title') # For logging
                logging.info(f"[UPGRADE] Processing confirmed upgrade for {item_identifier}")

                upgrading_cleanup_enabled = get_setting("Scraping", "enable_upgrading_cleanup", default=False)

                # --- Start: Torrent/File Removal Logic ---
                old_torrent_id = item.get('upgrading_from_torrent_id')
                old_filename = item.get('upgrading_from') # Filename of the file being replaced

                if not upgrading_cleanup_enabled:
                    logging.info(f"[UPGRADE] Scraping.enable_upgrading_cleanup is disabled — keeping old file/torrent for {item_identifier} and skipping removal.")
                    removal_successful = True
                elif old_torrent_id:
                    removal_successful = False
                    if str(old_torrent_id).startswith('nzb:'):
                        # NZB jobs are managed by cli_mount, not debrid — skip debrid removal
                        logging.debug(f"[UPGRADE] Old torrent {old_torrent_id} is an NZB job — skipping debrid removal")
                        removal_successful = True
                    else:
                        logging.info(f"[UPGRADE] Attempting to remove old torrent {old_torrent_id} via debrid API.")
                        try:
                            from debrid import get_debrid_provider, ProviderUnavailableError
                            debrid_provider = get_debrid_provider()
                            if not debrid_provider:
                                logging.debug(f"[UPGRADE] No debrid provider configured — skipping torrent removal for {old_torrent_id}")
                                removal_successful = True
                            else:
                                debrid_provider.remove_torrent(
                                    old_torrent_id,
                                    removal_reason="Removed old torrent after successful upgrade"
                                )
                                removal_successful = True
                                logging.info(f"[UPGRADE] Successfully initiated removal of old torrent {old_torrent_id} via debrid API.")
                        except ProviderUnavailableError:
                            logging.debug(f"[UPGRADE] Debrid provider unavailable — skipping torrent removal for {old_torrent_id}")
                            removal_successful = True
                        except Exception as remove_err:
                            # Check if it's a 404 (Not Found), which might mean it was already deleted
                            if '404' in str(remove_err):
                                logging.warning(f"[UPGRADE] Old torrent {old_torrent_id} not found on debrid (likely already removed). Proceeding.")
                                removal_successful = True
                            else:
                                logging.error(f"[UPGRADE] Failed to remove old torrent {old_torrent_id} via debrid API: {remove_err}")
                else:
                    removal_successful = False
                    old_file_path_from_item = item.get('original_path_for_symlink') # Get path from item dict
                    logging.warning(f"[UPGRADE] Old torrent ID is missing for item {item['id']}. Attempting local file deletion using item's original path: '{old_file_path_from_item}'")
                    # Directly use the path from the item dict
                    if old_file_path_from_item and os.path.exists(old_file_path_from_item):
                        try:
                            os.remove(old_file_path_from_item)
                            removal_successful = True # Assume success if os.remove doesn't raise error
                            logging.info(f"[UPGRADE] Successfully removed old local file: {old_file_path_from_item}")
                            # Also remove subtitle files with same stem (e.g. Movie.en.srt, Movie.srt)
                            _SUBTITLE_EXTS = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.sup', '.pgs'}
                            _old_dir = os.path.dirname(old_file_path_from_item)
                            _old_stem = os.path.splitext(os.path.basename(old_file_path_from_item))[0]
                            try:
                                for _f in os.listdir(_old_dir):
                                    _fpath = os.path.join(_old_dir, _f)
                                    _fname_no_ext, _fext = os.path.splitext(_f)
                                    # Match exact stem or stem.language (e.g. Movie.en)
                                    if (_fext.lower() in _SUBTITLE_EXTS and
                                            (_fname_no_ext == _old_stem or
                                             _fname_no_ext.startswith(_old_stem + '.'))):
                                        os.remove(_fpath)
                                        logging.info(f"[UPGRADE] Removed subtitle file: {_fpath}")
                            except Exception as _sub_err:
                                logging.debug(f"[UPGRADE] Subtitle cleanup error: {_sub_err}")
                            # Optionally, check if the file is truly gone
                            if os.path.exists(old_file_path_from_item):
                                logging.warning(f"[UPGRADE] Local file {old_file_path_from_item} still exists after os.remove attempt.")
                                removal_successful = False
                        except IsADirectoryError:
                            # Zurg mounts files as virtual directories under __all__; os.remove() can't
                            # delete them. The underlying torrent will be cleaned up separately via RD.
                            logging.warning(f"[UPGRADE] Old path '{old_file_path_from_item}' is a directory (likely Zurg mount). Cannot remove via os.remove — treating as success to unblock upgrade.")
                            removal_successful = True
                        except OSError as delete_err:
                            logging.error(f"[UPGRADE] Failed to delete old local file {old_file_path_from_item}: {delete_err}")
                    elif not old_file_path_from_item:
                         logging.error(f"[UPGRADE] Cannot attempt local file deletion: 'original_path_for_symlink' key is missing or None in item dict.")
                    else: # Path provided in item dict but doesn't exist
                         logging.warning(f"[UPGRADE] Cannot attempt local file deletion: Path from item dict '{old_file_path_from_item}' does not exist.")
                         removal_successful = True # If the file doesn't exist where expected, treat as success for cleanup

                # --- End: Torrent/File Removal Logic ---

                # Only proceed if removal was successful or deemed unnecessary
                if removal_successful:
                    logging.info("[UPGRADE] Old file/torrent removal successful or not needed, proceeding with symlink cleanup/creation.")

                    if not upgrading_cleanup_enabled:
                        logging.info(f"[UPGRADE] Scraping.enable_upgrading_cleanup is disabled — keeping old symlink in place for {item_identifier}.")
                    else:
                        _cleanup_old_symlink(item, item_identifier, source_file, old_filename)

                    # Note: Symlink creation moved outside the upgrade block to run unconditionally

                else:
                    logging.error(f"[UPGRADE] Failed to remove the old file/torrent for {item_identifier}. Skipping symlink cleanup and creation for the new file.")
                    # Exit the upgrade process for this item if old file couldn't be handled
                    # We need to signal failure back up the call stack if necessary
                    return False # Indicate failure

            # --- Unconditionally attempt to create/replace the symlink ---
            # This runs for both upgrades (after cleanup) and non-upgrades
            logging.info(f"Attempting to create/replace symlink: {source_file} -> {dest_file}")
            success = create_symlink(source_file, dest_file, item.get('id'), skip_verification=False)
            if not success:
                 logging.error(f"Failed to create/replace symlink at {dest_file}. Aborting process for this item.")
                 return False # Abort if symlink creation fails for any reason

            # --- Proceed with database update if symlink process was successful ---
            # Note: The 'if success:' check remains, now referring to the unconditional attempt above
            if success: 
                logging.info(f"Successfully processed symlink at: {dest_file}")

                # Set state based on whether this is an upgrade candidate
                new_state = 'Upgrading' if is_upgrade_candidate else 'Collected'
                logging.info(f"[UPGRADE] Setting item state to: {new_state} (is_manually_assigned={is_manually_assigned})")
                
                current_time = datetime.now()
                
                # Prepare update values
                update_values = {
                        'location_on_disk': dest_file,
                    'collected_at': current_time,
                    'original_collected_at': current_time,
                    'original_path_for_symlink': source_file,
                    'state': new_state,
                    'filled_by_title': item.get('filled_by_title'),
                    'filled_by_file': item.get('filled_by_file'),
                    'filled_by_magnet': item.get('filled_by_magnet'),
                    'filled_by_torrent_id': item.get('filled_by_torrent_id'),
                    'resolution': item.get('resolution'),
                    'upgrading_from': item.get('upgrading_from')  # Always include upgrading_from
                }
                
                logging.debug(f"[UPGRADE] Updating item with values: {update_values}")
                update_media_item(item['id'], **update_values)

                # Add post-processing call after state update
                updated_item = get_media_item_by_id(item['id'])
                if updated_item:
                    if new_state == 'Collected':
                        handle_state_change(dict(updated_item))
                    elif new_state == 'Upgrading':
                        handle_state_change(dict(updated_item))

                # --- REPLACE SEASON/MOVIE HOOK ---
                # If any other entry with the same imdb_id (and season/episode for shows) has
                # manual_replace=1, we've just replaced it — clean up the old entry from
                # Debrid, Plex, and the database.
                _item_type = item.get('type')
                _item_imdb = item.get('imdb_id')
                if new_state == 'Collected' and _item_imdb and _item_type in ('episode', 'movie'):
                    _is_episode = _item_type == 'episode'
                    _has_coords = not _is_episode or (item.get('season_number') is not None and item.get('episode_number') is not None)
                    if _has_coords:
                        _log_tag = 'REPLACE_SEASON' if _is_episode else 'REPLACE_MOVIE'
                        try:
                            from database.core import get_db_connection as _get_conn
                            _conn = _get_conn()
                            _sel = 'id, filled_by_torrent_id, filled_by_file, location_on_disk, title, episode_title'
                            if _is_episode:
                                old_replace_items = _conn.execute(
                                    f'''SELECT {_sel} FROM media_items
                                       WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                                       AND type = 'episode' AND manual_replace = 1 AND id != ?''',
                                    (_item_imdb, item['season_number'], item['episode_number'], item['id'])
                                ).fetchall()
                                _removal_reason = 'Replaced by new season pack'
                                _entry_label = 'episode'
                                _section_type = 'show'
                            else:
                                old_replace_items = _conn.execute(
                                    f'''SELECT {_sel} FROM media_items
                                       WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1 AND id != ?''',
                                    (_item_imdb, item['id'])
                                ).fetchall()
                                _removal_reason = 'Replaced by new movie torrent'
                                _entry_label = 'movie'
                                _section_type = 'movie'
                            _conn.close()

                            if old_replace_items:
                                from database.database_writing import remove_from_media_items as _remove_item
                                from debrid import get_debrid_provider as _get_debrid
                                from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
                                import os as _os_scan
                                _debrid = _get_debrid()  # May be None in symlink/usenet-only mode
                                new_torrent_id = item.get('filled_by_torrent_id')
                                _scan_paths = set()

                                for old_entry in old_replace_items:
                                    old_id = old_entry['id']
                                    old_torrent_id = old_entry['filled_by_torrent_id']

                                    # Remove old debrid torrent if different from new torrent
                                    if old_torrent_id and old_torrent_id != new_torrent_id and _debrid:
                                        try:
                                            _debrid.remove_torrent(old_torrent_id, removal_reason=_removal_reason)
                                            logging.info(f"[{_log_tag}] Removed old debrid torrent {old_torrent_id} for item {old_id}")
                                        except Exception as _debrid_err:
                                            if '404' in str(_debrid_err):
                                                logging.debug(f"[{_log_tag}] Old torrent {old_torrent_id} already removed (404)")
                                            else:
                                                logging.error(f"[{_log_tag}] Failed to remove old torrent {old_torrent_id}: {_debrid_err}")

                                    # Remove old entry from Plex
                                    _old_path = old_entry['location_on_disk'] or old_entry['filled_by_file']
                                    if _old_path:
                                        _ep_title = old_entry['episode_title'] if _is_episode else None
                                        try:
                                            if not remove_file_from_plex(old_entry['title'] or '', _old_path, _ep_title):
                                                logging.warning(f"[{_log_tag}] Direct Plex removal failed for item {old_id}, will scan+empty trash")
                                            else:
                                                logging.info(f"[{_log_tag}] Removed item {old_id} from Plex")
                                        except Exception as _plex_err:
                                            logging.warning(f"[{_log_tag}] Plex removal error for item {old_id}: {_plex_err}")
                                        _scan_paths.add(_os_scan.path.dirname(_old_path))

                                    # Hard-delete old entry from database
                                    if _remove_item(old_id):
                                        logging.info(f"[{_log_tag}] Deleted old {_entry_label} entry {old_id} after replacement")
                                    else:
                                        logging.warning(f"[{_log_tag}] Failed to delete old {_entry_label} entry {old_id}")

                                # Scan & empty Plex trash for all affected paths
                                if _scan_paths:
                                    try:
                                        scan_and_empty_plex_trash(paths=list(_scan_paths), section_type=_section_type)
                                        logging.info(f"[{_log_tag}] Triggered Plex scan+empty trash for paths: {list(_scan_paths)}")
                                    except Exception as _scan_err:
                                        logging.warning(f"[{_log_tag}] Plex scan+empty trash failed: {_scan_err}")
                        except Exception as _replace_err:
                            logging.error(f"[{_log_tag}] Error in replace hook: {_replace_err}")
                # --- END REPLACE SEASON/MOVIE HOOK ---

                # Add notification for all collections (including previously collected)
                # Check the item's state *before* this function's update.
                previous_state = item.get('state')

                if not item.get('upgrading_from'): # This indicates a regular collection, where new_state is 'Collected'
                    if previous_state != 'Collected':
                        from database.database_writing import add_to_collected_notifications
                        notification_item = item.copy()
                        notification_item.update(update_values)
                        notification_item['is_upgrade'] = False
                        notification_item['new_state'] = "Collected"
                        add_to_collected_notifications(notification_item)
                        logging.info(f"Added collection notification for item: {item_identifier}")
                    else:
                        logging.info(f"Item {item_identifier} was already 'Collected'. Skipping redundant collection notification.")
                # Add notification for upgrades
                elif item.get('upgrading_from'): # This indicates an upgrade, notification_item['new_state'] will be 'Upgraded'
                    # An item is 'Upgraded' from a previous version. Its state before this specific upgrade
                    # operation might have been 'Collected' (old version) or 'Upgrading'.
                    # We send the 'Upgraded' notification if it wasn't already 'Upgraded' to this new version.
                    if previous_state != 'Upgraded': # Check if it was already in the 'Upgraded' state.
                        from database.database_writing import add_to_collected_notifications
                        notification_item = item.copy()
                        notification_item.update(update_values)
                        notification_item['is_upgrade'] = True
                        notification_item['new_state'] = 'Upgraded'
                        add_to_collected_notifications(notification_item)
                        logging.info(f"Added upgrade notification for item: {item_identifier}")
                    else:
                        logging.info(f"Item {item_identifier} was already 'Upgraded'. Skipping redundant upgrade notification.")

                # --- EDIT: Call the callback on success ---
                # Construct the relative path format expected by the rclone queue
                relative_path_to_remove = os.path.join(item.get('filled_by_title', ''), item['filled_by_file'])
                if on_success_callback:
                    try:
                        logging.debug(f"Calling success callback for path: {relative_path_to_remove}")
                        # Call the provided function with the path
                        on_success_callback(relative_path_to_remove)
                    except Exception as cb_err:
                        logging.error(f"Error executing on_success_callback for {relative_path_to_remove}: {cb_err}")
                # --- END EDIT ---

                # --- MULTIFILE SUPPORT FOR SYMLINK MODE ---
                # For both movies and episodes, scan the source folder for additional video files and create
                # separate database entries for each (similar to how Plex mode handles multiple files)
                # This ensures all files from a torrent are properly tracked in the database
                # Skip if skip_multifile_scan is True (prevents redundant folder scans)
                if source_folder and not skip_multifile_scan:
                    try:
                        all_video_files = _find_all_video_files_in_folder(source_folder, source_file)
                        # Filter out the primary file we already processed
                        additional_files = [f for f in all_video_files if f != source_file]

                        if additional_files:
                            item_type = item.get('type', 'movie')
                            logging.info(f"[MultiFile] Found {len(additional_files)} additional video file(s) for {item_type} '{item.get('title')}'")

                            for additional_source_file in additional_files:
                                additional_filename = os.path.basename(additional_source_file)

                                # For movies, only the primary file (already symlinked above) should be
                                # tracked. Skip all additional files — trailers, samples, extras, etc.
                                if item_type != 'episode':
                                    logging.debug(f"[MultiFile] Skipping additional file for movie: {additional_filename}")
                                    continue

                                # For episodes, verify this is the same episode (alternate version), not a different episode
                                if item_type == 'episode':
                                    try:
                                        # Use existing PTT parser to extract season/episode from filename
                                        parsed = parse_with_ptt(additional_filename)
                                        # Try singular first, fallback to plural list (consistent with rclone_processing.py)
                                        # Safely handle empty lists
                                        seasons = parsed.get('seasons') or []
                                        episodes = parsed.get('episodes') or []
                                        parsed_season = parsed.get('season') or (seasons[0] if len(seasons) > 0 else None)
                                        parsed_episode = parsed.get('episode') or (episodes[0] if len(episodes) > 0 else None)

                                        if parsed_season is None or parsed_episode is None:
                                            logging.warning(f"[MultiFile] Could not parse episode info from filename: {additional_filename}. Skipping.")
                                            continue

                                        current_season = item.get('season_number')
                                        current_episode = item.get('episode_number')

                                        # Ensure type consistency for comparison (convert to int)
                                        if parsed_season is not None: parsed_season = int(parsed_season)
                                        if parsed_episode is not None: parsed_episode = int(parsed_episode)
                                        if current_season is not None: current_season = int(current_season)
                                        if current_episode is not None: current_episode = int(current_episode)

                                        # Only process if same episode (alternate version)
                                        if parsed_season != current_season or parsed_episode != current_episode:
                                            logging.debug(f"[MultiFile] Skipping {additional_filename} - S{parsed_season:02d}E{parsed_episode:02d} != S{current_season:02d}E{current_episode:02d} (different episode in season pack)")
                                            continue

                                        logging.info(f"[MultiFile] Processing additional file: {additional_filename} (alternate version of S{current_season:02d}E{current_episode:02d})")

                                    except Exception as parse_err:
                                        logging.error(f"[MultiFile] Error parsing episode info from {additional_filename}: {parse_err}. Skipping.")
                                        continue
                                else:
                                    # For movies, log that we're processing this additional file
                                    logging.info(f"[MultiFile] Processing additional file: {additional_filename}")

                                # Create a copy of the item for this additional file
                                additional_item = item.copy()
                                additional_item['filled_by_file'] = additional_filename

                                # For episodes: Re-parse filename to ensure correct season/episode numbers
                                # This is a safety measure in case the episode check failed or was skipped
                                if item_type == 'episode':
                                    try:
                                        reparsed = parse_with_ptt(additional_filename)
                                        # Safely handle empty lists
                                        seasons = reparsed.get('seasons') or []
                                        episodes = reparsed.get('episodes') or []
                                        reparsed_season = reparsed.get('season') or (seasons[0] if len(seasons) > 0 else None)
                                        reparsed_episode = reparsed.get('episode') or (episodes[0] if len(episodes) > 0 else None)
                                        if reparsed_season is not None:
                                            additional_item['season_number'] = int(reparsed_season)
                                        if reparsed_episode is not None:
                                            additional_item['episode_number'] = int(reparsed_episode)
                                        logging.debug(f"[MultiFile] Updated additional_item episode numbers from filename: S{additional_item.get('season_number')}E{additional_item.get('episode_number')}")
                                    except Exception as reparse_err:
                                        logging.warning(f"[MultiFile] Could not re-parse episode info for {additional_filename}: {reparse_err}")

                                # Generate symlink path for this additional file
                                additional_dest_file = get_symlink_path(additional_item, additional_source_file, skip_jikan_lookup=True)
                                if not additional_dest_file:
                                    logging.warning(f"[MultiFile] Failed to generate symlink path for additional file: {additional_filename}")
                                    continue

                                # Create symlink for the additional file (no verification queue, skip_verification=True)
                                additional_success = create_symlink(additional_source_file, additional_dest_file, media_item_id=None, skip_verification=True)
                                if not additional_success:
                                    logging.warning(f"[MultiFile] Failed to create symlink for additional file: {additional_filename}")
                                    continue

                                # Insert a new database entry for this additional file
                                # Use the same metadata as the primary item but with different file info
                                try:
                                    from database.core import get_db_connection
                                    conn = get_db_connection()
                                    try:
                                        # First, check if there's an existing entry for this movie/episode that's blacklisted or ghostlisted
                                        # This prevents creating duplicate entries when a blacklisted version already exists
                                        if item_type == 'episode':
                                            existing_movie_check = conn.execute('''
                                                SELECT id, state, blacklisted, ghostlisted
                                                FROM media_items
                                                WHERE imdb_id = ? AND type = ? AND version = ?
                                                AND season_number = ? AND episode_number = ?
                                                AND (blacklisted = 1 OR ghostlisted = 1)
                                                LIMIT 1
                                            ''', (item.get('imdb_id'), item_type, item.get('version'),
                                                  item.get('season_number'), item.get('episode_number'))).fetchone()
                                        else:  # movie
                                            existing_movie_check = conn.execute('''
                                                SELECT id, state, blacklisted, ghostlisted
                                                FROM media_items
                                                WHERE imdb_id = ? AND type = ? AND version = ?
                                                AND (blacklisted = 1 OR ghostlisted = 1)
                                                LIMIT 1
                                            ''', (item.get('imdb_id'), item_type, item.get('version'))).fetchone()

                                        if existing_movie_check:
                                            logging.info(f"[MultiFile] Skipping additional file {additional_filename} - found existing blacklisted/ghostlisted entry (ID: {existing_movie_check['id']}, blacklisted: {existing_movie_check['blacklisted']}, ghostlisted: {existing_movie_check['ghostlisted']})")
                                            conn.close()
                                            continue

                                        # Check if this specific file already exists in the database
                                        cursor = conn.execute(
                                            'SELECT id, blacklisted, ghostlisted FROM media_items WHERE filled_by_file = ? AND type = ?',
                                            (additional_filename, item_type)
                                        )
                                        existing = cursor.fetchone()
                                        cursor.close()

                                        if existing:
                                            # Check if the existing entry is blacklisted or ghostlisted
                                            if existing['blacklisted'] == 1 or existing['ghostlisted'] == 1:
                                                logging.info(f"[MultiFile] Skipping update for {additional_filename} - existing entry (ID: {existing['id']}) is blacklisted/ghostlisted")
                                                conn.close()
                                                continue

                                            logging.debug(f"[MultiFile] File {additional_filename} already exists in DB (ID: {existing['id']}), updating location")
                                            conn.execute('''
                                                UPDATE media_items
                                                SET location_on_disk = ?, original_path_for_symlink = ?, last_updated = ?
                                                WHERE id = ?
                                            ''', (additional_dest_file, additional_source_file, current_time, existing['id']))
                                        else:
                                            # Insert new entry with same identifiers but different file
                                            # Handle both movies and episodes
                                            if item_type == 'episode':
                                                conn.execute('''
                                                    INSERT INTO media_items (
                                                        imdb_id, tmdb_id, title, year, release_date, state, type,
                                                        season_number, episode_number, episode_title,
                                                        last_updated, version, collected_at, original_collected_at,
                                                        genres, filled_by_file, filled_by_title, filled_by_magnet,
                                                        filled_by_torrent_id, location_on_disk, original_path_for_symlink,
                                                        resolution, content_source, airtime
                                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                                ''', (
                                                    item.get('imdb_id'),
                                                    item.get('tmdb_id'),
                                                    item.get('title'),
                                                    item.get('year'),
                                                    item.get('release_date'),
                                                    new_state,
                                                    'episode',
                                                    additional_item.get('season_number'),
                                                    additional_item.get('episode_number'),
                                                    additional_item.get('episode_title'),
                                                    current_time,
                                                    item.get('version'),
                                                    current_time,
                                                    current_time,
                                                    item.get('genres'),
                                                    additional_filename,
                                                    item.get('filled_by_title'),
                                                    item.get('filled_by_magnet'),
                                                    item.get('filled_by_torrent_id'),
                                                    additional_dest_file,
                                                    additional_source_file,
                                                    item.get('resolution'),
                                                    item.get('content_source'),
                                                    item.get('airtime')
                                                ))
                                            else:  # movie
                                                conn.execute('''
                                                    INSERT INTO media_items (
                                                        imdb_id, tmdb_id, title, year, release_date, state, type,
                                                        last_updated, version, collected_at, original_collected_at,
                                                        genres, filled_by_file, filled_by_title, filled_by_magnet,
                                                        filled_by_torrent_id, location_on_disk, original_path_for_symlink,
                                                        resolution, content_source
                                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                                ''', (
                                                    item.get('imdb_id'),
                                                    item.get('tmdb_id'),
                                                    item.get('title'),
                                                    item.get('year'),
                                                    item.get('release_date'),
                                                    new_state,
                                                    'movie',
                                                    current_time,
                                                    item.get('version'),
                                                    current_time,
                                                    current_time,
                                                    item.get('genres'),
                                                    additional_filename,
                                                    item.get('filled_by_title'),
                                                    item.get('filled_by_magnet'),
                                                    item.get('filled_by_torrent_id'),
                                                    additional_dest_file,
                                                    additional_source_file,
                                                    item.get('resolution'),
                                                    item.get('content_source')
                                                ))
                                            logging.info(f"[MultiFile] Created new DB entry for additional file: {additional_filename}")

                                        conn.commit()
                                    except Exception as db_err:
                                        logging.error(f"[MultiFile] Database error for {additional_filename}: {db_err}")
                                        conn.rollback()
                                    finally:
                                        conn.close()
                                except Exception as e:
                                    logging.error(f"[MultiFile] Error creating DB entry for {additional_filename}: {e}")

                    except Exception as multifile_err:
                        logging.error(f"[MultiFile] Error processing additional files: {multifile_err}")
                # --- END MULTIFILE SUPPORT FOR SYMLINK MODE ---

                logging.debug(f"check_local_file_for_item succeeded.")
                return True
            else:
                 # This path should ideally not be reached if success is False above, but included for safety
                 logging.error("Reached end of check_local_file_for_item attempt without success.")
                 return False

        except Exception as e:
            if is_webhook and attempt < max_retries - 1:
                logging.warning(f"[UPGRADE] Attempt {attempt + 1}/{max_retries} failed: {str(e)}. Retrying in {retry_delay} second...")
                time.sleep(retry_delay)
                continue
            logging.error(f"[UPGRADE] Error checking local file for item: {str(e)}")
            return False
    
    return False

def local_library_scan(items: List[Dict[str, Any]], extract_resolution: bool = True) -> Dict[str, Dict[str, Any]]:
    """
    Scan local library for specific items' files when Symlinked/Local is enabled.
    This is used as an alternative to Plex scanning when working with symlinked files.

    Extracts from filesystem:
    - size_gb: File size in GB (always)
    - resolution: Video resolution like "2160p", "1080p" (optional)
    - location: File path

    Args:
        items: List of items to scan for (from database)
        extract_resolution: Whether to extract resolution (default: True)

    Returns:
        Dict mapping item IDs to their found file information with size_gb and resolution
    """
    results = {}
    scanned_count = 0
    size_found_count = 0
    resolution_found_count = 0

    import time
    scan_start_time = time.time()

    logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] ========== STARTING SCAN ==========")
    logging.info(f"[LOCAL_LIBRARY_SCAN] Starting scan of {len(items)} items from database")
    if extract_resolution:
        logging.info(f"[LOCAL_LIBRARY_SCAN] Resolution extraction: regex (primary) → ffprobe (fallback) → Plex API (per-item fallback)")
        logging.info(f"[LOCAL_LIBRARY_SCAN] Expected coverage: ~99.6% via regex (instant), ~0.4% via ffprobe (1-3 sec/file)")

    for idx, item in enumerate(items):
        # Log progress every 1000 items
        if (idx + 1) % 1000 == 0:
            elapsed = time.time() - scan_start_time
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] Progress: {idx + 1}/{len(items)} items ({rate:.1f} items/sec)")
        item_id = item.get('id')
        if not item_id:
            continue

        # Use _scan_location if available (from path remapping), otherwise location_on_disk
        location = item.get('_scan_location') or item.get('location_on_disk')
        if not location:
            logging.debug(f"[LOCAL_LIBRARY_SCAN] Item {item_id} has no location, skipping")
            continue

        scanned_count += 1

        # Check if file exists and get data
        if os.path.exists(location):
            try:
                # ALWAYS extract size (fast)
                size_bytes = os.path.getsize(location)
                size_gb = round(size_bytes / (1024**3), 2) if size_bytes else None

                if size_gb is not None:
                    item_with_data = item.copy()
                    item_with_data['size_gb'] = size_gb
                    item_with_data['location'] = item.get('location_on_disk') or location
                    size_found_count += 1

                    # OPTIONALLY extract resolution
                    if extract_resolution:
                        resolution = extract_resolution_hybrid(location)
                        if resolution:
                            item_with_data['resolution'] = resolution
                            resolution_found_count += 1
                            logging.debug(f"[LOCAL_LIBRARY_SCAN] Item {item_id}: {size_gb}GB, {resolution}")
                        else:
                            # Leave resolution as-is (don't overwrite with None)
                            logging.debug(f"[LOCAL_LIBRARY_SCAN] Item {item_id}: {size_gb}GB, resolution not detected")
                    else:
                        logging.debug(f"[LOCAL_LIBRARY_SCAN] Item {item_id}: {size_gb}GB")

                    # Remove temporary scan location field - don't write to database
                    item_with_data.pop('_scan_location', None)
                    item_with_data.pop('_remap_failed', None)

                    results[item_id] = item_with_data

            except OSError as e:
                logging.warning(f"[LOCAL_LIBRARY_SCAN] Could not process {location}: {e}")
        else:
            logging.debug(f"[LOCAL_LIBRARY_SCAN] File not found: {location}")

    # Calculate scan statistics
    scan_duration = time.time() - scan_start_time
    items_per_sec = scanned_count / scan_duration if scan_duration > 0 else 0

    logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] ========== SCAN COMPLETE ==========")
    logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] Total scan time: {scan_duration:.2f} seconds")
    logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] Processing rate: {items_per_sec:.1f} items/second")

    # Calculate scan statistics
    scan_duration = time.time() - scan_start_time
    items_per_sec = scanned_count / scan_duration if scan_duration > 0 else 0

    logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] ========== SCAN COMPLETE ==========")
    logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] Total scan time: {scan_duration:.2f} seconds")
    logging.info(f"[LOCAL_LIBRARY_SCAN_DEBUG] Processing rate: {items_per_sec:.1f} items/second")

    if extract_resolution:
        logging.info(f"[LOCAL_LIBRARY_SCAN] Scanned {scanned_count} items, found size for {size_found_count}, resolution for {resolution_found_count}")

        # MediaInfo statistics
        if _MEDIAINFO_AVAILABLE:
            success_rate = (_MEDIAINFO_SUCCESS_COUNT / _MEDIAINFO_CALL_COUNT * 100) if _MEDIAINFO_CALL_COUNT > 0 else 0
            logging.info(f"[MEDIAINFO_DEBUG] ========== MEDIAINFO STATISTICS ==========")
            logging.info(f"[MEDIAINFO_DEBUG] Total MediaInfo calls: {_MEDIAINFO_CALL_COUNT}")
            logging.info(f"[MEDIAINFO_DEBUG] Successful extractions: {_MEDIAINFO_SUCCESS_COUNT} ({success_rate:.1f}%)")
            logging.info(f"[MEDIAINFO_DEBUG] Failed extractions: {_MEDIAINFO_FAIL_COUNT}")

            if _MEDIAINFO_CALL_COUNT > 0:
                avg_time_per_call = (scan_duration / _MEDIAINFO_CALL_COUNT) * 1000  # in ms
                logging.info(f"[MEDIAINFO_DEBUG] Average time per MediaInfo call: {avg_time_per_call:.2f}ms")

            logging.info(f"[MEDIAINFO_DEBUG] ==========================================")
    else:
        logging.info(f"[LOCAL_LIBRARY_SCAN] Scanned {scanned_count} items, found size for {size_found_count}")

    return results

def recent_local_library_scan(items: List[Dict[str, Any]], max_files: int = 500) -> Dict[str, Dict[str, Any]]:
    """
    Perform a recent local library scan for specific items.
    Checks the most recent files to see if they match any of the provided items.
    
    Args:
        items: List of items to scan for
        max_files: Maximum number of recent files to check
        
    Returns:
        Dict mapping item IDs to their found file information
    """
    # Disabled for now
    return {}

def convert_item_to_symlink(item: Dict[str, Any], skip_verification: bool = False) -> Dict[str, Any]:
    """
    Converts a given library item to use a symlink based on configured templates.
    Returns a dictionary with success status, paths, and potential error message.
    """
    item_id = item.get('id')
    original_location = item.get('location_on_disk')

    logging.debug(f"convert_item_to_symlink received item with filename_real_path: {item.get('filename_real_path')}")

    if not item_id or not original_location:
        return {'success': False, 'error': 'Missing item ID or original location', 'item_id': item_id}

    # Check if original_location exists
    if not os.path.exists(original_location):
        logging.warning(f"Original file not found for item {item_id}: {original_location}")
        # Let the calling function decide how to handle (e.g., move to Wanted)
        return {'success': False, 'error': 'Source file not found', 'item_id': item_id, 'old_location': original_location}

    # Determine the filename to use for path generation
    # Prefer filename_real_path if it exists (set during initial scan if symlink was found)
    filename_for_path = item.get('filename_real_path') or os.path.basename(original_location)
    logging.debug(f"Calling get_symlink_path with filename_real_path: {item.get('filename_real_path')}")

    # Generate the new symlink path using the original filename's base name
    new_symlink_path = get_symlink_path(item, filename_for_path, skip_jikan_lookup=skip_verification)

    if not new_symlink_path:
        return {'success': False, 'error': 'Failed to generate new symlink path', 'item_id': item_id}

    # Create the symlink
    # Pass media_item_id for verification queue
    success = create_symlink(original_location, new_symlink_path, media_item_id=item_id, skip_verification=skip_verification)

    if success:
        return {
            'success': True,
            'item_id': item_id,
            'old_location': original_location,
            'new_location': new_symlink_path
        }
    else:
        return {
            'success': False,
            'error': 'Failed to create symlink',
            'item_id': item_id,
            'old_location': original_location,
            'new_location': new_symlink_path # Return path even on failure for logging
        }

def scan_for_broken_symlinks(library_path: str = None) -> Dict[str, Any]:
    """
    Scan the library for broken symlinks.
    
    Args:
        library_path: Optional specific library path to scan. If None, uses default symlinked path from settings.
        
    Returns:
        Dict containing:
            - total_symlinks: Total number of symlinks found
            - broken_symlinks: List of broken symlinks with details
            - broken_count: Number of broken symlinks
    """
    try:
        if not library_path:
            library_path = get_setting('File Management', 'symlinked_files_path')
            
        if not os.path.exists(library_path):
            logging.error(f"Library path does not exist: {library_path}")
            return {
                'total_symlinks': 0,
                'broken_symlinks': [],
                'broken_count': 0,
                'error': 'Library path does not exist'
            }
            
        logging.info(f"Starting symlink scan in: {library_path}")
        total_symlinks = 0
        broken_symlinks = []
        processed_files = 0
        
        # First count total files for progress tracking
        total_files = sum(len(files) for _, _, files in os.walk(library_path))
        logging.info(f"Found {total_files} total files to check")
        
        # Walk through the library
        for root, _, files in os.walk(library_path):
            relative_root = os.path.relpath(root, library_path)
            logging.debug(f"Scanning directory: {relative_root}")
            
            for file in files:
                processed_files += 1
                if processed_files % 100 == 0:  # Log progress every 100 files
                    progress = (processed_files / total_files) * 100
                    logging.info(f"Progress: {progress:.1f}% ({processed_files}/{total_files} files)")
                
                file_path = os.path.join(root, file)
                
                # Check if it's a symlink
                if os.path.islink(file_path):
                    total_symlinks += 1
                    target_path = os.path.realpath(file_path)
                    relative_path = os.path.relpath(file_path, library_path)
                    
                    logging.debug(f"Checking symlink: {relative_path} -> {target_path}")
                    
                    # Check if the target exists
                    if not os.path.exists(target_path):
                        logging.warning(f"Found broken symlink: {relative_path} -> {target_path}")
                        broken_symlinks.append({
                            'symlink_path': file_path,
                            'relative_path': relative_path,
                            'target_path': target_path,
                            'filename': file
                        })
                    else:
                        logging.debug(f"Symlink OK: {relative_path}")
        
        # Calculate health metrics
        health_percentage = ((total_symlinks - len(broken_symlinks)) / total_symlinks * 100) if total_symlinks > 0 else 100
        
        result = {
            'total_symlinks': total_symlinks,
            'broken_symlinks': broken_symlinks,
            'broken_count': len(broken_symlinks),
            'total_files_scanned': processed_files,
            'health_percentage': round(health_percentage, 1)
        }
        
        logging.info(f"Symlink scan complete:")
        logging.info(f"- Total files scanned: {processed_files}")
        logging.info(f"- Total symlinks found: {total_symlinks}")
        logging.info(f"- Broken symlinks found: {len(broken_symlinks)}")
        logging.info(f"- Health score: {health_percentage:.1f}%")
        
        if broken_symlinks:
            logging.info("Broken symlinks summary:")
            for symlink in broken_symlinks:
                logging.info(f"- {symlink['relative_path']} -> {symlink['target_path']}")
        
        return result
        
    except Exception as e:
        logging.error(f"Error scanning for broken symlinks: {str(e)}", exc_info=True)
        return {
            'total_symlinks': 0,
            'broken_symlinks': [],
            'broken_count': 0,
            'total_files_scanned': 0,
            'health_percentage': 0,
            'error': str(e)
        }

def repair_broken_symlink(symlink_path: str, new_target_path: str = None) -> Dict[str, Any]:
    """
    Attempt to repair a broken symlink.
    
    Args:
        symlink_path: Path to the broken symlink
        new_target_path: Optional new target path. If None, will attempt to find the file in original files path
        
    Returns:
        Dict containing:
            - success: Whether the repair was successful
            - message: Description of what was done or why it failed
            - old_target: The previous target path
            - new_target: The new target path (if successful)
    """
    try:
        if not os.path.islink(symlink_path):
            return {
                'success': False,
                'message': 'Path is not a symlink',
                'old_target': None,
                'new_target': None
            }
            
        old_target = os.path.realpath(symlink_path)
        
        # If no new target specified, try to find the file
        if not new_target_path:
            return {
                'success': False,
                'message': 'Could not find original file (automatic search disabled)',
                'old_target': old_target,
                'new_target': None
            }
        
        # Verify new target exists
        if not os.path.exists(new_target_path):
            return {
                'success': False,
                'message': 'New target path does not exist',
                'old_target': old_target,
                'new_target': new_target_path
            }
            
        # Remove old symlink and create new one
        os.unlink(symlink_path)
        os.symlink(new_target_path, symlink_path)
        
        return {
            'success': True,
            'message': 'Symlink repaired successfully',
            'old_target': old_target,
            'new_target': new_target_path
        }
        
    except Exception as e:
        logging.error(f"Error repairing symlink: {str(e)}")
        return {
            'success': False,
            'message': str(e),
            'old_target': old_target if 'old_target' in locals() else None,
            'new_target': new_target_path if 'new_target_path' in locals() else None
        }

# --- Add Helper Function for Source File Searching ---
def _find_source_file_in_base(item: Dict[str, Any], base_search_path: str, filename_only: str) -> Optional[str]:
    """Helper to search for filename_only under base_search_path using common folder structures."""
    if not base_search_path or not filename_only:
        return None

    logging.debug(f"[_find_source_file_in_base] Searching for '{filename_only}' under '{base_search_path}' for item ID {item.get('id')}")

    possible_folder_names = [
        item.get('debrid_folder_name', ''),
        item.get('original_scraped_torrent_title', ''),
        item.get('real_debrid_original_title', ''),
        item.get('filled_by_title', ''),
        None # For checking directly under the base path
    ]

    for folder_name_candidate in possible_folder_names:
        current_search_base = base_search_path
        if folder_name_candidate:
            # Try raw folder name
            potential_path = os.path.join(current_search_base, folder_name_candidate, filename_only)
            if os.path.exists(potential_path):
                found_path = os.path.normpath(potential_path)
                logging.debug(f"[_find_source_file_in_base] Found at raw folder: '{found_path}'")
                return found_path

            # Try trimmed folder name (if different)
            trimmed_folder_name = os.path.splitext(folder_name_candidate)[0]
            if trimmed_folder_name != folder_name_candidate:
                potential_path_trimmed = os.path.join(current_search_base, trimmed_folder_name, filename_only)
                if os.path.exists(potential_path_trimmed):
                    found_path = os.path.normpath(potential_path_trimmed)
                    logging.debug(f"[_find_source_file_in_base] Found at trimmed folder: '{found_path}'")
                    return found_path
        else: # Check directly under base_search_path
            potential_path = os.path.join(current_search_base, filename_only)
            if os.path.exists(potential_path):
                found_path = os.path.normpath(potential_path)
                logging.debug(f"[_find_source_file_in_base] Found directly under base: '{found_path}'")
                return found_path

    logging.debug(f"[_find_source_file_in_base] File '{filename_only}' not found under '{base_search_path}'")
    return None
# --- End Helper Function ---

def resync_symlinks_with_new_settings(
    old_original_files_path_setting: Optional[str] = None,
    new_original_files_path_setting: Optional[str] = None
):
    """
    Resynchronizes all existing symlinks based on the current application settings.
    Attempts to locate source files and align DB with current settings if possible.
    """
    logging.info("Starting symlink resynchronization process.")

    if get_setting('File Management', 'file_collection_management') != "Symlinked/Local":
        logging.info("Symlink resynchronization skipped: File management is not set to Symlinked/Local.")
        return {"status": "skipped", "message": "Not using Symlinked/Local file management."}

    try:
        collected_items = list(get_all_media_items(state='Collected'))
        # Also consider items in 'Upgrading' state if they have symlinks that need checking
        upgrading_items = list(get_all_media_items(state='Upgrading'))
        collected_items.extend(upgrading_items) # Combine lists
        # Remove duplicates if any item somehow ended up in both lists (by ID)
        seen_ids = set()
        unique_items = []
        for item in collected_items:
            if item['id'] not in seen_ids:
                unique_items.append(item)
                seen_ids.add(item['id'])
        collected_items = unique_items

    except Exception as e:
        logging.error(f"Failed to retrieve media items for symlink resync: {e}", exc_info=True)
        return {"status": "error", "message": "Failed to retrieve media items."}

    updated_count = 0
    error_count = 0
    skipped_count = 0
    created_count = 0
    source_path_updated_count = 0
    total_items = len(collected_items)
    current_original_setting = get_setting('File Management', 'original_files_path') # Get current setting once

    logging.info(f"Found {total_items} collected/upgrading items to check for symlink resynchronization.")
    if old_original_files_path_setting and new_original_files_path_setting and \
       old_original_files_path_setting != new_original_files_path_setting:
        logging.info(f"Explicit migration requested. Old: '{old_original_files_path_setting}', New: '{new_original_files_path_setting}'.")
    else:
        logging.info(f"Performing standard resync. Current original_files_path setting: '{current_original_setting}'.")
        if old_original_files_path_setting == new_original_files_path_setting:
             old_original_files_path_setting = None
             new_original_files_path_setting = None

    for i, item in enumerate(collected_items):
        item_id = item.get('id')
        item_title_log = item.get('title', 'Unknown Title')
        db_symlink_location = item.get('location_on_disk')
        db_source_path = item.get('original_path_for_symlink')
        filename_only = item.get('filled_by_file')

        if (i + 1) % 25 == 0 or (i + 1) == total_items:
            logging.info(f"Symlink resync progress: {i + 1}/{total_items} items. Updated Symlinks: {updated_count}, Created Symlinks: {created_count}, Source Paths Updated: {source_path_updated_count}, Errors: {error_count}, Skipped: {skipped_count}.")

        if not item_id or not filename_only:
            logging.warning(f"Skipping item (ID: {item_id if item_id else 'Unknown'}) due to missing ID or filename ('{filename_only}').")
            skipped_count += 1
            continue

        actual_source_file_to_use = None
        source_path_was_updated = False

        # --- Determine the correct source file path ---
        if old_original_files_path_setting and new_original_files_path_setting:
            # --- Explicit Migration Logic (using old/new params) ---
            logging.debug(f"Item ID {item_id} ('{item_title_log}'): Running explicit migration logic.")
            source_path_migrated_or_found = False
            if db_source_path and db_source_path.startswith(old_original_files_path_setting):
                try:
                    relative_part = os.path.relpath(db_source_path, old_original_files_path_setting)
                    potential_new_abs_path = os.path.join(new_original_files_path_setting, relative_part)
                    potential_new_abs_path = os.path.normpath(potential_new_abs_path)
                    if os.path.exists(potential_new_abs_path):
                        actual_source_file_to_use = potential_new_abs_path
                        source_path_migrated_or_found = True
                except ValueError: pass # Handle different drives case
            
            if not source_path_migrated_or_found:
                found_path = _find_source_file_in_base(item, new_original_files_path_setting, filename_only)
                if found_path:
                    actual_source_file_to_use = found_path
                    source_path_migrated_or_found = True
            
            if source_path_migrated_or_found and actual_source_file_to_use != db_source_path:
                try:
                    update_media_item(item_id, original_path_for_symlink=actual_source_file_to_use)
                    source_path_updated_count += 1
                    source_path_was_updated = True
                except Exception as db_update_e: error_count += 1; continue
            elif not source_path_migrated_or_found:
                 if db_source_path and os.path.exists(db_source_path): actual_source_file_to_use = db_source_path
                 else: skipped_count += 1; continue
            # If migration was successful, actual_source_file_to_use is set. If not, and DB path is also bad, we skip.
            if not actual_source_file_to_use and source_path_migrated_or_found: # Should not happen if logic is correct
                 actual_source_file_to_use = db_source_path # Fallback, though migration implies it was found

        else:
            # --- Automatic Source Path Logic ---
            logging.debug(f"Item ID {item_id} ('{item_title_log}'): Running automatic source path logic. DB Path: '{db_source_path}', Current Setting: '{current_original_setting}'.")
            
            db_path_valid_and_exists = db_source_path and os.path.exists(db_source_path)
            
            if db_path_valid_and_exists:
                # DB path is valid. Now check if it aligns with current setting.
                # Normalize both for comparison to avoid issues with trailing slashes etc.
                norm_db_source_path = os.path.normpath(db_source_path)
                norm_current_original_setting = os.path.normpath(current_original_setting)

                if norm_db_source_path.startswith(norm_current_original_setting):
                    # DB path is valid AND aligns with current setting. Use it.
                    logging.debug(f"Item ID {item_id}: DB source path '{db_source_path}' is valid and aligns with current setting. Using it.")
                    actual_source_file_to_use = db_source_path
                else:
                    # DB path is valid BUT does NOT align with current setting.
                    # Check if file ALSO exists under the current setting.
                    logging.info(f"Item ID {item_id}: DB source path '{db_source_path}' is valid but does not align with current setting '{current_original_setting}'. Checking current setting path.")
                    found_at_current_setting = _find_source_file_in_base(item, current_original_setting, filename_only)
                    
                    if found_at_current_setting:
                        # File found at current setting's path. Prefer this and update DB.
                        logging.info(f"Item ID {item_id}: File also found at '{found_at_current_setting}' (under current setting). Preferring this and updating DB.")
                        actual_source_file_to_use = found_at_current_setting
                        try:
                            update_media_item(item_id, original_path_for_symlink=actual_source_file_to_use)
                            source_path_updated_count += 1
                            source_path_was_updated = True
                        except Exception as db_update_e:
                            logging.error(f"Item ID {item_id}: Failed to update DB to '{actual_source_file_to_use}': {db_update_e}")
                            error_count += 1
                            actual_source_file_to_use = db_source_path # Fallback to original valid path if DB update fails
                    else:
                        # File NOT found under current setting path. Stick with the valid (but misaligned) DB path.
                        logging.warning(f"Item ID {item_id}: File not found under current setting path. Using existing valid DB path '{db_source_path}' despite misalignment with setting.")
                        actual_source_file_to_use = db_source_path
            else:
                # DB path is invalid or non-existent. Search under current setting.
                logging.warning(f"Item ID {item_id}: DB source path '{db_source_path}' is invalid or file missing. Searching under current setting '{current_original_setting}'.")
                found_at_current_setting = _find_source_file_in_base(item, current_original_setting, filename_only)
                if found_at_current_setting:
                    actual_source_file_to_use = found_at_current_setting
                    logging.info(f"Item ID {item_id}: Source file found via automatic search: '{actual_source_file_to_use}'")
                    try:
                        update_media_item(item_id, original_path_for_symlink=actual_source_file_to_use)
                        source_path_updated_count += 1
                        source_path_was_updated = True
                    except Exception as db_update_e:
                        logging.error(f"Item ID {item_id}: Failed to update DB to '{actual_source_file_to_use}': {db_update_e}")
                        error_count += 1; continue # Skip if DB update fails here
                else:
                    logging.error(f"Item ID {item_id} ('{item_title_log}'): Source file '{filename_only}' NOT found after checking DB path and searching under current setting '{current_original_setting}'. Skipping.")
                    skipped_count += 1
                    continue
        # --- End of Source File Path Determination ---

        if not actual_source_file_to_use:
             logging.error(f"Item ID {item_id}: Logic error - actual_source_file_to_use not determined. Skipping.")
             skipped_count += 1
             continue

        try:
            new_symlink_destination = get_symlink_path(item, actual_source_file_to_use, skip_jikan_lookup=True)
        except Exception as e:
            logging.error(f"Error generating new symlink destination for item ID {item_id} ('{item_title_log}'): {e}", exc_info=True)
            error_count += 1; continue

        if not new_symlink_destination:
            logging.error(f"Failed to generate new symlink destination for item ID {item_id} ('{item_title_log}'). Skipping.")
            error_count += 1; continue
        
        # --- Symlink creation/update logic (remains mostly the same as previous version)
        try:
            norm_db_symlink = os.path.normpath(db_symlink_location) if db_symlink_location else None
            norm_new_symlink = os.path.normpath(new_symlink_destination)

            if norm_db_symlink != norm_new_symlink:
                old_parent = os.path.dirname(db_symlink_location) if db_symlink_location else None
                new_parent = os.path.dirname(new_symlink_destination)

                if db_symlink_location and os.path.lexists(db_symlink_location):
                    if os.path.islink(db_symlink_location): os.unlink(db_symlink_location)

                # Move any subtitle sidecar files from old folder to new folder
                subtitle_extensions = {'.srt', '.ass', '.sub', '.ssa', '.vtt', '.idx', '.sup'}
                if old_parent and old_parent != new_parent and os.path.exists(old_parent):
                    try:
                        os.makedirs(new_parent, exist_ok=True)
                        for fname in os.listdir(old_parent):
                            if os.path.splitext(fname)[1].lower() in subtitle_extensions:
                                src = os.path.join(old_parent, fname)
                                dst = os.path.join(new_parent, fname)
                                shutil.move(src, dst)
                                logging.info(f"[RESYNC] Moved subtitle sidecar: {src} -> {dst}")
                    except Exception as e:
                        logging.warning(f"[RESYNC] Error moving subtitle files from {old_parent} to {new_parent}: {e}")

                symlink_created = create_symlink(actual_source_file_to_use, new_symlink_destination, item_id, skip_verification=True)
                if symlink_created:
                    try:
                        update_media_item(item_id, location_on_disk=new_symlink_destination)
                        if db_symlink_location: updated_count +=1
                        else: created_count +=1 
                    except Exception as db_update_e: error_count +=1
                else: error_count += 1
            elif norm_db_symlink == norm_new_symlink: # Path is same, verify integrity
                needs_recreate = False
                if not os.path.lexists(db_symlink_location): needs_recreate = True
                elif not os.path.islink(db_symlink_location): needs_recreate = True
                elif os.path.realpath(db_symlink_location) != os.path.realpath(actual_source_file_to_use): needs_recreate = True
                
                if needs_recreate:
                    symlink_recreated = create_symlink(actual_source_file_to_use, new_symlink_destination, item_id, skip_verification=True)
                    if symlink_recreated: updated_count += 1
                    else: error_count += 1
        except Exception as e:
            logging.error(f"Unhandled error processing symlink for item ID {item_id} (New Dest: '{new_symlink_destination}'): {e}", exc_info=True)
            error_count += 1
    # --- End Item Loop ---

    # --- Prune empty folders ---
    symlink_base_path = get_setting('File Management', 'symlinked_files_path')
    if symlink_base_path and os.path.exists(symlink_base_path):
        logging.info(f"Starting pruning of empty directories in {symlink_base_path}")
        pruned_count = 0
        # Walk the directory tree from bottom up
        for root, dirs, files in os.walk(symlink_base_path, topdown=False):
            for name in dirs:
                dir_path = os.path.join(root, name)
                try:
                    if not os.listdir(dir_path):  # Check if directory is empty
                        os.rmdir(dir_path)
                        logging.info(f"Pruned empty directory: {dir_path}")
                        pruned_count += 1
                except OSError as e:
                    logging.warning(f"Could not prune directory {dir_path}: {e}")
        logging.info(f"Finished pruning. Removed {pruned_count} empty directories.")
    # --- End Prune empty folders ---

    logging.info(f"Symlink resynchronization finished. Total: {total_items}, Symlinks Updated: {updated_count}, Symlinks Created: {created_count}, Source Paths Updated in DB: {source_path_updated_count}, Errors: {error_count}, Skipped: {skipped_count}.\n") # Added newline for better log readability
    return {
        "status": "completed",
        "total_items": total_items,
        "symlinks_updated_count": updated_count,
        "symlinks_created_count": created_count,
        "source_paths_db_updated_count": source_path_updated_count,
        "error_count": error_count,
        "skipped_count": skipped_count
    }
