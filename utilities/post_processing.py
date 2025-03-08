import logging
from typing import Dict, Any, Optional
from datetime import datetime
import os
import subprocess
from settings import get_setting
from .downsub import main as downsub_main

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

def run_custom_script(item: Dict[str, Any]) -> None:
    """
    Run custom post-processing script if configured.
    
    Args:
        item (Dict[str, Any]): The media item that triggered the state change
    """
    if not get_setting('Custom Post-Processing', 'enable_custom_script', False):
        return
        
    script_path = get_setting('Custom Post-Processing', 'custom_script_path', '')
    if not script_path or not os.path.isfile(script_path):
        logging.warning(f"Custom script not found at: {script_path}")
        return
        
    try:
        # Get argument template
        args_template = get_setting('Custom Post-Processing', 'custom_script_args', '{title} {imdb_id}')
        
        # Format arguments with item data
        formatted_args = args_template.format(
            title=item.get('title', ''),
            year=item.get('year', ''),
            type=item.get('type', ''),
            imdb_id=item.get('imdb_id', ''),
            location_on_disk=item.get('location_on_disk', ''),
            original_path_for_symlink=item.get('original_path_for_symlink', ''),
            state=item.get('state', ''),
            version=item.get('version', '')
        )
        
        # Build command
        cmd = [script_path] + formatted_args.split()
        
        logging.info(f"Running custom script with args: {' '.join(cmd)}")
        
        # Run script
        subprocess.Popen(cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
    except Exception as e:
        logging.error(f"Failed to run custom script: {str(e)}")

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
            # Run CineSync for items
            run_cinesync(dict(fresh_item))
            
            # Run subtitle downloader
            try:
                logging.info("Running subtitle downloader - this may take some time if it has never been run.")
                # Get the file path based on collection management setting
                file_path = None
                if get_setting('File Management', 'file_collection_management') == 'Plex':
                    if fresh_item.get('location_on_disk'):
                        file_path = fresh_item['location_on_disk']
                else:
                    if fresh_item.get('original_path_for_symlink'):
                        file_path = fresh_item['original_path_for_symlink']
                
                # Run downsub with the specific file path
                downsub_main(file_path)
            except Exception as e:
                logging.error(f"Failed to run subtitle downloader: {str(e)}")
                logging.exception("Subtitle downloader traceback:")
                
            # Run custom script if enabled
            run_custom_script(dict(fresh_item))
        else:
            logging.warning(f"Unhandled state {state} in post-processing")

    except Exception as e:
        logging.error(f"Error in state post-processing for item {item.get('id')}: {str(e)}")
        logging.exception("Traceback:") 
