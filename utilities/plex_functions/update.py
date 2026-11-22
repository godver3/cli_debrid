"""Trigger Plex scans for an item and build title variations."""

from utilities.settings import get_setting
import time
from typing import Dict, Any
import os
from plexapi.server import PlexServer
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from utilities import plex_health
from ._common import logger
from .collected import process_library_names
from .scan_cleanup import plex_connection_settings
from .sections_scan import PLEX_MODE_SCAN_COOLDOWN, _mirrored_library_path, claim_plex_mode_scan

def plex_update_item(item: Dict[str, Any]) -> bool:
    # Get display name for logging (title if available, otherwise path)
    item_display = item.get('title')
    if not item_display:
        item_path = item.get('full_path') or item.get('location_on_disk') or item.get('location')
        item_display = f"[batch scan: {item_path}]" if item_path else "Unknown"

    logger.info(f"Attempting to trigger media server scan for item: {item_display}")
    try:
        # Silo picked as the symlink media server: it scans, not Jellyfin or Plex
        from utilities.silo_functions import is_silo_mode, silo_update_item
        if is_silo_mode():
            return silo_update_item(item)

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
        
        # Proceed with Plex update (original logic). Plex mode scans with Plex.url/token:
        # reading only the symlink fields left Plex-mode configs without them never scanning.
        plex_url, plex_token = plex_connection_settings()

        if not plex_url or not plex_token:
            logger.warning("Plex URL or token not configured; can't ask Plex to scan.")
            return False
            
        # Initialise Plex client (timeout on connection itself)
        try:
            plex = PlexServer(plex_url, plex_token, timeout=30)
        except Exception as conn_err:
            logger.error(f"Could not connect to Plex server at '{plex_url}': {conn_err}")
            plex_health.record_contact_failure()
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
        if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            time.sleep(0.5)  # 500ms delay
            logger.debug("Added 500ms delay for symlink mode to allow filesystem to settle")

        # Obtain timeout from settings (seconds). Fallback to 60 s.
        try:
            plex_update_timeout = int(get_setting('File Management', 'plex_section_update_timeout', 1))
        except (TypeError, ValueError):
            plex_update_timeout = 60
            logger.warning(f"Invalid plex_section_update_timeout value; defaulting to {plex_update_timeout}s")

        # Set when some library section was tried, so a caller can tell "no
        # library holds this folder" (a retry can't help) from a failed scan.
        _tried_a_section = []

        def _update_section_with_timeout(sec, dir_path) -> bool:
            """Run `sec.update(path=dir_path)` in a background thread with a hard timeout."""
            _tried_a_section.append(True)
            section_name = getattr(sec, 'title', 'Unknown')
            if not claim_plex_mode_scan(dir_path):
                logger.debug(f"Skipping Plex section.update for '{section_name}' on '{dir_path}' — scanned under {PLEX_MODE_SCAN_COOLDOWN}s ago")
                return True
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

        # Fallback: the file sits in the mount's all-folder (e.g. /data2/__all__/<release>)
        # while the Plex library points at a sibling category folder that mirrors it
        # (/data2/shows/<release>), the layout the zurg/cli_mount docs recommend for
        # Plex mode. Scan the mirrored path in that library, but only if it exists.
        if not found_matching_section and item_type:
            for section in plex.library.sections():
                try:
                    if item_type == 'movie' and section.type != 'movie':
                        continue
                    if item_type == 'episode' and section.type != 'show':
                        continue
                    if allowed_library_keys is not None and str(section.key) not in allowed_library_keys:
                        continue
                    mirrored = _mirrored_library_path(directory, getattr(section, 'locations', None) or [])
                    if mirrored:
                        logger.info(f"Directory '{directory}' is mirrored in section '{section.title}' as '{mirrored}', scanning that")
                        if _update_section_with_timeout(section, mirrored):
                            return True
                except Exception as e:
                    logger.debug(f"Error trying mirrored path in section '{getattr(section, 'title', '?')}': {e}")

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
            if not _tried_a_section:
                item['_plex_scan_outcome'] = 'no_library'
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
