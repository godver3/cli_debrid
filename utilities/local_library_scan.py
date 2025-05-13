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
from database.database_reading import get_all_media_items, get_media_item_by_id

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
        organize_by_version = get_setting('File Management', 'symlink_organize_by_version', False)
        folder_order_str = get_setting('File Management', 'symlink_folder_order', "type,version,resolution")

        # Settings for type folder name determination
        enable_separate_anime_folders = get_setting('Debug', 'enable_separate_anime_folders', False)
        anime_movies_folder_name_setting = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies')
        anime_tv_shows_folder_name_setting = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows')
        movies_folder_name_setting = get_setting('Debug', 'movies_folder_name', 'Movies')
        tv_shows_folder_name_setting = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')

        logging.debug(f"[SymlinkPath] Settings: symlinked_path='{symlinked_path}', "
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
                item_version_for_resolution = item.get('version', '') # Resolution folder is derived from version's settings
                if item_version_for_resolution:
                    try:
                        from queues.config_manager import get_version_settings
                        # Strip asterisks before using the version to get settings
                        clean_version_for_resolution = item_version_for_resolution.strip('*') 
                        version_settings = get_version_settings(clean_version_for_resolution) 
                        if version_settings and 'max_resolution' in version_settings:
                            resolution_folder_name = version_settings['max_resolution']
                            if resolution_folder_name: # Ensure not empty
                                ordered_prefix_parts.append(resolution_folder_name) # Assume already clean from version settings
                                logging.debug(f"[SymlinkPath] Added resolution component to path: '{resolution_folder_name}'")
                    except Exception as e:
                        logging.error(f"[SymlinkPath] Error getting version settings for resolution folder: {str(e)}")
            
            elif component == "type" and organize_by_type:
                genres = item.get('genres', '') or ''
                if isinstance(genres, str):
                    try:
                        import json
                        genres = json.loads(genres)
                    except json.JSONDecodeError:
                        genres = [g.strip() for g in genres.split(',') if g.strip()]
                if not isinstance(genres, list):
                    genres = [str(genres)]
                is_anime = any('anime' in genre.lower() for genre in genres)
                
                folder_name_for_type = ""
                if is_anime and enable_separate_anime_folders:
                    folder_name_for_type = anime_movies_folder_name_setting if media_type == 'movie' else anime_tv_shows_folder_name_setting
                else:
                    folder_name_for_type = movies_folder_name_setting if media_type == 'movie' else tv_shows_folder_name_setting
                
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
        template_vars = {
            'title': item.get('title', 'Unknown'),
            'year': item.get('year', ''),
            'imdb_id': item.get('imdb_id', ''),
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
            episode_vars = {
                'season_number': int(item.get('season_number', 0)),
                'episode_number': int(item.get('episode_number', 0)),
                'episode_title': item.get('episode_title', '')
            }
            
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

            anidb_metadata_used = False
            if get_setting('Debug', 'anime_renaming_using_anidb', False) and is_anime_for_rename and not skip_jikan_lookup:
                logging.info(f"[SymlinkPath] Anime detected and AniDB renaming enabled. Attempting to get AniDB metadata for '{item.get('title')} S{episode_vars.get('season_number')}E{episode_vars.get('episode_number')}")
                from utilities.anidb_functions import get_anidb_metadata_for_item # Ensure this import is correct
                anime_metadata = get_anidb_metadata_for_item(item)
                if anime_metadata:
                    logging.info(f"[SymlinkPath] Successfully got AniDB metadata: {anime_metadata}")
                    anidb_metadata_used = True
                    episode_vars.update({
                        'season_number': int(anime_metadata.get('season_number', episode_vars['season_number'])),
                        'episode_number': int(anime_metadata.get('episode_number', episode_vars['episode_number'])),
                        'episode_title': anime_metadata.get('episode_title', episode_vars['episode_title'])
                    })
                    if anime_metadata.get('title'): template_vars['title'] = anime_metadata['title']
                    if anime_metadata.get('year'): template_vars['year'] = anime_metadata['year']
                else:
                    logging.warning(f"[SymlinkPath] Failed to get AniDB metadata for '{item.get('title')}'. Using original item data.")
            else:
                logging.debug(f"[SymlinkPath] AniDB renaming not used. Is Anime: {is_anime_for_rename}, Setting Enabled: {get_setting('Debug', 'anime_renaming_using_anidb', False)}")
            
            template_vars.update(episode_vars)
            template = get_setting('Debug', 'symlink_episode_template',
                                '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})')
        
        path_parts_from_template = template.split('/')
        logging.debug(f"[SymlinkPath] Using template: '{template}'")
        logging.debug(f"[SymlinkPath] Template variables: {template_vars}")
        
        final_filename = "" 

        for i, part_template_segment in enumerate(path_parts_from_template):
            formatted_part = part_template_segment.format(**template_vars)
            sanitized_template_part = sanitize_filename(formatted_part)
            
            if i == len(path_parts_from_template) - 1: # This is the filename part
                if not sanitized_template_part.endswith(extension):
                    sanitized_template_part += extension
                
                # Path length check
                # 'parts' at this point contains: ordered_prefix_parts + any preceding template directory parts
                current_dir_parts_for_check = os.path.join(symlinked_path, *parts)
                potential_full_path = os.path.join(current_dir_parts_for_check, sanitized_template_part)
                
                max_path_length = 255 
                if len(potential_full_path) > max_path_length:
                    excess = len(potential_full_path) - max_path_length
                    filename_without_ext = os.path.splitext(sanitized_template_part)[0]
                    if len(filename_without_ext) > excess + 3: # +3 for "..."
                        truncated_filename = filename_without_ext[:-(excess + 3)] + "..."
                        sanitized_template_part = truncated_filename + extension
                        logging.debug(f"[SymlinkPath] Truncated filename from {len(potential_full_path)} to {len(os.path.join(current_dir_parts_for_check, sanitized_template_part))} due to path length limit.")
                    else:
                        logging.warning(f"[SymlinkPath] Filename '{sanitized_template_part}' too short to truncate meaningfully for path length limit. Full path: {potential_full_path}")
                final_filename = sanitized_template_part
            else: # This is a directory part from the template
                if sanitized_template_part: # Ensure not empty
                    parts.append(sanitized_template_part)
        
        # 'parts' now contains: ordered_prefix_parts + directory_parts_from_template
        dir_path = os.path.join(symlinked_path, *parts)
        
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
                updated_item = get_media_item_by_id(item['id'])
                if updated_item:
                    if new_state == 'Collected':
                        handle_state_change(dict(updated_item))
                    elif new_state == 'Upgrading':
                        handle_state_change(dict(updated_item))

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

# --- Add Helper Function for Source File Searching ---
def _find_source_file_in_base(item: Dict[str, Any], base_search_path: str, filename_only: str) -> Optional[str]:
    """Helper to search for filename_only under base_search_path using common folder structures."""
    if not base_search_path or not filename_only:
        return None

    logging.debug(f"[_find_source_file_in_base] Searching for '{filename_only}' under '{base_search_path}' for item ID {item.get('id')}")

    possible_folder_names = [
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
        collected_items = get_all_media_items(state='Collected')
        # Also consider items in 'Upgrading' state if they have symlinks that need checking
        upgrading_items = get_all_media_items(state='Upgrading')
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
                if db_symlink_location and os.path.lexists(db_symlink_location):
                    if os.path.islink(db_symlink_location): os.unlink(db_symlink_location)
                
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

    logging.info(f"Symlink resynchronization finished. Total: {total_items}, Symlinks Updated: {updated_count}, Symlinks Created: {created_count}, Source Paths Updated in DB: {source_path_updated_count}, Errors: {error_count}, Skipped: {skipped_count}.")
    return {
        "status": "completed",
        "total_items": total_items,
        "symlinks_updated_count": updated_count,
        "symlinks_created_count": created_count,
        "source_paths_db_updated_count": source_path_updated_count,
        "error_count": error_count,
        "skipped_count": skipped_count
    }
