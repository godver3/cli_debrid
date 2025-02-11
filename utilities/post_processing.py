import logging
from typing import Dict, Any, Optional
from datetime import datetime
import os
import subprocess
from settings import get_setting

def validate_cinesync_path(path: str) -> bool:
    """
    Validate that the CineSync path is properly configured.
    
    Args:
        path (str): Path to validate
        
    Returns:
        bool: True if path is valid, False otherwise
    """
    if not path:
        return False
        
    if not path.endswith('/main.py'):
        logging.warning("CineSync path must end with /main.py")
        return False
        
    if not os.path.isfile(path):
        logging.warning(f"CineSync main.py not found at: {path}")
        return False
        
    return True

def run_cinesync(item: Dict[str, Any]) -> None:
    """
    Run the CineSync MediaHub if configured.
    
    Args:
        item (Dict[str, Any]): The media item that triggered the state change
    """
    cinesync_path = get_setting('Debug', 'cinesync_path', '')
    
    if not validate_cinesync_path(cinesync_path):
        return
        
    try:
        # Build command with arguments
        cmd = ['python', cinesync_path]
        
        # Add file path based on collection management setting as positional argument
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            if item.get('location_on_disk'):
                cmd.append(item['location_on_disk'])
        else:
            if item.get('original_path_for_symlink'):
                cmd.append(item['original_path_for_symlink'])
                
        # Add IMDb ID if present
        if item.get('imdb_id'):
            cmd.extend(['--imdb', item['imdb_id']])
            
        logging.info(f"Running CineSync with args: {' '.join(cmd)}")
            
        # Run CineSync with arguments
        subprocess.Popen(cmd, 
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
    except Exception as e:
        logging.error(f"Failed to start CineSync MediaHub: {str(e)}")

def handle_state_change(item: Dict[str, Any]) -> None:
    """
    Handle any post-processing needed when an item enters a new state.
    Currently handles 'Collected' and 'Upgrading' states.
    
    Args:
        item (Dict[str, Any]): The media item that has entered a new state
    """
    try:
        item_id = item.get('id')
        if not item_id:
            logging.error("No item ID provided for state post-processing")
            return

        state = item.get('state')
        if not state:
            logging.error("No state provided for post-processing")
            return

        #logging.info(f"Running post-processing for {state} state - Item ID: {item_id}")
        
        # Get fresh item data from database to ensure we have latest state
        from database import get_media_item_by_id
        fresh_item = get_media_item_by_id(item_id)
        if not fresh_item:
            logging.error(f"Could not find item {item_id} in database for post-processing")
            return
            
        # Log state change details
        if state == 'Collected' or state == 'Upgrading':
            #logging.info(f"Item collected: {fresh_item.get('title')} ({fresh_item.get('type')}) - Version: {fresh_item.get('version')}")
            # Run CineSync for items
            run_cinesync(dict(fresh_item))
            pass
        else:
            logging.warning(f"Unhandled state {state} in post-processing")

    except Exception as e:
        logging.error(f"Error in state post-processing for item {item.get('id')}: {str(e)}")
        logging.exception("Traceback:") 