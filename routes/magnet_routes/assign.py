"""Assign-magnet page."""

from flask import request, render_template, flash, redirect, url_for
from ..models import admin_required
import logging
import re
import asyncio
from ._common import _fetch_media_details_for_assigner, magnet_bp

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
                    raw_backdrop = result.get('backdropPath', '')
                    if raw_backdrop:
                        backdrop_final = f"https://image.tmdb.org/t/p/w780{raw_backdrop}"
                    else:
                        backdrop_final = None
                    formatted_result = {
                        'id': result.get('id'),
                        'title': result.get('title'),
                        'year': result.get('year'),
                        'posterPath': result.get('posterPath'),
                        'backdropPath': backdrop_final,
                        'mediaType': result.get('media_type', 'movie')
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
