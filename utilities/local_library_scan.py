import logging
from typing import List, Dict, Any, Optional
import os
from settings import get_setting
import shutil
from pathlib import Path
import re
from datetime import datetime
import time
from utilities.anidb_functions import format_filename_with_anidb
from database.database_writing import update_media_item_state, update_media_item
from utilities.post_processing import handle_state_change
from database import get_media_item_by_id

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be safe for symlinks."""
    # Convert Unicode characters to their ASCII equivalents where possible
    import unicodedata
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    
    # Replace problematic characters
    filename = re.sub(r'[<>|?*:"\'\&/\\]', '_', filename)  # Added slashes and backslashes
    return filename.strip()  # Just trim whitespace, don't mess with dots

def get_symlink_path(item: Dict[str, Any], original_file: str) -> str:
    """Get the full path for the symlink based on settings and metadata."""
    try:
        logging.debug(f"get_symlink_path received item with filename_real_path: {item.get('filename_real_path')}")
        logging.debug(f"Input item: type={item.get('type')}, genres={item.get('genres')}")
        
        symlinked_path = get_setting('File Management', 'symlinked_files_path')
        organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        logging.debug(f"Settings: symlinked_path={symlinked_path}, enable_separate_anime_folders={get_setting('Debug', 'enable_separate_anime_folders', False)}")
        
        # Get the original extension
        _, extension = os.path.splitext(original_file)
        
        # Build the path
        parts = []
        media_type = item.get('type', 'movie')
        
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
        root_folder_path = os.path.join(symlinked_path, folder_name)
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
            if get_setting('Debug', 'anime_renaming_using_anidb', False) and 'anime' in genres.lower():
                logging.debug(f"Checking for anime metadata for {item.get('title')}")
                from utilities.anidb_functions import get_anidb_metadata_for_item
                anime_metadata = get_anidb_metadata_for_item(item)
                if anime_metadata:
                    logging.debug(f"Using anime metadata for formatting: {anime_metadata}")
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
            
            template_vars.update(episode_vars)
            
            # Get the template for episodes
            template = get_setting('Debug', 'symlink_episode_template',
                                '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})')
        
        # Split template into folder structure parts
        path_parts = template.split('/')
        
        # Format and sanitize each part of the path
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
        
        # If the path exists, log it and return the path anyway
        if os.path.exists(full_path):
            logging.info(f"Symlink path already exists: {full_path}")
            
        return full_path
        
    except Exception as e:
        logging.error(f"Error getting symlink path: {str(e)}")
        return None

def create_symlink(source_path: str, dest_path: str, media_item_id: int = None) -> bool:
    """Create a symlink from source to destination path."""
    try:
        dest_dir = os.path.dirname(dest_path)
        
        # Check if a folder with same name but different case exists and use it
        if os.path.exists(os.path.dirname(dest_dir)):
            existing_items = os.listdir(os.path.dirname(dest_dir))
            folder_name = os.path.basename(dest_dir)
            for item in existing_items:
                if item.lower() == folder_name.lower() and item != folder_name:
                    logging.info(f"Using existing folder with different case: {item} instead of {folder_name}")
                    # Reconstruct the destination path using the existing folder name
                    dest_dir = os.path.join(os.path.dirname(dest_dir), item)
                    dest_path = os.path.join(dest_dir, os.path.basename(dest_path))
                    break
        
        # Ensure the destination directory exists
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        
        # Remove existing symlink if it exists
        if os.path.lexists(dest_path):
            os.unlink(dest_path)
            
        # Create the symlink
        os.symlink(source_path, dest_path)
        logging.info(f"Created symlink: {source_path} -> {dest_path}")

        # Add to verification queue if media_item_id is provided and library type is Symlinked/Local
        if media_item_id and get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            try:
                from database.symlink_verification import add_symlinked_file_for_verification
                add_symlinked_file_for_verification(media_item_id, dest_path)
                logging.info(f"Added file to verification queue: {dest_path}")
            except Exception as e:
                logging.error(f"Failed to add file to verification queue: {str(e)}")

        return True
    except Exception as e:
        logging.error(f"Failed to create symlink {source_path} -> {dest_path}: {str(e)}")
        return False

def find_file(filename: str, search_path: str) -> Optional[str]:
    """Find a file by name using the find command."""
    try:
        import subprocess
        result = subprocess.run(
            ['find', search_path, '-name', filename, '-type', 'f', '-print', '-quit'],
            capture_output=True, text=True
        )
        if result.stdout:
            found_path = result.stdout.strip()
            return found_path if found_path else None
        return None
    except Exception as e:
        logging.error(f"Error using find command: {str(e)}")
        return None

def check_local_file_for_item(item: Dict[str, Any], is_webhook: bool = False, extended_search: bool = False) -> bool:
    """
    Check if the local file for the item exists and create symlink if needed.
    When called from webhook endpoint, will retry up to 5 times with 1 second delay.
    
    Args:
        item: Dictionary containing item details
        is_webhook: If True, enables retry mechanism for webhook calls
        extended_search: If True, will perform an extended search for the file
    """
    max_retries = 10 if is_webhook else 1
    retry_delay = 1  # second
    
    for attempt in range(max_retries):
        try:
            if not item.get('filled_by_file'):
                return False
                
            original_path = get_setting('File Management', 'original_files_path')
            
            # First try the original path construction
            source_file = os.path.join(original_path, item.get('filled_by_title', ''), item['filled_by_file'])
            logging.debug(f"Trying original source file path: {source_file}")
            found_file = False

            # Always try both standard paths
            if os.path.exists(source_file):
                logging.debug(f"Found file at original path: {source_file}")
                found_file = True
            else:
                logging.debug(f"File not found at original path: {source_file}")
                # Try path with extension stripped from directory name
                title_dir = os.path.dirname(source_file)
                parent_dir = os.path.dirname(title_dir)
                title_without_ext = os.path.splitext(os.path.basename(title_dir))[0]
                source_file_no_ext = os.path.join(parent_dir, title_without_ext, os.path.basename(source_file))
                
                if os.path.exists(source_file_no_ext):
                    source_file = source_file_no_ext
                    logging.info(f"Found file at path with extension stripped: {source_file}")
                    found_file = True
                else:
                    logging.debug(f"File not found at stripped extension path: {source_file_no_ext}")

            # If file not found and extended search is enabled, try find command
            if not found_file and extended_search:
                # Only do extended search if item is not in a downloading state
                torrent_id = item.get('filled_by_torrent_id')
                is_downloading = False
                
                if torrent_id:
                    try:
                        from debrid import get_debrid_provider
                        debrid_provider = get_debrid_provider()
                        torrent_info = debrid_provider.get_torrent_info(torrent_id)
                        if torrent_info:
                            progress = torrent_info.get('progress', 0)
                            is_downloading = progress > 0 and progress < 100
                    except Exception as e:
                        logging.debug(f"Failed to check torrent status: {str(e)}")

                # Only perform find command search if not downloading
                if not is_downloading:
                    logging.debug(f"Attempting broad search in: {original_path}")
                    found_path = find_file(item['filled_by_file'], original_path)
                    if found_path:
                        source_file = found_path
                        # Update the filled_by_title to match the actual folder structure
                        item['filled_by_title'] = os.path.basename(os.path.dirname(found_path))
                        logging.info(f"Found file using find command: {source_file}")
                        found_file = True
                    else:
                        logging.debug(f"File not found after exhaustive search")

            if not found_file:
                if is_webhook and attempt < max_retries - 1:
                    logging.info(f"File not found, attempt {attempt + 1}/{max_retries}. Retrying in {retry_delay} second...")
                    time.sleep(retry_delay)
                    continue
                logging.warning(f"File not found in any checked location")
                return False
            
            # Get destination path based on settings
            dest_file = get_symlink_path(item, source_file)
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
            
            # If release is within last 7 days and upgrading is enabled, treat as potential upgrade
            is_upgrade_candidate = days_since_release <= 7 and get_setting("Scraping", "enable_upgrading", default=False)
            
            # Log upgrade status
            logging.debug(f"[UPGRADE] Processing item: {item_identifier}")
            logging.debug(f"[UPGRADE] Days since release: {days_since_release}")
            logging.debug(f"[UPGRADE] Is upgrade candidate: {is_upgrade_candidate}")
            logging.debug(f"[UPGRADE] Current file: {item.get('filled_by_file')}")
            logging.debug(f"[UPGRADE] Upgrading from: {item.get('upgrading_from')}")
            logging.debug(f"[UPGRADE] Torrent ID: {item.get('filled_by_torrent_id')}")
            
            # Only handle cleanup if we have a confirmed upgrade (upgrading_from is set)
            if item.get('upgrading_from'):
                logging.info(f"[UPGRADE] Processing confirmed upgrade for {item_identifier}")
                # Remove old torrent from debrid service if we have the ID
                is_downloading = False
                if item.get('filled_by_torrent_id'):
                    try:
                        from debrid import get_debrid_provider
                        debrid_provider = get_debrid_provider()
                        torrent_id = item.get('filled_by_torrent_id')
                        torrent_info = debrid_provider.get_torrent_info(torrent_id)
                        if torrent_info:
                            progress = torrent_info.get('progress', 0)
                            is_downloading = progress > 0 and progress < 100
                    except Exception as e:
                        logging.debug(f"Failed to check torrent status: {str(e)}")

                # Only perform cleanup if not downloading
                if not is_downloading:
                    # Remove old torrent from debrid service if we have the ID
                    if item.get('filled_by_torrent_id'):
                        try:
                            from debrid import get_debrid_provider
                            debrid_provider = get_debrid_provider()
                            debrid_provider.remove_torrent(
                                item['upgrading_from_torrent_id'],
                                removal_reason="Removed old torrent after successful upgrade"
                            )
                            logging.info(f"[UPGRADE] Removed old torrent {item['upgrading_from_torrent_id']} from debrid service")
                        except Exception as e:
                            logging.error(f"[UPGRADE] Failed to remove old torrent {item['filled_by_torrent_id']}: {str(e)}")
                
                # Remove old symlink if it exists
                # Use the old file's name to get the old symlink path
                old_source = os.path.join(original_path, item.get('filled_by_title'), item['upgrading_from'])
                # Save current values
                current_filled_by_file = item.get('filled_by_file')
                current_version = item.get('version')
                # Temporarily set the filled_by_file to the old file to get correct old path
                item['filled_by_file'] = item['upgrading_from']

                old_dest = get_symlink_path(item, old_source)
                # Restore current values
                item['filled_by_file'] = current_filled_by_file
                item['version'] = current_version

                if old_dest and os.path.lexists(old_dest):
                    try:
                        os.unlink(old_dest)
                        logging.info(f"[UPGRADE] Removed old symlink during upgrade: {old_dest}")
                        # Wait for media server to detect the removed symlink
                        time.sleep(1)
                        
                        # Remove the old file from Plex or Emby
                        if get_setting('Debug', 'emby_url', default=False):
                            from utilities.emby_functions import remove_file_from_emby
                            remove_file_from_emby(item['title'], old_dest, item.get('type') == 'episode' and item.get('episode_title'))
                        elif get_setting('File Management', 'plex_url_for_symlink', default=False):
                            from utilities.plex_functions import remove_file_from_plex
                            remove_file_from_plex(item['title'], old_dest, item.get('type') == 'episode' and item.get('episode_title'))

                    except Exception as e:
                        logging.error(f"[UPGRADE] Failed to remove old symlink {old_dest}: {str(e)}")
                else:
                    logging.debug(f"[UPGRADE] No old symlink found at {old_dest}")
            
            if not os.path.exists(dest_file):
                success = create_symlink(source_file, dest_file, item.get('id'))
                logging.debug(f"[UPGRADE] Created new symlink: {success}")
            else:
                # Verify existing symlink points to correct source
                if os.path.islink(dest_file):
                    real_source = os.path.realpath(dest_file)
                    if real_source == source_file:
                        success = True
                        logging.debug(f"[UPGRADE] Existing symlink is correct")
                    else:
                        # Recreate symlink if pointing to wrong source
                        os.unlink(dest_file)
                        success = create_symlink(source_file, dest_file, item.get('id'))
                        logging.debug(f"[UPGRADE] Recreated symlink with correct source: {success}")
                else:
                    logging.warning(f"[UPGRADE] Destination exists but is not a symlink: {dest_file}")
                    return False

            if success:
                logging.info(f"[UPGRADE] Successfully processed symlink at: {dest_file}")
                
                # Set state based on whether this is an upgrade candidate
                new_state = 'Upgrading' if is_upgrade_candidate else 'Collected'
                logging.info(f"[UPGRADE] Setting item state to: {new_state}")
                
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
            
            return success
            
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
    try:
        original_path = get_setting('File Management', 'original_files_path')
        
        if not os.path.exists(original_path):
            logging.error(f"Original files path does not exist: {original_path}")
            return {}
            
        found_items = {}
        
        # Create a map of filenames to look for
        target_files = {item.get('filled_by_file'): item for item in items if item.get('filled_by_file')}
        
        if not target_files:
            logging.debug("No files to scan for")
            return {}
            
        logging.info(f"Scanning for {len(target_files)} files")
        
        # Define common media file extensions
        media_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v'}
        
        # Keep track of processed files to avoid duplicates
        processed_files = set()
        
        # Walk through the original files directory
        for root, _, files in os.walk(original_path):
            for file in files:
                # Skip if not a media file
                if not any(file.lower().endswith(ext) for ext in media_extensions):
                    continue
                    
                if file in target_files and file not in processed_files:
                    source_file = os.path.join(root, file)
                    item = target_files[file]
                    
                    try:
                        # Get destination path based on settings
                        dest_file = get_symlink_path(item, source_file)
                        if not dest_file:
                            continue
                        
                        # Create symlink if it doesn't exist
                        success = False
                        if not os.path.exists(dest_file):
                            success = create_symlink(source_file, dest_file, item.get('id'))
                        else:
                            # Verify existing symlink points to correct source
                            if os.path.islink(dest_file):
                                real_source = os.path.realpath(dest_file)
                                if real_source == source_file:
                                    success = True
                                else:
                                    # Recreate symlink if pointing to wrong source
                                    os.unlink(dest_file)
                                    success = create_symlink(source_file, dest_file, item.get('id'))
                            else:
                                logging.warning(f"Destination exists but is not a symlink: {dest_file}")
                                continue
                        
                        if success:
                            found_items[item['id']] = {
                                'location': dest_file,
                                'original_path': source_file,
                                'filename': file,
                                'item': item
                            }
                            processed_files.add(file)
                            
                            # Update database with location
                            from database.database_writing import update_media_item
                            update_media_item(item['id'], location_on_disk=dest_file, collected_at=datetime.now())
                            
                    except Exception as e:
                        logging.error(f"Error processing file {file}: {str(e)}")
                        continue
                        
        logging.info(f"Local library scan found {len(found_items)} matching items")
        return found_items
        
    except Exception as e:
        logging.error(f"Error during local library scan: {e}", exc_info=True)
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
    try:
        original_path = get_setting('File Management', 'original_files_path')
        symlinked_path = get_setting('File Management', 'symlinked_files_path')
        
        if not os.path.exists(original_path):
            logging.error(f"Original files path does not exist: {original_path}")
            return {}
            
        # Create a map of filenames to look for
        target_files = {item.get('filled_by_file'): item for item in items if item.get('filled_by_file')}
        
        if not target_files:
            logging.debug("No files to scan for")
            return {}
            
        # Get all media files sorted by modification time
        media_files = []
        for root, _, files in os.walk(original_path):
            for file in files:
                if file in target_files:
                    full_path = os.path.join(root, file)
                    media_files.append((full_path, os.path.getmtime(full_path)))
                    
        # Sort by modification time and take the most recent
        media_files.sort(key=lambda x: x[1], reverse=True)
        recent_files = media_files[:max_files]
        
        found_items = {}
        for source_file, _ in recent_files:
            filename = os.path.basename(source_file)
            if filename in target_files:
                relative_path = get_relative_path(source_file)
                dest_file = os.path.join(symlinked_path, relative_path)
                
                # Create symlink if it doesn't exist
                if not os.path.exists(dest_file):
                    if create_symlink(source_file, dest_file, target_files[filename].get('id')):
                        item = target_files[filename]
                        found_items[item['id']] = {
                            'location': dest_file,
                            'original_path': source_file,
                            'filename': filename,
                            'item': item
                        }
                else:
                    # File already symlinked
                    item = target_files[filename]
                    found_items[item['id']] = {
                        'location': dest_file,
                        'original_path': source_file,
                        'filename': filename,
                        'item': item
                    }
                    
        logging.info(f"Recent local library scan found {len(found_items)} matching items")
        return found_items
        
    except Exception as e:
        logging.error(f"Error during recent local library scan: {e}", exc_info=True)
        return {}

def convert_item_to_symlink(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an existing library item to use symlinks.
    Returns a dict with success status and details about the conversion.
    """
    try:
        logging.debug(f"convert_item_to_symlink received item with filename_real_path: {item.get('filename_real_path')}")
        
        if not item.get('location_on_disk'):
            return {
                'success': False,
                'error': 'No location_on_disk found',
                'item_id': item.get('id')
            }

        # Get the original file path
        source_file = item['location_on_disk']
        if not os.path.exists(source_file):
            return {
                'success': False,
                'error': f'Source file not found: {source_file}',
                'item_id': item.get('id')
            }

        # Get destination path based on settings
        logging.debug(f"Calling get_symlink_path with filename_real_path: {item.get('filename_real_path')}")
        dest_file = get_symlink_path(item, source_file)
        if not dest_file:
            return {
                'success': False,
                'error': 'Failed to generate symlink path',
                'item_id': item.get('id')
            }

        # Create symlink if it doesn't exist
        success = False
        if not os.path.exists(dest_file):
            success = create_symlink(source_file, dest_file, item.get('id'))
        else:
            # Verify existing symlink points to correct source
            if os.path.islink(dest_file):
                real_source = os.path.realpath(dest_file)
                if real_source == source_file:
                    success = True
                else:
                    # Recreate symlink if pointing to wrong source
                    os.unlink(dest_file)
                    success = create_symlink(source_file, dest_file, item.get('id'))
            else:
                return {
                    'success': False,
                    'error': f'Destination exists but is not a symlink: {dest_file}',
                    'item_id': item.get('id')
                }

        if success:
            return {
                'success': True,
                'old_location': source_file,
                'new_location': dest_file,
                'item_id': item.get('id')
            }
        else:
            return {
                'success': False,
                'error': 'Failed to create symlink',
                'item_id': item.get('id')
            }

    except Exception as e:
        logging.error(f"Error converting item to symlink: {str(e)}")
        return {
            'success': False,
            'error': str(e),
            'item_id': item.get('id')
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
            original_path = get_setting('File Management', 'original_files_path')
            filename = os.path.basename(symlink_path)
            found_path = find_file(filename, original_path)
            
            if found_path:
                new_target_path = found_path
            else:
                return {
                    'success': False,
                    'message': 'Could not find original file',
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