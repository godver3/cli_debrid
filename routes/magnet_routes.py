from flask import Blueprint, request, render_template, flash, redirect, url_for, jsonify, session
from debrid import get_debrid_provider
from database.database_writing import add_media_item, update_media_item_torrent_id
from .models import admin_required
from queues.config_manager import load_config
from queues.checking_queue import CheckingQueue
from datetime import datetime, timezone
from queues.media_matcher import MediaMatcher
import logging
from cli_battery.app.direct_api import DirectAPI
from cli_battery.app.trakt_metadata import TraktMetadata
import os
import re
from database.torrent_tracking import record_torrent_addition, update_torrent_tracking, get_torrent_history
from content_checkers.content_source_detail import append_content_source_detail
from scraper.functions.ptt_parser import parse_with_ptt
import requests 
from content_checkers.trakt import get_trakt_headers, TRAKT_API_URL, REQUEST_TIMEOUT
import json
from fuzzywuzzy import fuzz
import asyncio
from utilities.web_scraper import get_media_meta
from utilities.settings import get_setting
from typing import List, Dict, Optional, Any

magnet_bp = Blueprint('magnet', __name__)

async def _fetch_media_details_for_assigner(id_value: str, id_kind: str, content_type_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Helper to fetch media details based on IMDb or TMDb ID for magnet assigner.
    Returns a list containing a single result dict if found, else empty list.
    """
    from metadata.metadata import get_metadata
    tmdb_api_key = get_setting('TMDB', 'api_key')
    has_tmdb = bool(tmdb_api_key)
    results = []
    metadata_result = None
    determined_media_type = None # Will be 'movie' or 'tv'

    try:
        if id_kind == 'imdb':
            # Try movie first, then tv for IMDb ID
            metadata_result = get_metadata(imdb_id=id_value, item_media_type='movie')
            if metadata_result and metadata_result.get('tmdb_id'): # Check if tmdb_id exists
                determined_media_type = 'movie'
            else:
                metadata_result = get_metadata(imdb_id=id_value, item_media_type='tv')
                if metadata_result and metadata_result.get('tmdb_id'):
                    determined_media_type = 'tv'
        elif id_kind == 'tmdb':
            try:
                tmdb_id_int = int(id_value)
                if content_type_hint == 'movie':
                    metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='movie')
                    if metadata_result and metadata_result.get('tmdb_id'):
                        determined_media_type = 'movie'
                elif content_type_hint == 'show' or content_type_hint == 'tv':
                    metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='tv')
                    if metadata_result and metadata_result.get('tmdb_id'):
                        determined_media_type = 'tv'
                else: # No hint, try movie then tv
                    metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='movie')
                    if metadata_result and metadata_result.get('tmdb_id'):
                        determined_media_type = 'movie'
                    else:
                        metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='tv')
                        if metadata_result and metadata_result.get('tmdb_id'):
                            determined_media_type = 'tv'
                
                # If get_metadata failed (returned empty dict), try with dummy IMDb ID
                if not metadata_result or not metadata_result.get('tmdb_id'):
                    logging.warning(f"Could not get metadata for TMDB ID {tmdb_id_int}. Trying with dummy IMDb ID...")
                    # Generate a dummy IMDb ID and try to get basic metadata
                    dummy_imdb_id = f"tt{tmdb_id_int:07d}"
                    logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id_int}")
                    
                    # Try to get basic TMDB metadata using get_media_meta
                    try:
                        media_type_for_meta = content_type_hint or 'movie'
                        media_meta_tuple = await asyncio.to_thread(
                            get_media_meta, str(tmdb_id_int), media_type_for_meta
                        )
                        
                        if media_meta_tuple:
                            poster_path, overview, genres, vote_average, backdrop_path = media_meta_tuple
                            # Create minimal metadata with dummy IMDb ID
                            metadata_result = {
                                'tmdb_id': tmdb_id_int,
                                'imdb_id': dummy_imdb_id,
                                'title': f'TMDB {tmdb_id_int}',  # Fallback title
                                'year': None,
                                'genres': genres or [],
                                'overview': overview or '',
                                'poster': poster_path
                            }
                            determined_media_type = 'movie' if media_type_for_meta == 'movie' else 'tv'
                            logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id_int}")
                        else:
                            logging.warning(f"Could not get media meta for TMDB ID {tmdb_id_int} with dummy IMDb ID")
                    except Exception as meta_error:
                        logging.error(f"Error getting media meta for TMDB ID {tmdb_id_int}: {meta_error}")
                        
            except ValueError:
                logging.error(f"Invalid TMDb ID format: {id_value}")
                return []

        if metadata_result and determined_media_type and metadata_result.get('tmdb_id'):
            tmdb_id_from_meta = str(metadata_result.get('tmdb_id'))
            title = metadata_result.get('title', 'N/A')
            year = metadata_result.get('year', 'N/A')
            
            poster_path_final = None
            
            # Use get_media_meta for consistent poster/details if possible
            # get_media_meta expects determined_media_type as 'movie' or 'tv'
            media_meta_tuple = await asyncio.to_thread(
                get_media_meta, tmdb_id_from_meta, determined_media_type
            )

            if media_meta_tuple:
                poster_path_from_meta, _, _, _, _ = media_meta_tuple
                poster_path_final = poster_path_from_meta # This is a relative TMDB path

            # Construct full poster path using proxy or use placeholder
            if poster_path_final and poster_path_final != "static/images/placeholder.png":
                if has_tmdb:
                    # Use the scraper's TMDB image proxy if TMDB key is set
                    poster_path_final = f"/scraper/tmdb_image/w300{poster_path_final}"
                else:
                    # Fallback if TMDB key is not set but we got a path (should ideally be placeholder already)
                    poster_path_final = "static/images/placeholder.png"
            elif metadata_result.get('poster') and has_tmdb : # Fallback to poster from get_metadata
                 poster_path_final = f"/scraper/tmdb_image/w300{metadata_result.get('poster')}"
            else: # Ultimate fallback to placeholder
                poster_path_final = "static/images/placeholder.png"


            formatted_result = {
                'id': tmdb_id_from_meta,
                'title': title,
                'year': year,
                'posterPath': poster_path_final, # Camel case for the template
                'mediaType': 'show' if determined_media_type == 'tv' else 'movie' # 'movie' or 'show'
            }
            results.append(formatted_result)
        else:
            logging.warning(f"Could not find metadata for {id_kind} ID: {id_value} with hint {content_type_hint}")

    except Exception as e:
        logging.error(f"Error during _fetch_media_details_for_assigner ({id_kind}={id_value}): {e}", exc_info=True)
    return results

def _fetch_trakt_season_data_directly(imdb_id: str) -> dict | None:
    """Fetch season episode counts directly from Trakt API, including Season 0."""
    headers = get_trakt_headers()
    if not headers:
        logging.error("Failed to get Trakt headers for direct season fetch.")
        return None
        
    # Use extended=episodes, we only need the counts
    url = f"{TRAKT_API_URL}/shows/{imdb_id}/seasons?extended=episodes" 
    logging.debug(f"Fetching season data directly from Trakt: {url}")
    
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        
        seasons_data = response.json()
        if not isinstance(seasons_data, list):
             logging.error(f"Unexpected response format from Trakt seasons endpoint: {type(seasons_data)}")
             return None

        # Process seasons, including season 0
        season_counts = {}
        for season in seasons_data:
            season_num = season.get('number')
            # Ensure season number is not None before processing
            if season_num is not None:
                # Use episode_count if available, otherwise count episodes list
                episode_count = season.get('episode_count', len(season.get('episodes', [])))
                season_counts[season_num] = episode_count
            else:
                logging.warning(f"Skipping season with null number for IMDb ID {imdb_id}")

        # Convert to the expected format (list of dictionaries)
        formatted_seasons = []
        for season_num, episode_count in season_counts.items():
            formatted_seasons.append({
                'number': season_num,
                'episode_count': episode_count
            })
        
        logging.debug(f"Directly fetched season counts for {imdb_id}: {season_counts}")
        return formatted_seasons

    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching season data directly from Trakt for {imdb_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Trakt API response status: {e.response.status_code}")
            logging.error(f"Trakt API response text: {e.response.text}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error fetching season data directly from Trakt: {e}", exc_info=True)
        return None

@magnet_bp.route('/get_versions')
def get_versions():
    settings = load_config()
    version_terms = settings.get('Scraping', {}).get('versions', {})
    # Return list of version keys
    return jsonify(list(version_terms.keys()))

@magnet_bp.route('/get_season_data')
def get_season_data():
    tmdb_id = request.args.get('tmdb_id')
    allow_specials_str = request.args.get('allow_specials', 'true').lower()
    allow_specials = allow_specials_str == 'true'

    if not tmdb_id:
        return jsonify({'error': 'tmdb_id is required'}), 400
    
    try:
        from metadata.metadata import get_imdb_id_if_missing
        imdb_id = get_imdb_id_if_missing({'tmdb_id': int(tmdb_id), 'media_type': 'show'})

        if not imdb_id:
            logging.warning(f"Could not find IMDb ID for TMDB ID: {tmdb_id}. Trying with dummy IMDb ID...")
            # Generate a dummy IMDb ID for testing purposes
            dummy_imdb_id = f"tt{tmdb_id.zfill(7)}"
            logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id}")
            imdb_id = dummy_imdb_id

        # Now we should have an IMDb ID. Let's try to fetch season data.
        try:
            # First, try to get seasons from the battery, which is faster.
            seasons_data, source = DirectAPI.get_show_seasons(imdb_id)
            if seasons_data and source == 'battery':
                logging.info(f"Successfully fetched season data from battery for IMDb ID: {imdb_id}")
                # Convert battery format to expected object format for frontend
                formatted_seasons = {}
                for season_num, season_data in seasons_data.items():
                    # Filter based on allow_specials
                    if allow_specials or season_num != 0:
                        formatted_seasons[str(season_num)] = season_data.get('episode_count', 0)
                logging.info(f"Returning {len(formatted_seasons)} seasons from battery for IMDb ID {imdb_id} (Allow Specials: {allow_specials}). Original count: {len(seasons_data)}")
                return jsonify(formatted_seasons)
            else:
                logging.info(f"Could not fetch season data from battery for {imdb_id}, trying Trakt directly.")
                season_data = _fetch_trakt_season_data_directly(imdb_id)
                if season_data:
                    # Convert to expected object format for frontend
                    formatted_seasons = {}
                    for season in season_data:
                        season_num = season.get('number')
                        # Filter based on allow_specials
                        if allow_specials or season_num != 0:
                            formatted_seasons[str(season_num)] = season.get('episode_count', 0)
                    logging.info(f"Returning {len(formatted_seasons)} seasons from Trakt for IMDb ID {imdb_id} (Allow Specials: {allow_specials}). Original count: {len(season_data)}")
                    return jsonify(formatted_seasons)
                else:
                    logging.error(f"Could not fetch season data directly from Trakt for IMDb ID: {imdb_id}")
                    return jsonify({"error": f"Could not fetch season data from Trakt for IMDb ID: {imdb_id}"}), 404
        except Exception as e:
            logging.error(f"An unexpected error occurred while fetching season data for IMDb ID {imdb_id}: {e}", exc_info=True)
            return jsonify({"error": "An unexpected error occurred"}), 500
        
    except Exception as e:
        logging.error(f"Error in get_season_data endpoint: {str(e)}", exc_info=True)
        return jsonify({'error': 'An internal error occurred'}), 500

@magnet_bp.route('/assign_magnet', methods=['GET', 'POST'])
@admin_required
def assign_magnet():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'search':
            search_term = request.form.get('search_term', '').strip()
            content_type = request.form.get('content_type', 'all') # 'movie', 'show', or 'all'
            
            if not search_term:
                flash('Please enter a search term or ID', 'error')
                return redirect(url_for('magnet.assign_magnet'))

            search_results = []
            is_id_search = False

            # IMDb ID check (e.g., tt1234567)
            imdb_match = re.fullmatch(r'(tt\d+)', search_term, re.IGNORECASE)
            if imdb_match:
                imdb_id = imdb_match.group(1)
                logging.info(f"IMDb ID detected: {imdb_id}. Fetching details...")
                search_results = asyncio.run(_fetch_media_details_for_assigner(imdb_id, 'imdb', content_type if content_type != 'all' else None))
                is_id_search = True
            else:
                # TMDb ID prefixed check (e.g., tmdb12345)
                tmdb_prefixed_match = re.fullmatch(r'tmdb(\d+)', search_term, re.IGNORECASE)
                if tmdb_prefixed_match:
                    tmdb_id = tmdb_prefixed_match.group(1)
                    logging.info(f"Prefixed TMDb ID detected: {tmdb_id}. Fetching details...")
                    # Determine hint: if content_type is 'all', we can't be sure.
                    # _fetch_media_details_for_assigner will try movie then TV.
                    type_hint = content_type if content_type != 'all' else None
                    search_results = asyncio.run(_fetch_media_details_for_assigner(tmdb_id, 'tmdb', type_hint))
                    is_id_search = True

            if not is_id_search:
                logging.info(f"No specific ID pattern detected. Performing Trakt search for: {search_term}")
                # Search Trakt for media if no ID was detected
                from utilities.web_scraper import search_trakt
                search_results = search_trakt(search_term)
                
                # Filter results based on content type if not 'all'
                if content_type != 'all':
                    # Handle 'tv' as well as 'show' from Trakt's media_type
                    normalized_content_type = 'show' if content_type == 'tv' else content_type
                    search_results = [result for result in search_results if result.get('media_type') == normalized_content_type]
                
                # Convert search results to template-expected format
                formatted_search_results = []
                for result in search_results:
                    formatted_result = {
                        'id': result.get('id'),
                        'title': result.get('title'),
                        'year': result.get('year'),
                        'posterPath': result.get('posterPath'),
                        'mediaType': result.get('media_type', 'movie')  # Convert media_type to mediaType for template
                    }
                    formatted_search_results.append(formatted_result)
                search_results = formatted_search_results
            
            if not search_results:
                flash(f'No results found for "{search_term}".', 'info')
                # Optionally, redirect or render with no results:
                # return redirect(url_for('magnet.assign_magnet')) 
                # Or, to show the search term and content type again:
                return render_template('magnet_assign.html', 
                                    search_results=[],
                                    search_term=search_term,
                                    content_type=content_type,
                                    step='results')


            return render_template('magnet_assign.html', 
                                search_results=search_results,
                                search_term=search_term,
                                content_type=content_type, # Pass content_type to template
                                step='results')
        else:
            flash('Invalid action performed.', 'warning')
            return redirect(url_for('magnet.assign_magnet'))
        
    elif request.method == 'GET':
        # from metadata.metadata import get_metadata # Already imported at top level of this thought block
        prefill_id = request.args.get('prefill_id')
        prefill_type = request.args.get('prefill_type') # 'movie' or 'tv' (or 'show')
        prefill_title = request.args.get('prefill_title')
        prefill_year = request.args.get('prefill_year')
        prefill_magnet = request.args.get('prefill_magnet')
        prefill_version = request.args.get('prefill_version')
        prefill_selection_raw = request.args.get('prefill_selection')
        prefill_seasons_raw = request.args.get('prefill_seasons')
        prefill_episode_raw = request.args.get('prefill_episode')
        prefill_selection = None
        prefill_seasons_csv = None
        prefill_episode = None

        # Normalize selection type to expected values: 'all', 'seasons', 'episode'
        if prefill_selection_raw:
            try:
                sel = prefill_selection_raw.strip().lower()
                selection_mapping = {
                    'all': 'all', 'full': 'all', 'full_series': 'all', 'series': 'all',
                    'seasons': 'seasons', 'season': 'seasons',
                    'episode': 'episode', 'single': 'episode', 'single_episode': 'episode'
                }
                prefill_selection = selection_mapping.get(sel)
                if prefill_selection:
                    logging.info(f"Prefill selection provided: {prefill_selection_raw} -> {prefill_selection}")
                else:
                    logging.warning(f"Ignoring unrecognized prefill_selection: {prefill_selection_raw}")
            except Exception as e:
                logging.warning(f"Error processing prefill_selection '{prefill_selection_raw}': {e}")

        # Parse seasons CSV (e.g., "1,2,0") and normalize to comma-separated digits
        if prefill_seasons_raw:
            try:
                tokens = re.split(r'[\s,]+', prefill_seasons_raw.strip())
                seasons = [t for t in tokens if t.isdigit()]
                if seasons:
                    prefill_seasons_csv = ','.join(seasons)
                    logging.info(f"Prefill seasons provided: {prefill_seasons_raw} -> {prefill_seasons_csv}")
                else:
                    logging.warning(f"No valid season numbers found in prefill_seasons: '{prefill_seasons_raw}'")
            except Exception as e:
                logging.warning(f"Error processing prefill_seasons '{prefill_seasons_raw}': {e}")

        # Parse episode number
        if prefill_episode_raw:
            try:
                ep = prefill_episode_raw.strip()
                if ep.isdigit():
                    prefill_episode = ep
                    logging.info(f"Prefill episode provided: {prefill_episode}")
                else:
                    logging.warning(f"Invalid prefill_episode value: '{prefill_episode_raw}'")
            except Exception as e:
                logging.warning(f"Error processing prefill_episode '{prefill_episode_raw}': {e}")

        if prefill_id and prefill_type:
            # Determine if prefill_id is IMDb ID or TMDB ID
            id_kind = 'tmdb'  # Default to TMDB
            if prefill_id.startswith('tt'):
                id_kind = 'imdb'
                logging.info(f"Attempting to prefill magnet assigner for IMDb ID: {prefill_id}, Type: {prefill_type}")
            else:
                logging.info(f"Attempting to prefill magnet assigner for TMDB ID: {prefill_id}, Type: {prefill_type}")
            
            if prefill_magnet:
                 logging.info(f"Prefill magnet link provided: {prefill_magnet[:60]}...")
            if prefill_version:
                 logging.info(f"Prefill version provided: {prefill_version}")
            
            # Use the new helper for consistency in fetching and formatting
            # prefill_type could be 'movie' or 'tv' (from scraper) or 'show'
            type_hint_for_helper = 'tv' if prefill_type == 'show' else prefill_type
            
            prefill_results = asyncio.run(_fetch_media_details_for_assigner(prefill_id, id_kind, type_hint_for_helper))
            
            if prefill_results:
                # _fetch_media_details_for_assigner returns a list, take the first (and only) item
                single_result = prefill_results[0]
                # The helper already formats title, year, posterPath, mediaType, and id (TMDB)
                # We can directly use this single_result.
                logging.info(f"Prefilled data via helper: {single_result}, Version: {prefill_version}")
                return render_template('magnet_assign.html',
                                    search_results=[single_result], # Pass as a list
                                    search_term=prefill_title or single_result['title'],
                                    content_type=single_result['mediaType'], # Use mediaType from helper
                                    step='results',
                                    is_prefilled=True,
                                    prefill_magnet=prefill_magnet,
                                    prefill_version=prefill_version,
                                    prefill_selection=prefill_selection,
                                    prefill_seasons=prefill_seasons_csv,
                                    prefill_episode=prefill_episode)
            else:
                logging.warning(f"Could not fetch details via helper for prefill ID: {prefill_id} ({id_kind.upper()}), Type: {prefill_type}")
                flash(f'Could not find details for {prefill_title} ({prefill_year}). Please search manually.', 'warning')
                
            # Fallback to standard search page if prefill fails
            return render_template('magnet_assign.html', step='search')
        else:
            # Standard GET request (no prefill)
            return render_template('magnet_assign.html', step='search')

@magnet_bp.route('/prepare_manual_assignment', methods=['POST'])
@admin_required
def prepare_manual_assignment():
    """Prepare the data for the manual file assignment screen."""
    # Get form data (same as original 'assign' action)
    tmdb_id = request.form.get('tmdb_id')
    media_type = request.form.get('media_type')
    magnet_link = request.form.get('magnet_link', '').strip()
    title = request.form.get('title')
    year = request.form.get('year')
    version = request.form.get('version')
    selection_type = request.form.get('selection_type')
    selected_seasons = request.form.get('selected_seasons', '').split(',') if request.form.get('selected_seasons') else []
    season = request.form.get('season')
    episode = request.form.get('episode')
    
    # Check for torrent file upload
    torrent_file = request.files.get('torrent_file')

    # Basic validation
    if not all([tmdb_id, media_type, title, year, version]):
        # Return JSON error for fetch request
        return jsonify({'success': False, 'error': 'Missing required information'}), 400
    
    # Validate that either magnet link or torrent file is provided, but not both
    if not magnet_link and not torrent_file:
        return jsonify({'success': False, 'error': 'Please provide either a magnet link or a torrent file'}), 400
    
    if magnet_link and torrent_file:
        return jsonify({'success': False, 'error': 'Please provide either a magnet link OR a torrent file, not both'}), 400

    try:
        # Handle torrent file upload if provided
        temp_file_path = None
        actual_magnet_link = magnet_link
        
        if torrent_file:
            # Save uploaded torrent file to temporary location
            import tempfile
            temp_fd, temp_file_path = tempfile.mkstemp(suffix='.torrent')
            os.close(temp_fd)
            torrent_file.save(temp_file_path)
            logging.info(f"Saved uploaded torrent file to: {temp_file_path}")
            actual_magnet_link = None  # Use None for magnet link when using torrent file
        
        # Get file list, filename, and torrent_id from debrid provider
        debrid_provider = get_debrid_provider()
        
        try:
            if actual_magnet_link:
                result = debrid_provider.get_torrent_file_list(actual_magnet_link)
                if result is None:
                    error_msg = 'Failed to retrieve file list from debrid service. Torrent might be invalid or provider error.'
                    logging.error(f"get_torrent_file_list returned None for magnet: {actual_magnet_link[:60]}")
                    return jsonify({'success': False, 'error': error_msg}), 503
                
                files, torrent_filename, torrent_id = result
            else:
                # For torrent files, we need to add it first, then get file list
                torrent_id = debrid_provider.add_torrent(None, temp_file_path)
                if not torrent_id:
                    error_msg = 'Failed to add torrent file to debrid service.'
                    logging.error(f"add_torrent returned None for torrent file: {temp_file_path}")
                    return jsonify({'success': False, 'error': error_msg}), 503
                
                # Wait a moment for RD to process the torrent before getting info
                import time
                time.sleep(3)
                
                # Get torrent info which contains the file list
                torrent_info = debrid_provider.get_torrent_info(torrent_id)
                if not torrent_info:
                    error_msg = 'Failed to get torrent info from debrid service.'
                    logging.error(f"get_torrent_info returned None for torrent ID: {torrent_id}")
                    return jsonify({'success': False, 'error': error_msg}), 503
                
                files = torrent_info.get('files', [])
                torrent_filename = torrent_info.get('filename', 'Unknown Filename')
                
                # Ensure files is a list
                if isinstance(files, dict):
                    files = list(files.values())
                elif not isinstance(files, list):
                    files = []
            
            logging.info(f"Retrieved {len(files)} files for manual assignment. Torrent ID: {torrent_id}, Filename: {torrent_filename}")
            
        finally:
            # Clean up temporary file if it was created
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                    logging.info(f"Cleaned up temporary torrent file: {temp_file_path}")
                except Exception as cleanup_error:
                    logging.warning(f"Failed to clean up temporary file {temp_file_path}: {cleanup_error}")

        # Filter for video files (.mkv, .mp4, .avi)
        video_files = []
        for f in files:
            path = f.get('path', '')
            if isinstance(path, str):
                path = path.lstrip('/')
                if any(path.lower().endswith(ext) for ext in ['.mkv', '.mp4', '.avi']):
                    filename = os.path.basename(path)
                    video_files.append({
                        'id': f.get('id'),
                        'path': path,
                        'filename': filename,
                        'bytes': f.get('bytes', 0)
                    })
            else:
                 logging.warning(f"Skipping file due to non-string path: {f}")

        if not video_files:
            error_msg = 'No video files found in the magnet link.'
            logging.warning(f"No video files found for magnet: {magnet_link[:60]}...")
            # Return JSON error
            return jsonify({'success': False, 'error': error_msg}), 400

        logging.info(f"Filtered down to {len(video_files)} video files.")

        # Get metadata
        from metadata.metadata import get_metadata
        metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type)
        if not metadata:
            logging.warning(f"Could not get metadata for TMDB ID {tmdb_id}. Trying with dummy IMDb ID...")
            # Generate a dummy IMDb ID and try to get basic metadata
            dummy_imdb_id = f"tt{tmdb_id.zfill(7)}"
            logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id}")
            
            # Try to get basic TMDB metadata using get_media_meta
            try:
                media_meta_tuple = asyncio.run(asyncio.to_thread(
                    get_media_meta, str(tmdb_id), media_type
                ))
                
                if media_meta_tuple:
                    poster_path, overview, genres, vote_average, backdrop_path = media_meta_tuple
                    # Create minimal metadata with dummy IMDb ID
                    metadata = {
                        'tmdb_id': tmdb_id,
                        'imdb_id': dummy_imdb_id,
                        'title': title,  # Use the title from the form
                        'year': year,    # Use the year from the form
                        'genres': genres or [],
                        'overview': overview or '',
                        'poster': poster_path,
                        'runtime': None,
                        'release_date': None
                    }
                    logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id}")
                else:
                    # Create even more minimal metadata if get_media_meta fails
                    metadata = {
                        'tmdb_id': tmdb_id,
                        'imdb_id': dummy_imdb_id,
                        'title': title,  # Use the title from the form
                        'year': year,    # Use the year from the form
                        'genres': [],
                        'overview': '',
                        'poster': None,
                        'runtime': None,
                        'release_date': None
                    }
                    logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id} (no TMDB meta available)")
            except Exception as meta_error:
                logging.error(f"Error getting media meta for TMDB ID {tmdb_id}: {meta_error}")
                # Create minimal metadata as fallback
                metadata = {
                    'tmdb_id': tmdb_id,
                    'imdb_id': dummy_imdb_id,
                    'title': title,  # Use the title from the form
                    'year': year,    # Use the year from the form
                    'genres': [],
                    'overview': '',
                    'poster': None,
                    'runtime': None,
                    'release_date': None
                }
                logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id} (fallback)")
        
        # Fetch TV show season data if needed
        if media_type in ['tv', 'show']:
            try:
                # --- MODIFICATION: Use TraktMetadata and pass flag ---
                trakt_metadata = TraktMetadata()
                # Pass include_specials=True here
                seasons_data, source = trakt_metadata.get_show_seasons_and_episodes(metadata.get('imdb_id'), include_specials=True)
                logging.debug(f"Fetched seasons data (incl specials) from {source} for {metadata.get('imdb_id')} in prepare_manual_assignment")
                # --- END MODIFICATION ---
                
                if seasons_data:
                    metadata['seasons'] = {}
                    # Convert integer keys from Trakt to string keys for internal use
                    for season_num, season_info in seasons_data.items():
                        metadata['seasons'][season_num] = {
                            'episodes': season_info.get('episodes', {}),
                            'episode_count': len(season_info.get('episodes', {}))
                        }
                else:
                     logging.warning(f"No season data fetched from Trakt for {metadata.get('imdb_id')}")
            except Exception as e:
                # Log but don't necessarily fail, maybe proceed without detailed episode titles
                logging.error(f"Error fetching season data from Trakt (non-critical): {str(e)}")

        # --- ADD LOGGING HERE ---
        logging.debug(f"Metadata seasons before creating items: {json.dumps(metadata.get('seasons', {}), indent=2)}")
        # --- END LOGGING ---

        # Determine target media items based on selection
        target_items = []
        if media_type == 'movie':
            item = create_movie_item(metadata, title, year, version, torrent_id, magnet_link)
            item['item_key'] = f"movie_{item['tmdb_id']}"
            target_items.append(item)
        else: # TV Show
            if selection_type == 'all':
                target_items = create_full_series_items(metadata, title, year, version, torrent_id, magnet_link)
            elif selection_type == 'seasons':
                target_items = create_season_items(metadata, title, year, version, torrent_id, magnet_link, selected_seasons)
            else: # Single episode
                try:
                    season_number = int(season)
                    episode_number = int(episode)
                    target_items = [create_episode_item(metadata, title, year, version, torrent_id, magnet_link, season_number, episode_number)]
                except (ValueError, TypeError):
                    # Return JSON error
                    return jsonify({'success': False, 'error': 'Invalid season or episode number provided.'}), 400
            
            # Add unique keys
            for item in target_items:
                 item['item_key'] = f"ep_{item['tmdb_id']}_s{item['season_number']:02d}e{item['episode_number']:02d}"

        if not target_items:
            error_msg = 'Could not determine target media items based on selection.'
            logging.error(f"Failed to determine target items. Selection: {selection_type}, Seasons: {selected_seasons}, S/E: {season}/{episode}")
            # Return JSON error
            return jsonify({'success': False, 'error': error_msg}), 400

        # --- SORT target_items --- 
        def sort_key(item):
            if item.get('type') == 'movie':
                # Assign a low sort order for movies to appear first
                return (-1, -1)
            else:
                # Sort by season then episode number
                return (item.get('season_number', 999), item.get('episode_number', 999))

        target_items.sort(key=sort_key)
        logging.debug(f"Sorted target_items: {[item.get('item_key', 'N/A') for item in target_items]}")
        # --- END SORT ---

        # --- Re-add PTT Parsing ---
        logging.info("Parsing video file names using PTT...")
        parsed_video_files = []
        for f in video_files:
            # Only parse if path is a non-empty string
            file_path = f.get('path')
            if isinstance(file_path, str) and file_path:
                filename = os.path.basename(file_path)
                logging.debug(f"Parsing filename: '{filename}' (from path: '{file_path}')")
                parsed = parse_with_ptt(filename)
                if not parsed.get('parsing_error'):
                    parsed_video_files.append({
                        'original': f, # Store the original file dict
                        'parsed': parsed,
                    })
                else:
                    logging.warning(f"PTT parsing error for filename '{filename}': {parsed.get('parsing_error')}")
            else:
                logging.warning(f"Skipping file parsing due to invalid path: {f.get('path')}")
        logging.info(f"Successfully parsed {len(parsed_video_files)} video file names.")
        # --- End Re-add PTT Parsing ---

        # --- Start Auto-assignment Logic ---
        logging.info("Attempting automatic file assignment...")
        
        # Add flags to track usage/assignment
        for f_info in parsed_video_files:
            f_info['used'] = False
        for item in target_items:
            item['assigned'] = False
            item['suggested_file_path'] = None # Ensure initialization

        assignment_count = 0

        # --- Phase 1: S/E Matching and Movie Matching ---
        logging.info("Phase 1: Attempting S/E and Movie matching...")
        for item in target_items:
            if item['assigned']: # Skip if already assigned (e.g., by movie logic below?)
                continue

            item_type = item.get('type')

            # --- Movie Logic: Assign largest file ---
            if item_type == 'movie':
                if video_files: # Ensure there are video files to check
                    # Find the largest *unused* video file
                    unused_parsed_files = [f for f in parsed_video_files if not f['used']]
                    if unused_parsed_files:
                        # Find original file dict corresponding to largest unused parsed file
                        largest_parsed_file_info = max(unused_parsed_files, key=lambda f: f['original'].get('bytes', 0))
                        largest_filename = largest_parsed_file_info['original'].get('filename')
                        if largest_filename:
                             item['suggested_file_path'] = largest_filename
                             item['assigned'] = True
                             largest_parsed_file_info['used'] = True # Mark this file as used
                             assignment_count += 1
                             logging.info(f"[Phase 1] Auto-assigned largest unused file '{largest_parsed_file_info['original'].get('path')}' to movie '{item.get('title')}'")
                        else:
                             logging.warning(f"[Phase 1] Could not assign largest file for movie '{item.get('title')}' - filename missing.")
                    else:
                         logging.warning(f"[Phase 1] Cannot assign largest file for movie '{item.get('title')}' - no unused video files found.")
                else:
                    logging.warning(f"[Phase 1] Cannot assign largest file for movie '{item.get('title')}' - no video files found at all.")
                continue # Move to the next item

            # --- Episode S/E Logic ---
            elif item_type == 'episode':
                potential_matches = []
                item_season = item.get('season_number')
                item_episode = item.get('episode_number')

                if item_season is None or item_episode is None:
                    logging.warning(f"[Phase 1] Skipping S/E match for item {item.get('item_key')} due to missing S/E numbers.")
                    continue

                # Iterate through UNUSED parsed files
                for file_info in [f for f in parsed_video_files if not f['used']]:
                    parsed = file_info['parsed']
                    match = False
                    parsed_seasons = parsed.get('seasons', [])
                    parsed_episodes = parsed.get('episodes', [])

                    try:
                        season_match_strict = (item_season in parsed_seasons)
                        episode_match = (item_episode in parsed_episodes)
                        season_match = season_match_strict
                        if not parsed_seasons and item_season == 1:
                            season_match = True
                        if season_match and episode_match:
                            match = True
                    except Exception as match_err:
                         logging.error(f"[Phase 1] Error during S/E matching logic for item {item.get('item_key')} and file {file_info['original']['path']}: {match_err}", exc_info=True)

                    if match:
                        potential_matches.append(file_info)

                # Assign if exactly one unique S/E match is found among unused files
                if len(potential_matches) == 1:
                    match_info = potential_matches[0]
                    suggested_filename = match_info['original'].get('filename')
                    if suggested_filename:
                        item['suggested_file_path'] = suggested_filename
                        item['assigned'] = True
                        match_info['used'] = True # Mark file as used
                        assignment_count += 1
                        logging.info(f"[Phase 1] Auto-assigned file '{match_info['original']['path']}' to item '{item.get('item_key')}' based on S/E match.")
                    else:
                         logging.warning(f"[Phase 1] Could not assign S/E matched file '{match_info['original']['path']}' to item '{item.get('item_key')}' - filename missing.")
                elif len(potential_matches) > 1:
                     logging.warning(f"[Phase 1] Found {len(potential_matches)} potential S/E file matches among unused files for item '{item.get('item_key')}'. Leaving unassigned for Phase 2. Files: {[p['original']['path'] for p in potential_matches]}")
            
            # --- Handle other item types ---
            else:
                 logging.warning(f"[Phase 1] Skipping assignment for unrecognized item type: {item_type} for item key {item.get('item_key')}")

        logging.info(f"Phase 1 Complete. Assigned {assignment_count} items based on S/E or Movie logic.")

        # --- Phase 2: Title Matching Fallback (File-Centric) ---
        logging.info("Phase 2: Attempting Title matching for remaining items...")
        phase2_assignment_count = 0
        MATCH_THRESHOLD = 85 # Same threshold as before, adjust if needed

        # Get unassigned items (episodes only, as movies handled in Phase 1)
        # Make a copy to allow removal while iterating
        unassigned_items = [item for item in target_items if not item['assigned'] and item['type'] == 'episode']
        # Get unused files (using the parsed_video_files structure which links to original)
        unused_files_info = [f_info for f_info in parsed_video_files if not f_info['used']]

        logging.debug(f"[Phase 2] Starting with {len(unassigned_items)} unassigned episode items and {len(unused_files_info)} unused files.")

        # Iterate through each UNUSED file
        for file_info in unused_files_info:
            original_file = file_info['original']
            raw_filename = original_file.get('filename', '')
            if not raw_filename:
                logging.debug(f"[Phase 2] Skipping file {original_file.get('path')} as it has no filename.")
                continue

            # Clean the filename for matching
            cleaned_filename_base, _ = os.path.splitext(raw_filename)
            cleaned_filename_base = cleaned_filename_base.lower()
            
            # --- AGGRESSIVE CLEANING --- 
            # Remove content in brackets/parentheses
            cleaned_filename = re.sub(r'[\(\[].*?[\)\]]', '', cleaned_filename_base)
            # Remove common resolutions/quality tags (simplified)
            cleaned_filename = re.sub(r'\b(480p|720p|1080p|2160p|4k|uhd|[0-9]{3,4}x[0-9]{3,4})\b', '', cleaned_filename, flags=re.IGNORECASE)
            # Remove common codecs/formats (simplified)
            cleaned_filename = re.sub(r'\b(x264|h264|x265|h265|aac|dts|ac3|web-dl|webrip|bluray|remux)\b', '', cleaned_filename, flags=re.IGNORECASE)
            
            # --- NEW: Remove S/E patterns --- 
            # e.g., s01e01, s1e1, season 1 episode 1, etc.
            cleaned_filename = re.sub(r'\b(s(\d{1,2})e(\d{1,3})|season\s*\d{1,2}\s*episode\s*\d{1,3})\b', '', cleaned_filename, flags=re.IGNORECASE)
            # --- END NEW ---

            # Remove trailing hyphens/dots/spaces often left after cleaning/before release group
            cleaned_filename = re.sub(r'[\s._-]+$', '', cleaned_filename).strip()
            # --- END AGGRESSIVE CLEANING ---

            logging.debug(f"[Phase 2] Processing file: '{raw_filename}' (BaseClean: '{cleaned_filename_base}', AggressiveClean: '{cleaned_filename}')")

            best_match_item = None
            best_match_score = 0
            best_match_item_index = -1 # Index within the current unassigned_items list

            # Compare this file against all currently UNASSIGNED episode items
            for idx, item in enumerate(unassigned_items):
                item_episode_title = item.get('episode_title', '').lower()
                item_key = item.get('item_key', 'N/A')

                # Skip generic titles or items without titles
                if not item_episode_title or item_episode_title == f'episode {item.get("episode_number")}':
                    continue

                # Calculate fuzzy match score
                # --- CHANGE: Use token_set_ratio --- 
                score = fuzz.token_set_ratio(item_episode_title, cleaned_filename)
                # --- END CHANGE ---

                # Check if this is the best match *for this file* so far and meets threshold
                if score > best_match_score and score >= MATCH_THRESHOLD:
                    best_match_score = score
                    best_match_item = item
                    best_match_item_index = idx

            # After checking all unassigned items for the current file:
            # If we found a best match above the threshold for this file
            if best_match_item is not None:
                # Assign this file to that best matching item
                best_match_item['suggested_file_path'] = original_file['filename']
                best_match_item['assigned'] = True
                file_info['used'] = True # Mark the file_info dict (containing original+parsed) as used
                phase2_assignment_count += 1
                assignment_count += 1 # Increment total count

                logging.info(f"[Phase 2] Auto-assigned file '{original_file.get('path')}' to item '{best_match_item.get('item_key')}' based on FUZZY FILENAME match ('{best_match_item.get('episode_title')}' vs AGGRESSIVELY CLEANED '{cleaned_filename}', Score: {best_match_score}).")

                # Remove the assigned item from the list so it can't be matched by another file
                del unassigned_items[best_match_item_index]
                logging.debug(f"[Phase 2] Removed assigned item {best_match_item.get('item_key')} from pool. Remaining unassigned: {len(unassigned_items)}")

        logging.info(f"Phase 2 Complete. Assigned {phase2_assignment_count} additional items based on Title logic.")
        # --- End Phase 2 ---

        logging.info(f"Completed automatic assignment attempt. Suggested assignments for {assignment_count} out of {len(target_items)} items.")

        # **MODIFICATION**: Store data in session instead of rendering template directly
        session['manual_assignment_data'] = {
            'target_items': target_items,
            'video_files': video_files,
            'magnet_link': actual_magnet_link,  # Use the actual magnet link (could be None for torrent files)
            'torrent_filename': torrent_filename,
            'torrent_id': torrent_id,
            'version': version,
            'is_torrent_file': torrent_file is not None  # Flag to indicate if this was a torrent file upload
        }
        
        # **MODIFICATION**: Return success JSON pointing to the new GET route
        return jsonify({'success': True, 'redirect_url': url_for('magnet.show_manual_assignment')})

    except Exception as e:
        # Catch-all for unexpected errors
        error_msg = f"An unexpected error occurred while preparing assignment: {str(e)}"
        logging.error(error_msg, exc_info=True)
        # Return JSON error
        return jsonify({'success': False, 'error': 'An internal server error occurred. Please check logs.'}), 500

@magnet_bp.route('/show_manual_assignment', methods=['GET'])
@admin_required
def show_manual_assignment():
    """Display the manual assignment page using data stored in the session."""
    assignment_data = session.pop('manual_assignment_data', None) # Get and remove data from session

    if not assignment_data:
        flash('No assignment data found. Please start the process again.', 'warning')
        return redirect(url_for('magnet.assign_magnet'))

    # Render the template with the retrieved data
    return render_template('manual_assignment.html', **assignment_data)

@magnet_bp.route('/confirm_manual_assignment', methods=['POST'])
@admin_required
def confirm_manual_assignment():
    """Confirm the manual file assignments and add items to the database."""
    try:
        # Get data submitted from the manual assignment form
        assignments = request.form.to_dict(flat=False) # Get as dict of lists
        
        # Extract common data
        magnet_link = assignments.pop('magnet_link', [None])[0]
        torrent_filename = assignments.pop('torrent_filename', [None])[0]
        initial_torrent_id = assignments.pop('torrent_id', [None])[0]
        version = assignments.pop('version', [None])[0]
        is_torrent_file = assignments.pop('is_torrent_file', [False])[0]

        if not all([torrent_filename, initial_torrent_id, version]):
             return jsonify({'success': False, 'error': 'Missing essential torrent information in submission.'}), 400

        # Extract torrent hash
        torrent_hash = None
        if magnet_link and magnet_link.startswith('magnet:'):
            hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
            if hash_match:
                torrent_hash = hash_match.group(1).lower()
        elif is_torrent_file:
            # For torrent files, we need to extract hash from the torrent ID
            # This is a limitation - we don't have the original torrent file to extract hash
            # We'll use the torrent ID as a fallback for tracking purposes
            logging.info(f"Using torrent ID {initial_torrent_id} as hash for torrent file tracking")
            torrent_hash = f"torrent_file_{initial_torrent_id}"
        
        added_items_count = 0
        failed_items_count = 0
        processed_items_info = [] # To store info for notifications
        successfully_added_items = [] # Store IDs of added items
        representative_tracking_item_data = None # Store data for tracking update

        # Each key in assignments (excluding common data) should be an item_key
        # The value will be a list containing the selected file path
        for item_key, selected_file_list in assignments.items():
            selected_filename = selected_file_list[0] if selected_file_list else None
            
            # Skip if no file was selected for this item
            if not selected_filename or selected_filename == '--ignore--':
                logging.info(f"Skipping item {item_key} as no file was selected or set to ignore.")
                continue

            # Reconstruct the item data based on item_key (this is complex)
            # We need to re-fetch metadata and rebuild the item based on the key parts
            try:
                parts = item_key.split('_')
                item_type = parts[0]
                tmdb_id = parts[1]
                
                # Re-fetch metadata (can be optimized by caching or passing more data)
                media_type_lookup = 'movie' if item_type == 'movie' else 'show'
                from metadata.metadata import get_metadata
                metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type_lookup)
                if not metadata:
                    logging.warning(f"Could not re-fetch metadata for {item_key}. Trying with dummy IMDb ID...")
                    # Generate a dummy IMDb ID and create minimal metadata
                    dummy_imdb_id = f"tt{tmdb_id.zfill(7)}"
                    logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id}")
                    
                    # Try to get TMDB metadata first, then create metadata with dummy IMDb ID
                    try:
                        from metadata.metadata import get_tmdb_metadata
                        media_meta_tuple = get_tmdb_metadata(str(tmdb_id), media_type_lookup)

                        logging.info(f"Media meta tuple: {media_meta_tuple}")
                        title = media_meta_tuple.get('title')
                        year = media_meta_tuple.get('year')
                        genres = media_meta_tuple.get('genres')
                        
                        if media_meta_tuple:
                            # Create metadata with TMDB data and dummy IMDb ID
                            metadata = {
                                'tmdb_id': tmdb_id,
                                'imdb_id': 'tt0000000',
                                'title': title, 
                                'year': year,
                                'genres': genres or [],
                            }
                            logging.info(f"Created metadata with TMDB data and dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id}")
                        else:
                            # Fallback to minimal metadata if get_media_meta fails
                            metadata = {
                                'tmdb_id': tmdb_id,
                                'imdb_id': dummy_imdb_id,
                                'title': f'TMDB {tmdb_id}',  # Fallback title
                                'year': None,
                                'genres': [],
                                'overview': '',
                                'poster': None,
                                'runtime': None,
                                'release_date': None
                            }
                            logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id} (no TMDB meta available)")
                    except Exception as meta_error:
                        logging.error(f"Error getting media meta for TMDB ID {tmdb_id}: {meta_error}")
                        # Create minimal metadata as fallback
                        metadata = {
                            'tmdb_id': tmdb_id,
                            'imdb_id': dummy_imdb_id,
                            'title': f'TMDB {tmdb_id}',  # Fallback title
                            'year': None,
                            'genres': [],
                            'overview': '',
                            'poster': None,
                            'runtime': None,
                            'release_date': None
                        }
                        logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id} (fallback)")
                
                # Base data
                title = metadata.get('title')
                year = metadata.get('year')

                if item_type == 'movie':
                    # Use magnet_link if available, otherwise use None for torrent files
                    item_magnet_link = magnet_link if magnet_link else None
                    item_data = create_movie_item(metadata, title, year, version, initial_torrent_id, item_magnet_link)
                elif item_type == 'ep':
                    # Expecting format like 's01e13' in parts[2]
                    if len(parts) < 3:
                        logging.error(f"Invalid item key format for episode: {item_key}. Expected at least 3 parts.")
                        failed_items_count += 1
                        continue
                    
                    se_part = parts[2] # e.g., s01e13
                    match = re.search(r's(\d+)e(\d+)', se_part, re.IGNORECASE)
                    if not match:
                         logging.error(f"Could not parse season/episode from item key part: '{se_part}' in key '{item_key}'")
                         failed_items_count += 1
                         continue

                    try:
                        season_number = int(match.group(1))
                        episode_number = int(match.group(2))
                    except (ValueError, IndexError):
                         logging.error(f"Error converting parsed season/episode to int from '{se_part}' in key '{item_key}'", exc_info=True)
                         failed_items_count += 1
                         continue

                    # Re-fetch season data if necessary
                    if 'seasons' not in metadata:
                        try:
                            # --- MODIFICATION: Use TraktMetadata and pass flag ---
                            trakt_metadata = TraktMetadata()
                            # Pass include_specials=True here
                            seasons_data, source = trakt_metadata.get_show_seasons_and_episodes(metadata.get('imdb_id'), include_specials=True)
                            logging.debug(f"Fetched seasons data (incl specials) from {source} for {metadata.get('imdb_id')} in confirm_manual_assignment")
                            # --- END MODIFICATION ---
                            
                            if seasons_data:
                                metadata['seasons'] = {}
                                # Convert integer keys from Trakt to string keys
                                for sn, si in seasons_data.items():
                                    metadata['seasons'][sn] = {'episodes': si.get('episodes', {}), 'episode_count': len(si.get('episodes', {}))}
                            else:
                                logging.warning(f"No season data fetched from Trakt for {metadata.get('imdb_id')} in confirmation step")
                        except Exception as e:
                             logging.warning(f"Could not re-fetch season data from Trakt for {item_key}: {e}")
                             # Proceed cautiously without detailed episode info
                    
                    # Use magnet_link if available, otherwise use None for torrent files
                    item_magnet_link = magnet_link if magnet_link else None
                    item_data = create_episode_item(metadata, title, year, version, initial_torrent_id, item_magnet_link, season_number, episode_number)
                else:
                    logging.warning(f"Unrecognized item key format: {item_key}")
                    failed_items_count += 1
                    continue
                    
                # Assign the manually selected FILENAME
                item_data['filled_by_file'] = selected_filename # Store only the filename
                item_data['filled_by_title'] = torrent_filename # Use the overall torrent filename
                
                # Add to database (remove internal/matcher keys first)
                db_item = {k: v for k, v in item_data.items() if k not in [
                    'series_title', 'season', 'episode', 'series_year', 'media_type', '_matcher_data', 'item_key'
                ]}
                
                item_id = add_media_item(db_item)
                if item_id:
                    added_items_count += 1
                    successfully_added_items.append(item_id)
                    # Prepare data for notification
                    processed_items_info.append({
                        'id': item_id,
                        'title': db_item.get('title', 'Unknown Title'),
                        'type': db_item.get('type', 'unknown'),
                        'year': db_item.get('year', ''),
                        'version': db_item.get('version', ''),
                        'season_number': db_item.get('season_number'),
                        'episode_number': db_item.get('episode_number'),
                        'new_state': 'Checking', # Assume it goes to Checking
                        'is_upgrade': False,
                        'upgrading_from': None,
                        'content_source': db_item.get('content_source'),
                        'content_source_detail': db_item.get('content_source_detail')
                    })
                    logging.info(f"Successfully added item {item_key} with file {selected_filename}, initial torrent ID: {initial_torrent_id}")
                    
                    # Prepare data for torrent tracking (only need one representative item)
                    if representative_tracking_item_data is None:
                        representative_tracking_item_data = {
                            'title': db_item.get('title'), 'year': db_item.get('year'), 
                            'media_type': db_item.get('type'), 'version': db_item.get('version'),
                            'tmdb_id': db_item.get('tmdb_id'), 'imdb_id': db_item.get('imdb_id'),
                            'filled_by_title': torrent_filename, 'filled_by_file': selected_filename, # Use filename here too
                            'torrent_id': initial_torrent_id
                        }
                        if db_item.get('type') == 'episode':
                            representative_tracking_item_data.update({'season_number': db_item.get('season_number'), 'episode_number': db_item.get('episode_number')})
                    
                else:
                    logging.error(f"Failed to add item {item_key} to database.")
                    failed_items_count += 1
            
            except Exception as item_error:
                logging.error(f"Error processing assignment for {item_key}: {item_error}", exc_info=True)
                failed_items_count += 1

        # If items were successfully added, re-add the torrent and update tracking/DB
        final_torrent_id = initial_torrent_id
        if added_items_count > 0:
            try:
                if magnet_link:
                    logging.info(f"Re-adding magnet torrent to debrid service after successful manual assignment: {magnet_link[:60]}...")
                    debrid_provider = get_debrid_provider() # Get provider instance
                    # Re-add the magnet torrent. The add_torrent method should handle file selection.
                    readd_result_id = debrid_provider.add_torrent(magnet_link)
                    if readd_result_id:
                        final_torrent_id = readd_result_id
                        logging.info(f"Successfully re-added magnet torrent with final ID: {final_torrent_id}")
                        
                        # Check if the final ID is different from the initial ID
                        if final_torrent_id != initial_torrent_id:
                            logging.info(f"Torrent ID changed from {initial_torrent_id} to {final_torrent_id}. Updating {len(successfully_added_items)} database items.")
                            for item_id in successfully_added_items:
                                update_media_item_torrent_id(item_id, final_torrent_id)
                        else:
                            logging.info(f"Torrent ID {final_torrent_id} remains the same, no DB update needed.")
                    else:
                        logging.warning(f"Failed to re-add magnet torrent {magnet_link[:60]}... It might already exist or an error occurred. Using initial ID: {initial_torrent_id}")
                else:
                    # For torrent files, we can't re-add since we don't have the original file
                    # The torrent should already be in the debrid service from the initial upload
                    logging.info(f"Using existing torrent ID {initial_torrent_id} for torrent file (no re-add needed)")
                    final_torrent_id = initial_torrent_id
            except Exception as readd_error:
                # Log the error but don't fail the overall success response
                if magnet_link:
                    logging.error(f"Error re-adding magnet torrent {magnet_link[:60]} after confirmation: {readd_error}", exc_info=True)
                else:
                    logging.error(f"Error handling torrent file re-add after confirmation: {readd_error}", exc_info=True)

            # Now, update torrent tracking with the final ID
            if torrent_hash and representative_tracking_item_data:
                # Update the torrent_id in the tracking data
                representative_tracking_item_data['torrent_id'] = final_torrent_id
                try:
                    update_torrent_tracking(
                        torrent_hash=torrent_hash,
                        item_data=representative_tracking_item_data,
                        trigger_details={'source': 'manual_assignment_confirm', 'user_initiated': True},
                        trigger_source='manual_assign_confirm',
                        rationale=f'User confirmed manual file assignment (Final Torrent ID: {final_torrent_id})'
                    )
                    logging.info(f"Updated torrent tracking for hash {torrent_hash} with final torrent ID {final_torrent_id}")
                except Exception as track_error:
                    logging.error(f"Failed to update torrent tracking for hash {torrent_hash}: {track_error}", exc_info=True)
            elif not torrent_hash:
                logging.warning("Could not extract torrent hash, skipping torrent tracking update.")
            elif not representative_tracking_item_data:
                 logging.warning("No representative item data found, skipping torrent tracking update.")

        # Send notifications for successfully added items
        if processed_items_info:
            try:
                from routes.notifications import send_notifications
                from routes.settings_routes import get_enabled_notifications_for_category
                from routes.extensions import app
                with app.app_context():
                    response = get_enabled_notifications_for_category('checking') # Or maybe a 'manual_add' category?
                    if response.json.get('success'):
                        enabled_notifications = response.json.get('enabled_notifications')
                        if enabled_notifications:
                            send_notifications(processed_items_info, enabled_notifications, notification_category='state_change')
            except Exception as notify_error:
                logging.error(f"Failed to send notifications after manual assignment: {notify_error}")

        if added_items_count > 0:
            message = f'Successfully assigned {added_items_count} item(s).' 
            if failed_items_count > 0:
                 message += f' Failed to assign {failed_items_count} item(s).' 
            return jsonify({'success': True, 'message': message, 'added_count': added_items_count, 'failed_count': failed_items_count})
        elif failed_items_count > 0:
            return jsonify({'success': False, 'error': f'Failed to assign {failed_items_count} item(s). Check logs.', 'added_count': 0, 'failed_count': failed_items_count}), 500
        else:
             return jsonify({'success': False, 'error': 'No items were assigned. Did you select files?', 'added_count': 0, 'failed_count': 0}), 400

    except Exception as e:
        logging.error(f"Error confirming manual assignment: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An unexpected error occurred: {str(e)}'}), 500

@magnet_bp.route('/verify_media_type')
@admin_required
def verify_media_type():
    tmdb_id = request.args.get('tmdb_id')
    media_type_hint = request.args.get('media_type_hint') # e.g., 'movie' or 'show'
    if not tmdb_id:
        return jsonify({'error': 'No TMDB ID provided'}), 400

    try:
        # First try to get movie info
        try:
            movie_info = DirectAPI.get_movie_info(str(tmdb_id))
            if movie_info:
                return jsonify({'success': True, 'media_type': 'movie'})
        except Exception as e:
            logging.debug(f"Not a movie: {str(e)}")

        # If not a movie, try TV show
        try:
            show_info = DirectAPI.get_show_info(str(tmdb_id))
            if show_info:
                return jsonify({'success': True, 'media_type': 'show'})
        except Exception as e:
            logging.debug(f"Not a show: {str(e)}")

        # If we get here, we couldn't determine the type
        return jsonify({'error': 'Could not determine media type'}), 404
    except Exception as e:
        logging.error(f"Error verifying media type: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

def create_movie_item(metadata, title, year, version, torrent_id, magnet_link):
    """Create a movie item dictionary"""
    item = {
        'type': 'movie',
        'title': title,
        'year': year,
        'version': version,
        'state': 'Checking',
        'filled_by_magnet': magnet_link,
        'filled_by_torrent_id': torrent_id,
        'imdb_id': metadata.get('imdb_id'),
        'tmdb_id': metadata.get('tmdb_id'),
        'genres': ','.join(metadata.get('genres', [])),
        'runtime': metadata.get('runtime'),
        'release_date': metadata.get('release_date'),
        'content_source': 'Magnet_Assigner'
    }
    return append_content_source_detail(item, source_type='Magnet_Assigner')

def create_episode_item(metadata, title, year, version, torrent_id, magnet_link, season_number, episode_number):
    """Create a single episode item dictionary"""
    # Ensure we're working with integers
    season_number = int(season_number)
    episode_number = int(episode_number)
    
    # --- ADD DETAILED LOGGING ---
    season_key = str(season_number)
    episode_key = str(episode_number)
    logging.debug(f"Creating item S{season_key}E{episode_key}")

    # Get season and episode data safely
    season_data = metadata.get('seasons', {}).get(season_number, {})
    logging.debug(f"  Season data retrieved for key '{season_number}': {{'keys': list(season_data.keys())}})") # Log only keys

    episodes_dict = season_data.get('episodes', {})
    logging.debug(f"  Episodes dict type: {type(episodes_dict)}, Keys type: {type(list(episodes_dict.keys())[0]) if episodes_dict else 'N/A'}, Example Key: {list(episodes_dict.keys())[0] if episodes_dict else 'N/A'}") # Log types
    # --- FIX: Use INTEGER key for episode lookup ---
    logging.debug(f"  Looking for episode key: {episode_number} (type: {type(episode_number)}) in episodes dict.")
    episode_data = episodes_dict.get(episode_number, {})
    # --- END FIX ---
    logging.debug(f"  Episode data retrieved using key {episode_number}: {{'keys': list(episode_data.keys())}})") # Log only keys

    extracted_title = episode_data.get('title', f'Episode {episode_number}')
    logging.debug(f"  Extracted title: '{extracted_title}'")

    # --- BEGIN MODIFICATION: Parse release_date correctly ---
    first_aired_str = episode_data.get('first_aired')
    release_date = 'Unknown' # Default value

    if first_aired_str:
        try:
            # Parse the UTC datetime string (expecting format like 2023-10-26T18:00:00.000Z)
            first_aired_utc = datetime.strptime(first_aired_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)

            # Convert UTC to local timezone using the helper
            from metadata.metadata import _get_local_timezone # Import the timezone helper
            local_tz = _get_local_timezone()
            if local_tz:
                 local_dt = first_aired_utc.astimezone(local_tz)
                 # Format the local date
                 release_date = local_dt.strftime("%Y-%m-%d")
                 logging.info(f"  Calculated local release date {release_date} from UTC {first_aired_str}")
            else:
                 logging.warning(f"  Could not determine local timezone. Using UTC date {first_aired_utc.strftime('%Y-%m-%d')} for release_date.")
                 release_date = first_aired_utc.strftime("%Y-%m-%d") # Fallback to UTC date part

        except ValueError as e:
            logging.error(f"  Invalid datetime format or conversion error for first_aired '{first_aired_str}': {e}. Setting release_date to 'Unknown'.")
            release_date = 'Unknown'
        except Exception as e:
            logging.error(f"  Unexpected error parsing first_aired '{first_aired_str}': {e}. Setting release_date to 'Unknown'.")
            release_date = 'Unknown'
    else:
        logging.warning("  No 'first_aired' date found in episode data. Setting release_date to 'Unknown'.")
        release_date = 'Unknown'
    # --- END MODIFICATION ---

    logging.debug(f"  Final release_date: '{release_date}'")
    # --- END DETAILED LOGGING ---
    
    # Create base item data with only database fields
    item_data = {
        'type': 'episode',
        'title': title, # Show title
        'year': year,
        'version': version,
        'state': 'Checking',
        'filled_by_magnet': magnet_link,
        'filled_by_torrent_id': torrent_id,
        'imdb_id': metadata.get('imdb_id'),
        'tmdb_id': metadata.get('tmdb_id'),
        'genres': ','.join(metadata.get('genres', [])),
        'runtime': episode_data.get('runtime') or metadata.get('runtime'), # Prefer episode runtime
        'season_number': season_number,
        'episode_number': episode_number,
        'episode_title': extracted_title, # Use the logged extracted title
        'release_date': release_date, # Use the correctly formatted local date
        'content_source': 'Magnet_Assigner'
        # NOTE: 'airtime' is not directly available here and usually calculated later or set to default.
        # It's primarily used by Wanted/Unreleased queues. If needed here, logic similar to release_date parsing would be required.
    }
    
    # Add MediaMatcher fields as temporary attributes that won't be stored in DB
    item_data.update({
        '_matcher_data': {
            'series_title': title,
            'season': season_number,
            'episode': episode_number,
            'series_year': year,
            'media_type': 'episode'
        }
    })
    
    return append_content_source_detail(item_data, source_type='Magnet_Assigner')

def create_season_items(metadata, title, year, version, torrent_id, magnet_link, selected_seasons):
    """Create items for selected seasons"""
    items = []
    for season in selected_seasons:
        try:
            season_number = int(season)
            season_data = metadata.get('seasons', {}).get(season_number, {})
            episodes = season_data.get('episodes', {})
            
            for episode_number in episodes:
                try:
                    episode_num = int(episode_number)
                    item = create_episode_item(metadata, title, year, version, torrent_id, magnet_link, season_number, episode_num)
                    items.append(item)
                except (ValueError, TypeError):
                    logging.warning(f"Invalid episode number: {episode_number}")
                    continue
        except (ValueError, TypeError):
            logging.warning(f"Invalid season number: {season}")
            continue
    
    return items

def create_full_series_items(metadata, title, year, version, torrent_id, magnet_link):
    """Create items for all episodes in the series"""
    items = []
    seasons = metadata.get('seasons', {})
    
    for season_number in sorted(seasons.keys()):
        try:
            season_num = int(season_number)
                
            season_data = seasons.get(season_num, {})  # Use integer key to access season data
            episodes = season_data.get('episodes', {})
            
            # Convert episode dictionary keys to integers and sort them
            episode_numbers = sorted([int(ep_num) for ep_num in episodes.keys()])
            
            for episode_num in episode_numbers:
                try:
                    item = create_episode_item(metadata, title, year, version, torrent_id, magnet_link, season_num, episode_num)
                    items.append(item)
                except (ValueError, TypeError) as e:
                    logging.warning(f"Error creating episode item S{season_num:02d}E{episode_num:02d}: {str(e)}")
                    continue
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid season number {season_number}: {str(e)}")
            continue
    
    return items
