import logging
import os
import time
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
# Removed asyncio and aiohttp as the new functions are sync wrappers
# Removed plexapi imports as connection happens within plex_functions
# from plexapi.server import PlexServer
# from plexapi.exceptions import NotFound

from utilities.settings import get_setting
from database.symlink_verification import (
    get_unverified_files,
    mark_file_as_verified,
    update_verification_attempt,
    get_verification_stats,
    mark_file_as_permanently_failed,
    mark_verification_as_max_attempts_failed
)
# Import the new functions
from utilities.plex_functions import (
    plex_update_item,
    sync_run_get_collected_from_plex,
    sync_run_get_recent_from_plex
)
# Removed original plex_update_item as it's now imported

from database.database_reading import get_media_item_by_id # Moved import here

logger = logging.getLogger(__name__)

# Removed the entire get_plex_library_contents function (approx. lines 17-212 of original)
# ... existing code ...

# Removed async keyword
def verify_media_file(file_data: Dict[str, Any], media_library: Dict[str, List[Dict[str, Any]]]) -> bool:
    """
    Verify if a file has been properly scanned into the media server by comparing with library contents.
    Works with both Plex and Jellyfin/Emby library formats.

    Args:
        file_data: Dictionary containing file information
        media_library: Dictionary containing media server library contents

    Returns:
        bool: True if file is verified in media server, False otherwise
    """
    try:
        # First check if file exists
        if not os.path.exists(file_data['full_path']):
            # Log changed from error to warning, as non-existence is handled later
            logger.warning(f"File does not exist on disk (checked early): {file_data['full_path']}")
            # Keep returning False, but the main logic handles this later as well
            return False

        # Check if it's a symlink and if it's valid
        if os.path.islink(file_data['full_path']):
            target_path = os.path.realpath(file_data['full_path'])
            if not os.path.exists(target_path):
                # Log changed from error to warning, handled later
                logger.warning(f"Symlink target does not exist (checked early): {target_path} for symlink: {file_data['full_path']}")
                # Keep returning False
                return False
            logger.debug(f"Symlink is valid. Target: {target_path}")

        # Extract the filename without extension for comparison
        db_filename = os.path.basename(file_data['full_path'])
        db_filename_no_ext = os.path.splitext(db_filename)[0]

        logger.debug(f"Verifying file in media server: {db_filename}")

        # Check if it's a movie or TV show
        if file_data['type'] == 'movie':
            # Try quick index lookup first
            movies_index = media_library.get('movies_index')
            if movies_index:
                indexed_matches = movies_index.get(db_filename_no_ext.lower()) or []
                for media_basename in indexed_matches:
                    if media_basename == db_filename:
                        logger.info(f"Verified movie in media server (indexed): {file_data['title']} - {db_filename}")
                        return True

            # Fall back to full scan (legacy path, still logs size)
            logger.debug(f"Scanning {len(media_library.get('movies', []))} movies in media server (fallback)")

            # Search for the movie in media server library
            for movie in media_library.get('movies', []):
                # Use 'location' field from the media server functions
                media_location = movie.get('location')
                if not media_location:
                    # logger.debug(f"Skipping movie entry with missing location: {movie.get('title')}")
                    continue # Skip if location is missing

                media_filename = os.path.basename(media_location)
                media_filename_no_ext = os.path.splitext(media_filename)[0]

                # Compare filenames (case insensitive)
                if media_filename_no_ext.lower() == db_filename_no_ext.lower():
                    # Ensure file paths match exactly after resolving basename
                    if media_filename == db_filename:
                         logger.info(f"Verified movie in media server: {file_data['title']} - {db_filename}")
                         return True
                    else:
                         logger.debug(f"Filename base match, but full name differs: DB='{db_filename}', Media='{media_filename}' for title '{file_data['title']}'")


            logger.warning(f"Movie not found in media server after {file_data.get('verification_attempts', 0)} attempts: {file_data['title']} - {db_filename}")

        else:  # TV show
            # Determine episode identifiers early so they are available for logging
            season_num = file_data.get('season_number', 'unknown')
            episode_num = file_data.get('episode_number', 'unknown')
            episode_info = f"S{season_num}E{episode_num}"

            # Quick index lookup for episodes
            episodes_index = media_library.get('episodes_index')
            if episodes_index:
                indexed_matches = episodes_index.get(db_filename_no_ext.lower()) or []
                for media_basename in indexed_matches:
                    if media_basename == db_filename:
                        logger.info(f"Verified episode in media server (indexed): {file_data['title']} {episode_info} - {file_data.get('episode_title', 'unknown')} - {db_filename}")
                        return True

            # Fallback to slow scan
            logger.debug(f"Scanning {len(media_library.get('episodes', []))} episodes in media server (fallback)")

            # Search for the episode in media server library
            for episode in media_library.get('episodes', []):
                 # Use 'location' field from the media server functions
                media_location = episode.get('location')
                if not media_location:
                    # logger.debug(f"Skipping episode entry with missing location: {episode.get('title')} S{episode.get('season_number')}E{episode.get('episode_number')}")
                    continue # Skip if location is missing

                media_filename = os.path.basename(media_location)
                media_filename_no_ext = os.path.splitext(media_filename)[0]

                # Compare filenames (case insensitive)
                if media_filename_no_ext.lower() == db_filename_no_ext.lower():
                    # Ensure file paths match exactly after resolving basename
                    if media_filename == db_filename:
                        logger.info(f"Verified episode in media server: {file_data['title']} {episode_info} - {file_data.get('episode_title', 'unknown')} - {db_filename}")
                        return True
                    else:
                         logger.debug(f"Filename base match, but full name differs: DB='{db_filename}', Media='{media_filename}' for title '{file_data['title']} {episode_info}'")


            logger.warning(f"Episode not found in media server after {file_data.get('verification_attempts', 0)} attempts: {file_data['title']} {episode_info} - {file_data.get('episode_title', 'unknown')} - {db_filename}")

        # If verification failed, log file permissions and ownership (only if file exists)
        if os.path.exists(file_data['full_path']):
            try:
                stat_info = os.stat(file_data['full_path'])
                logger.debug(f"File permissions (for failed verification): {oct(stat_info.st_mode)}, Owner: {stat_info.st_uid}, Group: {stat_info.st_gid}")
            except Exception as e:
                logger.error(f"Error getting file stats during failed verification: {str(e)}")
        else:
            logger.debug(f"File did not exist during permission check (failed verification): {file_data['full_path']}")


        return False

    except Exception as e:
        logger.error(f"Error verifying file in media server: {str(e)}", exc_info=True)
        return False

def run_media_verification_scan(max_files: int = 500, recent_only: bool = False, max_attempts: int = 10) -> Tuple[int, int]:
    """
    Run a verification scan to check if symlinked files are in the configured media server (Plex or Jellyfin/Emby).
    This function ALWAYS scans all movie/show libraries for verification purposes.

    Args:
        max_files: Maximum number of files to check in one run
        recent_only: If True, use the recent scan method across all libraries (Plex only)
        max_attempts: Maximum number of verification attempts before marking as failed

    Returns:
        Tuple of (verified_count, total_processed)
    """
    # Check for Jellyfin/Emby configuration first
    jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
    jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()
    
    if jellyfin_url and jellyfin_token:
        logger.info("Jellyfin/Emby configuration detected, using Jellyfin/Emby for verification")
        return _run_jellyfin_verification_scan(max_files, max_attempts)
    
    # Fall back to Plex verification
    logger.info("Using Plex for verification")
    return _run_plex_verification_scan(max_files, recent_only, max_attempts)

def _run_jellyfin_verification_scan(max_files: int, max_attempts: int) -> Tuple[int, int]:
    """
    Run verification scan using Jellyfin/Emby.
    
    Args:
        max_files: Maximum number of files to check in one run
        max_attempts: Maximum number of verification attempts before marking as failed
        
    Returns:
        Tuple of (verified_count, total_processed)
    """
    # Get unverified files
    from database.symlink_verification import get_unverified_files
    unverified_files = get_unverified_files(limit=max_files)
    
    if not unverified_files:
        logger.info("No unverified files to process in Jellyfin/Emby scan")
        return (0, 0)

    logger.info(f"Processing {len(unverified_files)} unverified files in Jellyfin/Emby scan")

    # Get Jellyfin/Emby library contents
    media_library: Optional[Dict[str, Any]] = None
    try:
        from utilities.emby_functions import get_collected_from_emby
        logger.info("Calling get_collected_from_emby(bypass=True)...")
        media_library = get_collected_from_emby(bypass=True)

        if media_library is None:
            logger.error("Jellyfin/Emby scan failed to return data. Aborting verification run.")
            return (0, 0)
        elif not media_library.get('movies') and not media_library.get('episodes'):
            logger.warning("Jellyfin/Emby scan returned no movies or episodes. Proceeding with verification check against empty library.")

        # Build quick-lookup indexes so each verify call is O(1)
        try:
            movies_index = {}
            for m in media_library.get('movies', []):
                loc = m.get('location')
                if loc:
                    key = os.path.splitext(os.path.basename(loc))[0].lower()
                    movies_index.setdefault(key, []).append(os.path.basename(loc))

            episodes_index = {}
            for e in media_library.get('episodes', []):
                loc = e.get('location')
                if loc:
                    key = os.path.splitext(os.path.basename(loc))[0].lower()
                    episodes_index.setdefault(key, []).append(os.path.basename(loc))

            # Store in media_library so verify_media_file can reuse without changing its signature
            media_library['movies_index'] = movies_index
            media_library['episodes_index'] = episodes_index
            logger.debug(f"Built Jellyfin/Emby library quick lookup indexes: movies={len(movies_index)}, episodes={len(episodes_index)}")
        except Exception as idx_err:
            logger.error(f"Error building Jellyfin/Emby library lookup indexes: {idx_err}")

    except Exception as e:
        logger.error(f"Unexpected error during Jellyfin/Emby scan: {e}", exc_info=True)
        return (0, 0)

    verified_count = 0
    total_processed = 0

    for file_data in unverified_files:
        total_processed += 1
        verification_id = file_data['verification_id']

        # --- Start: Consistency checks ---
        media_item_id = file_data.get('media_item_id')
        if not media_item_id:
            logger.error(f"Verification record {verification_id} missing media_item_id. Marking as failed.")
            mark_file_as_permanently_failed(verification_id, "Missing media_item_id in verification record")
            continue

        media_item = get_media_item_by_id(media_item_id)
        if not media_item:
            logger.warning(f"Media item ID {media_item_id} (Verification ID {verification_id}) not found in media_items. Marking verification as failed.")
            mark_file_as_permanently_failed(verification_id, "Associated media item record not found in database")
            continue

        verification_path = file_data.get('full_path')
        db_path = media_item.get('location_on_disk')

        if not verification_path:
             logger.error(f"Verification record {verification_id} (Media ID: {media_item_id}) missing 'full_path'. Marking as failed.")
             mark_file_as_permanently_failed(verification_id, "Missing full_path in verification record")
             continue

        # Compare paths for staleness
        if verification_path != db_path:
            logger.warning(f"Path mismatch for Media ID {media_item_id} (Verification ID {verification_id}). Verification path: '{verification_path}', DB path: '{db_path}'. Marking verification record as failed (stale).")
            base_verification_path = os.path.basename(verification_path)
            base_db_path = os.path.basename(db_path or '')
            mark_file_as_permanently_failed(verification_id, f"Path mismatch: Verification record path ('{base_verification_path}') differs from current DB path ('{base_db_path}')")
            continue
        # --- End: Consistency checks ---

        logger.debug(f"Media item {media_item_id} found and path '{verification_path}' matches DB. Proceeding with verification for ID {verification_id}.")

        # --- Max attempts check ---
        if file_data.get('verification_attempts', 0) >= max_attempts:
            failure_reason = f"Exceeded maximum verification attempts ({file_data.get('verification_attempts', 0)} >= {max_attempts})"
            logger.warning(f"File {file_data['full_path']} (Media ID: {media_item_id}, Verification ID: {verification_id}) has reached max verification attempts. Marking as failed in verification queue only. Reason: {failure_reason}")
            mark_verification_as_max_attempts_failed(verification_id, failure_reason)
            continue

        # Check if the file exists before calling verify_media_file
        if not os.path.exists(file_data['full_path']):
            logger.warning(f"File does not exist: {file_data['full_path']} (Attempt {file_data.get('verification_attempts', 0) + 1}). Incrementing attempt count.")
            update_verification_attempt(verification_id)
            continue

        # Verify the file in Jellyfin/Emby
        is_verified = verify_media_file(file_data, media_library)

        if is_verified:
            # Mark as verified
            if mark_file_as_verified(verification_id):
                verified_count += 1
                logger.info(f"Successfully marked verification ID {verification_id} as verified for '{os.path.basename(file_data['full_path'])}'")
            else:
                logger.error(f"Failed to mark verification ID {verification_id} as verified in DB for '{os.path.basename(file_data['full_path'])}'")
        else:
            # Update attempt count
            _ = update_verification_attempt(verification_id)
            current_attempts = file_data.get('verification_attempts', 0) + 1
            logger.warning(f"Verification failed for ID {verification_id} ('{os.path.basename(file_data['full_path'])}'). Attempt count updated to {current_attempts}.")

            # Try to trigger a Jellyfin/Emby library update for the item's directory if verification failed
            try:
                file_type = file_data.get('type', 'unknown')
                base_filename = os.path.basename(file_data['full_path'])

                if file_type == 'movie':
                    logger.info(
                        f"Attempting Jellyfin/Emby directory scan for failed movie verification: {file_data['title']} - {base_filename}"
                    )
                else:  # TV show
                    season_num = file_data.get('season_number', 'unknown')
                    episode_num = file_data.get('episode_number', 'unknown')
                    logger.info(
                        f"Attempting Jellyfin/Emby directory scan for failed episode verification: {file_data['title']} - S{season_num}E{episode_num} - {file_data.get('episode_title', 'unknown')} - {base_filename}"
                    )

                # Prepare item data for emby_update_item
                item_for_update = {'full_path': file_data['full_path'], 'title': file_data['title']}

                # Attempt the Jellyfin/Emby update
                from utilities.emby_functions import emby_update_item
                update_result = emby_update_item(item_for_update)
                if update_result:
                    logger.info(f"Successfully triggered Jellyfin/Emby directory scan potentially including: {base_filename}")
                else:
                    logger.warning(f"Jellyfin/Emby directory scan trigger failed or returned False for directory containing: {base_filename}")
            except Exception as e:
                logger.error(f"Error triggering Jellyfin/Emby update after failed verification: {str(e)}", exc_info=True)

    # Log stats
    try:
        stats = get_verification_stats()
        logger.info(f"Jellyfin/Emby verification scan completed. Checked: {total_processed}, Newly Verified: {verified_count}. "
                    f"Overall Stats: Verified={stats['verified']}, Unverified={stats['unverified']}, Failed={stats['permanently_failed']}, Total={stats['total']}. "
                    f"Percent Verified: {stats['percent_verified']:.2f}%")
    except Exception as stat_error:
        logger.error(f"Failed to retrieve final verification stats: {stat_error}")

    return (verified_count, total_processed)

def _run_plex_verification_scan(max_files: int, recent_only: bool, max_attempts: int) -> Tuple[int, int]:
    """
    Run verification scan using Plex (original implementation).
    
    Args:
        max_files: Maximum number of files to check in one run
        recent_only: If True, use the recent scan method across all libraries
        max_attempts: Maximum number of verification attempts before marking as failed
        
    Returns:
        Tuple of (verified_count, total_processed)
    """
    # Get Plex settings needed for the initial check, though the scan functions get them internally
    plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
    plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')

    if not plex_url or not plex_token:
        logger.warning("Plex URL or token not configured for symlink verification in settings. Cannot proceed.")
        return (0, 0)

    # Get unverified files based on scan type
    if recent_only:
        from database.symlink_verification import get_recent_unverified_files
        unverified_files = get_recent_unverified_files(hours=6, limit=max_files)
        scan_type = "recent (all libraries)"
    else:
        from database.symlink_verification import get_unverified_files
        unverified_files = get_unverified_files(limit=max_files)
        scan_type = "full (all libraries)"

    if not unverified_files:
        logger.info(f"No unverified files to process in {scan_type} scan")
        return (0, 0)

    logger.info(f"Processing {len(unverified_files)} unverified files in {scan_type} scan")

    # Get Plex library contents using the new functions, forcing scan_all_libraries=True
    plex_library: Optional[Dict[str, Any]] = None
    try:
        if recent_only:
            logger.info("Calling sync_run_get_recent_from_plex(scan_all_libraries=True)...")
            plex_library = sync_run_get_recent_from_plex(scan_all_libraries=True)
        else:
            logger.info("Calling sync_run_get_collected_from_plex(request='all', scan_all_libraries=True)...")
            plex_library = sync_run_get_collected_from_plex(request='all', scan_all_libraries=True)

        if plex_library is None:
             logger.error(f"Plex scan ({scan_type}) failed to return data. Aborting verification run.")
             return (0, 0)
        elif not plex_library.get('movies') and not plex_library.get('episodes'):
             logger.warning(f"Plex scan ({scan_type}) returned no movies or episodes. Proceeding with verification check against empty library.")

        # Build quick-lookup indexes so each verify call is O(1)
        try:
            movies_index = {}
            for m in plex_library.get('movies', []):
                loc = m.get('location')
                if loc:
                    key = os.path.splitext(os.path.basename(loc))[0].lower()
                    movies_index.setdefault(key, []).append(os.path.basename(loc))

            episodes_index = {}
            for e in plex_library.get('episodes', []):
                loc = e.get('location')
                if loc:
                    key = os.path.splitext(os.path.basename(loc))[0].lower()
                    episodes_index.setdefault(key, []).append(os.path.basename(loc))

            # Store in plex_library so verify_media_file can reuse without changing its signature
            plex_library['movies_index'] = movies_index
            plex_library['episodes_index'] = episodes_index
            logger.debug(f"Built Plex library quick lookup indexes: movies={len(movies_index)}, episodes={len(episodes_index)}")
        except Exception as idx_err:
            logger.error(f"Error building Plex library lookup indexes: {idx_err}")

    except Exception as e:
         logger.error(f"Unexpected error during Plex scan ({scan_type}): {e}", exc_info=True)
         return (0, 0)

    verified_count = 0
    total_processed = 0

    for file_data in unverified_files:
        total_processed += 1
        verification_id = file_data['verification_id']

        # --- Start: Consistency checks ---
        media_item_id = file_data.get('media_item_id')
        if not media_item_id:
            logger.error(f"Verification record {verification_id} missing media_item_id. Marking as failed.")
            mark_file_as_permanently_failed(verification_id, "Missing media_item_id in verification record")
            continue

        media_item = get_media_item_by_id(media_item_id)
        if not media_item:
            logger.warning(f"Media item ID {media_item_id} (Verification ID {verification_id}) not found in media_items. Marking verification as failed.")
            mark_file_as_permanently_failed(verification_id, "Associated media item record not found in database")
            continue

        verification_path = file_data.get('full_path')
        db_path = media_item.get('location_on_disk')

        if not verification_path:
             logger.error(f"Verification record {verification_id} (Media ID: {media_item_id}) missing 'full_path'. Marking as failed.")
             mark_file_as_permanently_failed(verification_id, "Missing full_path in verification record")
             continue

        # Compare paths for staleness
        if verification_path != db_path:
            logger.warning(f"Path mismatch for Media ID {media_item_id} (Verification ID {verification_id}). Verification path: '{verification_path}', DB path: '{db_path}'. Marking verification record as failed (stale).")
            base_verification_path = os.path.basename(verification_path)
            base_db_path = os.path.basename(db_path or '')
            mark_file_as_permanently_failed(verification_id, f"Path mismatch: Verification record path ('{base_verification_path}') differs from current DB path ('{base_db_path}')")
            continue
        # --- End: Consistency checks ---

        logger.debug(f"Media item {media_item_id} found and path '{verification_path}' matches DB. Proceeding with verification for ID {verification_id}.")

        # --- Max attempts check ---
        if file_data.get('verification_attempts', 0) >= max_attempts:
            failure_reason = f"Exceeded maximum verification attempts ({file_data.get('verification_attempts', 0)} >= {max_attempts})"
            logger.warning(f"File {file_data['full_path']} (Media ID: {media_item_id}, Verification ID: {verification_id}) has reached max verification attempts. Marking as failed in verification queue only. Reason: {failure_reason}")
            mark_verification_as_max_attempts_failed(verification_id, failure_reason)
            continue

        # Check if the file exists before calling verify_media_file
        if not os.path.exists(file_data['full_path']):
            logger.warning(f"File does not exist: {file_data['full_path']} (Attempt {file_data.get('verification_attempts', 0) + 1}). Incrementing attempt count.")
            update_verification_attempt(verification_id)
            continue

        # Verify the file in Plex
        is_verified = verify_media_file(file_data, plex_library)

        if is_verified:
            # Mark as verified
            if mark_file_as_verified(verification_id):
                verified_count += 1
                logger.info(f"Successfully marked verification ID {verification_id} as verified for '{os.path.basename(file_data['full_path'])}'")
            else:
                logger.error(f"Failed to mark verification ID {verification_id} as verified in DB for '{os.path.basename(file_data['full_path'])}'")
        else:
            # Update attempt count
            _ = update_verification_attempt(verification_id)
            current_attempts = file_data.get('verification_attempts', 0) + 1
            logger.warning(f"Verification failed for ID {verification_id} ('{os.path.basename(file_data['full_path'])}'). Attempt count updated to {current_attempts}.")

            # Try to trigger a Plex library update for the item's directory if verification failed
            try:
                file_type = file_data.get('type', 'unknown')
                base_filename = os.path.basename(file_data['full_path'])

                if file_type == 'movie':
                    logger.info(
                        f"Attempting Plex directory scan for failed movie verification: {file_data['title']} - {base_filename}"
                    )
                else:  # TV show
                    season_num = file_data.get('season_number', 'unknown')
                    episode_num = file_data.get('episode_number', 'unknown')
                    logger.info(
                        f"Attempting Plex directory scan for failed episode verification: {file_data['title']} - S{season_num}E{episode_num} - {file_data.get('episode_title', 'unknown')} - {base_filename}"
                    )

                # Prepare item data for plex_update_item
                item_for_update = {'full_path': file_data['full_path'], 'title': file_data['title']}

                # Attempt the Plex update using the imported function
                update_result = plex_update_item(item_for_update)
                if update_result:
                    logger.info(f"Successfully triggered Plex directory scan potentially including: {base_filename}")
                else:
                    logger.warning(f"Plex directory scan trigger failed or returned False for directory containing: {base_filename}")
            except Exception as e:
                logger.error(f"Error triggering Plex update after failed verification: {str(e)}", exc_info=True)

    # Log stats
    try:
        stats = get_verification_stats()
        logger.info(f"Plex verification ({scan_type}) scan completed. Checked: {total_processed}, Newly Verified: {verified_count}. "
                    f"Overall Stats: Verified={stats['verified']}, Unverified={stats['unverified']}, Failed={stats['permanently_failed']}, Total={stats['total']}. "
                    f"Percent Verified: {stats['percent_verified']:.2f}%")
    except Exception as stat_error:
        logger.error(f"Failed to retrieve final verification stats: {stat_error}")

    return (verified_count, total_processed)

# Keep the old function name for backward compatibility
def run_plex_verification_scan(max_files: int = 500, recent_only: bool = False, max_attempts: int = 10) -> Tuple[int, int]:
    """
    Backward compatibility wrapper for run_media_verification_scan.
    """
    return run_media_verification_scan(max_files, recent_only, max_attempts)

# Keep the old function name for backward compatibility  
def verify_plex_file(file_data: Dict[str, Any], plex_library: Dict[str, List[Dict[str, Any]]]) -> bool:
    """
    Backward compatibility wrapper for verify_media_file.
    """
    return verify_media_file(file_data, plex_library)

# --- Test Function for Jellyfin/Emby Verification ---

def test_jellyfin_verification_support():
    """
    Test function to verify that Jellyfin/Emby verification is working correctly.
    This function can be called to test the verification system.
    """
    logger.info("Testing Jellyfin/Emby verification support...")
    
    # Check Jellyfin/Emby configuration
    jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
    jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()
    
    if not jellyfin_url or not jellyfin_token:
        logger.info("Jellyfin/Emby not configured. Verification will use Plex.")
        logger.info("To test Jellyfin/Emby verification, configure emby_jellyfin_url and emby_jellyfin_token in settings.")
        return False
    
    logger.info(f"Jellyfin/Emby configured: {jellyfin_url}")
    
    # Test the verification scan
    try:
        verified_count, total_processed = run_media_verification_scan(max_files=10, recent_only=False)
        logger.info(f"Jellyfin/Emby verification test completed: {verified_count} verified out of {total_processed} processed")
        return True
    except Exception as e:
        logger.error(f"Jellyfin/Emby verification test failed: {e}")
        return False

# Example usage:
# if __name__ == "__main__":
#     test_jellyfin_verification_support()
