from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from functools import wraps
import os
from pathlib import Path
from utilities.local_library_scan import scan_for_broken_symlinks, repair_broken_symlink
import arrow # Import arrow
from datetime import datetime # Added for strptime if needed, though arrow might handle all

from cli_battery.app.direct_api import DirectAPI
from utilities.settings import load_config
from database.database_reading import get_distinct_library_shows, get_collected_episodes_count
import logging
from .models import admin_required # Import admin_required from models

library_management = Blueprint('library_management', __name__)

@library_management.route('/library-management')
@admin_required
def manage_libraries():
    return render_template('library_management.html')

@library_management.route('/api/libraries', methods=['GET'])
@admin_required
def get_libraries():
    # TODO: Implement logic to get all configured libraries
    libraries = []  # This will be populated with actual library data
    return jsonify(libraries)

@library_management.route('/api/libraries', methods=['POST'])
@admin_required
def create_library():
    data = request.get_json()
    # TODO: Implement library creation logic
    return jsonify({'message': 'Library created successfully'})

@library_management.route('/api/libraries/<library_id>', methods=['PUT'])
@admin_required
def update_library(library_id):
    data = request.get_json()
    # TODO: Implement library update logic
    return jsonify({'message': 'Library updated successfully'})

@library_management.route('/api/libraries/<library_id>', methods=['DELETE'])
@admin_required
def delete_library(library_id):
    # TODO: Implement library deletion logic
    return jsonify({'message': 'Library deleted successfully'})

@library_management.route('/api/libraries/verify', methods=['POST'])
@admin_required
def verify_library_path():
    data = request.get_json()
    path = data.get('path')
    
    if not path:
        return jsonify({'error': 'Path is required'}), 400
        
    path_obj = Path(path)
    exists = path_obj.exists()
    is_dir = path_obj.is_dir() if exists else False
    is_symlink = path_obj.is_symlink() if exists else False
    
    return jsonify({
        'exists': exists,
        'is_directory': is_dir,
        'is_symlink': is_symlink,
        'valid': exists and is_dir
    })

@library_management.route('/api/libraries/scan-broken', methods=['POST'])
@admin_required
def scan_broken_symlinks():
    """Scan for broken symlinks in the library."""
    data = request.get_json()
    library_path = data.get('path') if data else None
    
    results = scan_for_broken_symlinks(library_path)
    return jsonify(results)

@library_management.route('/api/libraries/repair-symlink', methods=['POST'])
@admin_required
def repair_symlink():
    """Attempt to repair a broken symlink."""
    data = request.get_json()
    
    if not data or 'symlink_path' not in data:
        return jsonify({'error': 'Symlink path is required'}), 400
        
    symlink_path = data.get('symlink_path')
    new_target_path = data.get('new_target_path')  # Optional
    
    result = repair_broken_symlink(symlink_path, new_target_path)
    return jsonify(result) 

@library_management.route('/library-shows-overview')
@admin_required
def library_shows_overview_page():
    """
    Renders a page listing distinct shows in the library, optionally filtered by letter.
    Details for each show (collection status, total episodes) will be lazy-loaded via JavaScript.
    """
    try:
        selected_letter = request.args.get('letter', None)
        config = load_config() # Load config to get version names
        version_names = list(config.get('Scraping', {}).get('versions', {}).keys())
        
        library_shows = get_distinct_library_shows(letter=selected_letter)
        
        if not library_shows and selected_letter:
            logging.info(f"No distinct shows found in the library database for letter '{selected_letter}'.")
        elif not library_shows:
            logging.info("No distinct shows found in the library database for overview page.")
        
        alphabet_list = ['#'] + [chr(i) for i in range(ord('A'), ord('Z') + 1)]

        return render_template(
            'library_shows_overview.html', 
            initial_shows_data=library_shows,
            alphabet=alphabet_list,
            current_letter=selected_letter,
            version_names=version_names # Pass version names to the template
        )

    except Exception as e:
        logging.error(f"Error generating initial library shows overview page: {e}", exc_info=True)
        alphabet_list = ['#'] + [chr(i) for i in range(ord('A'), ord('Z') + 1)]
        config = load_config() # Ensure config is loaded for error case as well
        version_names = list(config.get('Scraping', {}).get('versions', {}).keys())
        return render_template(
            'library_shows_overview.html', 
            initial_shows_data=[], 
            alphabet=alphabet_list,
            current_letter=None, # No current letter on error
            version_names=version_names, # Pass version names even on error
            error_message="Failed to load show list."
        )

@library_management.route('/api/library-show-details/<imdb_id>')
@admin_required
def get_library_show_details(imdb_id):
    """
    API endpoint to fetch detailed collection status for a single show.
    """
    try:
        config = load_config()
        version_names = list(config.get('Scraping', {}).get('versions', {}).keys())
        today = arrow.utcnow().date() # Get today's date
        logging.info(f"API: Called get_library_show_details for IMDb ID: {imdb_id}. Today's date: {today}")

        # Fetch detailed metadata from DirectAPI (primarily for total episode count)
        # Also get the title from DirectAPI to ensure it's the most up-to-date canonical one for details display
        metadata, source = DirectAPI.get_show_metadata(imdb_id)
        
        show_details = {
            "imdb_id": imdb_id,
            "title": f"Details for IMDb: {imdb_id}", # Fallback title
            "error": None,
            "versions_details": [],
            "total_show_episodes": 0
        }

        if not metadata:
            logging.warning(f"API: Could not retrieve metadata via DirectAPI for IMDb ID: {imdb_id} in details endpoint.")
            show_details["error"] = "Failed to load full metadata from API; total episode count may be inaccurate."
        else:
            show_details["title"] = metadata.get('title', show_details["title"]) # Prefer API title for details
            total_episodes_from_api = 0
            seasons_map = metadata.get('seasons')

            if isinstance(seasons_map, dict):
                for season_number, season_data in seasons_map.items():
                    if isinstance(season_data, dict):
                        episodes_dict = season_data.get('episodes')
                        if isinstance(episodes_dict, dict):
                            for episode_id, episode_data in episodes_dict.items():
                                if isinstance(episode_data, dict):
                                    first_aired_str = episode_data.get('first_aired') # Use 'first_aired' from raw API output
                                    
                                    parsed_episode_date_utc = None # Initialize

                                    if first_aired_str and isinstance(first_aired_str, str): # Ensure it's a non-empty string
                                        try:
                                            arrow_dt = None
                                            # Attempt to parse with arrow.get.
                                            # If first_aired_str is naive (no 'Z' or offset), arrow.get(naive_str) makes it local.
                                            # We need to explicitly provide timezone for naive strings.
                                            
                                            # Try parsing as a full ISO string first. If it has offset, arrow.get() is fine.
                                            try:
                                                dt_attempt = arrow.get(first_aired_str)
                                                # Check if the parsed datetime is timezone-aware
                                                if dt_attempt.tzinfo is not None and dt_attempt.tzinfo.utcoffset(dt_attempt) is not None:
                                                    arrow_dt = dt_attempt # It was aware
                                                else:
                                                    # It parsed but was naive (arrow made it local). This isn't what we want for source data.
                                                    # Force re-interpretation with explicit timezone.
                                                    raise arrow.parser.ParserError("Handled as naive, needs explicit TZ")
                                            except arrow.parser.ParserError:
                                                # This means it's either truly unparsable or a naive string that arrow.get() couldn't make aware automatically.
                                                show_timezone_str = metadata.get('airs', {}).get('timezone') # e.g., 'Asia/Tokyo'
                                                
                                                if show_timezone_str:
                                                    try:
                                                        arrow_dt = arrow.get(first_aired_str, show_timezone_str)
                                                    except Exception as e_show_tz: # Catch if show_timezone_str is invalid or parse fails
                                                        logging.warning(f"Failed to parse '{first_aired_str}' with show_timezone '{show_timezone_str}': {e_show_tz}. Falling back to UTC assumption.")
                                                        arrow_dt = arrow.get(first_aired_str, 'UTC') # Fallback: assume naive is UTC
                                                else:
                                                    logging.warning(f"Show timezone missing for {imdb_id}. Assuming UTC for naive 'first_aired' string '{first_aired_str}'.")
                                                    arrow_dt = arrow.get(first_aired_str, 'UTC') # Fallback: assume naive is UTC
                                            
                                            if arrow_dt:
                                                parsed_episode_date_utc = arrow_dt.to('utc').date()

                                        except arrow.parser.ParserError as ape:
                                            logging.warning(f"API: Episode {imdb_id} S{season_number}E{episode_id} - Final Arrow ParserError for 'first_aired' string '{first_aired_str}': {ape}. Not counting.")
                                        except ValueError as ve: # Catches strptime issues if arrow internally uses it or for other parsing logic
                                            logging.warning(f"API: Episode {imdb_id} S{season_number}E{episode_id} - ValueError parsing 'first_aired' string '{first_aired_str}': {ve}. Not counting.")
                                        except Exception as e_date: # Catch any other unexpected error during parsing
                                            logging.error(f"API: Episode {imdb_id} S{season_number}E{episode_id} - Unexpected error processing 'first_aired' string '{first_aired_str}': {e_date}. Not counting.", exc_info=True)
                                    
                                    if parsed_episode_date_utc:
                                        if parsed_episode_date_utc <= today:
                                            total_episodes_from_api += 1
                                else:
                                    logging.warning(f"API: For IMDb ID {imdb_id}, Season {season_number}, episode data for E'{episode_id}' is not a dictionary (type: {type(episode_data)}). Cannot check release date. Not counting.")
                        else:
                            if episodes_dict is not None:
                                logging.warning(f"API: For IMDb ID {imdb_id}, Season {season_number}, 'episodes' field is not a dictionary (type: {type(episodes_dict)}). Cannot count episodes for this season.")
                            else:
                                logging.warning(f"API: For IMDb ID {imdb_id}, Season {season_number}, 'episodes' field is missing. Cannot count episodes for this season.")
                    else:
                        logging.warning(f"API: For IMDb ID {imdb_id}, data for season {season_number} is not a dictionary (type: {type(season_data)}).")
            elif seasons_map is not None:
                logging.warning(f"API: For IMDb ID {imdb_id}, 'seasons' field is not a dictionary (type: {type(seasons_map)}).")
            else:
                logging.warning(f"API: For IMDb ID {imdb_id}, 'seasons' field is missing. Total episodes will be 0 from API.")
            
            show_details["total_show_episodes"] = total_episodes_from_api

        current_versions_details = []
        total_episodes_for_status = show_details["total_show_episodes"]

        for version_name in version_names:
            collected_count = get_collected_episodes_count(imdb_id, version_name)
            current_versions_details.append({
                "name": version_name,
                "collected_episodes": collected_count,
                "total_episodes_for_version": total_episodes_for_status,
                "status_text": f"{collected_count}/{total_episodes_for_status if total_episodes_for_status > 0 else 'N/A'}"
            })
        show_details["versions_details"] = current_versions_details
        
        return jsonify(show_details)

    except Exception as e:
        logging.error(f"Error fetching details for show {imdb_id}: {e}", exc_info=True)
        # Ensure a 500 error response in case of unexpected failure
        return jsonify({"error": "An unexpected server error occurred", "imdb_id": imdb_id, "details": str(e)}), 500