import logging
import os
import requests
from typing import Dict, Any, Optional
from settings import get_setting
from database.database_reading import get_media_item_by_id

def normalize_path_for_emby(path: str) -> str:
    """
    Normalize a path for Emby API by converting OS-specific separators to forward slashes.
    Emby API expects forward slashes regardless of OS.
    
    Args:
        path: The file path to normalize
        
    Returns:
        str: Normalized path with forward slashes
    """
    # First normalize according to OS, then convert to forward slashes for Emby
    return os.path.normpath(path).replace(os.path.sep, '/')

def get_emby_library_info(emby_url: str, headers: dict, file_path: str) -> Optional[Dict]:
    """
    Get the Emby library information for a given file path.
    
    Args:
        emby_url: Base URL for Emby server
        headers: Headers containing authentication
        file_path: Path to the media file
        
    Returns:
        Optional[Dict]: Library information if found, None otherwise
    """
    try:
        # Get all media folders from Emby
        response = requests.get(f"{emby_url}/Library/MediaFolders", headers=headers, timeout=30)
        if response.status_code != 200:
            logging.error(f"Failed to get Emby libraries. Status code: {response.status_code}")
            return None
            
        libraries = response.json().get('Items', [])
        normalized_file_path = file_path.replace('\\', '/')
        
        # Find which library contains our path
        for library in libraries:
            library_path = library.get('Path', '').replace('\\', '/')
            if normalized_file_path.startswith(library_path):
                return {
                    'Id': library.get('Id'),
                    'Path': library_path,
                    'Name': library.get('Name')
                }
                
        logging.warning(f"Could not find matching Emby library for path: {file_path}")
        return None
        
    except Exception as e:
        logging.error(f"Error getting Emby library info: {str(e)}")
        return None

def emby_update_item(item: Dict[str, Any]) -> bool:
    """
    Update Emby library for a specific item by scanning its directory.
    
    Args:
        item: Dictionary containing item details including location_on_disk
        
    Returns:
        bool: True if update was successful, False otherwise
    """
    try:
        emby_url = get_setting('Debug', 'emby_url', default='').rstrip('/')
        emby_token = get_setting('Debug', 'emby_token', default='')
        
        if not emby_url or not emby_token:
            logging.warning("Emby URL or token not configured")
            return False
            
        # Get the fresh item data from the database
        updated_item = get_media_item_by_id(item['id'])
        if not updated_item:
            logging.error(f"Could not get updated item from database for item {item['id']}")
            return False
            
        # Get the file location from the updated item
        file_location = updated_item['location_on_disk']
        logging.debug(f"Emby update - Item details: id={item.get('id')}, title={item.get('title')}, location={file_location}")
        
        if not file_location:
            logging.error(f"No file location provided in item: {item}")
            return False
            
        # Prepare headers with API key
        headers = {
            'X-Emby-Token': emby_token,
            'Content-Type': 'application/json'
        }
        
        # Normalize path for Emby API
        file_location = normalize_path_for_emby(file_location)
        
        # Make the API request
        refresh_url = f"{emby_url}/Library/Media/Updated"
        data = {
            'Updates': [{
                'Path': file_location,
                'UpdateType': 'Created'
            }]
        }
        
        response = requests.post(refresh_url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 204:  # Emby returns 204 No Content on success
            logging.info(f"Successfully triggered Emby refresh for: {file_location}")
            return True
        else:
            logging.error(f"Failed to trigger Emby refresh. Status code: {response.status_code}")
            return False
            
    except requests.exceptions.Timeout:
        logging.error("Timeout while trying to update Emby")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error updating Emby: {str(e)}")
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
        emby_url = get_setting('Debug', 'emby_url', default='').rstrip('/')
        emby_token = get_setting('Debug', 'emby_token', default='')
        
        if not emby_url or not emby_token:
            logging.warning("Emby URL or token not configured")
            return False
            
        # Prepare headers with API key
        headers = {
            'X-Emby-Token': emby_token,
            'Content-Type': 'application/json'
        }
        
        # Normalize path for Emby API
        item_path = normalize_path_for_emby(item_path)
        
        # Make the API request
        refresh_url = f"{emby_url}/Library/Media/Updated"
        data = {
            'Updates': [{
                'Path': item_path,
                'UpdateType': 'Deleted'
            }]
        }
        
        response = requests.post(refresh_url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 204:  # Emby returns 204 No Content on success
            logging.info(f"Successfully notified Emby about removed file: {item_path}")
            return True
        else:
            logging.error(f"Failed to notify Emby about removed file. Status code: {response.status_code}")
            return False
            
    except requests.exceptions.Timeout:
        logging.error("Timeout while trying to update Emby")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error updating Emby: {str(e)}")
        return False
    except Exception as e:
        logging.error(f"Error removing file from Emby: {str(e)}")
        return False 