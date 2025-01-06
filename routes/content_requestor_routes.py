from flask import Blueprint, jsonify, request, render_template
from .models import user_required, onboarding_required
from web_scraper import search_trakt
from cli_battery.app.direct_api import DirectAPI
from config_manager import load_config
from metadata.metadata import process_metadata
from database.wanted_items import add_wanted_items
import logging

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
            
        results = search_trakt(search_term)
        
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
        versions = data.get('versions', [])  # Get selected versions
        
        # Convert 'show' to 'tv' for consistency
        if media_type == 'show':
            media_type = 'tv'
            
        # Convert TMDB ID to IMDB ID with media type hint
        imdb_id, source = DirectAPI.tmdb_to_imdb(tmdb_id, media_type=media_type)
        
        if not imdb_id:
            return jsonify({'error': f'Could not convert TMDB ID {tmdb_id} to IMDB ID for {media_type}'}), 400
            
        # Create wanted item in the format expected by process_metadata
        wanted_item = {
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'media_type': media_type
        }
        
        # Process metadata
        processed_items = process_metadata([wanted_item])
        if not processed_items:
            return jsonify({'error': 'Failed to process metadata'}), 400
            
        # Combine movies and episodes from processed items
        all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
        if not all_items:
            return jsonify({'error': 'No valid items after processing'}), 400
            
        # Add items to wanted items database
        add_wanted_items(all_items, versions)
        
        logging.info(f"Content request processed: TMDB ID {tmdb_id} -> IMDB ID {imdb_id} ({media_type}) with versions {versions}")
        return jsonify({'success': True, 'item': wanted_item})
        
    except Exception as e:
        logging.error(f"Error processing content request: {str(e)}")
        return jsonify({'error': str(e)}), 500

@content_requestor_bp.route('/versions', methods=['GET'])
@user_required
def get_versions():
    """Get available versions from config."""
    try:
        config = load_config()
        versions = list(config.get('Scraping', {}).get('versions', {}).keys())
        return jsonify({'versions': versions})
    except Exception as e:
        logging.error(f"Error getting versions: {str(e)}")
        return jsonify({'error': str(e)}), 500 