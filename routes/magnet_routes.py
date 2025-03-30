from flask import Blueprint, request, render_template, flash, redirect, url_for, jsonify, session
from debrid import get_debrid_provider
from database.database_writing import add_media_item, update_media_item_torrent_id
from metadata.metadata import get_metadata, _get_local_timezone, get_all_season_episode_counts
from .models import admin_required
from queues.config_manager import load_config
from queues.checking_queue import CheckingQueue
from datetime import datetime, timezone
from queues.media_matcher import MediaMatcher
import logging
from cli_battery.app.direct_api import DirectAPI
import os
import re
from database.torrent_tracking import record_torrent_addition, update_torrent_tracking, get_torrent_history
from content_checkers.content_source_detail import append_content_source_detail
from scraper.functions.ptt_parser import parse_with_ptt

magnet_bp = Blueprint('magnet', __name__)

@magnet_bp.route('/get_versions')
def get_versions():
    settings = load_config()
    version_terms = settings.get('Scraping', {}).get('versions', {})
    # Return list of version keys
    return jsonify(list(version_terms.keys()))

@magnet_bp.route('/get_season_data')
def get_season_data():
    tmdb_id = request.args.get('tmdb_id')
    if not tmdb_id:
        return jsonify({'error': 'No TMDB ID provided'}), 400

    try:
        # Convert TMDB ID to IMDb ID
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type='show')
        if not imdb_id:
            return jsonify({'error': 'Could not find IMDb ID'}), 404

        # Get season episode counts
        season_counts = get_all_season_episode_counts(imdb_id)
        if not season_counts:
            return jsonify({'error': 'Could not fetch season data'}), 404

        # Convert all season numbers to strings for JSON serialization
        season_counts = {str(season): count for season, count in season_counts.items()}
        return jsonify(season_counts)
    except Exception as e:
        logging.error(f"Error getting season data: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@magnet_bp.route('/assign_magnet', methods=['GET', 'POST'])
@admin_required
def assign_magnet():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'search':
            search_term = request.form.get('search_term')
            content_type = request.form.get('content_type', 'all')
            season = request.form.get('season')
            episode = request.form.get('episode')
            
            if not search_term:
                flash('Please enter a search term', 'error')
                return redirect(url_for('magnet.assign_magnet'))

            # Search Trakt for media
            from utilities.web_scraper import search_trakt
            search_results = search_trakt(search_term)
            
            # Filter results based on content type
            if content_type != 'all':
                search_results = [result for result in search_results if result['mediaType'] == content_type]
            
            # Add season/episode info to results if provided
            if season and content_type == 'show':
                for result in search_results:
                    result['selected_season'] = season
                    if episode:
                        result['selected_episode'] = episode

            return render_template('magnet_assign.html', 
                                search_results=search_results,
                                search_term=search_term,
                                content_type=content_type,
                                step='results')
        else:
            # Handle POST requests with actions other than 'search' (e.g., invalid/unexpected)
            flash('Invalid action performed.', 'warning') # Optional: inform user
            return redirect(url_for('magnet.assign_magnet')) # Redirect back to the GET route
        
    # Handle GET request (this part is fine)
    return render_template('magnet_assign.html', step='search')

@magnet_bp.route('/prepare_manual_assignment', methods=['POST'])
@admin_required
def prepare_manual_assignment():
    """Prepare the data for the manual file assignment screen."""
    # Get form data (same as original 'assign' action)
    tmdb_id = request.form.get('tmdb_id')
    media_type = request.form.get('media_type')
    magnet_link = request.form.get('magnet_link')
    title = request.form.get('title')
    year = request.form.get('year')
    version = request.form.get('version')
    selection_type = request.form.get('selection_type')
    selected_seasons = request.form.get('selected_seasons', '').split(',') if request.form.get('selected_seasons') else []
    season = request.form.get('season')
    episode = request.form.get('episode')

    # Basic validation
    if not all([tmdb_id, media_type, magnet_link, title, year, version]):
        # Return JSON error for fetch request
        return jsonify({'success': False, 'error': 'Missing required information'}), 400

    try:
        # Get file list, filename, and torrent_id from debrid provider
        debrid_provider = get_debrid_provider()
        result = debrid_provider.get_torrent_file_list(magnet_link)
        
        if result is None:
            error_msg = 'Failed to retrieve file list from debrid service. Torrent might be invalid or provider error.'
            logging.error(f"get_torrent_file_list returned None for magnet: {magnet_link[:60]}")
            # Return JSON error
            return jsonify({'success': False, 'error': error_msg}), 503 # Service Unavailable

        files, torrent_filename, torrent_id = result
        logging.info(f"Retrieved {len(files)} files for manual assignment. Torrent ID: {torrent_id}, Filename: {torrent_filename}")

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
        metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type)
        if not metadata:
            error_msg = 'Failed to get metadata for the selected item.'
            logging.error(f"Metadata fetch failed for tmdb_id: {tmdb_id}, type: {media_type}")
            # Return JSON error
            return jsonify({'success': False, 'error': error_msg}), 500
        
        # Fetch TV show season data if needed
        if media_type in ['tv', 'show']:
            try:
                seasons_data, _ = DirectAPI.get_show_seasons(metadata.get('imdb_id'))
                if seasons_data:
                    metadata['seasons'] = {}
                    for season_num, season_info in seasons_data.items():
                        if isinstance(season_num, str): season_num = int(season_num)
                        metadata['seasons'][str(season_num)] = {
                            'episodes': season_info.get('episodes', {}),
                            'episode_count': len(season_info.get('episodes', {}))
                        }
            except Exception as e:
                # Log but don't necessarily fail, maybe proceed without detailed episode titles
                logging.error(f"Error fetching season data (non-critical): {str(e)}")

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

        # --- Start Auto-assignment Logic ---
        logging.info("Attempting automatic file assignment using PTT parser...")
        parsed_video_files = []
        for f in video_files:
            # Only parse if path is a non-empty string
            file_path = f.get('path')
            if isinstance(file_path, str) and file_path:
                # --- MODIFICATION: Parse only the filename ---
                filename = os.path.basename(file_path)
                logging.debug(f"Parsing filename: '{filename}' (from path: '{file_path}')")
                parsed = parse_with_ptt(filename)
                # --- END MODIFICATION ---
                if not parsed.get('parsing_error'):
                    parsed_video_files.append({
                        'original': f, # Store the original file dict
                        'parsed': parsed,
                        #'assigned': False # REMOVED: Allow file reuse for multi-episode
                    })
            else:
                logging.warning(f"Skipping file parsing due to invalid path: {f.get('path')}")
                
        logging.info(f"Successfully parsed {len(parsed_video_files)} video file names.")

        assignment_count = 0
        # Iterate through target items and try to find a unique match
        for item in target_items:
            item['suggested_file_path'] = None # Initialize suggestion field
            item_type = item.get('type')

            # --- NEW Movie Logic: Assign largest file ---
            if item_type == 'movie':
                if video_files: # Ensure there are video files to check
                    # Find the video file with the maximum size
                    largest_file = max(video_files, key=lambda f: f.get('bytes', 0))
                    largest_filename = largest_file.get('filename')
                    if largest_filename:
                         item['suggested_file_path'] = largest_filename
                         assignment_count += 1
                         logging.info(f"Auto-assigned largest file '{largest_file.get('path')}' (using filename: {largest_filename}) to movie item '{item.get('title')}' (TMDB ID: {item.get('tmdb_id')}).")
                    else:
                         logging.warning(f"Could not assign largest file for movie '{item.get('title')}' - filename missing.")
                else:
                    logging.warning(f"Cannot assign largest file for movie '{item.get('title')}' - no video files found.")
                continue # Move to the next item after handling the movie

            # --- MODIFIED Episode Logic ---
            elif item_type == 'episode':
                potential_matches = []
                item_season = item.get('season_number')
                item_episode = item.get('episode_number')
                item_title_lower = item.get('title', '').lower() # Keep for logging clarity

                # Ensure item has season/episode numbers before trying to match
                if item_season is None or item_episode is None:
                    logging.warning(f"Skipping auto-assignment for item {item.get('item_key', item.get('title'))} due to missing season/episode number.")
                    continue

                for file_info in parsed_video_files:
                    parsed = file_info['parsed']
                    match = False

                    # Ensure required parsed fields exist
                    parsed_seasons = parsed.get('seasons', [])
                    parsed_episodes = parsed.get('episodes', [])

                    try:
                        logging.debug(f"-- Comparing Item (Episode): S{item_season:02d}E{item_episode:02d} ('{item_title_lower}') with File: '{file_info['original']['path']}'")
                        logging.debug(f"    Parsed Seasons: {parsed_seasons}, Parsed Episodes: {parsed_episodes}")

                        # Perform individual checks (NO title check)
                        season_match_strict = (item_season in parsed_seasons) # Keep original check for logging/debugging
                        episode_match = (item_episode in parsed_episodes)

                        # --- NEW: Handle missing season for Season 1 ---
                        season_match = season_match_strict # Start with the strict match result
                        if not parsed_seasons and item_season == 1:
                            # If parser found no season AND we are looking for Season 1, consider it a match
                            season_match = True
                            logging.debug(f"    Assuming Season 1 match because parser found no seasons.")
                        # --- END NEW ---

                        logging.debug(f"    Checks: Season Match? {season_match} (Strict: {season_match_strict}), Episode Match? {episode_match}")

                        # MODIFIED Condition: Use the potentially relaxed season_match
                        if season_match and episode_match:
                            match = True

                    except Exception as match_err:
                         logging.error(f"Error during matching logic for item {item.get('item_key', 'N/A')} and file {file_info['original']['path']}: {match_err}", exc_info=True)

                    # Log the outcome of the check for this specific file
                    logging.debug(f"    >>> Overall Match Result for this file: {match}")

                    if match:
                        potential_matches.append(file_info)

                # Assign if exactly one unique match is found based on S/E
                if len(potential_matches) == 1:
                    match_info = potential_matches[0]
                    # Use the original filename from the unfiltered video_files list
                    suggested_filename = match_info['original'].get('filename')
                    if suggested_filename:
                        item['suggested_file_path'] = suggested_filename
                        assignment_count += 1
                        logging.info(f"Auto-assigned file '{match_info['original']['path']}' (using filename: {suggested_filename}) to item '{item.get('item_key', item.get('title'))}' based on S/E match.")
                    else:
                         logging.warning(f"Could not assign file '{match_info['original']['path']}' to item '{item.get('item_key', item.get('title'))}' - filename missing.")
                elif len(potential_matches) > 1:
                     logging.warning(f"Found {len(potential_matches)} potential S/E file matches for item '{item.get('item_key', item.get('title'))}'. Leaving unassigned for manual selection. Files: {[p['original']['path'] for p in potential_matches]}")
                elif len(potential_matches) == 0:
                     logging.debug(f"No suitable S/E file matches found for item '{item.get('item_key', item.get('title'))}' after checking all files.")
            
            # --- Handle other item types or unexpected cases ---
            else:
                 logging.warning(f"Skipping auto-assignment for unrecognized item type: {item_type} for item key {item.get('item_key', 'N/A')}")

        logging.info(f"Completed automatic assignment attempt. Suggested assignments for {assignment_count} out of {len(target_items)} items.")
        # --- End Auto-assignment Logic ---

        # **MODIFICATION**: Store data in session instead of rendering template directly
        session['manual_assignment_data'] = {
            'target_items': target_items,
            'video_files': video_files,
            'magnet_link': magnet_link,
            'torrent_filename': torrent_filename,
            'torrent_id': torrent_id,
            'version': version
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

        if not all([magnet_link, torrent_filename, initial_torrent_id, version]):
             return jsonify({'success': False, 'error': 'Missing essential torrent information in submission.'}), 400

        # Extract torrent hash
        torrent_hash = None
        if magnet_link.startswith('magnet:'):
            hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
            if hash_match:
                torrent_hash = hash_match.group(1).lower()
        
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
                metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type_lookup)
                if not metadata:
                    logging.error(f"Could not re-fetch metadata for {item_key}")
                    failed_items_count += 1
                    continue
                
                # Base data
                title = metadata.get('title')
                year = metadata.get('year')

                if item_type == 'movie':
                    item_data = create_movie_item(metadata, title, year, version, initial_torrent_id, magnet_link)
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
                            seasons_data, _ = DirectAPI.get_show_seasons(metadata.get('imdb_id'))
                            if seasons_data:
                                metadata['seasons'] = {}
                                for sn, si in seasons_data.items():
                                    metadata['seasons'][str(sn)] = {'episodes': si.get('episodes', {}), 'episode_count': len(si.get('episodes', {}))}
                        except Exception as e:
                             logging.warning(f"Could not re-fetch season data for {item_key}: {e}")
                             # Proceed cautiously without detailed episode info
                    
                    item_data = create_episode_item(metadata, title, year, version, initial_torrent_id, magnet_link, season_number, episode_number)
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
                logging.info(f"Re-adding torrent to debrid service after successful manual assignment: {magnet_link[:60]}...")
                debrid_provider = get_debrid_provider() # Get provider instance
                # Re-add the torrent. The add_torrent method should handle file selection.
                readd_result_id = debrid_provider.add_torrent(magnet_link)
                if readd_result_id:
                    final_torrent_id = readd_result_id
                    logging.info(f"Successfully re-added torrent with final ID: {final_torrent_id}")
                    
                    # Check if the final ID is different from the initial ID
                    if final_torrent_id != initial_torrent_id:
                        logging.info(f"Torrent ID changed from {initial_torrent_id} to {final_torrent_id}. Updating {len(successfully_added_items)} database items.")
                        for item_id in successfully_added_items:
                            update_media_item_torrent_id(item_id, final_torrent_id)
                    else:
                        logging.info(f"Torrent ID {final_torrent_id} remains the same, no DB update needed.")
                else:
                    logging.warning(f"Failed to re-add torrent {magnet_link[:60]}... It might already exist or an error occurred. Using initial ID: {initial_torrent_id}")
            except Exception as readd_error:
                # Log the error but don't fail the overall success response
                logging.error(f"Error re-adding torrent {magnet_link[:60]} after confirmation: {readd_error}", exc_info=True)

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
def verify_media_type():
    tmdb_id = request.args.get('tmdb_id')
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
    
    # Get season and episode data safely
    season_data = metadata.get('seasons', {}).get(str(season_number), {})
    episode_data = season_data.get('episodes', {}).get(str(episode_number), {})
    
    # Create base item data with only database fields
    item_data = {
        'type': 'episode',
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
        'season_number': season_number,
        'episode_number': episode_number,
        'episode_title': episode_data.get('title', f'Episode {episode_number}'),
        'release_date': episode_data.get('first_aired', '1970-01-01'),
        'content_source': 'Magnet_Assigner'
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
            season_data = metadata.get('seasons', {}).get(str(season_number), {})
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
            if season_num == 0:  # Skip specials
                continue
                
            season_data = seasons.get(str(season_num), {})  # Use string key to access season data
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
