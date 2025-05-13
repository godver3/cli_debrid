from flask import jsonify, request, Blueprint, current_app
import logging
from queues.run_program import process_overseerr_webhook
from routes.extensions import app
from queues.queue_manager import QueueManager
from utilities.local_library_scan import check_local_file_for_item, get_symlink_path, create_symlink
from utilities.plex_functions import plex_update_item
from utilities.emby_functions import emby_update_item
from utilities.settings import get_setting
from urllib.parse import unquote
import os.path
from content_checkers.overseerr import get_overseerr_details, get_overseerr_headers
from routes.api_tracker import api
from typing import Dict, Any
from scraper.functions.ptt_parser import parse_with_ptt
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.direct_api import DirectAPI
from fuzzywuzzy import fuzz
from database.core import get_db_connection
from utilities.reverse_parser import parse_filename_for_version
from datetime import datetime, timezone
from database.database_writing import update_media_item_state, add_media_item, update_release_date_and_state
from database.database_reading import get_media_item_by_id, get_media_item_presence, get_media_item_by_filename, check_item_exists_by_directory_name
import json
import time
import requests # Added for TMDB API calls
import threading # Added for background task
import uuid # Added for task_id generation
from pathlib import Path # Added for path manipulation
from routes.debug_routes import _run_rclone_to_symlink_task # Added for the rclone processing task
import os # Added for os.path.basename and os.path.join

webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/', methods=['POST'])
def webhook():
    data = request.json
    logging.debug(f"Received webhook: {data}")
    try:
        # Handle test notifications separately
        if data.get('notification_type') == 'TEST_NOTIFICATION':
            logging.info("Received test notification from Overseerr")
            return jsonify({"status": "success", "message": "Test notification received"}), 200

        # If this is a TV show request, look for season information
        if data.get('media', {}).get('media_type') == 'tv':
            # Look for season information in the extra field
            extra_items = data.get('extra', [])
            for item in extra_items:
                if item.get('name') == 'Requested Seasons':
                    try:
                        # The value could be a single season or a comma-separated list
                        seasons_str = item.get('value', '')
                        requested_seasons = [int(s.strip()) for s in seasons_str.split(',')]
                        if requested_seasons:
                            # Add to media section
                            data['media']['requested_seasons'] = requested_seasons
                            logging.info(f"Added season information to webhook data: {requested_seasons}")
                    except ValueError as e:
                        logging.error(f"Error parsing season information from webhook: {str(e)}")

            # Only process if partial requests are allowed
            if get_setting('debug', 'allow_partial_overseerr_requests', False):
                logging.info("Partial requests are not allowed, clearing requested seasons")
                if 'requested_seasons' in data['media']:
                    del data['media']['requested_seasons']

        # Mark this request as coming from Overseerr
        if data.get('media'):
            data['media']['from_overseerr'] = True

        logging.debug(f"Final webhook data before processing: {data}")
        process_overseerr_webhook(data)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@webhook_bp.route('/rclone', methods=['POST', 'GET'])
def rclone_webhook():
    """
    Receives a relative path from rclone, extracts the final directory component,
    checks if any item for this directory name already exists in the DB based on title fields, 
    and if not, triggers a background task to process it.
    Returns 202 Accepted immediately if processing is initiated.
    Ignores requests if file management mode is set to Plex.
    Ignores requests if an item matching the directory name already exists in the database.
    """
    try:
        # Check file management mode first
        file_management_mode = get_setting('File Management', 'file_collection_management', 'Symlinked/Local')
        if file_management_mode == 'Plex':
            logging.info("Ignoring rclone webhook because file management mode is set to 'Plex'.")
            return jsonify({
                "status": "ignored",
                "message": "Rclone webhook ignored; file management mode is 'Plex'."
            }), 200

        full_relative_path_str = request.args.get('file')
        
        if not full_relative_path_str:
            logging.error("Rclone webhook received request without 'file' argument for the relative media path.")
            return jsonify({"status": "error", "message": "No relative media path provided via 'file' argument"}), 400

        full_relative_path_str = unquote(full_relative_path_str)
        logging.info(f"Received rclone webhook for relative path: {full_relative_path_str}")

        # This component is assumed to be the item's directory name (e.g., "Movie Title (Year)")
        # relative to the original_files_base_path categories (like "movies", "tv").
        # Or, if original_files_base_path directly contains item folders, then this is the item folder name.
        final_dir_component = os.path.basename(full_relative_path_str)
        if not final_dir_component:
             logging.error(f"Could not extract final directory component from path: {full_relative_path_str}")
             return jsonify({"status": "error", "message": "Could not determine final directory name from provided path."}), 400
        logging.info(f"Extracted final directory component (assumed item folder name): {final_dir_component}")

        original_files_base_path = get_setting('File Management', 'original_files_path')
        if not original_files_base_path:
             logging.error("The 'original_files_path' setting under [File Management] is not configured. Cannot determine the full path.")
             return jsonify({"status": "error", "message": "Original files base path setting is missing."}), 500
        
        # This is the absolute path to the directory that rclone is reporting and needs scanning.
        # Example: /mnt/zurg/__all__ + Movie Title (Year) = /mnt/zurg/__all__/Movie Title (Year)
        # OR if full_relative_path_str was "movies/Movie Title (Year)", then final_dir_component is "Movie Title (Year)",
        # and this path assumes original_files_base_path is /mnt/zurg/__all__ (not /mnt/zurg/__all__/movies).
        # This path is used for the DB check and passed to the symlink task.
        absolute_item_dir_path = os.path.join(original_files_base_path, final_dir_component)
        logging.info(f"Constructed absolute item directory path to check/scan: {absolute_item_dir_path}")

        # --- Check database for existing item matching the directory name component against title fields ---
        if check_item_exists_by_directory_name(final_dir_component):
            logging.info(f"Ignoring rclone webhook for directory component '{final_dir_component}'. Item(s) with this name found in 'filled_by_title' or 'real_debrid_original_title'.")
            return jsonify({
                "status": "ignored",
                "message": f"Item(s) matching directory name '{final_dir_component}' already exist based on title fields."
            }), 200
        else:
            logging.info(f"No existing database entries found matching directory/title name '{final_dir_component}'. Proceeding with webhook processing.")
        # --- End database check ---

        symlink_base_path_str = get_setting('File Management', 'symlinked_files_path')
        if not symlink_base_path_str:
            symlink_base_path_str = "/mnt/symlinked_media" 
            logging.warning(f"Symlink base path not found in settings, using default: {symlink_base_path_str}")
        
        task_id = str(uuid.uuid4())
        dry_run = False

        logging.info(f"Initiating rclone to symlinks task. Task ID: {task_id}, Scan Dir: {absolute_item_dir_path}, Symlink Base: {symlink_base_path_str}")

        thread = threading.Thread(
            target=_run_rclone_to_symlink_task,
            args=(absolute_item_dir_path, symlink_base_path_str, dry_run, task_id, True)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            "status": "accepted",
            "message": f"Relative path received. Background task (ID: {task_id}) initiated to process directory: {absolute_item_dir_path}.",
            "task_id": task_id
        }), 202

    except Exception as e:
        logging.error(f"Error processing rclone webhook request: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500

@webhook_bp.route('/plex_scan', methods=['POST'])
def plex_scan_webhook():
    """
    Webhook endpoint for the custom Plex scanner.
    Receives a file path, looks it up in the database, fetches detailed metadata
    from TMDB if it's a movie, and returns the combined media info.
    """
    data = request.json
    file_path = data.get('file_path')

    if not file_path:
        logging.error("Plex scan webhook received request without 'file_path'")
        return jsonify({"status": "error", "message": "Missing 'file_path' in request"}), 400

    filename = os.path.basename(file_path)
    logging.info(f"Plex scan webhook received request for file: {filename} (Full path: {file_path})" )

    try:
        media_item = get_media_item_by_filename(filename)

        if not media_item:
            logging.warning(f"No database entry found for filename: {filename}")
            return jsonify({"status": "error", "message": f"Media item not found for file: {filename}"}), 404

        logging.info(f"Found media item for {filename}: ID {media_item.get('id')}, Title: {media_item.get('title')}, Type: {media_item.get('type')}")

        # Initialize response with basic data from DB
        response_data = {
            "status": "success",
            "imdb_id": media_item.get('imdb_id'),
            "tmdb_id": media_item.get('tmdb_id'),
            "tvdb_id": media_item.get('tvdb_id'),
            "type": media_item.get('type'),
            "title": media_item.get('title'),
            "year": media_item.get('year'),
        }

        # If it's a movie, try to fetch enhanced metadata from TMDB
        if response_data['type'] == 'movie' and response_data['tmdb_id']:
            tmdb_id = response_data['tmdb_id']
            logging.info(f"Attempting to fetch enhanced TMDB data for movie TMDB ID: {tmdb_id}")
            
            tmdb_api_key = get_setting('TMDB', 'api_key', '')
            if not tmdb_api_key:
                logging.warning("TMDB API key is missing. Cannot fetch enhanced metadata.")
            else:
                # Append ',videos' to get trailer information
                tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&append_to_response=credits,images,videos"
                try:
                    tmdb_response = requests.get(tmdb_url, timeout=15)
                    tmdb_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                    
                    tmdb_data = tmdb_response.json()
                    logging.info(f"Successfully fetched TMDB data for {tmdb_id}")

                    # --- Extract and add enhanced data --- 
                    response_data['summary'] = tmdb_data.get('overview')
                    response_data['tagline'] = tmdb_data.get('tagline')
                    response_data['rating'] = tmdb_data.get('vote_average')
                    response_data['originally_available_at'] = tmdb_data.get('release_date')

                    if tmdb_data.get('genres'):
                        response_data['genres'] = [genre['name'] for genre in tmdb_data['genres']]
                        
                    if tmdb_data.get('belongs_to_collection'):
                         response_data['collection'] = tmdb_data['belongs_to_collection'].get('name')

                    # Extract credits
                    directors = []
                    writers = []
                    if tmdb_data.get('credits') and tmdb_data['credits'].get('crew'):
                        for crew_member in tmdb_data['credits']['crew']:
                            if crew_member.get('job') == 'Director':
                                directors.append(crew_member['name'])
                            # Combine multiple writing roles
                            if crew_member.get('department') == 'Writing' and crew_member.get('name') not in writers:
                                writers.append(crew_member['name'])
                    response_data['directors'] = directors
                    response_data['writers'] = writers
                    
                    # --- Extract Trailers (YouTube only) ---
                    trailers = []
                    if tmdb_data.get('videos') and tmdb_data['videos'].get('results'):
                        for video in tmdb_data['videos']['results']:
                            # Prioritize official trailers on YouTube
                            if video.get('site') == 'YouTube' and video.get('type') == 'Trailer' and video.get('official') and video.get('key'):
                                trailers.append({
                                    'key': video['key'],
                                    'site': video['site'], # Should be 'YouTube'
                                    'type': video['type'], # Should be 'Trailer'
                                    'name': video.get('name', 'Trailer') # Include name if available
                                })
                    # Add other types if no official trailer found (optional, keeping it simple for now)
                    response_data['trailers'] = trailers
                    logging.info(f"Extracted {len(trailers)} YouTube trailers.")
                    # --- End Trailer Extraction ---

                    # Extract images (file paths only)
                    posters = []
                    art = []
                    if tmdb_data.get('images'):
                        # Sort posters by vote_average desc, take English first if available
                        sorted_posters = sorted(tmdb_data['images'].get('posters', []), key=lambda x: x.get('vote_average', 0), reverse=True)
                        english_posters = [p['file_path'] for p in sorted_posters if p.get('iso_639_1') == 'en']
                        other_posters = [p['file_path'] for p in sorted_posters if p.get('iso_639_1') != 'en']
                        response_data['posters'] = english_posters + other_posters # Return list of file_paths

                        # Sort backdrops similarly
                        sorted_art = sorted(tmdb_data['images'].get('backdrops', []), key=lambda x: x.get('vote_average', 0), reverse=True)
                        english_art = [a['file_path'] for a in sorted_art if a.get('iso_639_1') == 'en' or a.get('iso_639_1') is None or a.get('iso_639_1') == ''] # Include null/empty lang
                        other_art = [a['file_path'] for a in sorted_art if a.get('iso_639_1') not in ['en', None, '']]
                        response_data['art'] = english_art + other_art # Return list of file_paths
                        
                    logging.debug(f"Enhanced response data prepared: {response_data}")

                except requests.exceptions.RequestException as e:
                    logging.error(f"Error fetching TMDB data for {tmdb_id}: {e}")
                except Exception as e:
                     logging.error(f"Error processing TMDB data for {tmdb_id}: {e}", exc_info=True)
                     
        # For episodes, add basic season/episode info if available
        elif response_data['type'] == 'episode':
            response_data.update({
                "season_number": media_item.get('season_number'),
                "episode_number": media_item.get('episode_number'),
                "show_title": media_item.get('show_title'), 
                "show_year": media_item.get('show_year')
            })
            logging.info(f"Returning basic episode info for {filename}")
            # TODO: Optionally fetch enhanced TV episode data from TMDB here if needed

        return jsonify(response_data), 200

    except Exception as e:
        logging.error(f"Error processing Plex scan webhook for file {filename}: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500