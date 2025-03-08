import logging
import os
import time
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import asyncio
import aiohttp
from plexapi.server import PlexServer

from settings import get_setting
from database.symlink_verification import (
    get_unverified_files,
    mark_file_as_verified,
    update_verification_attempt,
    get_verification_stats,
    mark_file_as_permanently_failed
)
from utilities.plex_functions import plex_update_item

logger = logging.getLogger(__name__)

async def get_plex_library_contents(plex_url: str, plex_token: str, recent_only: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get media files from Plex libraries.
    
    Args:
        plex_url: Plex server URL
        plex_token: Plex authentication token
        recent_only: If True, only get recently added items
        
    Returns:
        Dict with keys 'movies' and 'episodes', each containing a list of media files
    """
    try:
        plex = PlexServer(plex_url, plex_token)
        
        # Initialize results
        all_media = {
            'movies': [],
            'episodes': []
        }
        
        # Get all library sections
        for section in plex.library.sections():
            logger.info(f"Processing Plex library section: {section.title} ({'recent only' if recent_only else 'all items'})")
            
            # Skip photo, music, or other non-video libraries
            if section.type not in ['movie', 'show']:
                continue
                
            # Process movie libraries
            if section.type == 'movie':
                # Get movies (either all or recently added)
                movies = section.recentlyAdded() if recent_only else section.all()
                for movie in movies:
                    for media_part in movie.iterParts():
                        all_media['movies'].append({
                            'title': movie.title,
                            'year': movie.year,
                            'file_path': media_part.file,
                            'filename': os.path.basename(media_part.file)
                        })
            
            # Process TV show libraries
            elif section.type == 'show':
                if recent_only:
                    # For recent items, we need to get recently added episodes
                    recent_items = section.recentlyAdded()
                    for item in recent_items:
                        # Recent items can be shows, seasons, or episodes
                        if hasattr(item, 'TYPE') and item.TYPE == 'episode':
                            for media_part in item.iterParts():
                                all_media['episodes'].append({
                                    'title': item.grandparentTitle,
                                    'episode_title': item.title,
                                    'season_number': item.seasonNumber,
                                    'episode_number': item.index,
                                    'file_path': media_part.file,
                                    'filename': os.path.basename(media_part.file)
                                })
                else:
                    # For full scan, process all episodes
                    for show in section.all():
                        for episode in show.episodes():
                            for media_part in episode.iterParts():
                                all_media['episodes'].append({
                                    'title': show.title,
                                    'episode_title': episode.title,
                                    'season_number': episode.seasonNumber,
                                    'episode_number': episode.index,
                                    'file_path': media_part.file,
                                    'filename': os.path.basename(media_part.file)
                                })
        
        logger.info(f"Found {len(all_media['movies'])} movies and {len(all_media['episodes'])} episodes in Plex ({'recent only' if recent_only else 'all items'})")
        return all_media
        
    except Exception as e:
        logger.error(f"Error getting Plex library contents: {str(e)}")
        return {'movies': [], 'episodes': []}

async def verify_plex_file(file_data: Dict[str, Any], plex_library: Dict[str, List[Dict[str, Any]]]) -> bool:
    """
    Verify if a file has been properly scanned into Plex by comparing with Plex library contents.
    
    Args:
        file_data: Dictionary containing file information
        plex_library: Dictionary containing Plex library contents
        
    Returns:
        bool: True if file is verified in Plex, False otherwise
    """
    try:
        # First check if file exists
        if not os.path.exists(file_data['full_path']):
            logger.error(f"File does not exist on disk: {file_data['full_path']}")
            return False
            
        # Check if it's a symlink and if it's valid
        if os.path.islink(file_data['full_path']):
            target_path = os.path.realpath(file_data['full_path'])
            if not os.path.exists(target_path):
                logger.error(f"Symlink target does not exist: {target_path} for symlink: {file_data['full_path']}")
                return False
            logger.debug(f"Symlink is valid. Target: {target_path}")
        
        # Extract the filename without extension for comparison
        filename = os.path.basename(file_data['full_path'])
        filename_no_ext = os.path.splitext(filename)[0]
        
        logger.debug(f"Verifying file in Plex: {filename}")
        
        # Check if it's a movie or TV show
        if file_data['type'] == 'movie':
            # Log the number of movies in Plex library
            logger.debug(f"Searching among {len(plex_library['movies'])} movies in Plex")
            
            # Search for the movie in Plex library
            for movie in plex_library['movies']:
                plex_filename = movie['filename']
                plex_filename_no_ext = os.path.splitext(plex_filename)[0]
                
                # Compare filenames (case insensitive)
                if plex_filename_no_ext.lower() == filename_no_ext.lower():
                    logger.info(f"Verified movie in Plex: {file_data['title']} - {filename}")
                    return True
                
            logger.warning(f"Movie not found in Plex after {file_data.get('verification_attempts', 0)} attempts: {file_data['title']} - {filename}")
                    
        else:  # TV show
            # Log the number of episodes in Plex library
            logger.debug(f"Searching among {len(plex_library['episodes'])} episodes in Plex")
            
            # Format episode info for logging
            season_num = file_data.get('season_number', 'unknown')
            episode_num = file_data.get('episode_number', 'unknown')
            episode_info = f"S{season_num}E{episode_num}"
            
            # Search for the episode in Plex library
            for episode in plex_library['episodes']:
                plex_filename = episode['filename']
                plex_filename_no_ext = os.path.splitext(plex_filename)[0]
                
                # Compare filenames (case insensitive)
                if plex_filename_no_ext.lower() == filename_no_ext.lower():
                    logger.info(f"Verified episode in Plex: {file_data['title']} {episode_info} - {file_data.get('episode_title', 'unknown')} - {filename}")
                    return True
            
            logger.warning(f"Episode not found in Plex after {file_data.get('verification_attempts', 0)} attempts: {file_data['title']} {episode_info} - {file_data.get('episode_title', 'unknown')} - {filename}")
        
        # If verification failed, log file permissions and ownership
        try:
            stat_info = os.stat(file_data['full_path'])
            logger.debug(f"File permissions: {oct(stat_info.st_mode)}, Owner: {stat_info.st_uid}, Group: {stat_info.st_gid}")
        except Exception as e:
            logger.error(f"Error getting file stats: {str(e)}")
        
        return False
        
    except Exception as e:
        logger.error(f"Error verifying file in Plex: {str(e)}", exc_info=True)
        return False

def run_plex_verification_scan(max_files: int = 50, recent_only: bool = False, max_attempts: int = 10) -> Tuple[int, int]:
    """
    Run a verification scan to check if symlinked files are in Plex.
    
    Args:
        max_files: Maximum number of files to check in one run
        recent_only: If True, only check recently added files in Plex
        max_attempts: Maximum number of verification attempts before marking as failed
        
    Returns:
        Tuple of (verified_count, total_processed)
    """
    # Get Plex settings
    plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
    plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
    
    if not plex_url or not plex_token:
        logger.warning("Plex URL or token not configured for symlink verification")
        return (0, 0)
    
    # Get unverified files based on scan type
    if recent_only:
        from database.symlink_verification import get_recent_unverified_files
        unverified_files = get_recent_unverified_files(hours=24, limit=max_files)
        scan_type = "recent"
    else:
        from database.symlink_verification import get_unverified_files
        unverified_files = get_unverified_files(limit=max_files)
        scan_type = "full"
    
    if not unverified_files:
        logger.info(f"No unverified files to process in {scan_type} scan")
        return (0, 0)
    
    logger.info(f"Processing {len(unverified_files)} unverified files in {scan_type} scan")
    
    # Get Plex library contents - pass recent_only parameter
    plex_library = asyncio.run(get_plex_library_contents(plex_url, plex_token, recent_only))
    
    verified_count = 0
    total_processed = 0
    
    for file_data in unverified_files:
        total_processed += 1
        
        # Check if max attempts exceeded
        if file_data.get('verification_attempts', 0) >= max_attempts:
            logger.error(f"File exceeded maximum verification attempts ({max_attempts}): {file_data['full_path']}")
            
            # Check if file exists
            if not os.path.exists(file_data['full_path']):
                logger.error(f"File does not exist on disk and has exceeded max attempts: {file_data['full_path']}")
                mark_file_as_permanently_failed(
                    file_data['verification_id'],
                    f"File does not exist on disk after {max_attempts} verification attempts"
                )
                continue
                
            # Check if it's a symlink and if it's valid
            if os.path.islink(file_data['full_path']):
                target_path = os.path.realpath(file_data['full_path'])
                if not os.path.exists(target_path):
                    logger.error(f"Symlink target does not exist and has exceeded max attempts. Target: {target_path}, Symlink: {file_data['full_path']}")
                    mark_file_as_permanently_failed(
                        file_data['verification_id'],
                        f"Symlink target does not exist after {max_attempts} verification attempts. Target: {target_path}"
                    )
                    continue
            
            # If file exists but still fails, log detailed information and mark as failed
            try:
                stat_info = os.stat(file_data['full_path'])
                failure_reason = (
                    f"File exists but verification keeps failing after {max_attempts} attempts. "
                    f"Permissions: {oct(stat_info.st_mode)}, Owner: {stat_info.st_uid}, Group: {stat_info.st_gid}"
                )
                logger.error(failure_reason)
                mark_file_as_permanently_failed(file_data['verification_id'], failure_reason)
            except Exception as e:
                logger.error(f"Error getting file stats: {str(e)}")
                mark_file_as_permanently_failed(
                    file_data['verification_id'],
                    f"Error accessing file after {max_attempts} attempts: {str(e)}"
                )
            
            continue
        
        # Check if the file exists
        if not os.path.exists(file_data['full_path']):
            logger.warning(f"File does not exist: {file_data['full_path']}")
            update_verification_attempt(file_data['verification_id'])
            continue
        
        # Verify the file in Plex
        is_verified = asyncio.run(verify_plex_file(file_data, plex_library))
        
        if is_verified:
            # Mark as verified
            if mark_file_as_verified(file_data['verification_id']):
                verified_count += 1
        else:
            # Update attempt count
            update_verification_attempt(file_data['verification_id'])
            
            # Try to update Plex for this item
            try:
                # Log more details about the file before attempting update
                file_type = file_data.get('type', 'unknown')
                if file_type == 'movie':
                    logger.info(f"Attempting Plex update for movie: {file_data['title']} - {os.path.basename(file_data['full_path'])}")
                else:  # TV show
                    season_num = file_data.get('season_number', 'unknown')
                    episode_num = file_data.get('episode_number', 'unknown')
                    logger.info(f"Attempting Plex update for episode: {file_data['title']} - S{season_num}E{episode_num} - {file_data.get('episode_title', 'unknown')} - {os.path.basename(file_data['full_path'])}")
                
                # Attempt the Plex update
                update_result = plex_update_item(file_data)
                if update_result:
                    logger.info(f"Successfully triggered Plex update for: {file_data['title']}")
                else:
                    logger.warning(f"Plex update triggered but returned False for: {file_data['title']}")
            except Exception as e:
                logger.error(f"Error triggering Plex update: {str(e)}", exc_info=True)
    
    # Log stats
    stats = get_verification_stats()
    logger.info(f"Plex verification scan completed. Verified: {verified_count}/{total_processed}. "
                f"Overall progress: {stats['verified']}/{stats['total']} ({stats['percent_verified']}%). "
                f"Permanently failed: {stats['permanently_failed']}")
    
    return (verified_count, total_processed)
