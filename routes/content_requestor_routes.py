from flask import Blueprint, jsonify, request, render_template
from .models import user_required, onboarding_required
from web_scraper import search_trakt, parse_search_term, get_available_versions
from cli_battery.app.direct_api import DirectAPI
from config_manager import load_config
from metadata.metadata import process_metadata
from database.wanted_items import add_wanted_items
import logging
import re

content_requestor_bp = Blueprint('content', __name__)

@content_requestor_bp.route('/')
@user_required
@onboarding_required
def index():
    """Render the content requestor interface."""
    # Get available versions from config
    config = load_config()
    versions = list(config.get('Scraping', {}).get('versions', {}).keys())
    return render_template('content_requestor.html', versions=versions)

@content_requestor_bp.route('/search', methods=['POST'])
@user_required
def search():
    """Search for content using Trakt."""
    try:
        data = request.json
        search_term = data.get('search_term')
        
        if not search_term:
            return jsonify({'error': 'No search term provided'}), 400
            
        # Use the parse_search_term function from web_scraper
        base_title, season, episode, year, multi = parse_search_term(search_term)
        
        # Use the parsed title and year for search
        results = search_trakt(base_title, year)
        
        # Log the first few results for debugging
        if results:
            logging.info(f"First result: {results[0]}")
            logging.info(f"Poster URL from first result: {results[0].get('posterPath')}")
            
        return jsonify(results)
        
    except Exception as e:
        logging.error(f"Error searching for content: {str(e)}")
        return jsonify({'error': str(e)}), 500

@content_requestor_bp.route('/request', methods=['POST'])
@user_required
def request_content():
    """Handle content request."""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        tmdb_id = str(data.get('id'))
        media_type = data.get('mediaType', '').lower()
        selected_versions = data.get('versions', [])  # Get selected versions as a list
        selected_seasons = data.get('seasons', [])  # Get selected seasons if provided
        
        # Convert selected versions to dictionary format
        versions = {version: True for version in selected_versions}
                  
        logging.info(f"Received versions: {versions}")
        if selected_seasons:
            logging.info(f"Received seasons: {selected_seasons}")

        # Convert TMDB ID to IMDB ID with media type hint
        if media_type == 'movie':
            imdb_id, source = DirectAPI.tmdb_to_imdb(tmdb_id, media_type=media_type)
        else:
            imdb_id, source = DirectAPI.tmdb_to_imdb(tmdb_id, media_type='show')
        
        # Convert 'show' to 'tv' for consistency
        if media_type == 'show':
            media_type = 'tv'

        if not imdb_id:
            return jsonify({'error': f'Could not convert TMDB ID {tmdb_id} to IMDB ID for {media_type}'}), 400
            
        # Create wanted item in the format expected by process_metadata
        wanted_item = {
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'media_type': media_type
        }
        
        # If specific seasons were selected for a TV show, add them to the wanted item
        if media_type == 'tv' and selected_seasons:
            wanted_item['requested_seasons'] = selected_seasons
            
        # Process metadata
        processed_items = process_metadata([wanted_item])
        if not processed_items:
            return jsonify({'error': 'Failed to process metadata'}), 400
            
        # Combine movies and episodes from processed items
        all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
        if not all_items:
            return jsonify({'error': 'No valid items after processing'}), 400
            
        # Add content source to all items
        for item in all_items:
            item['content_source'] = 'content_requestor'
            
        # Pass versions dictionary to add_wanted_items
        add_wanted_items(all_items, versions)
        
        logging.info(f"Content request processed: TMDB ID {tmdb_id} -> IMDB ID {imdb_id} ({media_type}) with versions {versions}")
        return jsonify({'success': True, 'item': wanted_item})
        
    except Exception as e:
        logging.error(f"Error processing content request: {str(e)}")
        return jsonify({'error': str(e)}), 500

@content_requestor_bp.route('/versions', methods=['GET'])
@user_required
def get_versions():
    """Get available versions from sources."""
    try:
        versions = get_available_versions()
        logging.info(f"Returning available versions: {versions}")
        return jsonify({'versions': versions})
    except Exception as e:
        logging.error(f"Error getting versions: {str(e)}")
        return jsonify({'error': str(e)}), 500

@content_requestor_bp.route('/show_seasons', methods=['GET'])
@user_required
def get_show_seasons():
    """Get seasons available for a TV show."""
    try:
        tmdb_id = request.args.get('tmdb_id')
        logging.info(f"Fetching seasons for TMDB ID: {tmdb_id}")
        
        if not tmdb_id:
            logging.error("No TMDB ID provided")
            return jsonify({'error': 'No TMDB ID provided'}), 400
            
        # Convert TMDB ID to IMDB ID
        logging.info(f"Converting TMDB ID {tmdb_id} to IMDB ID")
        imdb_id, source = DirectAPI.tmdb_to_imdb(tmdb_id, media_type='show')
        logging.info(f"Conversion result: IMDB ID: {imdb_id}, Source: {source}")
        
        if not imdb_id:
            logging.error(f"Could not convert TMDB ID {tmdb_id} to IMDB ID")
            return jsonify({'error': f'Could not convert TMDB ID {tmdb_id} to IMDB ID'}), 400
            
        # Get show seasons from API
        logging.info(f"Fetching seasons for show with IMDB ID {imdb_id}")
        try:
            seasons_data, source = DirectAPI.get_show_seasons(imdb_id)
            logging.info(f"Got seasons data from {source}")
            logging.debug(f"Full seasons data: {seasons_data}")
        except Exception as e:
            logging.error(f"Error in DirectAPI.get_show_seasons: {str(e)}")
            return jsonify({'error': f'API error: {str(e)}'}), 500
        
        if not seasons_data:
            logging.error(f"No seasons data returned for IMDB ID {imdb_id}")
            return jsonify({'error': 'Could not retrieve seasons data: Empty response'}), 404
            
        # The seasons_data structure is different than expected
        # It has season numbers as keys directly in the dictionary
        # instead of a 'seasons' list of objects with 'season_number' property
        try:
            # Extract season numbers from the dictionary keys
            # Ensure keys are integers or can be converted to integers
            season_numbers = [int(season_num) for season_num in seasons_data.keys() if str(season_num).isdigit()]
            logging.info(f"Found season numbers in data keys: {season_numbers}")
            
            # Filter out season 0 (specials) if present
            season_numbers = [season for season in season_numbers if season > 0]
            
            logging.info(f"Found {len(season_numbers)} seasons for show with TMDB ID {tmdb_id} (IMDB ID: {imdb_id}): {season_numbers}")
            return jsonify({'success': True, 'seasons': season_numbers})
        except Exception as e:
            logging.error(f"Error processing seasons data: {str(e)}")
            return jsonify({'error': f'Error processing seasons data: {str(e)}'}), 500
        
    except Exception as e:
        logging.error(f"Error getting show seasons: {str(e)}")
        return jsonify({'error': str(e)}), 500 