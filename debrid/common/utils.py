import re
import logging
from typing import List, Union, Dict, Tuple

# Common video file extensions
VIDEO_EXTENSIONS = [
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'webm', 'mpg', 'mpeg', 'm2ts', 'ts'
]

def is_video_file(filename: str) -> bool:
    """Check if a file is a video file based on its extension"""
    result = any(filename.lower().endswith(f'.{ext}') for ext in VIDEO_EXTENSIONS)
    #logging.info(f"is_video_file check for {filename}: {result}")
    return result

def is_unwanted_file(filename: str) -> bool:
    """Check if a file is unwanted (e.g., sample files)"""
    result = 'sample' in filename.lower()
    #logging.info(f"is_unwanted_file check for {filename}: {result}")
    return result

def extract_hash_from_magnet(magnet_link: str) -> str:
    """Extract hash from magnet link or download and extract from HTTP link."""
    try:
        # If it's an HTTP link, download and extract hash
        if magnet_link.startswith('http'):
            from debrid.common import download_and_extract_hash
            return download_and_extract_hash(magnet_link)
            
        # For magnet links, extract hash directly
        if not magnet_link.startswith('magnet:'):
            raise ValueError("Invalid magnet link format")
            
        # Extract hash from magnet link
        hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
        if not hash_match:
            raise ValueError("Could not find valid hash in magnet link")
            
        return hash_match.group(1).lower()
    except Exception as e:
        logging.error(f"Error extracting hash: {str(e)}")
        raise ValueError("Invalid magnet link format")

def is_valid_hash(hash_string: str) -> bool:
    """Check if a string is a valid hash"""
    return bool(re.match(r'^[a-fA-F0-9]{40}$', hash_string))

def process_hashes(hashes: Union[str, List[str]], batch_size: int = 100) -> List[str]:
    """Process and validate a list of hashes"""
    if isinstance(hashes, str):
        hashes = [hashes]
    
    # Remove duplicates and invalid hashes
    return list(set(h.lower() for h in hashes if is_valid_hash(h)))

def format_torrent_status(active_torrents: List[Dict], download_stats: Tuple[int, int]) -> str:
    """
    Format torrent status information into a human-readable string.
    Shows both active downloads and recently completed downloads.
    
    Args:
        active_torrents: List of dictionaries containing torrent information
        download_stats: Tuple of (active_count, max_downloads)
    
    Returns:
        Formatted string containing torrent status information
    """
    active_count, max_downloads = download_stats
    status_lines = [f"Active Downloads: {active_count}/{max_downloads}"]
    
    # Split torrents into active and completed
    downloading_torrents = []
    completed_torrents = []
    
    for torrent in active_torrents:
        if torrent.get('progress', 0) == 100 and torrent.get('status', '').lower() == 'downloaded':
            completed_torrents.append(torrent)
        else:
            downloading_torrents.append(torrent)
    
    # Show active downloads
    if not downloading_torrents:
        status_lines.append("\nNo active downloads")
    else:
        status_lines.append("\nActive Downloads:")
        for torrent in downloading_torrents:
            filename = torrent.get('filename', 'Unknown')
            progress = torrent.get('progress', 0)
            status = torrent.get('status', 'unknown')
            status_lines.append(f"- {filename}")
            status_lines.append(f"  Progress: {progress}%, Status: {status}")
    
    # Show completed downloads
    if completed_torrents:
        status_lines.append("\nRecently Completed:")
        for torrent in completed_torrents:
            filename = torrent.get('filename', 'Unknown')
            status_lines.append(f"- {filename}")
    
    return "\n".join(status_lines)
