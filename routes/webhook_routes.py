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
from database.database_reading import get_media_item_by_id, get_media_item_presence, get_media_item_by_filename
import json
from metadata.metadata import get_release_date
import time
import requests # Added for TMDB API calls

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
    Receives a path from rclone, validates it, and adds it to a queue
    for background processing by ProgramRunner. Returns 202 Accepted immediately.
    Ignores requests if file management mode is set to Plex.
    """
    return jsonify({"status": "success"}), 200
    # function disabled for now
    
    try:
        # Check file management mode first
        file_management_mode = get_setting('File Management', 'file_collection_management', 'Symlinked/Local') # Default to avoid errors if missing
        if file_management_mode == 'Plex':
            logging.info(f"Ignoring rclone webhook because file management mode is set to 'Plex'.")
            return jsonify({
                "status": "ignored",
                "message": "Rclone webhook ignored; file management mode is 'Plex'."
            }), 200 # Return 200 OK but indicate ignored status

        file_path = request.args.get('file')
        
        if not file_path:
            logging.error("Rclone webhook received request without 'file_path'")
            return jsonify({"status": "error", "message": "No file path provided"}), 400

        # URL decode the file path
        file_path = unquote(file_path)
        logging.info(f"Received rclone webhook for path: {file_path}")

        # Strip off the leading directory (e.g., movies/ or shows/) if present
        original_received_path = file_path # Keep for logging if needed
        if '/' in file_path:
            # Split only once to handle paths like 'category/folder/file.mkv'
            # We want 'folder/file.mkv'
            prefix, path_after_prefix = file_path.split('/', 1)
            logging.debug(f"Stripped prefix '{prefix}', processing path: {path_after_prefix}")
            file_path = path_after_prefix # Use the path after the first slash
        else:
            # If no slash, assume it's already the intended relative path
             logging.debug(f"No prefix found, processing path as is: {file_path}")
             # file_path remains unchanged

        # Ensure program_runner is available via current_app
        if not hasattr(current_app, 'program_runner') or not current_app.program_runner:
            logging.error("ProgramRunner instance is not available on current_app. Cannot queue rclone path.")
            return jsonify({
                "status": "error",
                "message": "Background processor not ready"
            }), 503 # Service Unavailable

        # Add the path (relative to the base media dir, e.g., 'Movie Title (Year)/file.mkv')
        # to the ProgramRunner's pending list
        try:
            # Make sure add_pending_rclone_path exists in ProgramRunner accessed via current_app
            if hasattr(current_app.program_runner, 'add_pending_rclone_path'):
                 current_app.program_runner.add_pending_rclone_path(file_path)
                 logging.info(f"Added path '{file_path}' to pending rclone processing queue.")
            else:
                 logging.error("ProgramRunner on current_app does not have 'add_pending_rclone_path' method.")
                 # Decide how to handle this - maybe return error or log and proceed?
                 # For now, log error and return success assuming it's handled elsewhere
                 return jsonify({
                     "status": "error",
                     "message": "Background processor method missing"
                 }), 500 # Internal Server Error

        except Exception as e:
            logging.error(f"Error adding path to ProgramRunner queue: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "message": f"Failed to queue path for processing: {str(e)}"
            }), 500

        # Return 202 Accepted immediately
        return jsonify({
            "status": "accepted",
            "message": "File path received and scheduled for background processing."
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