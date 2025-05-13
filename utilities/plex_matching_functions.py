import logging
import os
from plexapi.server import PlexServer
from utilities.settings import get_setting
from typing import Dict, List, Any, Optional
from database.core import get_db_connection
import requests
import re
import time  # Add time import for sleep
import urllib.parse
from difflib import SequenceMatcher # For comparing titles
from cli_battery.app.direct_api import DirectAPI # Added import

def _similar(a, b):
    """Helper function for string similarity"""
    return SequenceMatcher(None, a, b).ratio()

def force_match_with_tmdb(db_title: str, db_year: Optional[str], tmdb_id: str, plex_rating_key: Optional[str] = None) -> bool:
    """
    Force matches a Plex item with a specific TMDB ID using the best available match result.
    If the item contains multiple movies (based on our database records), it will attempt to split them first.

    Args:
        db_title (str): The correct title from our database.
        db_year (Optional[str]): The correct year from our database.
        tmdb_id (str): The TMDB ID to match with (used for verification post-match).
        plex_rating_key (Optional[str]): The Plex rating key of the item to fix.

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
            # Need rating key to identify the item in Plex
            logging.error("No Plex rating key provided for item")
            return False
            
        logging.info(f"Attempting to match item with rating key: {plex_rating_key} to DB Title: '{db_title}', Year: {db_year}, Target TMDB ID: {tmdb_id}")
        
        # Clean up the base URL
        baseurl = baseurl.rstrip('/')
        # Ensure slash after port if present
        if ':' in baseurl.split('/')[-1] and not baseurl.endswith('/'):
             baseurl += '/'
            
        # Connect to Plex
        plex = PlexServer(baseurl, token)
        
        # Get the item by rating key
        try:
            item = plex.fetchItem(int(plex_rating_key))
        except Exception as e:
            logging.error(f"Failed to fetch item with rating key {plex_rating_key}: {str(e)}")
            return False

        # Set up headers for potential future direct API calls if needed
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
            
            if len(file_locations) > 1: # Only split if more than one file
                logging.info(f"Found {len(file_locations)} file(s) in Plex item, checking if split is needed.")

                # Check our database for each file to see if they belong together
                conn = get_db_connection()
                cursor = conn.cursor()
                target_file_for_split = None
                other_files_for_split = []
                found_mismatch = False

                try:
                    # Find which file should correspond to the target TMDB ID
                    # Requires querying based on tmdb_id and filename
                    # This logic might need refinement depending on how `filled_by_file` is stored
                    # Assuming db_title/year/tmdb_id correspond to one of the files
                    # A simpler check: If files belong to different TMDB IDs in DB, split needed.
                    db_tmdb_ids = set()
                    for location in file_locations:
                        filename = os.path.basename(location)
                        # Query DB for TMDB ID associated with this filename
                        cursor.execute('''
                            SELECT DISTINCT tmdb_id
                            FROM media_items
                            WHERE filled_by_file LIKE ?
                            AND tmdb_id IS NOT NULL
                            AND state IN ('Collected', 'Upgrading', 'Checking')
                        ''', (f'%{filename}%',))
                        result = cursor.fetchone()
                        if result and result['tmdb_id']:
                            db_tmdb_ids.add(str(result['tmdb_id']))


                    if len(db_tmdb_ids) > 1:
                        logging.info(f"Files in item belong to different TMDB IDs ({db_tmdb_ids}). Initiating split.")
                        # Split the item
                        split_url = f"{baseurl}/library/metadata/{plex_rating_key}/split"
                        response = requests.put(split_url, headers=headers)

                        if response.status_code == 200:
                            logging.info("Successfully initiated split operation. Waiting...")
                            time.sleep(10) # Increase wait time for split

                            # We need to re-find the correct item after split
                            # This is complex: requires searching the library for the item
                            # associated with the file corresponding to db_title/db_year/tmdb_id
                            # For now, let's assume the split worked and we need to find the new rating key.
                            # A robust solution would involve finding the file belonging to tmdb_id
                            # and then finding the Plex item containing that file.
                            logging.warning("Item split. Re-run needed to match the correct new item.")
                            # We cannot reliably get the new item's rating key here easily.
                            # The safest approach is to let the next run pick up the split items individually.
                            return False # Indicate failure for this run, let next cycle handle split items
                        else:
                            logging.warning(f"Failed to split item: HTTP {response.status_code}. Proceeding without split.")
                    else:
                        logging.info("Files in item belong to the same TMDB ID or couldn't be verified in DB. No split needed.")

                finally:
                    cursor.close()
                    conn.close()
            else:
                logging.debug(f"Only {len(file_locations)} file(s) found, no split needed.")
        # --- Split Logic End ---


        # --- Matching Logic ---
        try:
            # Get the agent from the library section
            is_tv_show = bool(item.type == 'episode')
            library_section = item.section()
            agent = library_section.agent
            logging.info(f"Using agent {agent} from library section '{library_section.title}'")

            # For TV shows, work with the series (grandparent)
            if is_tv_show:
                if not hasattr(item, 'grandparentRatingKey'):
                    logging.error("Episode missing grandparentRatingKey")
                    return False
                plex_rating_key = str(item.grandparentRatingKey)
                item = plex.fetchItem(int(plex_rating_key)) # Get the show item
                logging.info(f"Working with TV series '{item.title}' (rating key: {plex_rating_key})")

            # Check current external IDs
            current_tmdb_id = None
            current_imdb_id = None
            for guid_obj in getattr(item, 'guids', []):
                guid_str = str(guid_obj)
                if 'tmdb://' in guid_str:
                    match = re.search(r'tmdb://(?:tv/)?(\d+)', guid_str)
                    if match: current_tmdb_id = match.group(1)
                elif 'imdb://' in guid_str:
                    match = re.search(r'imdb://(tt\d+)', guid_str)
                    if match: current_imdb_id = match.group(1)

            # If already matched correctly, return True
            if current_tmdb_id and current_tmdb_id == str(tmdb_id):
                 logging.info(f"Item '{item.title}' is already matched with the correct TMDB ID {tmdb_id}. Skipping fix.")
                 return True
            # Add check for IMDb ID if TMDB ID is missing but we have an IMDb ID in DB? (Requires passing IMDb ID)
            # elif current_imdb_id and current_imdb_id == db_imdb_id: ...

            logging.info(f"Item '{item.title}' (Current IDs: TMDB={current_tmdb_id}, IMDB={current_imdb_id}) needs matching to TMDB ID {tmdb_id}.")

            # --- Find Match by Trial-and-Error ---
            target_match_found = False
            try:
                # --- Search using the CORRECT title/year from the database ---
                search_title = db_title
                search_year = db_year # db_year is already Optional[str]
                logging.info(f"Searching matches using DB info: title='{search_title}', year='{search_year}', agent='{agent}'")
                # Pass the correct db_title and db_year to the search
                matches = item.matches(agent=agent, title=search_title, year=search_year)
                logging.info(f"Found {len(matches)} potential matches. Will attempt each sequentially.")

                # --- Add logging for the received match list order ---
                if matches:
                    logging.info("--- Received Match List Order ---")
                    for i, res in enumerate(matches):
                        res_name = getattr(res, 'name', 'N/A')
                        res_year = getattr(res, 'year', 'N/A')
                        res_guid = getattr(res, 'guid', 'N/A')
                        logging.info(f"  {i+1}: Name='{res_name}', Year='{res_year}', GUID='{res_guid}'")
                    logging.info("--- End of Received Match List ---")
                else:
                     logging.warning("No matches returned by item.matches()")
                # --- End logging ---

                if not matches:
                    logging.error(f"No potential matches found for '{search_title}' ({search_year}). Cannot apply fix.")
                    return False

                # Loop through each result, try matching, verify, and unmatch if wrong
                # This loop naturally processes from the top (index 0) of the 'matches' list
                for idx, match_result in enumerate(matches, 1):
                    if idx > 5: break # Limit to 5 matches
                    result_name = getattr(match_result, 'name', 'N/A')
                    result_year = getattr(match_result, 'year', 'N/A')
                    result_guid = getattr(match_result, 'guid', 'N/A') # This is likely plex://
                    logging.info(f"--- Attempting Match {idx}/{len(matches)}: Name='{result_name}', Year='{result_year}', ResultGUID='{result_guid}' ---")

                    try:
                        # Apply this match result
                        item.fixMatch(searchResult=match_result)
                        logging.info(f"Applied match {idx}. Waiting for Plex to process...")
                        time.sleep(4) # Increased wait after applying match

                        # Verify the result
                        item.reload()
                        guids = getattr(item, 'guids', [])
                        logging.info(f"Verification: GUIDs after attempting match {idx}: {guids}")

                        matched_tmdb_id = None
                        for guid_obj in guids:
                            guid_str = str(guid_obj)
                            if 'tmdb://' in guid_str:
                                tmdb_search = re.search(r'tmdb://(?:tv/)?(\d+)', guid_str)
                                if tmdb_search:
                                    matched_tmdb_id = tmdb_search.group(1)
                                    break # Found TMDB ID

                        # Check if this attempt resulted in the correct TMDB ID
                        if matched_tmdb_id and matched_tmdb_id == str(tmdb_id):
                            logging.info(f"SUCCESS: Match attempt {idx} resulted in correct TMDB ID: {matched_tmdb_id}")
                            target_match_found = True
                            break # Exit the loop, we are done
                        else:
                            logging.warning(f"Match attempt {idx} resulted in incorrect/missing TMDB ID (Found: {matched_tmdb_id}, Expected: {tmdb_id}). Unmatching...")
                            try:
                                item.unmatch()
                                logging.info(f"Unmatch command sent after attempt {idx}. Waiting...")
                                time.sleep(3) # Wait for unmatch to process
                                item.reload() # Reload to confirm unmatch (optional check)
                                logging.info(f"Item state after unmatch attempt {idx}: GUIDs={getattr(item, 'guids', [])}")
                            except Exception as unmatch_e:
                                logging.error(f"Failed to unmatch after incorrect match attempt {idx}: {str(unmatch_e)}")
                                # Continue to next attempt despite unmatch error? Risky, could leave item wrongly matched.
                                # Let's break the loop here to be safe, as state is uncertain.
                                logging.error("Stopping further attempts due to unmatch error.")
                                return False # Indicate failure due to unstable state


                    except Exception as match_e:
                        logging.error(f"Failed to apply or verify match for result {idx} (Name='{result_name}'): {str(match_e)}")
                        # Attempt to unmatch if fixMatch failed mid-way
                        try:
                            logging.warning(f"Attempting to unmatch item after error during match attempt {idx}...")
                            item.unmatch()
                            time.sleep(3)
                            logging.info(f"Item unmatched successfully after error during attempt {idx}.")
                        except Exception as unmatch_e_after_error:
                             logging.error(f"Failed to unmatch after error during fixMatch attempt {idx}: {str(unmatch_e_after_error)}")
                             logging.error("Stopping further attempts due to unmatch error.")
                             return False # Indicate failure due to unstable state
                        # Continue to the next potential match in the outer loop

                # After the loop finishes
                if not target_match_found:
                    logging.error(f"Iterated through all {len(matches)} match results, none resulted in the correct TMDB ID {tmdb_id}.")
                    return False
                else:
                    # This part should only be reached if break happened due to success
                    return True

            except Exception as e:
                logging.error(f"Error during Plex match search or trial-and-error loop: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
                return False

        except Exception as e:
            logging.error(f"Error in force_match_with_tmdb outer logic: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    except Exception as e:
        logging.error(f"Error setting up Plex connection or fetching item: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
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
        logging.debug(f"Checking match status for item dict: {item}") # Use debug for full dict maybe

        plex_title = item.get('title', 'N/A')
        plex_year = item.get('year')
        plex_rating_key = item.get('ratingKey')
        # Extract IDs directly from the dict provided by the caller (e.g., from get_all_content)
        # These might be sparse depending on how get_all_content works
        plex_guid_str = item.get('guid', '') # Assuming get_all_content provides guid string
        plex_imdb_id = None
        plex_tmdb_id = None

        # Attempt to parse from GUID if available in the input dict
        if 'imdb://' in plex_guid_str:
            match = re.search(r'imdb://(tt\d+)', plex_guid_str)
            if match: plex_imdb_id = match.group(1)
        elif 'tmdb://' in plex_guid_str:
             match = re.search(r'tmdb://(?:tv/)?(\d+)', plex_guid_str)
             if match: plex_tmdb_id = match.group(1)

        # Fallback to dedicated fields if they exist in the dict
        if not plex_imdb_id: plex_imdb_id = item.get('imdb_id')
        if not plex_tmdb_id: plex_tmdb_id = item.get('tmdb_id')

        logging.info(f"Checking item from Plex Scan: Title='{plex_title}', Year={plex_year}, RatingKey={plex_rating_key}, Found IDs: IMDb={plex_imdb_id}, TMDB={plex_tmdb_id}")

        # Check minimum required metadata
        # Use the parsed/extracted IDs for the check
        has_id = bool(plex_imdb_id or plex_tmdb_id)
        if not has_id:
            logging.info(f"Item '{plex_title}' (RatingKey: {plex_rating_key}) missing required external ID (IMDb/TMDB) in scan results.")
            # Cannot reliably check against DB without an ID from Plex
            # Consider if this item should be flagged for a refresh/rematch attempt anyway?
            # Returning True means we don't try to fix it here, False means we do.
            # Let's return False to trigger a fix attempt where *might* find the ID.
            return False

        # Check against database
        location = item.get('location')
        if not location:
            logging.warning(f"Item '{plex_title}' (RatingKey: {plex_rating_key}) has no location info in scan results.")
            # Cannot reliably check against DB without location/filename
            return True # Assume matched if no location? Or False to try fix? Needs decision. Let's assume True.

        plex_filename = os.path.basename(location)
        conn = get_db_connection()
        cursor = conn.execute('''
            SELECT id, filled_by_file, imdb_id, tmdb_id, state, title as db_title, year as db_year
            FROM media_items
            WHERE (filled_by_file LIKE ? OR location_on_disk LIKE ?)
            AND state IN ('Collected', 'Upgrading', 'Checking')
        ''', (f'%{plex_filename}%', f'%{plex_filename}%')) # Ensure LIKE pattern is correct

        matching_db_items = cursor.fetchall()
        conn.close()

        if not matching_db_items:
            logging.info(f"No active items found in database for file: {plex_filename}")
            # This file exists in Plex but not in our DB active states. Assume okay for now.
            return True

        # Check if *any* matching DB item corresponds to the Plex IDs
        is_correctly_matched = False
        for db_item in matching_db_items:
            db_imdb = db_item['imdb_id']
            db_tmdb = str(db_item['tmdb_id']) if db_item['tmdb_id'] else None
            db_title = db_item['db_title']
            db_year = db_item['db_year']

            logging.debug(f"  Comparing Plex file '{plex_filename}' (Plex IDs: TMDB={plex_tmdb_id}, IMDb={plex_imdb_id}) against DB item ID {db_item['id']} (DB IDs: TMDB={db_tmdb}, IMDb={db_imdb}, Title='{db_title}', Year={db_year}, State={db_item['state']})")

            # Prioritize TMDB ID match
            if plex_tmdb_id and db_tmdb and plex_tmdb_id == db_tmdb:
                logging.info(f"Correct match found for '{plex_filename}' based on TMDB ID ({plex_tmdb_id}) matching DB ID {db_item['id']} (State: {db_item['state']}).")
                is_correctly_matched = True
                break # Found a correct match

            # Fallback to IMDb ID match
            if plex_imdb_id and db_imdb and plex_imdb_id == db_imdb:
                logging.info(f"Correct match found for '{plex_filename}' based on IMDb ID ({plex_imdb_id}) matching DB ID {db_item['id']} (State: {db_item['state']}).")
                is_correctly_matched = True
                break # Found a correct match

        if is_correctly_matched:
            return True
        else:
            # If we reached here, none of the DB items matching the filename had matching IDs with Plex
            logging.warning(f"ID mismatch for file '{plex_filename}'. Plex IDs (TMDB={plex_tmdb_id}, IMDb={plex_imdb_id}) do not match any corresponding active DB item IDs.")
            # Log the DB IDs found for this file for clarity
            for db_item in matching_db_items:
                 logging.warning(f"  -> Potential DB match: ID={db_item['id']}, TMDB={db_item['tmdb_id']}, IMDb={db_item['imdb_id']}, Title='{db_item['db_title']}', State={db_item['state']}")
            return False # Item is considered incorrectly matched

    except Exception as e:
        logging.error(f"Error checking if item is matched: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return False # Treat errors as potentially unmatched

def check_and_fix_unmatched_items(collected_content: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Check collected content for unmatched items and attempt to fix them.
    """
    matched_movies = []
    matched_episodes = []
    processed_show_rating_keys = set() # Keep track of shows already processed

    try:
        # Process movies
        logging.info("--- Checking and Fixing Movie Matches ---")
        direct_api = DirectAPI() # Instantiate API client once for efficiency
        for movie_data in collected_content.get('movies', []):
            if not is_item_matched(movie_data):
                plex_filename = os.path.basename(movie_data.get('location', ''))
                plex_title = movie_data.get('title', '')
                plex_year = movie_data.get('year') # Get year from Plex data
                plex_rating_key = movie_data.get('ratingKey')
                logging.warning(f"Found potentially unmatched movie: '{plex_title}' (File: {plex_filename}, RatingKey: {plex_rating_key})")

                if not plex_rating_key or not plex_filename:
                     logging.error("Movie data missing ratingKey or location, cannot fix match.")
                     matched_movies.append(movie_data) # Keep movie data even if we can't fix
                     continue # Skip if essential info missing

                # Find the corresponding DB item to get correct details
                conn = get_db_connection()
                cursor = conn.execute('''
                    SELECT tmdb_id, year, title, imdb_id
                    FROM media_items
                    WHERE (filled_by_file LIKE ? OR location_on_disk LIKE ?)
                    AND state IN ('Collected', 'Upgrading', 'Checking')
                    ORDER BY id DESC LIMIT 1
                ''', (f'%{plex_filename}%', f'%{plex_filename}%'))
                db_item = cursor.fetchone()
                conn.close()

                if db_item and db_item['tmdb_id']:
                    db_title = db_item['title'] or plex_title # Use DB title or fallback
                    db_year = str(db_item['year']) if db_item['year'] else None
                    db_tmdb_id = str(db_item['tmdb_id'])
                    logging.info(f"Attempting to fix match for '{plex_filename}' using DB info: Title='{db_title}', Year={db_year}, TMDB ID={db_tmdb_id}")
                    if force_match_with_tmdb(db_title, db_year, db_tmdb_id, plex_rating_key):
                        logging.info(f"Successfully fixed match for movie '{db_title}'.")
                        # Assume fixed, add to matched (or re-query Plex state if needed)
                    else:
                        logging.error(f"Failed to fix match for movie '{db_title}'.")
                    # Keep movie_data regardless of fix outcome for this path
                    matched_movies.append(movie_data)
                else:
                    # --- Fallback Trakt/Metadata Search Logic ---
                    logging.warning(f"No suitable DB entry found for movie file '{plex_filename}'. Attempting metadata provider lookup based on Plex info: Title='{plex_title}', Year={plex_year}.")
                    provider_tmdb_id = None
                    provider_title = plex_title # Default to plex title
                    provider_year = str(plex_year) if plex_year else None # Default to plex year

                    try:
                        # Search metadata provider (e.g., Trakt via DirectAPI)
                        search_results, _ = direct_api.search_media(query=plex_title, year=plex_year, media_type='movie')

                        if search_results:
                            top_result = search_results[0]
                            logging.info(f"Metadata lookup found potential match: {top_result.get('title')} ({top_result.get('year')}) ID: {top_result.get('tmdb_id') or top_result.get('imdb_id')}")

                            # Prioritize TMDB ID from search result
                            provider_tmdb_id = top_result.get('tmdb_id')
                            provider_imdb_id = top_result.get('imdb_id')
                            provider_title = top_result.get('title', plex_title) # Prefer provider title
                            provider_year = str(top_result.get('year')) if top_result.get('year') else provider_year # Prefer provider year

                            # If no TMDB ID but IMDb ID exists, try to get TMDB ID via metadata fetch
                            if not provider_tmdb_id and provider_imdb_id:
                                logging.info(f"Provider result has IMDb ID ({provider_imdb_id}) but no TMDB ID. Attempting metadata fetch to find TMDB ID.")
                                meta_check, _ = direct_api.get_movie_metadata(imdb_id=provider_imdb_id)
                                if meta_check and isinstance(meta_check, dict) and meta_check.get('ids', {}).get('tmdb'):
                                     provider_tmdb_id = str(meta_check['ids']['tmdb'])
                                     logging.info(f"Obtained TMDB ID {provider_tmdb_id} from metadata fetch using IMDb {provider_imdb_id}")
                                else:
                                     logging.warning(f"Could not obtain TMDB ID from metadata fetch using IMDb {provider_imdb_id}.")

                        else:
                             logging.warning(f"Metadata provider lookup failed to find any results for '{plex_title}' ({plex_year}).")

                    except Exception as provider_lookup_error:
                         logging.error(f"Error during metadata provider fallback lookup for '{plex_title}': {provider_lookup_error}", exc_info=True)

                    # Attempt fix if we found a TMDB ID through the provider lookup
                    if provider_tmdb_id:
                        match_tmdb_id = str(provider_tmdb_id)
                        logging.info(f"Attempting to fix match for '{plex_filename}' using provider info: Title='{provider_title}', Year={provider_year}, TMDB ID={match_tmdb_id}")
                        if force_match_with_tmdb(provider_title, provider_year, match_tmdb_id, plex_rating_key):
                            logging.info(f"Successfully fixed match for movie '{provider_title}' using provider fallback.")
                        else:
                            logging.error(f"Failed to fix match for movie '{provider_title}' using provider fallback.")
                    else:
                        logging.warning(f"Provider fallback lookup for '{plex_title}' did not yield a usable TMDB ID. Cannot attempt fix.")

                    # Keep the original movie data in the list as the DB lookup failed initially
                    matched_movies.append(movie_data)
                    # --- End Fallback Logic ---

            else: # Item was matched correctly initially
                matched_movies.append(movie_data)

        # Process episodes (group by show's grandparentRatingKey)
        logging.info("--- Checking and Fixing TV Show Matches ---")
        shows_to_fix = {} # Key: grandparentRatingKey, Value: { details }
        for episode_data in collected_content.get('episodes', []):
            # We need the show's rating key to fix the match at the show level
            show_rating_key = episode_data.get('grandparentRatingKey')
            if not show_rating_key:
                logging.warning(f"Episode data missing grandparentRatingKey: {episode_data.get('title')}")
                matched_episodes.append(episode_data) # Cannot fix without show key
                continue

            # Only check/process each show once
            if show_rating_key in processed_show_rating_keys:
                matched_episodes.append(episode_data) # Already handled or checked
                continue

            if not is_item_matched(episode_data):
                plex_filename = os.path.basename(episode_data.get('location', ''))
                plex_show_title = episode_data.get('grandparentTitle', episode_data.get('title', '')) # Use grandparent title if available
                logging.warning(f"Found potentially unmatched episode from show '{plex_show_title}' (File: {plex_filename}, Show RatingKey: {show_rating_key})")

                if not plex_filename:
                     logging.error(f"Episode from show '{plex_show_title}' missing location, cannot reliably find DB info.")
                     processed_show_rating_keys.add(show_rating_key)
                     # Add this and related episodes ? For now, just mark processed.
                     # Need a way to add all episodes for this show key if we skip fixing.
                     matched_episodes.append(episode_data) # Keep episode, skip fix for show
                     continue

                # Find corresponding DB item for this *episode's file*
                conn = get_db_connection()
                cursor = conn.execute('''
                    SELECT tmdb_id, year, title, imdb_id
                    FROM media_items
                    WHERE (filled_by_file LIKE ? OR location_on_disk LIKE ?)
                    AND state IN ('Collected', 'Upgrading', 'Checking')
                    ORDER BY id DESC LIMIT 1
                ''', (f'%{plex_filename}%', f'%{plex_filename}%'))
                db_item = cursor.fetchone()
                conn.close()

                if db_item and db_item['tmdb_id']:
                    # Use DB info for the fix attempt
                    # IMPORTANT: We need the SHOW's title/year/tmdb_id from the DB,
                    #            not the episode's. This query needs adjustment
                    #            if media_items stores episode-level TMDB IDs.
                    #            Assuming tmdb_id in media_items is the SHOW's TMDB ID for TV episodes.
                    db_show_title = db_item['title'] or plex_show_title # Fallback needed
                    db_show_year = str(db_item['year']) if db_item['year'] else None
                    db_show_tmdb_id = str(db_item['tmdb_id'])

                    # Store info to fix the show once
                    shows_to_fix[show_rating_key] = {
                        'db_title': db_show_title,
                        'db_year': db_show_year,
                        'db_tmdb_id': db_show_tmdb_id,
                        'plex_show_title': plex_show_title # For logging
                    }
                    # Don't add episode to matched list yet, handle after show fix attempt
                else:
                     logging.warning(f"No suitable DB entry found for episode file '{plex_filename}' to identify show info for fix.")
                     processed_show_rating_keys.add(show_rating_key)
                     matched_episodes.append(episode_data) # Keep episode, cannot fix show

            else:
                # Episode is matched correctly, add it and mark show as processed
                matched_episodes.append(episode_data)
                processed_show_rating_keys.add(show_rating_key)

        # Now, attempt to fix the shows identified
        for show_rating_key, fix_info in shows_to_fix.items():
             logging.info(f"Attempting to fix match for show '{fix_info['plex_show_title']}' (RatingKey: {show_rating_key}) using DB info: Title='{fix_info['db_title']}', Year={fix_info['db_year']}, TMDB ID={fix_info['db_tmdb_id']}")
             success = force_match_with_tmdb(
                 fix_info['db_title'],
                 fix_info['db_year'],
                 fix_info['db_tmdb_id'],
                 str(show_rating_key) # Pass rating key as string
             )
             processed_show_rating_keys.add(show_rating_key) # Mark as processed regardless of outcome

             if success:
                 logging.info(f"Successfully fixed match for show '{fix_info['db_title']}'.")
             else:
                 logging.error(f"Failed to fix match for show '{fix_info['db_title']}'.")

             # Now add all episodes belonging to this show (fixed or not) to the final list
             for ep_data in collected_content.get('episodes', []):
                 if ep_data.get('grandparentRatingKey') == show_rating_key:
                     # Check if already added? Add only if not.
                     # This logic needs care to avoid duplicates if an episode was added earlier.
                     # Simplest: rebuild matched_episodes including these now.
                     # Let's clear and rebuild based on processed_show_rating_keys might be safer.
                     pass # See refinement below

        # Refinement: Rebuild matched_episodes list to include episodes from fixed/attempted shows
        final_matched_episodes = []
        handled_keys = set()
        for episode_data in collected_content.get('episodes', []):
            show_key = episode_data.get('grandparentRatingKey')
            # Add if the show was processed (fix attempted or initially matched)
            # or if it had no key (and was added initially)
            if not show_key or show_key in processed_show_rating_keys:
                 # Avoid adding duplicates if show processed multiple episodes
                 # Use episode rating key for uniqueness check
                 episode_key = episode_data.get('ratingKey')
                 if episode_key and episode_key not in handled_keys:
                     final_matched_episodes.append(episode_data)
                     handled_keys.add(episode_key)
                 elif not episode_key: # Handle cases without episode key?
                     final_matched_episodes.append(episode_data) # Add anyway


        logging.info("--- Finished Checking and Fixing Matches ---")
        return {
            'movies': matched_movies,
            'episodes': final_matched_episodes # Use the rebuilt list
        }

    except Exception as e:
        logging.error(f"Error during check_and_fix_unmatched_items: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        # Return original content on error to avoid data loss
        return collected_content 