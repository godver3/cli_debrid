import logging
import os
import requests
from typing import Dict, Any, Optional, List
from utilities.settings import get_setting
from database.database_reading import get_media_item_by_id
import time
import json

def normalize_path_for_emby(path: str) -> str:
    """
    Normalize a path for Emby/Jellyfin API by converting OS-specific separators to forward slashes.
    Emby/Jellyfin API expects forward slashes regardless of OS.
    
    Args:
        path: The file path to normalize
        
    Returns:
        str: Normalized path with forward slashes
    """
    # First normalize according to OS, then convert to forward slashes for Emby/Jellyfin
    return os.path.normpath(path).replace(os.path.sep, '/')

def get_emby_library_info(emby_url: str, headers: dict, file_path: str) -> Optional[Dict]:
    """
    Get the Emby/Jellyfin library information for a given file path.
    
    Args:
        emby_url: Base URL for Emby/Jellyfin server
        headers: Headers containing authentication
        file_path: Path to the media file
        
    Returns:
        Optional[Dict]: Library information if found, None otherwise
    """
    try:
        # Get all media folders from Emby/Jellyfin
        response = requests.get(f"{emby_url}/Library/MediaFolders", headers=headers, timeout=30)
        if response.status_code != 200:
            logging.error(f"Failed to get Emby/Jellyfin libraries. Status code: {response.status_code}")
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
                
        logging.warning(f"Could not find matching Emby/Jellyfin library for path: {file_path}")
        return None
        
    except Exception as e:
        logging.error(f"Error getting Emby/Jellyfin library info: {str(e)}")
        return None

def emby_update_item(item: Dict[str, Any]) -> bool:
    """
    Update Emby/Jellyfin library for a specific item by scanning its directory.
    
    Args:
        item: Dictionary containing item details including location_on_disk
        
    Returns:
        bool: True if update was successful, False otherwise
    """
    try:
        emby_url = get_setting('Debug', 'emby_jellyfin_url', default='').rstrip('/')
        emby_token = get_setting('Debug', 'emby_jellyfin_token', default='')
        
        if not emby_url or not emby_token:
            logging.warning("Emby/Jellyfin URL or token not configured")
            return False
            
        # Get the fresh item data from the database
        updated_item = get_media_item_by_id(item['id'])
        if not updated_item:
            logging.error(f"Could not get updated item from database for item {item['id']}")
            return False
            
        # Get the file location from the updated item
        file_location = updated_item['location_on_disk']
        logging.debug(f"Emby/Jellyfin update - Item details: id={item.get('id')}, title={item.get('title')}, location={file_location}")
        
        if not file_location:
            logging.error(f"No file location provided in item: {item}")
            return False
            
        # Prepare headers with API key
        headers = {
            'X-Emby-Token': emby_token,
            'Content-Type': 'application/json'
        }
        
        # Normalize path for Emby/Jellyfin API
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
        
        if response.status_code == 204:  # Emby/Jellyfin returns 204 No Content on success
            logging.info(f"Successfully triggered Emby/Jellyfin refresh for: {file_location}")
            return True
        else:
            logging.error(f"Failed to trigger Emby/Jellyfin refresh. Status code: {response.status_code}")
            return False
            
    except requests.exceptions.Timeout:
        logging.error("Timeout while trying to update Emby/Jellyfin")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error updating Emby/Jellyfin: {str(e)}")
        return False
    except Exception as e:
        logging.error(f"Error updating item in Emby/Jellyfin: {str(e)}")
        return False

def remove_file_from_emby(item_title: str, item_path: str, episode_title: str = None) -> bool:
    """
    Remove a file from Emby/Jellyfin's library.
    
    Args:
        item_title: The title of the show or movie
        item_path: The full path to the file
        episode_title: Optional episode title for TV shows
        
    Returns:
        bool: True if removal was successful, False otherwise
    """
    try:
        emby_url = get_setting('Debug', 'emby_jellyfin_url', default='').rstrip('/')
        emby_token = get_setting('Debug', 'emby_jellyfin_token', default='')
        
        if not emby_url or not emby_token:
            logging.warning("Emby/Jellyfin URL or token not configured")
            return False
            
        # Prepare headers with API key
        headers = {
            'X-Emby-Token': emby_token,
            'Content-Type': 'application/json'
        }
        
        # Normalize path for Emby/Jellyfin API
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
        
        if response.status_code == 204:  # Emby/Jellyfin returns 204 No Content on success
            logging.info(f"Successfully notified Emby/Jellyfin about removed file: {item_path}")
            return True
        else:
            logging.error(f"Failed to notify Emby/Jellyfin about removed file. Status code: {response.status_code}")
            return False
            
    except requests.exceptions.Timeout:
        logging.error("Timeout while trying to update Emby/Jellyfin")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error updating Emby/Jellyfin: {str(e)}")
        return False
    except Exception as e:
        logging.error(f"Error removing file from Emby/Jellyfin: {str(e)}")
        return False 

# Helper function to extract IMDb/TMDb IDs
def _extract_ids(provider_ids: Optional[Dict[str, str]]) -> tuple[Optional[str], Optional[str]]:
    imdb_id = None
    tmdb_id = None
    if provider_ids:
        imdb_id = provider_ids.get('Imdb')
        tmdb_id = provider_ids.get('Tmdb')
    return imdb_id, tmdb_id

# Helper function to fetch items from Emby/Jellyfin API
def _fetch_emby_items(session: requests.Session, base_url: str, headers: dict, user_id: str, params: dict) -> Optional[List[Dict]]:
    """Fetches items from the Emby/Jellyfin API with error handling."""
    default_params = {
        'userId': user_id,
        'Fields': 'Path,ProviderIds,DateCreated,ProductionYear,Genres,ParentIndexNumber,IndexNumber,PremiereDate,MediaSources',
    }
    full_params = {**default_params, **params}
    
    try:
        response = session.get(f"{base_url}/Users/{user_id}/Items", headers=headers, params=full_params, timeout=60)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        return data.get('Items', [])
    except requests.exceptions.Timeout:
        logging.error(f"Timeout fetching Emby/Jellyfin items with params: {params}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching Emby/Jellyfin items: {e}. Params: {params}")
    except json.JSONDecodeError:
        logging.error(f"Error decoding JSON response from Emby/Jellyfin. Params: {params}")
    return None

# Helper function to process a movie item
def _process_emby_movie(movie_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Processes a single Emby/Jellyfin movie item and returns a formatted dictionary."""
    try:
        imdb_id, tmdb_id = _extract_ids(movie_item.get('ProviderIds'))
        path = None
        if movie_item.get('MediaSources'):
            path = movie_item['MediaSources'][0].get('Path')
        elif movie_item.get('Path'):
            path = movie_item.get('Path')

        if not path:
            logging.warning(f"No path found for movie: {movie_item.get('Name')}")
            return None

        return {
            'title': movie_item.get('Name'),
            'year': movie_item.get('ProductionYear'),
            'addedAt': movie_item.get('DateCreated'), # Note: Emby/Jellyfin 'DateCreated' is item creation, not file addition like Plex 'addedAt'
            'guid': movie_item.get('Id'), # Using Item ID as a unique key
            'ratingKey': movie_item.get('Id'),
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'type': 'movie',
            'genres': movie_item.get('Genres', []),
            'release_date': movie_item.get('PremiereDate', '')[:10] if movie_item.get('PremiereDate') else None,
            'location': normalize_path_for_emby(path) # Ensure path is normalized
        }
    except Exception as e:
        logging.error(f"Error processing Emby/Jellyfin movie item {movie_item.get('Id')}: {e}")
        return None

# Helper function to process an episode item
def _process_emby_episode(episode_item: Dict[str, Any], show_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Processes a single Emby/Jellyfin episode item and returns a formatted dictionary."""
    try:
        show_imdb_id, show_tmdb_id = _extract_ids(show_item.get('ProviderIds'))
        ep_imdb_id, ep_tmdb_id = _extract_ids(episode_item.get('ProviderIds'))

        path = None
        if episode_item.get('MediaSources'):
            path = episode_item['MediaSources'][0].get('Path')
        elif episode_item.get('Path'):
             path = episode_item.get('Path')
             
        if not path:
            logging.warning(f"No path found for episode: {show_item.get('Name')} S{episode_item.get('ParentIndexNumber')}E{episode_item.get('IndexNumber')}")
            return None

        return {
            'title': show_item.get('Name'),
            'episode_title': episode_item.get('Name'),
            'season_number': episode_item.get('ParentIndexNumber'),
            'episode_number': episode_item.get('IndexNumber'),
            'year': show_item.get('ProductionYear'),
            'show_year': show_item.get('ProductionYear'),
            'addedAt': episode_item.get('DateCreated'),
            'guid': episode_item.get('Id'),
            'ratingKey': episode_item.get('Id'),
            'release_date': episode_item.get('PremiereDate', '')[:10] if episode_item.get('PremiereDate') else None,
            'imdb_id': show_imdb_id,
            'tmdb_id': show_tmdb_id,
            'episode_imdb_id': ep_imdb_id or show_imdb_id, # Fallback to show ID if episode specific is missing
            'episode_tmdb_id': ep_tmdb_id or show_tmdb_id, # Fallback to show ID
            'type': 'episode',
            'genres': show_item.get('Genres', []),
            'location': normalize_path_for_emby(path) # Ensure path is normalized
        }
    except Exception as e:
        logging.error(f"Error processing Emby/Jellyfin episode item {episode_item.get('Id')} for show {show_item.get('Id')}: {e}")
        return None

def get_collected_from_emby(progress_callback=None, bypass=False) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """
    Gather all collected items (movies and episodes) from Emby/Jellyfin server libraries.

    Args:
        progress_callback: Optional callback function for progress updates.
        bypass: If True, ignore library settings and scan all movie/show libraries.

    Returns:
        A dictionary containing 'movies' and 'episodes' lists, or None on failure.
    """
    start_time = time.time()
    logging.info("Starting Emby/Jellyfin content collection.")
    if progress_callback: progress_callback('scanning', 'Connecting to Emby/Jellyfin server...')

    emby_url = get_setting('Debug', 'emby_jellyfin_url', default='').rstrip('/')
    emby_token = get_setting('Debug', 'emby_jellyfin_token', default='')

    if not emby_url or not emby_token:
        logging.error("Emby/Jellyfin URL or token not configured.")
        if progress_callback: progress_callback('error', 'Emby/Jellyfin URL or token not configured.')
        return None

    headers = {
        'X-Emby-Token': emby_token,
        'Accept': 'application/json'
    }

    all_movies = []
    all_episodes = []

    with requests.Session() as session:
        try:
            # 1. Get User ID
            if progress_callback: progress_callback('scanning', 'Fetching user information...')
            users_response = session.get(f"{emby_url}/Users", headers=headers, params={'isHidden': False, 'isDisabled': False}, timeout=30)
            users_response.raise_for_status()
            users = users_response.json()
            if not users:
                logging.error("No active users found on Emby/Jellyfin server.")
                if progress_callback: progress_callback('error', 'No active users found.')
                return None
            user_id = users[0]['Id'] # Use the first active user
            logging.info(f"Using Emby/Jellyfin User ID: {user_id}")

            # 2. Get Libraries (Media Folders represent the roots)
            if progress_callback: progress_callback('scanning', 'Retrieving library sections...')
            libs_response = session.get(f"{emby_url}/Library/MediaFolders", headers=headers, timeout=30)
            libs_response.raise_for_status()
            # The actual library items (like 'Movies', 'TV Shows') are often under '/Users/{userId}/Items' with no ParentId
            # Let's fetch top-level items to find CollectionFolders which represent libraries
            root_items_response = session.get(f"{emby_url}/Users/{user_id}/Items", headers=headers, params={'IncludeItemTypes': 'CollectionFolder'}, timeout=30)
            root_items_response.raise_for_status()
            library_folders = root_items_response.json().get('Items', [])

            libraries_by_id = {lib['Id']: lib for lib in library_folders}
            libraries_by_name = {lib['Name']: lib['Id'] for lib in library_folders}
            
            # 3. Filter Libraries
            target_movie_lib_ids = set()
            target_show_lib_ids = set()

            if bypass:
                for lib_id, lib_data in libraries_by_id.items():
                    # Guess type based on common names, as CollectionFolder doesn't have a strict type
                    name_lower = lib_data.get('Name', '').lower()
                    # Check CollectionType if available (more reliable)
                    collection_type = lib_data.get('CollectionType') 
                    if collection_type == 'movies' or 'movie' in name_lower:
                         target_movie_lib_ids.add(lib_id)
                    elif collection_type == 'tvshows' or 'tv' in name_lower or 'show' in name_lower:
                         target_show_lib_ids.add(lib_id)
            else:
                 # TODO: Add settings keys for Emby/Jellyfin libraries if needed
                 # movie_lib_names = get_setting('Emby', 'movie_libraries', '').split(',')
                 # show_lib_names = get_setting('Emby', 'shows_libraries', '').split(',')
                 # For now, assume bypass=True logic or implement setting retrieval
                 logging.warning("Bypassing library settings (or settings not implemented yet). Scanning all detected Movie/TV libraries.")
                 for lib_id, lib_data in libraries_by_id.items():
                    name_lower = lib_data.get('Name', '').lower()
                    collection_type = lib_data.get('CollectionType')
                    if collection_type == 'movies' or 'movie' in name_lower:
                         target_movie_lib_ids.add(lib_id)
                    elif collection_type == 'tvshows' or 'tv' in name_lower or 'show' in name_lower:
                         target_show_lib_ids.add(lib_id)

            logging.info(f"Movie libraries to process: {[libraries_by_id[lid]['Name'] for lid in target_movie_lib_ids]}")
            logging.info(f"TV Show libraries to process: {[libraries_by_id[lid]['Name'] for lid in target_show_lib_ids]}")

            # 4. Fetch Movies
            total_movies_processed = 0
            if progress_callback: progress_callback('scanning', f'Retrieving items from {len(target_movie_lib_ids)} movie libraries...')
            for lib_id in target_movie_lib_ids:
                 lib_name = libraries_by_id[lib_id]['Name']
                 logging.info(f"Fetching movies from library: {lib_name} ({lib_id})")
                 movie_items = _fetch_emby_items(session, emby_url, headers, user_id, params={'ParentId': lib_id, 'IncludeItemTypes': 'Movie', 'Recursive': True})
                 if movie_items:
                     logging.info(f"Found {len(movie_items)} movies in {lib_name}.")
                     for movie_item in movie_items:
                         processed_movie = _process_emby_movie(movie_item)
                         if processed_movie:
                             all_movies.append(processed_movie)
                         total_movies_processed += 1
                         if progress_callback and total_movies_processed % 50 == 0:
                             progress_callback('scanning', f'Processed {total_movies_processed} movie items...')
                 else:
                     logging.warning(f"No movies found or error fetching from library: {lib_name}")

            # 5. Fetch Shows and Episodes
            total_shows_processed = 0
            total_episodes_found = 0
            if progress_callback: progress_callback('scanning', f'Retrieving items from {len(target_show_lib_ids)} TV show libraries...')
            for lib_id in target_show_lib_ids:
                lib_name = libraries_by_id[lib_id]['Name']
                logging.info(f"Fetching shows from library: {lib_name} ({lib_id})")
                show_items = _fetch_emby_items(session, emby_url, headers, user_id, params={'ParentId': lib_id, 'IncludeItemTypes': 'Series', 'Recursive': True})
                
                if show_items:
                    logging.info(f"Found {len(show_items)} shows in {lib_name}.")
                    for show_item in show_items:
                        show_id = show_item['Id']
                        show_name = show_item.get('Name', 'Unknown Show')
                        logging.debug(f"Processing show: {show_name} ({show_id})")
                        
                        # Fetch episodes directly for the show - Recursive should cover seasons
                        episode_items = _fetch_emby_items(session, emby_url, headers, user_id, params={'ParentId': show_id, 'IncludeItemTypes': 'Episode', 'Recursive': True})
                        
                        if episode_items:
                             logging.debug(f"Found {len(episode_items)} episodes for show {show_name}.")
                             for episode_item in episode_items:
                                 processed_episode = _process_emby_episode(episode_item, show_item)
                                 if processed_episode:
                                     all_episodes.append(processed_episode)
                                     total_episodes_found +=1
                        else:
                             logging.debug(f"No episodes found or error fetching for show: {show_name}")
                             
                        total_shows_processed += 1
                        if progress_callback and total_shows_processed % 10 == 0:
                             progress_callback('scanning', f'Processed {total_shows_processed} shows ({total_episodes_found} episodes found)...')
                else:
                    logging.warning(f"No shows found or error fetching from library: {lib_name}")

            end_time = time.time()
            logging.info(f"Emby/Jellyfin collection complete. Found {len(all_movies)} movies and {len(all_episodes)} episodes.")
            logging.info(f"Total time: {end_time - start_time:.2f} seconds")
            
            if progress_callback: 
                progress_callback('complete', 'Scan complete', {
                    'movies_found': len(all_movies),
                    'episodes_found': len(all_episodes)
                })

            return {'movies': all_movies, 'episodes': all_episodes}

        except requests.exceptions.RequestException as e:
            logging.error(f"HTTP Error during Emby/Jellyfin scan: {e}")
            if progress_callback: progress_callback('error', f'HTTP Error: {e}')
            return None
        except Exception as e:
            logging.error(f"Unexpected error during Emby/Jellyfin scan: {e}", exc_info=True)
            if progress_callback: progress_callback('error', f'Unexpected Error: {e}')
            return None 