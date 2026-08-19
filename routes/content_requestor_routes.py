from flask import Blueprint, jsonify, request, render_template
from flask_login import current_user
from .models import user_required, onboarding_required
from .utils import is_user_system_enabled
from utilities.web_scraper import search_trakt, parse_search_term, get_available_versions
from cli_battery.app.direct_api import DirectAPI
from database.wanted_items import add_wanted_items
import logging
import re
from utilities.settings import load_config

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
    from metadata.metadata import process_metadata
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        tmdb_id = str(data.get('id'))
        media_type = data.get('mediaType', '').lower()
        selected_versions = data.get('versions', [])  # Get selected versions as a list
        selected_seasons = data.get('seasons', [])  # Get selected seasons if provided
        selected_folder = data.get('selected_folder')  # Custom folder for symlink mode
        selected_folder_is_custom = data.get('selected_folder_is_custom', False)
        selected_tags = data.get('selected_tags') or None  # Tags for Plex mode NZB routing

        # Convert selected versions to dictionary format
        versions = {version: True for version in selected_versions}

        logging.info(f"Received versions: {versions}")
        if selected_seasons:
            logging.info(f"Received seasons: {selected_seasons}")
        if selected_folder:
            logging.info(f"Received folder: {selected_folder} (custom={selected_folder_is_custom})")

        # Convert TMDB ID to IMDB ID with media type hint
        if media_type == 'movie':
            imdb_id, source = DirectAPI.tmdb_to_imdb(tmdb_id, media_type=media_type)
        else:
            imdb_id, source = DirectAPI.tmdb_to_imdb(tmdb_id, media_type='show')
        
        # Convert 'show' to 'tv' for consistency
        if media_type == 'show':
            media_type = 'tv'

        if not imdb_id:
            # Check if any Jackett scrapers are enabled
            config = load_config()
            has_enabled_jackett = False
            
            for instance, settings in config.get('Scrapers', {}).items():
                if isinstance(settings, dict):
                    if settings.get('type') == 'Jackett' and settings.get('enabled', False):
                        has_enabled_jackett = True
                        break
            
            # Get the title directly from the request data
            title = data.get('title', '')
            
            # Only proceed if Jackett is enabled AND title contains UFC
            if not has_enabled_jackett or 'UFC' not in title.upper():
                return jsonify({'error': f'Could not convert TMDB ID {tmdb_id} to IMDB ID for {media_type}. This is only supported for UFC content with Jackett enabled.'}), 400
            else:
                logging.info(f"No IMDB ID found for UFC content with TMDB ID {tmdb_id}, proceeding with Jackett scraper(s)")
            
        # If media_type is 'tv' and no specific seasons are selected (i.e., "whole show"),
        # fetch all available seasons.
        if media_type == 'tv' and not selected_seasons and imdb_id:
            logging.info(f"No specific seasons selected for TMDB ID {tmdb_id} (IMDB ID: {imdb_id}). Fetching all available seasons.")
            try:
                seasons_data, _ = DirectAPI.get_show_seasons(imdb_id)
                if seasons_data:
                    # Extract season numbers, filtering out season 0 (specials)
                    all_season_numbers = [int(season_num) for season_num in seasons_data.keys() if str(season_num).isdigit() and int(season_num) > 0]
                    if all_season_numbers:
                        selected_seasons = all_season_numbers
                        logging.info(f"Fetched all available seasons for IMDB ID {imdb_id}: {selected_seasons}")
                    else:
                        logging.warning(f"No valid seasons found for IMDB ID {imdb_id} after fetching all seasons.")
                else:
                    logging.warning(f"Could not retrieve seasons data for IMDB ID {imdb_id} when fetching all seasons.")
                    
                    # Try fallback: force refresh metadata to get seasons data
                    logging.info(f"Attempting fallback metadata refresh for {imdb_id} to get seasons data...")
                    try:
                        refreshed_metadata, _ = DirectAPI.force_refresh_metadata(imdb_id)
                        if refreshed_metadata and 'seasons' in refreshed_metadata:
                            refreshed_seasons = refreshed_metadata['seasons']
                            if isinstance(refreshed_seasons, dict) and refreshed_seasons:
                                # Extract season numbers from the refreshed data
                                refreshed_season_numbers = [int(season_num) for season_num in refreshed_seasons.keys() if str(season_num).isdigit() and int(season_num) > 0]
                                if refreshed_season_numbers:
                                    selected_seasons = refreshed_season_numbers
                                    logging.info(f"Successfully fetched {len(refreshed_season_numbers)} seasons via fallback for IMDB ID {imdb_id}: {refreshed_season_numbers}")
                                else:
                                    logging.warning(f"Fallback seasons fetch returned no valid season numbers for IMDB ID {imdb_id}")
                            else:
                                logging.warning(f"Fallback seasons fetch returned invalid seasons data for IMDB ID {imdb_id}")
                        else:
                            logging.warning(f"Fallback metadata refresh failed or returned no seasons data for IMDB ID {imdb_id}")
                    except Exception as fallback_error:
                        logging.error(f"Error during fallback seasons fetch for IMDB ID {imdb_id}: {fallback_error}")
            except Exception as e:
                logging.error(f"Error fetching all seasons for IMDB ID {imdb_id}: {str(e)}")
                # Proceed without pre-populating seasons, behavior will be as before for this case.

        # Create wanted item in the format expected by process_metadata
        wanted_item = {
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'media_type': media_type,
            'title': data.get('title'),  # Title from frontend
            'year': data.get('year'),  # Year from frontend
            'release_date': data.get('releaseDate'),  # Release date from frontend
            'overview': data.get('overview'),  # Overview/description if available
            'vote_average': data.get('voteAverage'),  # Rating if available
            'backdrop_path': data.get('backdropPath')  # Backdrop image path if available
        }
        
        # Handle genres formatting
        genres = data.get('genres', [])
        if isinstance(genres, str):
            # If genres come as comma-separated string, convert to list
            genres = [g.strip() for g in genres.split(',')]
        elif not isinstance(genres, list):
            genres = []
        wanted_item['genres'] = genres
        
        # If specific seasons were selected for a TV show, add them to the wanted item
        if media_type == 'tv' and selected_seasons:
            wanted_item['requested_seasons'] = selected_seasons
            
        # Add the versions to the wanted_item
        wanted_item['versions'] = versions
            
        # Process metadata
        processed_items = process_metadata([wanted_item])

        # If metadata is missing or empty, auto-refresh battery and retry once — for both movies and TV shows.
        # This handles the case where the item has never been in the library (no library page to refresh from).
        all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) if processed_items else []
        if not all_items and imdb_id:
            logging.info(f"No items from process_metadata for {imdb_id} — forcing TMDB mapping refresh and battery refresh, then retrying.")
            try:
                # Step 1: Force-refresh the TMDB→IMDB mapping in case it was stale/wrong.
                # This clears the cached entry and re-fetches from Trakt API.
                refresh_media_type = 'movie' if media_type == 'movie' else 'show'
                refreshed_imdb_id, refresh_source = DirectAPI.force_refresh_tmdb_mapping(tmdb_id, media_type=refresh_media_type)
                if refreshed_imdb_id and refreshed_imdb_id != imdb_id:
                    logging.info(f"TMDB mapping refreshed: {imdb_id} → {refreshed_imdb_id} (source: {refresh_source}). Updating wanted item.")
                    imdb_id = refreshed_imdb_id
                    wanted_item['imdb_id'] = imdb_id
                elif refreshed_imdb_id:
                    logging.info(f"TMDB mapping refresh confirmed same IMDB ID: {imdb_id}")
                else:
                    logging.warning(f"TMDB mapping refresh returned no IMDB ID for TMDB {tmdb_id}, keeping {imdb_id}")

                # Step 2: Force-refresh battery metadata for the (possibly corrected) IMDB ID.
                DirectAPI.force_refresh_metadata(imdb_id)
                processed_items = process_metadata([wanted_item])
                if processed_items:
                    all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
            except Exception as e_refresh:
                logging.warning(f"Auto-refresh failed for {imdb_id}: {e_refresh}")

        if not all_items:
            logging.warning(f"No processable items found after metadata processing for TMDB ID {tmdb_id}.")
            return jsonify({'error': 'Could not retrieve metadata for this title. Please try again in a moment.'}), 400
            
        # Add content source to all items.
        # When user system is enabled, record the requesting user's username as the detail
        # so each user's requests get their own Plex label. Falls back to 'CD-Discover'.
        if is_user_system_enabled() and current_user.is_authenticated:
            source_detail = current_user.username
        else:
            source_detail = 'CD-Discover'
        for item in all_items:
            item['content_source'] = 'content_requester'
            item['content_source_detail'] = source_detail
            if selected_folder:
                item['selected_folder'] = selected_folder
                item['selected_folder_is_custom'] = selected_folder_is_custom
            if selected_tags:
                item['tags'] = selected_tags

        # Pass versions dictionary to add_wanted_items
        items_added = add_wanted_items(all_items, versions)

        # If nothing was added for a TV show, the battery may have stale/incomplete episode data.
        # Force-refresh and retry once to pick up any missing episodes.
        if items_added == 0 and media_type == 'tv' and imdb_id:
            logging.info(f"No new items added for TV show {imdb_id} (battery may be stale) — forcing refresh and retrying.")
            try:
                DirectAPI.force_refresh_metadata(imdb_id)
                processed_items = process_metadata([wanted_item])
                if processed_items:
                    all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
                    for item in all_items:
                        item['content_source'] = 'content_requester'
                        item['content_source_detail'] = source_detail
                        if selected_folder:
                            item['selected_folder'] = selected_folder
                            item['selected_folder_is_custom'] = selected_folder_is_custom
                        if selected_tags:
                            item['tags'] = selected_tags
                    items_added = add_wanted_items(all_items, versions)
                    logging.info(f"After refresh, added {items_added} items for {imdb_id}")
            except Exception as e_refresh:
                logging.warning(f"Post-add refresh failed for {imdb_id}: {e_refresh}")

        logging.info(f"Content request processed: TMDB ID {tmdb_id} -> IMDB ID {imdb_id} ({media_type}) with versions {versions}, items added: {items_added}")
        try:
            from utilities.ai_habits import track_action
            _uid = current_user.username if is_user_system_enabled() and current_user.is_authenticated else 'system'
            _detail = f"{data.get('title', '')} ({data.get('year', '')}) [{media_type}]"
            track_action('library_add_manual', detail=_detail, user_id=_uid)
        except Exception:
            pass
        if items_added == 0:
            return jsonify({
                'success': True,
                'item': wanted_item,
                'items_added': 0,
                'message': f"Already have {data.get('title', 'this title')} in the requested version(s)"
            })
        return jsonify({'success': True, 'item': wanted_item, 'items_added': items_added})
        
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
            
            # Try fallback: force refresh metadata to get seasons data
            logging.info(f"Attempting fallback metadata refresh for {imdb_id} to get seasons data...")
            try:
                refreshed_metadata, _ = DirectAPI.force_refresh_metadata(imdb_id)
                if refreshed_metadata and 'seasons' in refreshed_metadata:
                    refreshed_seasons = refreshed_metadata['seasons']
                    if isinstance(refreshed_seasons, dict) and refreshed_seasons:
                        # Extract season numbers from the refreshed data
                        refreshed_season_numbers = [int(season_num) for season_num in refreshed_seasons.keys() if str(season_num).isdigit() and int(season_num) > 0]
                        if refreshed_season_numbers:
                            # Filter out season 0 (specials) if present
                            refreshed_season_numbers = [season for season in refreshed_season_numbers if season > 0]
                            logging.info(f"Successfully fetched {len(refreshed_season_numbers)} seasons via fallback for IMDB ID {imdb_id}: {refreshed_season_numbers}")
                            return jsonify({'success': True, 'seasons': refreshed_season_numbers})
                        else:
                            logging.warning(f"Fallback seasons fetch returned no valid season numbers for IMDB ID {imdb_id}")
                    else:
                        logging.warning(f"Fallback seasons fetch returned invalid seasons data for IMDB ID {imdb_id}")
                else:
                    logging.warning(f"Fallback metadata refresh failed or returned no seasons data for IMDB ID {imdb_id}")
            except Exception as fallback_error:
                logging.error(f"Error during fallback seasons fetch for IMDB ID {imdb_id}: {fallback_error}")
            
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