import logging
import os
from plexapi.server import PlexServer
from settings import get_setting
from typing import Dict, List, Any, Optional
from database.core import get_db_connection
import requests
import re
import time  # Add time import for sleep
import urllib.parse

def force_match_with_tmdb(title: str, year: str, tmdb_id: str, plex_rating_key: Optional[str] = None) -> bool:
    """
    Force matches a Plex item with a specific TMDB ID using the direct API endpoint.
    If the item contains multiple movies (based on our database records), it will attempt to split them first.
    
    Args:
        title (str): The title of the item to match (this is actually the filename)
        tmdb_id (str): The TMDB ID to match with
        plex_rating_key (Optional[str]): The Plex rating key if we already know the item
        
    Returns:
        bool: True if successful, False otherwise
    """
    
    try:
        # Get Plex connection details from settings
        baseurl = get_setting("Plex", "url", default="http://localhost:32400")
        token = get_setting("Plex", "token")
        
        if not token:
            logging.error("No Plex token found in settings")
            return False
            
        if not plex_rating_key:
            logging.error("No Plex rating key provided for item")
            return False
            
        logging.info(f"Attempting to match item with rating key: {plex_rating_key}")
        
        # Clean up the base URL
        baseurl = baseurl.rstrip('/')
        if ':' in baseurl and not baseurl.endswith('/'):
            # If we have a port number, make sure we have a slash after it
            baseurl = f"{baseurl}/"
            
        # Connect to Plex
        plex = PlexServer(baseurl, token)
        
        # Get the item by rating key
        try:
            item = plex.fetchItem(int(plex_rating_key))
        except Exception as e:
            logging.error(f"Failed to fetch item with rating key {plex_rating_key}: {str(e)}")
            return False

        # Set up headers for all requests
        headers = {
            'X-Plex-Token': token,
            'Accept': 'application/json'
        }

        # First check if we need to split this item
        if hasattr(item, 'media'):
            # Get all file locations from this item
            file_locations = []
            for media in item.media:
                for part in media.parts:
                    if hasattr(part, 'file'):
                        file_locations.append(part.file)
            
            if len(file_locations) > 0:
                logging.info(f"Found {len(file_locations)} file(s) in Plex item")
                
                # Check our database for each file
                conn = get_db_connection()
                cursor = conn.cursor()
                
                try:
                    # First, check if any of these files should be matched with our target TMDB ID
                    target_file = None
                    other_files = []
                    
                    for location in file_locations:
                        filename = os.path.basename(location)
                        cursor.execute('''
                            SELECT DISTINCT tmdb_id, title, year 
                            FROM media_items 
                            WHERE filled_by_file LIKE ? 
                            AND tmdb_id IS NOT NULL 
                            AND state IN ('Collected', 'Upgrading', 'Checking')
                        ''', (f'%{filename}%',))
                        db_item = cursor.fetchone()
                        
                        if db_item and str(db_item['tmdb_id']) == str(tmdb_id):
                            target_file = location
                        else:
                            other_files.append(location)
                    
                    if target_file and len(other_files) > 0:
                        logging.info(f"Found target file and {len(other_files)} other files that need to be split")
                        
                        # Split the item
                        split_url = f"{baseurl}/library/metadata/{plex_rating_key}/split"
                        response = requests.put(split_url, headers=headers)
                        
                        if response.status_code == 200:
                            logging.info("Successfully initiated split operation")
                            time.sleep(5)  # Give Plex time to process the split
                            
                            # Get all items in the library
                            library_section = item.section()
                            all_items = library_section.all()
                            
                            # Find our target item and other items
                            target_item = None
                            items_to_combine = []
                            
                            for plex_item in all_items:
                                if hasattr(plex_item, 'media'):
                                    for media in plex_item.media:
                                        for part in media.parts:
                                            if part.file == target_file:
                                                target_item = plex_item
                                                break
                                            elif part.file in other_files:
                                                items_to_combine.append(plex_item)
                                                break
                            
                            # Combine the other items if needed
                            if len(items_to_combine) > 1:
                                base_item = items_to_combine[0]
                                try:
                                    # Get the rating keys of all items to merge (except the base item)
                                    rating_keys = [item.ratingKey for item in items_to_combine[1:]]
                                    logging.info(f"Attempting to merge items with rating keys {rating_keys} into {base_item.ratingKey}")
                                    
                                    # Use the PlexAPI merge function
                                    base_item.merge(rating_keys)
                                    logging.info(f"Successfully combined items")
                                    time.sleep(2)  # Give Plex time to process
                                except Exception as e:
                                    logging.error(f"Error combining items: {str(e)}")
                            
                            if target_item:
                                # Update our item reference to the split item we want to match
                                item = target_item
                            else:
                                logging.error("Could not find target item after split")
                                return False
                        else:
                            logging.warning(f"Failed to split item: HTTP {response.status_code}")
                finally:
                    cursor.close()
                    conn.close()
            
        # Try direct match with TMDB ID without unmatching first
        try:
            # Get the agent from the library section
            is_tv_show = bool(item.type == 'episode')
            library_section = item.section()
            agent = library_section.agent
            logging.info(f"Using agent {agent} from library section '{library_section.title}'")
            
            # For TV shows, we need to work with the series (grandparent) instead of the episode
            if is_tv_show:
                if not hasattr(item, 'grandparentRatingKey'):
                    logging.error("Episode missing grandparentRatingKey")
                    return False
                plex_rating_key = str(item.grandparentRatingKey)
                item = plex.fetchItem(int(plex_rating_key))
                logging.info(f"Working with TV series '{item.title}' (rating key: {plex_rating_key})")
            
            # Check current TMDB ID if any
            current_tmdb_id = None
            for guid in getattr(item, 'guids', []):
                guid_str = str(guid)
                if 'tmdb://' in guid_str:
                    match = re.search(r'tmdb://(?:tv/)?(\d+)', guid_str)
                    if match:
                        current_tmdb_id = match.group(1)
                        break
            
            # Try direct match first unless disabled
            guid = f'tmdb://{tmdb_id}' if not is_tv_show else f'tmdb://tv/{tmdb_id}'
            
            try:
                # Get available matches
                matches = item.matches(title=title, year=year)
                logging.info(f"Found {len(matches)} potential matches")
                
                # Log all matches for debugging
                for idx, match in enumerate(matches, 1):
                    logging.info(f"Match {idx}:")
                    logging.info(f"  - Title: {getattr(match, 'title', 'N/A')}")
                    logging.info(f"  - Year: {getattr(match, 'year', 'N/A')}")
                    logging.info(f"  - GUID: {getattr(match, 'guid', 'N/A')}")
                    logging.info(f"  - Score: {getattr(match, 'score', 'N/A')}")
                    logging.info(f"  - Name: {getattr(match, 'name', 'N/A')}")
                    # Log any additional useful attributes
                    for attr in dir(match):
                        if not attr.startswith('_') and attr not in ['title', 'year', 'guid', 'score', 'name']:
                            value = getattr(match, attr, None)
                            if value is not None:
                                logging.info(f"  - {attr}: {value}")
                
                # Try to find an exact name/year match first
                target_match = None
                for match in matches:
                    match_name = getattr(match, 'name', '')
                    match_year = str(getattr(match, 'year', ''))
                    
                    logging.info(f"Comparing '{match_name}' ({match_year}) with '{title}' ({year})")
                    
                    # Check for exact name match and year match if provided
                    if match_name and match_name.lower() == title.lower():
                        if not year or match_year == year:
                            target_match = match
                            logging.info(f"Found exact name/year match: {match_name} ({match_year})")
                            break
                
                # If no exact match found, take the first result that has a year
                if not target_match and matches:
                    # Filter for matches that have a year
                    valid_matches = [m for m in matches if getattr(m, 'year', '')]
                    if valid_matches:
                        target_match = valid_matches[0]
                        match_name = getattr(target_match, 'name', '')
                        match_year = getattr(target_match, 'year', '')
                        logging.info(f"No exact match found, using first result with year: {match_name} ({match_year})")
                    else:
                        logging.warning("No matches found with valid year information")
                
                # Apply the match if we found one
                if target_match:
                    logging.info(f"Applying match with: {getattr(target_match, 'name', 'N/A')} ({getattr(target_match, 'year', 'N/A')})")
                    item.fixMatch(searchResult=target_match)
                    time.sleep(2)
                else:
                    logging.error("No matches found")
                    return False
                
                # Verify the match
                max_wait = 30  # Maximum seconds to wait
                interval = 2   # Check every 2 seconds
                start_time = time.time()
                
                while time.time() - start_time < max_wait:
                    # Refresh the item to get updated metadata
                    item.reload()
                    guids = getattr(item, 'guids', [])
                    
                    # Log current metadata state
                    logging.info(f"Metadata state at {int(time.time() - start_time)}s:")
                    logging.info(f"  - Title: {item.title}")
                    logging.info(f"  - Year: {getattr(item, 'year', 'N/A')}")
                    logging.info(f"  - All GUIDs: {guids}")
                    
                    # Check if we have the correct TMDB ID
                    matched_tmdb_id = None
                    for guid_obj in guids:
                        guid_str = str(guid_obj)
                        if 'tmdb://' in guid_str:
                            match = re.search(r'tmdb://(?:tv/)?(\d+)', guid_str)
                            if match:
                                matched_tmdb_id = match.group(1)
                                break
                    
                    if matched_tmdb_id:
                        if matched_tmdb_id == str(tmdb_id):
                            logging.info(f"Successfully verified TMDB ID match: {matched_tmdb_id}")
                            return True
                        else:
                            logging.warning(f"Incorrect TMDB ID match: got {matched_tmdb_id}, expected {tmdb_id}")
                            if time.time() - start_time >= max_wait - interval:
                                return False
                    
                    time.sleep(interval)
                
                logging.error("Failed to verify TMDB ID in metadata after waiting")
                return False
                
            except Exception as e:
                logging.error(f"Failed to match item: {str(e)}")
                return False
            
        except Exception as e:
            logging.error(f"Failed to fix match using API: {str(e)}")
            return False
            
    except Exception as e:
        logging.error(f"Error force matching item in Plex: {str(e)}")
        return False

def is_item_matched(item: Dict[str, Any]) -> bool:
    """
    Check if a Plex item has required metadata and is correctly matched.
    
    Args:
        item: Dictionary containing item information from Plex
        
    Returns:
        bool: True if item has required metadata and is correctly matched, False otherwise
    """
    try:
        # First check if item has minimum required metadata
        has_id = bool(item.get('imdb_id') or item.get('tmdb_id'))
        if not has_id:
            logging.info(f"Item missing required ID metadata: {item.get('title')}")
            return False
            
        # For episodes, check additional required metadata
        if item.get('type') == 'episode':
            if not all(item.get(field) for field in ['season_number', 'episode_number']):
                logging.info(f"Episode missing required season/episode metadata: {item.get('title')}")
                return False
                
        # Get the location/filename from the Plex item
        location = item.get('location')
        if not location:
            logging.info(f"Item has no location: {item.get('title')}")
            return False
            
        # Get the basename for comparison
        plex_filename = os.path.basename(location)
        
        # Check against our database items in relevant states
        conn = get_db_connection()
        cursor = conn.execute('''
            SELECT id, filled_by_file, imdb_id, tmdb_id, state
            FROM media_items
            WHERE state IN ('Collected', 'Upgrading', 'Checking')
            AND (
                filled_by_file LIKE ? 
                OR location_on_disk LIKE ?
            )
        ''', (f'%{plex_filename}', f'%{plex_filename}'))
        
        matching_items = cursor.fetchall()
        conn.close()
        
        if not matching_items:
            logging.info(f"No matching items found in database for file: {plex_filename}")
            return True  # Item is matched but not in our database yet
            
        # Check if any matching items have matching IDs
        for db_item in matching_items:
            # Compare IMDb IDs if available
            if item.get('imdb_id') and db_item['imdb_id']:
                if item['imdb_id'] == db_item['imdb_id']:
                    logging.info(f"Correct match found by IMDb ID for {plex_filename} (state: {db_item['state']})")
                    return True
                else:
                    logging.warning(f"IMDb ID mismatch for {plex_filename} - Plex: {item['imdb_id']}, DB: {db_item['imdb_id']}")
                    return False
                    
            # Compare TMDB IDs if available
            if item.get('tmdb_id') and db_item['tmdb_id']:
                if item['tmdb_id'] == db_item['tmdb_id']:
                    logging.info(f"Correct match found by TMDB ID for {plex_filename} (state: {db_item['state']})")
                    return True
                else:
                    logging.warning(f"TMDB ID mismatch for {plex_filename} - Plex: {item['tmdb_id']}, DB: {db_item['tmdb_id']}")
                    return False
                    
        # If we found matching filenames but no matching IDs, it's incorrectly matched
        logging.warning(f"Found file matches but no ID matches for {plex_filename}")
        return False
        
    except Exception as e:
        logging.error(f"Error checking if item is matched: {str(e)}")
        return False

def check_and_fix_unmatched_items(collected_content: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Check collected content for unmatched items and attempt to fix them.
    
    Args:
        collected_content: Dictionary containing 'movies' and 'episodes' lists
        
    Returns:
        Dict with matched content
    """
    try:
        matched_movies = []
        matched_episodes = []
        
        # Process movies
        for movie in collected_content.get('movies', []):
            if not is_item_matched(movie):
                # Get the actual filename from the Plex item
                plex_filename = os.path.basename(movie.get('location', ''))
                plex_title = movie.get('title', '')
                logging.info(f"Found unmatched movie: {plex_title} (file: {plex_filename})")
                
                # Check if we have a TMDB ID from our database for this file
                conn = get_db_connection()
                cursor = conn.execute('''
                    SELECT tmdb_id, year, title
                    FROM media_items 
                    WHERE filled_by_file LIKE ? 
                    AND tmdb_id IS NOT NULL 
                    AND state IN ('Collected', 'Upgrading', 'Checking')
                ''', (f'%{plex_filename}',))
                db_item = cursor.fetchone()
                conn.close()
                
                if db_item and db_item['tmdb_id']:
                    logging.info(f"Found TMDB ID {db_item['tmdb_id']} in database for {plex_filename}")
                    # Get title from database or fallback to cleaned filename
                    clean_title = db_item['title']
                    if not clean_title:
                        logging.info(f"No title in database for {plex_filename}, using cleaned filename")
                        clean_title = os.path.splitext(plex_filename)[0]
                        # Remove common suffixes and quality indicators
                        clean_title = re.sub(r'\.(19|20)\d{2}.*$', '', clean_title)  # Remove year and everything after
                        clean_title = re.sub(r'[._]', ' ', clean_title)  # Replace dots and underscores with spaces
                        clean_title = re.sub(r'\s+', ' ', clean_title)  # Normalize spaces
                        clean_title = clean_title.strip()
                        
                    if not clean_title:  # If we still don't have a title
                        clean_title = plex_title if plex_title else plex_filename
                        
                    logging.info(f"Using title for match: '{clean_title}'")
                    
                    # Use year from database or extract from filename
                    year = ''
                    if db_item['year'] is not None:
                        year = str(db_item['year'])
                    else:
                        # Try to extract year from filename
                        year_match = re.search(r'(19|20)\d{2}', plex_filename)
                        if year_match:
                            year = year_match.group(0)
                    
                    if force_match_with_tmdb(clean_title, year, str(db_item['tmdb_id']), movie.get('ratingKey')):
                        matched_movies.append(movie)
                        continue
                # If we have a TMDB ID in the Plex item, use that
                elif movie.get('tmdb_id'):
                    year = str(movie.get('year', '')) if movie.get('year') else ''
                    if force_match_with_tmdb(plex_title, year, str(movie['tmdb_id']), movie.get('ratingKey')):
                        matched_movies.append(movie)
                        continue
                # If we have no TMDB ID at all, we need to trigger a match refresh
                else:
                    logging.info(f"No TMDB ID available for {plex_filename}, skipping item")
                    continue  # Skip items without TMDB IDs since we can't match them
            else:
                matched_movies.append(movie)
                
        # Group episodes by show before processing
        shows_to_process = {}  # Dict to store show info keyed by show title
        for episode in collected_content.get('episodes', []):
            if not is_item_matched(episode):
                # Get TMDB ID from database for this episode
                plex_filename = os.path.basename(episode.get('location', ''))
                conn = get_db_connection()
                cursor = conn.execute('''
                    SELECT tmdb_id, year, title
                    FROM media_items 
                    WHERE filled_by_file LIKE ? 
                    AND tmdb_id IS NOT NULL 
                    AND state IN ('Collected', 'Upgrading', 'Checking')
                ''', (f'%{plex_filename}',))
                db_item = cursor.fetchone()
                conn.close()
                
                if db_item:
                    show_title = db_item['title']
                    if not show_title:
                        logging.info(f"No title in database for show {plex_filename}, using cleaned filename")
                        show_title = os.path.splitext(plex_filename)[0]
                        # Clean up the filename to get a reasonable show title
                        show_title = re.sub(r'\.S\d+E\d+.*$', '', show_title)  # Remove season/episode info and everything after
                        show_title = re.sub(r'\.(19|20)\d{2}.*$', '', show_title)  # Remove year and everything after
                        show_title = re.sub(r'[._]', ' ', show_title)  # Replace dots and underscores with spaces
                        show_title = re.sub(r'\s+', ' ', show_title)  # Normalize spaces
                        show_title = show_title.strip()
                        
                    if not show_title:  # If we still don't have a title
                        show_title = episode.get('grandparentTitle', plex_filename)
                    
                    logging.info(f"Using show title for match: '{show_title}'")
                    
                    if show_title not in shows_to_process:
                        shows_to_process[show_title] = {
                            'episodes': [],
                            'tmdb_id': str(db_item['tmdb_id']),
                            'year': db_item['year'],
                            'title': show_title,
                            'rating_key': episode.get('ratingKey')
                        }
                    shows_to_process[show_title]['episodes'].append(episode)
                else:
                    logging.warning(f"No database entry found for file: {plex_filename}")
            else:
                matched_episodes.append(episode)
                
        # Process each show once
        for show_title, show_info in shows_to_process.items():
            if show_info['tmdb_id'] and show_info['rating_key']:
                logging.info(f"Processing show: {show_title} with TMDB ID {show_info['tmdb_id']}")
                if force_match_with_tmdb(show_info['title'], str(show_info['year']) if show_info['year'] else '', show_info['tmdb_id'], show_info['rating_key']):
                    matched_episodes.extend(show_info['episodes'])
                    
        return {
            'movies': matched_movies,
            'episodes': matched_episodes
        }
        
    except Exception as e:
        logging.error(f"Error checking and fixing unmatched items: {str(e)}")
        return collected_content 