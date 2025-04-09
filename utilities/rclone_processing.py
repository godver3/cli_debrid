import logging
import os
import json
from typing import Dict, Any
from datetime import datetime, timezone
import time

# Database Imports
from database.core import get_db_connection
from database.database_writing import update_media_item_state, add_media_item, update_release_date_and_state
from database.database_reading import get_media_item_presence, get_media_item_by_id

# Utility Imports
from utilities.settings import get_setting
from utilities.local_library_scan import get_symlink_path, create_symlink, check_local_file_for_item
from utilities.plex_functions import plex_update_item
from utilities.emby_functions import emby_update_item
from utilities.reverse_parser import parse_filename_for_version
from utilities.post_processing import handle_state_change

# Scraping/Metadata Imports (Careful with potential cycles here too)
from scraper.functions.ptt_parser import parse_with_ptt
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.direct_api import DirectAPI
from fuzzywuzzy import fuzz


def handle_rclone_file(file_path: str) -> Dict[str, Any]:
    """
    Handle a file path received from rclone.
    If it matches an item in the 'Checking' state, it calls check_local_file_for_item
    to handle symlinking, state updates, and potential upgrade cleanup.
    If it's an untracked file (and setting allows), it performs metadata lookup
    and adds it as a new 'Collected' item.

    Returns:
        Dict containing:
            - success (bool): True if processing should be considered complete for this path,
                              False if an error occurred or it should be retried.
            - message (str): Description of the outcome.
            - processed_items (list): List of item IDs successfully processed (if applicable).
    """
    overall_success = False # Track if the path should be removed from queue
    processing_message = "Processing started"
    processed_item_ids = []

    try:
        # Get the full path components
        path_parts = file_path.split('/')
        filename = path_parts[-1]  # The actual file
        folder_name = path_parts[-2] if len(path_parts) > 1 else ''  # The containing folder
        logging.debug(f"[RcloneProcessing] Processing rclone file: {filename} from folder: {folder_name} (full path: {file_path})")

        # Get original path from settings - needed for source path construction
        original_path = get_setting('File Management', 'original_files_path')
        if not original_path:
             raise ValueError("Original files path setting is missing or empty.")
        source_file = os.path.join(original_path, file_path) # Full path to the source file

        # Check database for items in Checking state with matching filename
        conn = get_db_connection()
        # Ensure all relevant columns are selected, including upgrade info
        cursor = conn.execute('''
            SELECT * FROM media_items
            WHERE state = 'Checking'
            AND filled_by_file = ?
        ''', (filename,))
        # Fetch all potentially relevant columns
        matched_items_raw = cursor.fetchall()
        conn.close()

        matched_items = [dict(row) for row in matched_items_raw]
        logging.info(f"[RcloneProcessing] Found {len(matched_items)} matching items in Checking state in database for file '{filename}'")

        if matched_items:
            # --- Handle items found in 'Checking' state ---
            all_items_processed_successfully = True # Track success for *this specific file path*
            for item_dict in matched_items:
                item_id = item_dict.get('id', 'Unknown')
                logging.info(f"[RcloneProcessing] Processing matched 'Checking' item {item_id} for file '{filename}'")
                logging.debug(f"[RcloneProcessing] Item data before calling check_local_file: {item_dict}") # Log the full dict

                # Ensure the source_file path is correct (it should be from above)
                if not os.path.exists(source_file):
                     logging.error(f"[RcloneProcessing] Source file '{source_file}' does not exist. Cannot process item {item_id}.")
                     all_items_processed_successfully = False # Mark this path as failed
                     continue # Skip this item

                # Call check_local_file_for_item which handles symlink, state, cleanup
                item_processing_successful = check_local_file_for_item(
                    item_dict,
                    is_webhook=True, # Treat as webhook for retry logic within check_local_file
                    extended_search=False # Let check_local_file_for_item decide based on its internal logic/timing
                    # on_success_callback=... # Cannot easily provide callback here yet
                )

                if item_processing_successful:
                    logging.info(f"[RcloneProcessing] Successfully processed item {item_id} via check_local_file_for_item.")
                    processed_item_ids.append(item_id)
                    # --- EDIT: Add Media Server Updates Here ---
                    # Check for Plex or Emby/Jellyfin configuration and update accordingly
                    # Use item_dict which is the full dictionary for the processed item
                    if get_setting('Debug', 'emby_jellyfin_url', default=False):
                        logging.info(f"[RcloneProcessing] Updating Emby/Jellyfin for item {item_id} after check_local_file_for_item.")
                        emby_update_item(item_dict) # Pass the item dictionary
                    elif get_setting('File Management', 'plex_url_for_symlink', default=False):
                        logging.info(f"[RcloneProcessing] Updating Plex for item {item_id} after check_local_file_for_item.")
                        plex_update_item(item_dict) # Pass the item dictionary
                    # --- END EDIT ---
                else:
                    logging.error(f"[RcloneProcessing] check_local_file_for_item failed for item {item_id}. Item may remain in Checking state.")
                    # If even one item fails processing via check_local_file_for_item,
                    # we might want to keep the path in the queue for retry.
                    all_items_processed_successfully = False

            # Determine overall success for this path based on processing all matched items
            overall_success = all_items_processed_successfully
            if overall_success:
                processing_message = f"Successfully processed {len(processed_item_ids)} checking item(s) for file '{filename}' via check_local_file_for_item."
            else:
                 processing_message = f"Failed to process one or more checking items for file '{filename}'. Path may be retried."

            # Return the result based on processing the matched 'Checking' items
            return {
                'success': overall_success, # Indicates if path should be removed from queue
                'message': processing_message,
                'processed_items': processed_item_ids
            }

        # --- Logic for non-checking items ---
        logging.info(f"[RcloneProcessing] No matching 'Checking' items found for '{filename}'. Checking setting for processing non-checking items.")
        # Check if we should process non-checking items
        if not get_setting('File Management', 'process_non_checking_items', default=False):
            logging.info("[RcloneProcessing] Processing of non-checking items is disabled, skipping metadata lookup")
            # Since no checking items were found and non-checking processing is off, consider this path done.
            overall_success = True
            processing_message = "No checking items found and processing of non-checking items is disabled. Path considered processed."
            return {
                'success': overall_success,
                'message': processing_message,
                'processed_items': []
            }

        # --- Handle Untracked/New Item ---
        logging.info(f"[RcloneProcessing] Proceeding with metadata lookup for untracked file: '{filename}'")
        metadata_result = process_rclone_file(file_path) # Pass full relative path

        if not metadata_result['success']:
             logging.error(f"[RcloneProcessing] process_rclone_file failed for '{file_path}': {metadata_result.get('message')}")
             overall_success = False # Keep path in queue if metadata lookup fails
             processing_message = f"Metadata lookup failed: {metadata_result.get('message')}"
             return {
                'success': overall_success,
                'message': processing_message,
                'processed_items': []
            }

        if not (metadata_result.get('metadata') and metadata_result.get('best_match')):
             logging.error(f"[RcloneProcessing] process_rclone_file succeeded but missing metadata or match info for '{file_path}'")
             overall_success = False # Keep path in queue if data is missing
             processing_message = "Missing metadata or match information after processing file"
             return {
                 'success': overall_success,
                 'message': processing_message,
                 'processed_items': []
             }

        # --- Create new item based on metadata ---
        logging.info(f"[RcloneProcessing] Metadata lookup successful for '{filename}'. Preparing to add new item.")
        metadata = metadata_result['metadata']
        best_match = metadata_result['best_match']
        parsed_info = metadata_result['parsed_info']
        search_type = 'movie' # Default, will be refined below
        if parsed_info.get('episodes') or parsed_info.get('seasons'):
             search_type = 'show'


        # Construct the item dictionary
        item_to_add = {
            'title': metadata.get('title'),
            'year': metadata.get('year'),
            'imdb_id': best_match['ids'].get('imdb'),
            'tmdb_id': best_match['ids'].get('tmdb'),
            'tvdb_id': best_match['ids'].get('tvdb'), # Ensure TVDB ID is included if available
            'type': 'movie' if search_type == 'movie' else 'episode', # Set type based on search_type
            'version': metadata_result.get('version', ''),
            'filled_by_file': filename,
            'filled_by_title': folder_name, # Use folder name derived earlier
            'genres': json.dumps(metadata.get('genres', [])), # Convert genres list to JSON string
            'release_date': metadata_result.get('release_date'), # Use the release date from process_rclone_file
            'state': 'Collected', # Set initial state as Collected
            'collected_at': datetime.now(timezone.utc), # Use timezone-aware datetime
            'original_collected_at': datetime.now(timezone.utc), # Use timezone-aware datetime
            'original_path_for_symlink': source_file, # Use full source path
            'overview': metadata.get('overview'),
            'rating': metadata.get('vote_average') # Example if TMDB data is fetched
        }

        # Add episode-specific information if it's a TV show
        if item_to_add['type'] == 'episode':
             # Get show title/year from the best_match if possible
             show_title = best_match.get('title', item_to_add['title']) # Fallback to item title
             show_year = best_match.get('year', item_to_add['year']) # Fallback to item year
             # PTT uses 'season', ensure consistency
             season_num = parsed_info.get('season') or parsed_info.get('seasons', [None])[0]
             episode_num = parsed_info.get('episodes', [None])[0] # Use first parsed episode

             item_to_add.update({
                 'show_title': show_title,
                 'show_year': show_year,
                 'season_number': season_num,
                 'episode_number': episode_num,
                 # Try 'name' first for episodes from metadata (TMDB/TVDB), fallback to 'title'
                 'episode_title': metadata.get('name') or metadata.get('title')
             })
             # Refine main title if it's the same as the show title (common for episodes)
             if item_to_add.get('title') == item_to_add['show_title']:
                  item_to_add['title'] = item_to_add['episode_title'] # Make main title the episode title

        # Get destination path before adding to database
        logging.debug(f"[RcloneProcessing] Getting symlink path for new item based on source: {source_file}")
        dest_file = get_symlink_path(item_to_add, source_file)
        if not dest_file:
             logging.error(f"[RcloneProcessing] Failed to generate symlink path for new item '{item_to_add.get('title')}'")
             overall_success = False # Keep path in queue
             processing_message = "Failed to generate symlink path for new item"
             return {
                 'success': overall_success,
                 'message': processing_message,
                 'processed_items': []
             }
        logging.info(f"[RcloneProcessing] Generated destination symlink path: {dest_file}")

        # Add location to item
        item_to_add['location_on_disk'] = dest_file

        # Add item to database
        logging.info(f"[RcloneProcessing] Adding new item to database: Title='{item_to_add.get('title')}', Type='{item_to_add['type']}'")
        item_id = add_media_item(item_to_add)
        if not item_id:
            logging.error(f"[RcloneProcessing] Failed to add item to database: {item_to_add.get('title')}")
            overall_success = False # Keep path in queue
            processing_message = "Failed to add item to database"
            return {
                'success': overall_success,
                'message': processing_message,
                'processed_items': []
             }

        item_to_add['id'] = item_id # Add the new ID back to the item dict for subsequent calls
        logging.info(f"[RcloneProcessing] Successfully added item to database with ID: {item_id}")

        # Update release date separately (can potentially be combined with add_media_item if function allows)
        if item_to_add.get('release_date'):
            update_release_date_and_state(
                item_id,
                item_to_add['release_date'],
                'Collected'
            )

        # Create symlink
        logging.info(f"[RcloneProcessing] Creating symlink for new item {item_id}: {source_file} -> {dest_file}")
        symlink_success = create_symlink(source_file, dest_file, item_id)
        if not symlink_success:
            logging.error(f"[RcloneProcessing] Failed to create symlink for new item {item_id}")
            overall_success = False # Keep path in queue
            processing_message = "Failed to create symlink for new item"
            # Consider cleanup: remove item from DB? Or leave it for manual intervention?
            return {
                'success': overall_success,
                'message': processing_message,
                'processed_items': []
            }
        logging.info(f"[RcloneProcessing] Successfully created symlink for new item {item_id}")

        # Update media servers
        if get_setting('Debug', 'emby_jellyfin_url', default=False):
            logging.info(f"[RcloneProcessing] Updating Emby/Jellyfin for new item {item_id}")
            emby_update_item(item_to_add)
        elif get_setting('File Management', 'plex_url_for_symlink', default=False):
            logging.info(f"[RcloneProcessing] Updating Plex for new item {item_id}")
            plex_update_item(item_to_add)

        # Call handle_state_change for the newly collected item
        try:
            newly_added_item_full = get_media_item_by_id(item_id) # Fetch the complete item
            if newly_added_item_full:
                 handle_state_change(dict(newly_added_item_full))
            else:
                 logging.warning(f"[RcloneProcessing] Could not retrieve newly added item {item_id} for post-processing.")
        except Exception as post_proc_err:
            logging.error(f"[RcloneProcessing] Error during post-processing for new item {item_id}: {post_proc_err}")

        # If we successfully added the new item, the path is considered processed.
        overall_success = True
        processing_message = f"Successfully processed untracked file and added new item ID {item_id}"
        processed_item_ids.append(item_id)

        return {
            'success': overall_success,
            'message': processing_message,
            'processed_items': processed_item_ids
        }

    except Exception as e:
        logging.error(f"[RcloneProcessing] Unhandled error handling rclone file '{file_path}': {str(e)}", exc_info=True)
        # Ensure overall_success is False so the path is retried
        return {
            'success': False,
            'message': f"Unhandled error: {str(e)}",
            'processed_items': processed_item_ids # Include any IDs processed before the error
        }


def process_rclone_file(file_path: str) -> Dict[str, Any]:
    """
    Process a file path (relative to base media dir, e.g., 'Movie Title (Year)/file.mkv')
    received from rclone, parse it, find metadata, and check for existing entries.
    Moved from webhook_routes.py to break circular import.

    Args:
        file_path: The relative path to the file (e.g., 'Folder Name/filename.ext')

    Returns:
        Dict containing the processing results: success status, message, parsed_info,
        metadata, source, filename, version, best_match, release_date.
    """
    try:
        from metadata.metadata import get_release_date, _get_local_timezone # Added import here
        # Get just the filename for matching and parsing
        filename = os.path.basename(file_path)
        folder_name = os.path.basename(os.path.dirname(file_path)) # Get folder name too

        logging.info(f"[RcloneProcessing] Starting process_rclone_file for: '{filename}' in folder '{folder_name}' (full path: '{file_path}')")

        # Parse the filename using PTT
        logging.debug(f"[RcloneProcessing] Parsing filename with PTT: {filename}")
        parsed_info = parse_with_ptt(filename)

        if not parsed_info or parsed_info.get('parsing_error'):
            logging.error(f"[RcloneProcessing] Failed to parse filename using PTT: {filename}")
            # Try parsing folder name as fallback? No, stick to filename for consistency.
            return {
                'success': False,
                'message': f"Failed to parse filename: {filename}"
            }
        logging.debug(f"[RcloneProcessing] PTT Parsing result: {parsed_info}")

        # Initialize APIs
        trakt = TraktMetadata()
        api = DirectAPI() # Assuming DirectAPI handles TMDB/TVDB etc.

        # Clean up title for search
        # Prioritize title from PTT, fallback to folder name if PTT title is empty/generic
        sanitized_title = parsed_info.get('title', '').strip()
        if not sanitized_title or sanitized_title.lower() == 'unknown':
             sanitized_title = folder_name # Use folder name as fallback
             # Attempt to clean year from folder name if used as title
             sanitized_title = re.sub(r'\(\d{4}\)$', '', sanitized_title).strip()
             logging.info(f"[RcloneProcessing] PTT title empty/unknown, using cleaned folder name for search: '{sanitized_title}'")
        else:
             # Clean year from PTT title if present
             sanitized_title = re.sub(r'\(\d{4}\)$', '', sanitized_title).strip()
             logging.info(f"[RcloneProcessing] Using cleaned PTT title for search: '{sanitized_title}'")

        # Determine search type based on parsed info
        search_type = 'movie'
        if parsed_info.get('episodes') or parsed_info.get('seasons'):
            search_type = 'show'
        logging.info(f"[RcloneProcessing] Detected media type: {search_type}")

        # --- Trakt Search ---
        logging.info(f"[RcloneProcessing] Searching Trakt ({search_type}) for: '{sanitized_title}'")
        # Use parsed year if available for better matching
        search_year = parsed_info.get('year')
        # Construct search query - Trakt usually handles year in query well, but we can filter later
        query = sanitized_title
        # if search_year: # Optionally add year to query, but filtering results is often better
        #      query += f" {search_year}"

        # For TV shows with 'US' often appended for disambiguation, add it if present in filename/folder
        # Be cautious not to add it wrongly. Check filename first.
        if search_type == 'show':
             import re # Import re locally if not already imported globally
             # Check filename and folder name for ' US ' pattern
             if re.search(r'\bUS\b', filename, re.IGNORECASE) or re.search(r'\bUS\b', folder_name, re.IGNORECASE):
                  # Avoid adding if title already ends with US (case-insensitive)
                  if not query.lower().endswith(' us'):
                       query += " US"
                       logging.info(f"[RcloneProcessing] Appended ' US' to Trakt search query: '{query}'")

        url = f"{trakt.base_url}/search/{search_type}?query={query}"
        logging.debug(f"[RcloneProcessing] Trakt search URL: {url}")
        response = trakt._make_request(url)

        if not response or response.status_code != 200:
            logging.error(f"[RcloneProcessing] Failed to search Trakt for '{sanitized_title}'. Status: {response.status_code if response else 'No response'}")
            return {
                'success': False,
                'message': f"Failed to find metadata for '{sanitized_title}' via Trakt search."
            }

        results = response.json()
        if not results:
            logging.warning(f"[RcloneProcessing] No Trakt results found for '{sanitized_title}'.")
            # Consider trying TMDB directly as a fallback? For now, return failure.
            return {
                'success': False,
                'message': f"No metadata found for '{sanitized_title}' via Trakt search."
            }
        logging.debug(f"[RcloneProcessing] Found {len(results)} Trakt results.")

        # --- Find Best Match ---
        best_match = None
        best_score = 0

        for result in results:
            media_data = result.get(search_type)
            if not media_data: continue

            trakt_title = media_data.get('title', '')
            trakt_year = media_data.get('year')

            # Calculate match score (using fuzzywuzzy)
            title_similarity = fuzz.token_sort_ratio(sanitized_title.lower(), trakt_title.lower()) # Use token_sort_ratio for better word order handling

            # Year match bonus/penalty
            year_match_score = 0
            if search_year and trakt_year:
                if int(search_year) == int(trakt_year):
                    year_match_score = 20 # Bonus for exact year match
                else:
                    # Optional: Small penalty for year mismatch? Let's skip penalty for now.
                    pass # year_match_score = -10
            elif search_year and not trakt_year:
                 pass # Parsed year but no Trakt year - neutral
            elif not search_year and trakt_year:
                 # If PTT didn't find a year, but Trakt has one, use Trakt's year for subsequent checks
                 search_year = trakt_year
                 logging.debug(f"[RcloneProcessing] Using Trakt year {search_year} as PTT did not parse one.")
                 pass # No parsed year but Trakt has one - neutral

            final_score = title_similarity + year_match_score

            logging.debug(f"[RcloneProcessing] Match candidate: '{trakt_title}' ({trakt_year}) - Title Sim: {title_similarity}, Year Score: {year_match_score}, Final: {final_score}")

            if final_score > best_score:
                best_score = final_score
                best_match = media_data

        # Require a minimum match score
        min_score = 70 # Adjusted threshold, might need tuning based on token_sort_ratio
        if not best_match or best_score < min_score:
            logging.error(f"[RcloneProcessing] No good Trakt match found for '{sanitized_title}' (Best score: {best_score}, Required: {min_score})")
            return {
                'success': False,
                'message': f"No confident metadata match found for '{sanitized_title}'"
            }

        logging.info(f"[RcloneProcessing] Best Trakt match: '{best_match.get('title')}' ({best_match.get('year')}) with score {best_score}")

        # Get IMDb ID from best Trakt match
        imdb_id = best_match.get('ids', {}).get('imdb')
        tmdb_id = best_match.get('ids', {}).get('tmdb') # Also get TMDB ID from Trakt if available
        tvdb_id = best_match.get('ids', {}).get('tvdb') # And TVDB ID

        if not imdb_id and not tmdb_id: # Require at least one major ID
            logging.error(f"[RcloneProcessing] No IMDb or TMDB ID found for matched title: {best_match.get('title')}")
            # Attempt lookup via TMDB ID if IMDb is missing? DirectAPI might handle this.
            # For now, fail if both are missing.
            return {
                'success': False,
                'message': f"No IMDb or TMDB ID found for matched title: {best_match.get('title')}"
            }
        logging.debug(f"[RcloneProcessing] Found IDs: IMDb='{imdb_id}', TMDB='{tmdb_id}', TVDB='{tvdb_id}'")

        # --- Check if this item (by ID) already exists in our database ---
        # Prioritize checking by IMDb ID if available
        lookup_id_log = imdb_id if imdb_id else f"tmdb:{tmdb_id}" # Use TMDB ID as fallback identifier for logging/lookup
        logging.info(f"[RcloneProcessing] Checking database presence for ID: '{lookup_id_log}'")
        item_state, existing_item_details = get_media_item_presence(imdb_id=imdb_id, tmdb_id=tmdb_id) # Pass both IDs if available

        logging.info(f"[RcloneProcessing] Database presence check result for '{lookup_id_log}': State='{item_state}'")

        if item_state != "Missing" and existing_item_details:
            # Item exists. Check if it's the same file or a different one.
            logging.info(f"[RcloneProcessing] Item already exists in database (State: {item_state}). Comparing filenames.")
            # Compare the new filename with the existing item's filename
            existing_filename = existing_item_details.get('filled_by_file')
            if existing_filename and existing_filename.lower() == filename.lower(): # Case-insensitive compare
                logging.warning(f"[RcloneProcessing] File '{filename}' is exactly the same as the existing item's file. Skipping duplicate processing.")
                # This path is considered successfully handled (duplicate ignored)
                return {
                    'success': True, # Not a processing failure, but signal to stop & remove from queue
                    'message': f"File '{filename}' matching ID '{lookup_id_log}' already exists in database with the same filename."
                    # Add existing item details maybe?
                }
            else:
                # Different filename, could be an upgrade or just a different source.
                # If process_non_checking_items is true, allow adding it.
                # Potential duplicates need manual handling or a separate cleanup task.
                logging.warning(f"[RcloneProcessing] Item ID '{lookup_id_log}' exists but with a different filename ('{existing_filename}'). Proceeding with metadata fetch for new file '{filename}'. Potential duplicate.")
        else:
             logging.info(f"[RcloneProcessing] Item ID '{lookup_id_log}' is new to the database.")

        # --- Get Full Metadata using DirectAPI ---
        # DirectAPI should handle using IMDb or TMDB ID appropriately
        logging.info(f"[RcloneProcessing] Fetching full metadata using DirectAPI for ID: '{lookup_id_log}'")
        metadata = None
        source = None
        try:
            if search_type == 'movie':
                metadata, source = api.get_movie_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id)
            else: # show
                # Ensure we pass the correct season/episode numbers if available from PTT
                season_num_ptt = parsed_info.get('season') or parsed_info.get('seasons', [None])[0]
                episode_num_ptt = parsed_info.get('episodes', [None])[0]
                metadata, source = api.get_show_metadata(
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                    tvdb_id=tvdb_id, # Pass all available IDs
                    season=season_num_ptt,
                    episode=episode_num_ptt
                )

            if not metadata:
                logging.error(f"[RcloneProcessing] Failed to get metadata from DirectAPI for ID: {lookup_id_log}")
                return {
                    'success': False,
                    'message': f"Failed to get full metadata for {best_match.get('title', lookup_id_log)}"
                }
            logging.info(f"[RcloneProcessing] Successfully fetched metadata via DirectAPI from source: {source}")
            logging.debug(f"[RcloneProcessing] Fetched metadata keys: {list(metadata.keys()) if metadata else 'None'}")

        except Exception as api_err:
             logging.error(f"[RcloneProcessing] Error fetching metadata from DirectAPI for ID '{lookup_id_log}': {api_err}", exc_info=True)
             return {
                 'success': False,
                 'message': f"Error fetching metadata for {best_match.get('title', lookup_id_log)}: {api_err}"
             }

        # --- Determine Version and Release Date ---
        logging.debug(f"[RcloneProcessing] Determining version from filename: {filename}")
        version = parse_filename_for_version(filename)
        logging.info(f"[RcloneProcessing] Determined version: {version}")

        logging.debug(f"[RcloneProcessing] Determining release date...")
        release_date = None
        try:
            if search_type == 'movie':
                # Use get_release_date utility which might handle API calls
                # Pass the fetched metadata along with IDs
                release_date = get_release_date({'imdb_id': imdb_id, 'tmdb_id': tmdb_id, **metadata}, imdb_id)
            else: # show/episode
                # For episodes, try to find episode-specific air date from fetched metadata
                # Use parsed PTT info first
                season_num = parsed_info.get('season') or parsed_info.get('seasons', [None])[0]
                episode_num = parsed_info.get('episodes', [None])[0]

                # Check if DirectAPI returned episode-specific metadata directly
                if metadata.get('air_date'): # Check top-level first (common if specific episode requested)
                    release_date = metadata['air_date']
                    logging.info(f"[RcloneProcessing] Found episode-specific air date directly in metadata: {release_date}")
                elif season_num and episode_num and metadata.get('seasons'):
                    # Navigate TMDB/TVDB structures within metadata['seasons'] - this needs adaptation based on DirectAPI output
                    # Example: Assume metadata['seasons'] is a list of season objects
                    season_data = next((s for s in metadata.get('seasons', []) if s.get('season_number') == season_num), None)
                    if season_data and season_data.get('episodes'):
                         episode_data = next((ep for ep in season_data.get('episodes', []) if ep.get('episode_number') == episode_num), None)
                         if episode_data and episode_data.get('air_date'):
                              release_date = episode_data['air_date']
                              logging.info(f"[RcloneProcessing] Found episode-specific air date in nested structure: {release_date}")

                # Fallback to show's first air date if episode date not found or not applicable
                if not release_date:
                     release_date = metadata.get('first_air_date') or metadata.get('first_aired') # Check common keys
                     if release_date:
                          logging.info(f"[RcloneProcessing] Using show's first air date as fallback: {release_date}")

            # Format the date string (handle ISO format, ensure YYYY-MM-DD)
            if release_date and isinstance(release_date, str):
                 try:
                    # Handle potential timezone info and get date part
                    if 'T' in release_date:
                         dt_obj = datetime.fromisoformat(release_date.replace('Z', '+00:00'))
                         release_date = dt_obj.strftime("%Y-%m-%d")
                    else:
                         # Validate YYYY-MM-DD format directly
                         datetime.strptime(release_date, "%Y-%m-%d")
                 except ValueError:
                      logging.warning(f"[RcloneProcessing] Release date '{release_date}' is not in a recognized YYYY-MM-DD or ISO format. Setting to None.")
                      release_date = None
            elif release_date: # Not a string? Log warning
                 logging.warning(f"[RcloneProcessing] Release date is not a string: {release_date}. Setting to None.")
                 release_date = None

        except Exception as date_err:
             logging.error(f"[RcloneProcessing] Error determining release date: {date_err}", exc_info=True)
             release_date = None # Ensure it's None on error

        logging.info(f"[RcloneProcessing] Final release date: {release_date}")

        # --- Return Results ---
        return {
            'success': True, # Metadata found successfully
            'message': f"Successfully processed file and found metadata for '{filename}'",
            'parsed_info': parsed_info,
            'metadata': metadata, # Full metadata from DirectAPI
            'source': source, # Source of the metadata (e.g., tmdb, tvdb)
            'file_path': file_path, # Original relative path passed in
            'filename': filename,
            'version': version,
            'best_match': best_match, # Trakt match info (includes IDs)
            'release_date': release_date
        }

    except Exception as e:
        logging.error(f"[RcloneProcessing] Unexpected error in process_rclone_file for '{file_path}': {str(e)}", exc_info=True)
        return {
            'success': False, # Indicate failure
            'message': f"Unexpected error processing file: {str(e)}"
        }