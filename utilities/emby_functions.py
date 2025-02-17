import logging
import os
import requests
from typing import Dict, Any
from settings import get_setting

def emby_update_item(item: Dict[str, Any]) -> bool:
    """
    Update Emby library for a specific item by scanning its directory.
    
    Args:
        item: Dictionary containing item details including location_on_disk
        
    Returns:
        bool: True if update was successful, False otherwise
    """
    try:
        emby_url = get_setting('Debug', 'emby_url', default='')
        emby_token = get_setting('Debug', 'emby_token', default='')
        
        if not emby_url or not emby_token:
            logging.warning("Emby URL or token not configured")
            return False
            
        # Get the file location from the item
        file_location = item.get('location_on_disk')
        if not file_location:
            logging.error("No file location provided in item")
            return False
            
        # Get the directory containing the file
        directory = os.path.dirname(file_location)
        
        # Construct the Emby API URL for refreshing the path
        refresh_url = f"{emby_url.rstrip('/')}/Library/Media/Updated"
        
        # Prepare headers with API key
        headers = {
            'X-Emby-Token': emby_token,
            'Content-Type': 'application/json'
        }
        
        # Prepare the request data
        data = {
            'Updates': [{
                'Path': directory,
                'UpdateType': 'Modified'
            }]
        }
        
        # Make the API request
        response = requests.post(refresh_url, headers=headers, json=data)
        
        if response.status_code == 204:  # Emby returns 204 No Content on success
            logging.info(f"Successfully triggered Emby refresh for directory: {directory}")
            return True
        else:
            logging.error(f"Failed to trigger Emby refresh. Status code: {response.status_code}, Response: {response.text}")
            return False
            
    except Exception as e:
        logging.error(f"Error updating item in Emby: {str(e)}")
        return False

def remove_file_from_emby(item_title: str, item_path: str, episode_title: str = None) -> bool:
    """
    Remove a file from Emby's library.
    
    Args:
        item_title: The title of the show or movie
        item_path: The full path to the file
        episode_title: Optional episode title for TV shows
        
    Returns:
        bool: True if removal was successful, False otherwise
    """
    try:
        emby_url = get_setting('Debug', 'emby_url', default='')
        emby_token = get_setting('Debug', 'emby_token', default='')
        
        if not emby_url or not emby_token:
            logging.warning("Emby URL or token not configured")
            return False
            
        # Construct the Emby API URL for refreshing the path
        refresh_url = f"{emby_url.rstrip('/')}/Library/Media/Updated"
        
        # Prepare headers with API key
        headers = {
            'X-Emby-Token': emby_token,
            'Content-Type': 'application/json'
        }
        
        # Get the directory containing the file
        directory = os.path.dirname(item_path)
        
        # Prepare the request data
        data = {
            'Updates': [{
                'Path': directory,
                'UpdateType': 'Deleted'
            }]
        }
        
        # Make the API request
        response = requests.post(refresh_url, headers=headers, json=data)
        
        if response.status_code == 204:  # Emby returns 204 No Content on success
            logging.info(f"Successfully notified Emby about removed file: {item_path}")
            return True
        else:
            logging.error(f"Failed to notify Emby about removed file. Status code: {response.status_code}, Response: {response.text}")
            return False
            
    except Exception as e:
        logging.error(f"Error removing file from Emby: {str(e)}")
        return False 