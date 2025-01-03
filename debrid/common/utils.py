import re
import logging
from typing import List, Union

# Common video file extensions
VIDEO_EXTENSIONS = [
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'webm', 'mpg', 'mpeg', 'm2ts', 'ts'
]

def is_video_file(filename: str) -> bool:
    """Check if a file is a video file based on its extension"""
    result = any(filename.lower().endswith(f'.{ext}') for ext in VIDEO_EXTENSIONS)
    logging.info(f"is_video_file check for {filename}: {result}")
    return result

def is_unwanted_file(filename: str) -> bool:
    """Check if a file is unwanted (e.g., sample files)"""
    result = 'sample' in filename.lower()
    logging.info(f"is_unwanted_file check for {filename}: {result}")
    return result

def extract_hash_from_magnet(magnet_link: str) -> str:
    """Extract hash from a magnet link"""
    try:
        # Try exact btih format first
        match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
        if match:
            return match.group(1).lower()
            
        # Try xt=urn:btih: format
        match = re.search(r'xt=urn:btih:([a-fA-F0-9]{40})', magnet_link)
        if match:
            return match.group(1).lower()
            
        # Try just finding any 40-char hex string
        match = re.search(r'[a-fA-F0-9]{40}', magnet_link)
        if match:
            return match.group(0).lower()
            
        raise ValueError("Could not find valid hash in magnet link")
    except Exception as e:
        logging.error(f"Error extracting hash from magnet link: {str(e)}")
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
