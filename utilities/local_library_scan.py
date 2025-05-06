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

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be safe for symlinks."""
    # Convert Unicode characters to their ASCII equivalents where possible
    import unicodedata
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    
    # Replace problematic characters
    filename = re.sub(r'[<>|?*:"\'\&/\\]', '_', filename)  # Added slashes and backslashes
    return filename.strip()  # Just trim whitespace, don't mess with dots

def get_symlink_path(item: Dict[str, Any], original_file: str, skip_jikan_lookup: bool = False) -> str:
    """Get the full path for the symlink based on settings and metadata."""
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
        logging.debug(f"Input item: type={item.get('type')}, genres={item.get('genres')}")
        
        symlinked_path = get_setting('File Management', 'symlinked_files_path')
        organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        logging.debug(f"Settings: symlinked_path={symlinked_path}, enable_separate_anime_folders={get_setting('Debug', 'enable_separate_anime_folders', False)}, organize_by_resolution={organize_by_resolution}")
        
        # Get the original extension
        _, extension = os.path.splitext(original_file)
        
        # Build the path
        parts = []
        media_type = item.get('type', 'movie')
        
        # If organizing by resolution is enabled, get the resolution from the version
        if organize_by_resolution:
            version = item.get('version', '')
            # If we have a version, check if there are corresponding version settings
            if version:
                try:
                    # Import here to avoid circular imports
                    from queues.config_manager import get_version_settings
                    version_settings = get_version_settings(version)
                    if version_settings and 'max_resolution' in version_settings:
                        resolution_folder = version_settings['max_resolution']
                        parts.append(resolution_folder)
                        logging.debug(f"Added resolution folder: {resolution_folder}")
                except Exception as e:
                    logging.error(f"Error getting version settings: {str(e)}")
        
        # Check if this is an anime
        genres = item.get('genres', '') or ''
        # Handle both string and list formats of genres
        if isinstance(genres, str):
            try:
                # Try to parse as JSON first (for database-stored genres)
                import json
                genres = json.loads(genres)
            except json.JSONDecodeError:
                # If not JSON, split by comma (for comma-separated strings)
                genres = [g.strip() for g in genres.split(',') if g.strip()]
        # Ensure genres is a list
        if not isinstance(genres, list):
            genres = [str(genres)]
        # Check for anime in any genre
        is_anime = any('anime' in genre.lower() for genre in genres)
        logging.debug(f"Content type: {'Anime' if is_anime else 'Regular'} {media_type}")
        
        # Determine the appropriate root folder
        # If it's anime and we have separate folders enabled, use anime folders
        if is_anime and get_setting('Debug', 'enable_separate_anime_folders', False):
            if media_type == 'movie':
                folder_name = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies')
                logging.debug(f"Using anime movies folder name: {folder_name}")
            else:
                folder_name = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows')
                logging.debug(f"Using anime TV shows folder name: {folder_name}")
        # Otherwise use regular folders
        else:
            if media_type == 'movie':
                folder_name = get_setting('Debug', 'movies_folder_name', 'Movies')
                logging.debug(f"Using movies folder name: {folder_name}")
            else:
                folder_name = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
                logging.debug(f"Using TV shows folder name: {folder_name}")

        # Validate folder name
        if not folder_name or folder_name.strip() == '':
            logging.error("Invalid folder name: folder name is empty")
            return None
            
        folder_name = folder_name.strip()
        parts.append(folder_name)
        logging.debug(f"parts after adding folder name: {parts}")
        
        # Create root folder if it doesn't exist
        root_folder_path = os.path.join(symlinked_path, *parts)
        if not os.path.exists(root_folder_path):
            try:
                os.makedirs(root_folder_path, exist_ok=True)
                logging.info(f"Created root folder: {root_folder_path}")
            except Exception as e:
                logging.error(f"Failed to create root folder {root_folder_path}: {str(e)}")
                return None
        
        # Prepare common template variables
        template_vars = {
            'title': item.get('title', 'Unknown'),
            'year': item.get('year', ''),
            'imdb_id': item.get('imdb_id', ''),
            'tmdb_id': item.get('tmdb_id', ''),
            'version': item.get('version', '').strip('*'),  # Remove all asterisks from start/end
            'quality': item.get('quality', ''),
            'original_filename': os.path.splitext(item.get('filled_by_file', ''))[0],  # Remove extension from original filename
            'content_source': item.get('content_source', ''),  # Add content source for template use
            'resolution': item.get('resolution', '')  # Add resolution for template use
        }

        if item.get('filename_real_path'):
            logging.debug(f"Using filename_real_path for original_filename: {item.get('filename_real_path')}")
            template_vars['original_filename'] = os.path.splitext(item.get('filename_real_path'))[0]
        
        if media_type == 'movie':
            # Get the template for movies
            template = get_setting('Debug', 'symlink_movie_template',
                                '{title} ({year})/{title} ({year}) - {imdb_id} - {version} - ({original_filename})')
        else:
            # Add episode-specific variables
            episode_vars = {
                'season_number': int(item.get('season_number', 0)),
                'episode_number': int(item.get('episode_number', 0)),
                'episode_title': item.get('episode_title', '')
            }
            
            logging.debug(f'anime_renaming_using_anidb: {get_setting("Debug", "anime_renaming_using_anidb", False)}')
            logging.debug(f'item.get("genres", ""): {item.get("genres", "")}')

            # Try to get anime metadata if enabled and item is anime
            genres = item.get('genres', '') or ''
            # Handle both string and list formats of genres
            if isinstance(genres, str):
                try:
                    # Try to parse as JSON first (for database-stored genres)
                    import json
                    genres = json.loads(genres)
                except json.JSONDecodeError:
                    # If not JSON, split by comma (for comma-separated strings)
                    genres = [g.strip() for g in genres.split(',') if g.strip()]
            # Ensure genres is a list
            if not isinstance(genres, list):
                genres = [str(genres)]
            # Check for anime in any genre
            is_anime = any('anime' in genre.lower() for genre in genres)
            
            # --- BEGIN Logging for AniDB Call --- 
            anidb_metadata_used = False
            if get_setting('Debug', 'anime_renaming_using_anidb', False) and is_anime and not skip_jikan_lookup:
                logging.info(f"[SymlinkPath] Anime detected and Jikan renaming enabled. Attempting to get Jikan metadata for '{item.get('title')} S{episode_vars.get('season_number')}E{episode_vars.get('episode_number')}")
                from utilities.anidb_functions import get_anidb_metadata_for_item
                anime_metadata = get_anidb_metadata_for_item(item)
                if anime_metadata:
                    logging.info(f"[SymlinkPath] Successfully got Jikan metadata: {anime_metadata}")
                    anidb_metadata_used = True
                    # Update only the episode-specific variables with anime metadata
                    episode_vars.update({
                        'season_number': int(anime_metadata.get('season_number', episode_vars['season_number'])),
                        'episode_number': int(anime_metadata.get('episode_number', episode_vars['episode_number'])),
                        'episode_title': anime_metadata.get('episode_title', episode_vars['episode_title'])
                    })
                    # Update main variables only if we have better data
                    if anime_metadata.get('title'):
                        template_vars['title'] = anime_metadata['title']
                    if anime_metadata.get('year'):
                        template_vars['year'] = anime_metadata['year']
                else:
                    logging.warning(f"[SymlinkPath] Failed to get Jikan metadata for '{item.get('title')}'. Using original item data.")
            else:
                logging.debug(f"[SymlinkPath] Jikan renaming not used. Is Anime: {is_anime}, Setting Enabled: {get_setting('Debug', 'anime_renaming_using_anidb', False)}")
            # --- END Logging for AniDB Call ---
            
            template_vars.update(episode_vars)
            
            # Get the template for episodes
            template = get_setting('Debug', 'symlink_episode_template',
                                '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})')
        
        # Split template into folder structure parts
        path_parts = template.split('/')
        
        # Format and sanitize each part of the path
        logging.debug(f"[SymlinkPath] Using template: '{template}'")
        logging.debug(f"[SymlinkPath] Using template variables: {template_vars}")
        for i, part in enumerate(path_parts):
            formatted_part = part.format(**template_vars)
            sanitized_part = sanitize_filename(formatted_part)
            
            # Add extension to the final part if it's not present
            if i == len(path_parts) - 1:
                if not sanitized_part.endswith(extension):
                    sanitized_part += extension
                
                # Check if full path would exceed max length (260 chars for Windows)
                max_path_length = 255
                dir_path = os.path.join(symlinked_path, *parts)
                full_path = os.path.join(dir_path, sanitized_part)
                
                if len(full_path) > max_path_length:
                    # Calculate how much we need to truncate
                    excess = len(full_path) - max_path_length
                    filename_without_ext = os.path.splitext(sanitized_part)[0]
                    # Add 3 more chars for the "..." we'll add
                    truncated = filename_without_ext[:-excess-3] + "..."
                    sanitized_part = truncated + extension
                    logging.debug(f"Truncated full path from {len(full_path)} to {len(os.path.join(dir_path, sanitized_part))} chars")
                
                # For the final part (filename), we'll add it later
                final_filename = sanitized_part
            else:
                parts.append(sanitized_part)
        
        # Create the directory path first
        dir_path = os.path.join(symlinked_path, *parts)
        
        # Ensure the full directory path exists
        try:
            os.makedirs(dir_path, exist_ok=True)
            logging.debug(f"Ensured directory path exists: {dir_path}")
        except Exception as e:
            logging.error(f"Failed to create directory path {dir_path}: {str(e)}")
            return None
        
        # Then join with the final filename
        base_path = os.path.join(dir_path, os.path.splitext(final_filename)[0])
        full_path = f"{base_path}{extension}"
        
        logging.info(f"[SymlinkPath] Generated path: {full_path}") # Log the final generated path
        
        # If the path exists, log it and return the path anyway
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
        
    # If destination exists and is a symlink, check if it points to the correct source
    if os.path.islink(dest_path):
        try:
            current_target = os.path.realpath(dest_path)
            if current_target == source_path:
                logging.info(f"Symlink already exists and points to the correct target: {dest_path}")
                return True # Already correctly linked
            else:
                logging.warning(f"Symlink exists but points to wrong target ({current_target}). Removing and recreating.")
                os.unlink(dest_path)
        except Exception as e:
            logging.error(f"Error checking existing symlink {dest_path}: {e}. Removing and recreating.")
            try:
                os.unlink(dest_path)
            except Exception as unlink_err:
                logging.error(f"Failed to remove existing incorrect symlink {dest_path}: {unlink_err}")
                return False # Cannot proceed if we can't remove the wrong link

    elif os.path.exists(dest_path):
        # If destination exists but is not a symlink (e.g., a regular file), log an error or handle as needed.
        # For now, let's log an error and return False to avoid overwriting.
        logging.error(f"Destination path exists but is not a symlink: {dest_path}. Cannot create symlink.")
        return False
        
    # Ensure the directory for the destination path exists
    dest_dir = os.path.dirname(dest_path)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        logging.error(f"Failed to create directory for symlink {dest_path}: {e}")
        return False

    # Create the symlink
    try:
        os.symlink(source_path, dest_path)
        logging.info(f"Created symlink: {dest_path} -> {source_path}")
        
        # Add the file to the verification queue if needed and media_item_id is provided
        if media_item_id is not None and not skip_verification: # Add condition here
             try:
                 add_symlinked_file_for_verification(media_item_id, dest_path)
                 logging.info(f"Added file to verification queue: {dest_path}")
             except Exception as e:
                 logging.error(f"Failed to add file to verification queue {dest_path}: {e}", exc_info=True)
                 # Continue even if adding to verification fails for now

        return True
    except Exception as e:
        logging.error(f"Failed to create symlink from {source_path} to {dest_path}: {e}")
        return False

def check_local_file_for_item(item: Dict[str, Any], is_webhook: bool = False, extended_search: bool = False, on_success_callback: Optional[Callable[[str], None]] = None) -> bool:
    """
    Check if the local file for the item exists and create symlink if needed.
    When called from webhook endpoint, will retry up to 5 times with 1 second delay.
    Calls on_success_callback(relative_path) upon successful processing.

    Args:
        item: Dictionary containing item details
        is_webhook: If True, enables retry mechanism for webhook calls
        extended_search: If True, will perform an extended search for the file
        on_success_callback: Optional function to call with the relative path upon success.

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
            current_filename = item['filled_by_file'] # The actual file we are looking for

            found_file = False
            source_file = None # Initialize source_file

            # --- Check Order: Original Torrent Title -> Filled By Title ---

            # 1. Check original_scraped_torrent_title (raw)
            if original_torrent_title:
                potential_path = os.path.join(original_path, original_torrent_title, current_filename)
                logging.debug(f"Attempt 1: Checking path using original_scraped_torrent_title: {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    found_file = True
                    logging.info(f"Found file using original_scraped_torrent_title (raw): {source_file}")

            # 2. Check original_scraped_torrent_title (trimmed)
            if not found_file and original_torrent_title:
                original_torrent_title_trimmed = os.path.splitext(original_torrent_title)[0]
                if original_torrent_title_trimmed != original_torrent_title: # Only check if trimming actually changed the name
                    potential_path = os.path.join(original_path, original_torrent_title_trimmed, current_filename)
                    logging.debug(f"Attempt 2: Checking path using trimmed original_scraped_torrent_title: {potential_path}")
                    if os.path.exists(potential_path):
                        source_file = potential_path
                        found_file = True
                        logging.info(f"Found file using original_scraped_torrent_title (trimmed): {source_file}")

            # 3. Check real_debrid_original_title (raw) (NEW)
            if not found_file and real_debrid_original_title:
                potential_path = os.path.join(original_path, real_debrid_original_title, current_filename)
                logging.debug(f"Attempt 3 (New): Checking path using real_debrid_original_title: {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    found_file = True
                    logging.info(f"Found file using real_debrid_original_title (raw): {source_file}")
            
            # 4. Check real_debrid_original_title (trimmed) (NEW)
            if not found_file and real_debrid_original_title:
                real_debrid_original_title_trimmed = os.path.splitext(real_debrid_original_title)[0]
                if real_debrid_original_title_trimmed != real_debrid_original_title:
                    potential_path = os.path.join(original_path, real_debrid_original_title_trimmed, current_filename)
                    logging.debug(f"Attempt 4 (New): Checking path using trimmed real_debrid_original_title: {potential_path}")
                    if os.path.exists(potential_path):
                        source_file = potential_path
                        found_file = True
                        logging.info(f"Found file using real_debrid_original_title (trimmed): {source_file}")

            # 5. Check filled_by_title (raw)
            if not found_file and filled_by_title:
                potential_path = os.path.join(original_path, filled_by_title, current_filename)
                logging.debug(f"Attempt 5: Checking path using filled_by_title: {potential_path}")
                if os.path.exists(potential_path):
                    source_file = potential_path
                    found_file = True
                    logging.info(f"Found file using filled_by_title (raw): {source_file}")

            # 6. Check filled_by_title (trimmed)
            if not found_file and filled_by_title:
                filled_by_title_trimmed = os.path.splitext(filled_by_title)[0]
                if filled_by_title_trimmed != filled_by_title: # Only check if trimming actually changed the name
                    potential_path = os.path.join(original_path, filled_by_title_trimmed, current_filename)
                    logging.debug(f"Attempt 6: Checking path using trimmed filled_by_title: {potential_path}")
                    if os.path.exists(potential_path):
                        source_file = potential_path
                        found_file = True
                        logging.info(f"Found file using filled_by_title (trimmed): {source_file}")

            # 7. Check direct path (less common, added for completeness)
            if not found_file:
                 potential_path = os.path.join(original_path, current_filename)
                 logging.debug(f"Attempt 7: Checking direct path under original_files_path: {potential_path}")
                 if os.path.exists(potential_path):
                     source_file = potential_path
                     found_file = True
                     logging.info(f"Found file directly under original_files_path: {source_file}")


            # --- Handling not found after all checks ---
            if not found_file:
                if is_webhook and attempt < max_retries - 1:
                    logging.info(f"File '{current_filename}' not found using any title variation, attempt {attempt + 1}/{max_retries}. Retrying in {retry_delay} second...")
                    time.sleep(retry_delay)
                    continue
                logging.warning(f"File '{current_filename}' not found in any checked location")
                return False
            
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
            if item.get('release_date', '').lower() in ['unknown', 'none', '']:
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

                # --- Start: Torrent/File Removal Logic ---
                removal_successful = False
                old_torrent_id = item.get('upgrading_from_torrent_id')
                old_filename = item.get('upgrading_from') # Filename of the file being replaced

                if old_torrent_id:
                    logging.info(f"[UPGRADE] Attempting to remove old torrent {old_torrent_id} via debrid API.")
                    try:
                        from debrid import get_debrid_provider
                        debrid_provider = get_debrid_provider()
                        # Assuming remove_torrent returns True/False or raises Exception
                        debrid_provider.remove_torrent(
                            old_torrent_id,
                            removal_reason="Removed old torrent after successful upgrade"
                        )
                        removal_successful = True # Assume success if no exception
                        logging.info(f"[UPGRADE] Successfully initiated removal of old torrent {old_torrent_id} via debrid API.")
                    except Exception as remove_err:
                        # Check if it's a 404 (Not Found), which might mean it was already deleted
                        if '404' in str(remove_err):
                             logging.warning(f"[UPGRADE] Old torrent {old_torrent_id} not found on debrid (likely already removed). Proceeding.")
                             removal_successful = True # Treat as success
                        else:
                            logging.error(f"[UPGRADE] Failed to remove old torrent {old_torrent_id} via debrid API: {remove_err}")
                else:
                    old_file_path_from_item = item.get('original_path_for_symlink') # Get path from item dict
                    logging.warning(f"[UPGRADE] Old torrent ID is missing for item {item['id']}. Attempting local file deletion using item's original path: '{old_file_path_from_item}'")
                    # Directly use the path from the item dict
                    if old_file_path_from_item and os.path.exists(old_file_path_from_item):
                        try:
                            os.remove(old_file_path_from_item)
                            removal_successful = True # Assume success if os.remove doesn't raise error
                            logging.info(f"[UPGRADE] Successfully removed old local file: {old_file_path_from_item}")
                            # Optionally, check if the file is truly gone
                            if os.path.exists(old_file_path_from_item):
                                logging.warning(f"[UPGRADE] Local file {old_file_path_from_item} still exists after os.remove attempt.")
                                removal_successful = False
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
                    # Remove old symlink if it exists
                    # Use the old file's name to get the old symlink path

                    # Determine the source path of the OLD file to find its corresponding symlink
                    # We need the original base path where the old file *would* have been downloaded.
                    # Using the new file's directory might be the best guess if they are typically co-located.
                    old_base_path = os.path.dirname(source_file) if source_file else None

                    if old_base_path and old_filename:
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

                        if old_dest and os.path.lexists(old_dest):
                            try:
                                os.unlink(old_dest)
                                logging.info(f"[UPGRADE] Removed old symlink during upgrade: {old_dest}")

                                # --- EDIT: Remove old verification entry ---
                                try:
                                    removed_count = remove_verification_by_media_item_id(item['id'])
                                    if removed_count > 0:
                                        logging.info(f"[UPGRADE] Removed {removed_count} old verification record(s) for media item ID {item['id']}")
                                    else:
                                        logging.debug(f"[UPGRADE] No existing verification record found to remove for media item ID {item['id']}")
                                except Exception as db_remove_err:
                                    logging.error(f"[UPGRADE] Failed to remove old verification record for media item ID {item['id']}: {db_remove_err}")
                                # --- END EDIT ---

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
                                            from utilities.plex_functions import remove_file_from_plex
                                            remove_file_from_plex(item['title'], old_dest, episode_title)
                                    except Exception as media_server_remove_err:
                                         logging.error(f"[UPGRADE] Failed removing old file {old_dest} from {media_server_type}: {media_server_remove_err}")

                            except Exception as e:
                                logging.error(f"[UPGRADE] Failed to remove old symlink {old_dest}: {str(e)}")
                        else:
                            logging.debug(f"[UPGRADE] No old symlink found at {old_dest} (or path couldn't be determined).")
                    else:
                         logging.warning("[UPGRADE] Could not determine old source path components for symlink removal.")

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
                from database import get_media_item_by_id
                updated_item = get_media_item_by_id(item['id'])
                if updated_item:
                    if new_state == 'Collected':
                        handle_state_change(dict(updated_item))
                    elif new_state == 'Upgrading':
                        handle_state_change(dict(updated_item))

                # Add notification for all collections (including previously collected)
                if not item.get('upgrading_from'):
                    from database.database_writing import add_to_collected_notifications
                    notification_item = item.copy()
                    notification_item.update(update_values)
                    notification_item['is_upgrade'] = False
                    notification_item['new_state'] = "Collected"
                    add_to_collected_notifications(notification_item)
                    logging.info(f"Added collection notification for item: {item_identifier}")
                # Add notification for upgrades
                elif item.get('upgrading_from'):
                    from database.database_writing import add_to_collected_notifications
                    notification_item = item.copy()
                    notification_item.update(update_values)
                    notification_item['is_upgrade'] = True
                    notification_item['new_state'] = 'Upgraded'
                    add_to_collected_notifications(notification_item)

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

def local_library_scan(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Scan local library for specific items' files when Symlinked/Local is enabled.
    This is used as an alternative to Plex scanning when working with symlinked files.
    
    Args:
        items: List of items to scan for
    
    Returns:
        Dict mapping item IDs to their found file information
    """
    # Disabled for now
    return {}

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
