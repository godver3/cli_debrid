from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from functools import wraps
import os
from pathlib import Path
from utilities.local_library_scan import scan_for_broken_symlinks, repair_broken_symlink

from cli_battery.app.direct_api import DirectAPI
from utilities.settings import load_config
from database.database_reading import get_distinct_library_shows, get_collected_episodes_count
import logging

library_management = Blueprint('library_management', __name__)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return jsonify({'error': 'Admin privileges required'}), 403
        return f(*args, **kwargs)
    return decorated_function

@library_management.route('/library-management')
@login_required
@admin_required
def manage_libraries():
    return render_template('library_management.html')

@library_management.route('/api/libraries', methods=['GET'])
@login_required
@admin_required
def get_libraries():
    # TODO: Implement logic to get all configured libraries
    libraries = []  # This will be populated with actual library data
    return jsonify(libraries)

@library_management.route('/api/libraries', methods=['POST'])
@login_required
@admin_required
def create_library():
    data = request.get_json()
    # TODO: Implement library creation logic
    return jsonify({'message': 'Library created successfully'})

@library_management.route('/api/libraries/<library_id>', methods=['PUT'])
@login_required
@admin_required
def update_library(library_id):
    data = request.get_json()
    # TODO: Implement library update logic
    return jsonify({'message': 'Library updated successfully'})

@library_management.route('/api/libraries/<library_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_library(library_id):
    # TODO: Implement library deletion logic
    return jsonify({'message': 'Library deleted successfully'})

@library_management.route('/api/libraries/verify', methods=['POST'])
@login_required
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
@login_required
@admin_required
def scan_broken_symlinks():
    """Scan for broken symlinks in the library."""
    data = request.get_json()
    library_path = data.get('path') if data else None
    
    results = scan_for_broken_symlinks(library_path)
    return jsonify(results)

@library_management.route('/api/libraries/repair-symlink', methods=['POST'])
@login_required
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
@login_required
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
@login_required
@admin_required
def get_library_show_details(imdb_id):
    """
    API endpoint to fetch detailed collection status for a single show.
    """
    try:
        config = load_config()
        version_names = list(config.get('Scraping', {}).get('versions', {}).keys())

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
            # Attempt to get title from DB as a fallback if API fails completely
            # This requires a small DB query, or we assume the client already has a title.
            # For simplicity, we'll let the frontend handle the originally displayed title if API fails.
        else:
            show_details["title"] = metadata.get('title', show_details["title"]) # Prefer API title for details
            total_episodes_from_api = 0
            # metadata is guaranteed to be non-None here due to the surrounding if/else
            seasons_map = metadata.get('seasons') # Expects a dict like {1: season_data, 2: season_data}

            if isinstance(seasons_map, dict):
                for season_number, season_data in seasons_map.items():
                    if isinstance(season_data, dict):
                        episodes_dict = season_data.get('episodes')
                        if isinstance(episodes_dict, dict):
                            total_episodes_from_api += len(episodes_dict)
                        else:
                            # Log if 'episodes' exists but isn't a dict, or is missing.
                            if episodes_dict is not None:
                                logging.warning(f"API: For IMDb ID {imdb_id}, Season {season_number}, 'episodes' field is not a dictionary (type: {type(episodes_dict)}). Cannot count episodes for this season.")
                            # else: 'episodes' key missing, count remains unchanged.
                    else:
                        logging.warning(f"API: For IMDb ID {imdb_id}, data for season {season_number} is not a dictionary (type: {type(season_data)}).")
            elif seasons_map is not None: # 'seasons' key exists but is not a dict
                logging.warning(f"API: For IMDb ID {imdb_id}, 'seasons' field is not a dictionary (type: {type(seasons_map)}).")
            # If seasons_map is None (key 'seasons' was missing), total_episodes_from_api remains 0.
            
            show_details["total_show_episodes"] = total_episodes_from_api

        # Fetch collection counts for each version
        # This part runs even if DirectAPI metadata failed, to show whatever collection data exists.
        current_versions_details = []
        # We need total_show_episodes for the status text, use the one from API if available.
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
        return jsonify({"error": "Failed to load show details", "imdb_id": imdb_id, "details": str(e)}), 500