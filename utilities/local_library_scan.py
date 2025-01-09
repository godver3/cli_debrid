import logging
from typing import List, Dict, Any, Optional
import os
from settings import get_setting
import shutil
from pathlib import Path
import re
from datetime import datetime
import time

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be safe for symlinks."""
    # Only replace characters that are truly problematic for symlinks
    filename = re.sub(r'[<>|?*]', '_', filename)  # Removed :"/\ from the list as they're valid in paths
    return filename.strip()  # Just trim whitespace, don't mess with dots

def format_symlink_name(item: Dict[str, Any], original_extension: str) -> str:
    """Format symlink name based on item metadata and settings."""
    try:
        media_type = item.get('type', 'movie')
        template = get_setting('Debug', 
                             'symlink_movie_template' if media_type == 'movie' else 'symlink_episode_template',
                             '{title} ({year}) - {imdb_id}' if media_type == 'movie' else 
                             '{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {imdb_id}')
        
        # Prepare template variables
        template_vars = {
            'title': item.get('title', 'Unknown'),
            'year': item.get('year', ''),
            'imdb_id': item.get('imdb_id', ''),
            'tmdb_id': item.get('tmdb_id', ''),
            'version': item.get('version', '').strip('*'),  # Remove all asterisks from start/end
            'quality': item.get('quality', ''),
            'original_filename': item.get('filled_by_file', '')
        }
        
        # Add episode-specific variables if needed
        if media_type == 'episode':
            template_vars.update({
                'season_number': int(item.get('season_number', 0)),
                'episode_number': int(item.get('episode_number', 0)),
                'episode_title': item.get('episode_title', '')
            })
        
        # Format the filename
        filename = template.format(**template_vars)
        filename = sanitize_filename(filename)
        
        # Add extension if configured
        if get_setting('Debug', 'symlink_preserve_extension', True):
            # Extract extension from original file if no extension provided
            if not original_extension and item.get('filled_by_file'):
                _, original_extension = os.path.splitext(item['filled_by_file'])
            if original_extension and not original_extension.startswith('.'):
                original_extension = f".{original_extension}"
            filename = f"{filename}{original_extension}"
            
        return filename
        
    except Exception as e:
        logging.error(f"Error formatting symlink name: {str(e)}")
        return None

def get_symlink_path(item: Dict[str, Any], original_file: str) -> str:
    """Get the full path for the symlink based on settings and metadata."""
    try:
        symlinked_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
        organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        
        # Get the original extension
        _, extension = os.path.splitext(original_file)
        
        # Build the path
        parts = []
        media_type = item.get('type', 'movie')
        
        # Add Movies/TV Shows root folder if enabled
        if organize_by_type:
            parts.append('Movies' if media_type == 'movie' else 'TV Shows')
        
        # Prepare common template variables
        template_vars = {
            'title': item.get('title', 'Unknown'),
            'year': item.get('year', ''),
            'imdb_id': item.get('imdb_id', ''),
            'tmdb_id': item.get('tmdb_id', ''),
            'version': item.get('version', '').strip('*'),  # Remove all asterisks from start/end
            'quality': item.get('quality', ''),
            'original_filename': os.path.splitext(item.get('filled_by_file', ''))[0]  # Remove extension from original filename
        }
        
        if media_type == 'movie':
            # Get the template for movies
            template = get_setting('Debug', 'symlink_movie_template',
                                '{title} ({year})/{title} ({year}) - {imdb_id} - {version} - ({original_filename})')
        else:
            # Add episode-specific variables
            template_vars.update({
                'season_number': int(item.get('season_number', 0)),
                'episode_number': int(item.get('episode_number', 0)),
                'episode_title': item.get('episode_title', '')
            })
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
                # Check if filename exceeds max length
                max_filename_length = 247
                filename_without_ext = os.path.splitext(sanitized_part)[0]
                if len(sanitized_part) > max_filename_length:
                    # Truncate the filename part only, preserving extension
                    excess = len(sanitized_part) - max_filename_length
                    truncated = filename_without_ext[:-excess-3] + "..."
                    sanitized_part = truncated + extension
                    logging.debug(f"Truncated filename from {len(filename_without_ext + extension)} to {len(sanitized_part)} chars: {sanitized_part}")
                # For the final part (filename), we'll add it later
                final_filename = sanitized_part
            else:
                parts.append(sanitized_part)
        
        # Create the directory path first
        dir_path = os.path.join(symlinked_path, *parts)
        
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

def create_symlink(source_path: str, dest_path: str) -> bool:
    """Create a symlink from source to destination path."""
    try:
        # Ensure the destination directory exists
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        
        # Remove existing symlink if it exists
        if os.path.lexists(dest_path):
            os.unlink(dest_path)
            
        # Create the symlink
        os.symlink(source_path, dest_path)
        logging.info(f"Created symlink: {source_path} -> {dest_path}")

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
                
            original_path = get_setting('Debug', 'original_files_path', '/mnt/zurg/__all__')
            
            # First try the original path construction
            source_file = os.path.join(original_path, item.get('filled_by_title', ''), item['filled_by_file'])
            logging.debug(f"Trying original source file path: {source_file}")
            
            if extended_search:
                # If file doesn't exist at the original path, search for it using find command
                if not os.path.exists(source_file):
                    logging.debug(f"File not found at original path, searching in {original_path}")
                    found_path = find_file(item['filled_by_file'], original_path)
                if found_path:
                    source_file = found_path
                    # Update the filled_by_title to match the actual folder structure
                    item['filled_by_title'] = os.path.basename(os.path.dirname(found_path))
                    logging.info(f"Found file at alternate location: {source_file}")
            
            if not os.path.exists(source_file):
                if is_webhook and attempt < max_retries - 1:
                    logging.info(f"File not found, attempt {attempt + 1}/{max_retries}. Retrying in {retry_delay} second...")
                    time.sleep(retry_delay)
                    continue
                logging.warning(f"Original file not found: {source_file}")
                return False
                
            # Get destination path based on settings
            dest_file = get_symlink_path(item, source_file)
            if not dest_file:
                return False
            
            success = False
            
            # Check if this is a potential upgrade based on release date
            release_date = datetime.strptime(item.get('release_date', '1970-01-01'), '%Y-%m-%d').date()
            days_since_release = (datetime.now().date() - release_date).days
            
            # If release is within last 7 days and upgrading is enabled, treat as potential upgrade
            is_upgrade_candidate = days_since_release <= 7 and get_setting("Scraping", "enable_upgrading", default=False)
            
            # Log upgrade status
            item_identifier = f"{item.get('title')} ({item.get('year', '')})"
            if item.get('type') == 'episode':
                item_identifier += f" S{item.get('season_number', '00'):02d}E{item.get('episode_number', '00'):02d}"
            
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
                if item.get('filled_by_torrent_id'):
                    try:
                        from debrid import get_debrid_provider
                        debrid_provider = get_debrid_provider()
                        debrid_provider.remove_torrent(item['upgrading_from_torrent_id'])
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
                    except Exception as e:
                        logging.error(f"[UPGRADE] Failed to remove old symlink {old_dest}: {str(e)}")
                else:
                    logging.debug(f"[UPGRADE] No old symlink found at {old_dest}")

            if not os.path.exists(dest_file):
                success = create_symlink(source_file, dest_file)
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
                        success = create_symlink(source_file, dest_file)
                        logging.debug(f"[UPGRADE] Recreated symlink with correct source: {success}")
                else:
                    logging.warning(f"[UPGRADE] Destination exists but is not a symlink: {dest_file}")
                    return False

            if success:
                logging.info(f"[UPGRADE] Successfully processed symlink at: {dest_file}")
                from database.database_writing import update_media_item
                
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
                    'filled_by_torrent_id': item.get('filled_by_torrent_id')
                }
                
                # Only set upgrading_from if this is a confirmed upgrade
                if item.get('upgrading_from'):
                    update_values['upgrading_from'] = item['upgrading_from']
                
                logging.debug(f"[UPGRADE] Updating item with values: {update_values}")
                update_media_item(item['id'], **update_values)

                # Add notification for new collections (not upgrades)
                if not item.get('upgrading_from') and not item.get('collected_at'):
                    from database.database_writing import add_to_collected_notifications
                    notification_item = item.copy()
                    notification_item.update(update_values)
                    notification_item['is_upgrade'] = False
                    add_to_collected_notifications(notification_item)
                    logging.info(f"Added collection notification for new item: {item_identifier}")

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
        original_path = get_setting('Debug', 'original_files_path', '/mnt/zurg/__all__')
        
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
                            success = create_symlink(source_file, dest_file)
                        else:
                            # Verify existing symlink points to correct source
                            if os.path.islink(dest_file):
                                real_source = os.path.realpath(dest_file)
                                if real_source == source_file:
                                    success = True
                                else:
                                    # Recreate symlink if pointing to wrong source
                                    os.unlink(dest_file)
                                    success = create_symlink(source_file, dest_file)
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
        original_path = get_setting('Debug', 'original_files_path', '/mnt/zurg/__all__')
        symlinked_path = get_setting('Debug', 'symlinked_files_path', '/mnt/symlinked')
        
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
                    if create_symlink(source_file, dest_file):
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
            success = create_symlink(source_file, dest_file)
        else:
            # Verify existing symlink points to correct source
            if os.path.islink(dest_file):
                real_source = os.path.realpath(dest_file)
                if real_source == source_file:
                    success = True
                else:
                    # Recreate symlink if pointing to wrong source
                    os.unlink(dest_file)
                    success = create_symlink(source_file, dest_file)
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