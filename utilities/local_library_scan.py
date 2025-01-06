import logging
from typing import List, Dict, Any
import os

def local_library_scan() -> List[Dict[str, Any]]:
    """
    Scan local library for collected media files when symlink_collected_files is enabled.
    This is used as an alternative to Plex scanning when working with symlinked files.
    Runs regularly as part of program operations, intended to catch items missed by the targeted/
    recently added scans.
    
    Returns:
        List[Dict[str, Any]]: List of collected media items with their metadata
    """
    try:
        # TODO: Implement local library scanning logic here
        # This should:
        # 1. Scan configured media directories
        # 2. Identify newly added/collected files
        # 3. Extract relevant metadata
        # 4. Return list of collected items in same format as Plex scanning
        
        logging.info("Performing local library scan for collected files")
        return []
        
    except Exception as e:
        logging.error(f"Error during local library scan: {e}", exc_info=True)
        return [] 
    
def recent_local_library_scan():
    """
    Perform a recent local library scan for collected files. Check for most recenty 500 files
    to see if they have been symlinked yet.
    """
    logging.info("Performing recent local library scan for collected files")
    return []

def check_local_file_for_item(item: Dict[str, Any]) -> bool:
    """
    Check if the local file for the item exists
    """
    return os.path.exists(item['local_file_path'])