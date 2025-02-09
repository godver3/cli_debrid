from flask import Blueprint, request, render_template, flash, redirect, url_for, jsonify
from debrid import get_debrid_provider
from database.database_writing import add_media_item
from metadata.metadata import get_metadata, _get_local_timezone, get_all_season_episode_counts
from .models import admin_required
from config_manager import load_config
from queues.checking_queue import CheckingQueue
from datetime import datetime, timezone
from queues.media_matcher import MediaMatcher
import logging
from cli_battery.app.direct_api import DirectAPI
import os
import re
from database.torrent_tracking import record_torrent_addition, update_torrent_tracking, get_torrent_history
from content_checkers.content_source_detail import append_content_source_detail

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
            from web_scraper import search_trakt
            search_results = search_trakt(search_term, content_type)
            
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
        
        elif action == 'assign':
            # Get form data
            tmdb_id = request.form.get('tmdb_id')
            media_type = request.form.get('media_type')
            magnet_link = request.form.get('magnet_link')
            title = request.form.get('title')
            year = request.form.get('year')
            version = request.form.get('version')
            
            # Get TV show specific data
            selection_type = request.form.get('selection_type')
            selected_seasons = request.form.get('selected_seasons', '').split(',') if request.form.get('selected_seasons') else []
            season = request.form.get('season')
            episode = request.form.get('episode')

            # Check if all required fields are present
            if not all([tmdb_id, media_type, magnet_link, title, year, version]):
                flash('Missing required information', 'error')
                return redirect(url_for('magnet.assign_magnet'))

            try:
                # Add torrent to debrid service
                debrid_provider = get_debrid_provider()
                torrent_id = debrid_provider.add_torrent(magnet_link)
                
                if not torrent_id:
                    flash('Failed to add magnet to debrid service', 'error')
                    return redirect(url_for('magnet.assign_magnet'))

                # Extract torrent hash from magnet link
                torrent_hash = None
                if magnet_link.startswith('magnet:'):
                    hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
                    if hash_match:
                        torrent_hash = hash_match.group(1).lower()

                # Get torrent info for file matching
                torrent_info = debrid_provider.get_torrent_info(torrent_id)
                if not torrent_info:
                    flash('Failed to get torrent info', 'error')
                    return redirect(url_for('magnet.assign_magnet'))

                # Record torrent addition if we have a hash
                if torrent_hash:
                    # Check recent history for this hash
                    history = get_torrent_history(torrent_hash)
                    
                    # Prepare item data for tracking
                    tracking_item_data = {
                        'title': title,
                        'year': year,
                        'media_type': media_type,
                        'version': version,
                        'tmdb_id': tmdb_id,
                        'filled_by_title': torrent_info.get('filename', ''),
                        'torrent_id': torrent_id
                    }
                    
                    # Add TV show specific data if applicable
                    if media_type in ['tv', 'show']:
                        tracking_item_data.update({
                            'selection_type': selection_type,
                            'season_number': season if season else None,
                            'episode_number': episode if episode else None,
                            'selected_seasons': selected_seasons if selected_seasons else None
                        })

                    # If there's a recent entry, update it instead of creating new one
                    if history:
                        update_torrent_tracking(
                            torrent_hash=torrent_hash,
                            item_data=tracking_item_data,
                            trigger_details={
                                'source': 'magnet_assign',
                                'user_initiated': True,
                                'torrent_info': torrent_info
                            },
                            trigger_source='manual_assign',
                            rationale='User manually assigned via magnet assignment'
                        )
                        logging.info(f"Updated existing torrent tracking entry for {title} (hash: {torrent_hash})")
                    else:
                        # Record new addition if no history exists
                        record_torrent_addition(
                            torrent_hash=torrent_hash,
                            trigger_source='manual_assign',
                            rationale='User manually assigned via magnet assignment',
                            item_data=tracking_item_data,
                            trigger_details={
                                'source': 'magnet_assign',
                                'user_initiated': True,
                                'torrent_info': torrent_info
                            }
                        )
                        logging.info(f"Recorded new torrent addition for {title} with hash {torrent_hash}")

                # Log raw torrent info for debugging
                #logging.debug(f"Raw torrent info: {torrent_info}")
                
                # Get files and ensure they're in the correct format
                files = torrent_info.get('files', [])
                if isinstance(files, dict):
                    # If files is a dictionary, convert to list
                    files = list(files.values())
                elif not isinstance(files, list):
                    files = []
                
                # Extract video file paths
                video_files = []
                for f in files:
                    # Handle both dictionary and string file formats
                    if isinstance(f, dict):
                        path = f.get('path', '')
                    else:
                        path = str(f)
                    
                    # Clean up path and check for video files
                    path = path.lstrip('/')  # Remove leading slash
                    if any(ext in path.lower() for ext in ['.mkv', '.mp4', '.avi']):
                        # Use just the filename for matching, but keep the full path
                        video_files.append({
                            'path': os.path.basename(path),  # Just the filename for matching
                            'full_path': path  # Keep the full path for later use
                        })
                
                logging.info(f"Found {len(files)} total files, {len(video_files)} video files")
                #logging.debug(f"Video files: {[f['path'] for f in video_files]}")

                # Get metadata
                metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type)
                if not metadata:
                    flash('Failed to get metadata', 'error')
                    return redirect(url_for('magnet.assign_magnet'))

                # For TV shows, fetch additional season data
                if media_type in ['tv', 'show']:
                    try:
                        # Get seasons data which contains episodes
                        seasons_data, _ = DirectAPI.get_show_seasons(metadata.get('imdb_id'))
                        if not seasons_data:
                            flash('Failed to get season data', 'error')
                            return redirect(url_for('magnet.assign_magnet'))
                        
                        # Add seasons data to metadata
                        metadata['seasons'] = {}
                        for season_num, season_info in seasons_data.items():
                            if isinstance(season_num, str):
                                season_num = int(season_num)
                            metadata['seasons'][str(season_num)] = {
                                'episodes': season_info.get('episodes', {}),
                                'episode_count': len(season_info.get('episodes', {}))
                            }
                        logging.info(f"Added seasons data to metadata: {list(metadata['seasons'].keys())}")
                    except Exception as e:
                        logging.error(f"Error fetching season data: {str(e)}")
                        flash('Failed to get season data', 'error')
                        return redirect(url_for('magnet.assign_magnet'))

                # Create MediaMatcher instance for file matching with relaxed matching enabled for manual assignments
                media_matcher = MediaMatcher(relaxed_matching=True)

                if media_type == 'movie':
                    # Handle movie
                    item_data = create_movie_item(metadata, title, year, version, torrent_id, magnet_link)
                    matches = media_matcher.match_content(video_files, item_data)
                    if matches:
                        # Use the full path from our matched file
                        matched_file = next(f['full_path'] for f in video_files if f['path'] == matches[0][0])
                        item_data['filled_by_file'] = matched_file
                        item_data['filled_by_title'] = torrent_info.get('filename', '')
                        items_to_add = [item_data]
                        logging.info(f"Created movie item: {item_data}")
                    else:
                        flash('No matching video file found in torrent', 'error')
                        return redirect(url_for('magnet.assign_magnet'))
                else:
                    # Handle TV show based on selection type
                    items_to_add = []
                    logging.info(f"Processing TV show with selection type: {selection_type}")
                    logging.info(f"Selected seasons: {selected_seasons}")
                    
                    if selection_type == 'all':
                        # Full series - get all seasons and episodes
                        items_to_add = create_full_series_items(metadata, title, year, version, torrent_id, magnet_link)
                        logging.info(f"Created {len(items_to_add)} items for full series")
                    elif selection_type == 'seasons':
                        # Selected seasons
                        items_to_add = create_season_items(metadata, title, year, version, torrent_id, magnet_link, selected_seasons)
                        logging.info(f"Created {len(items_to_add)} items for selected seasons: {selected_seasons}")
                    else:
                        # Single episode
                        try:
                            season_number = int(season)
                            episode_number = int(episode)
                            item_data = create_episode_item(metadata, title, year, version, torrent_id, magnet_link, season_number, episode_number)
                            items_to_add = [item_data]
                            logging.info(f"Created single episode item: S{season_number}E{episode_number}")
                        except (ValueError, TypeError):
                            flash('Invalid season or episode number', 'error')
                            return redirect(url_for('magnet.assign_magnet'))

                    # Match files for each item
                    added_items = []
                    for item_data in items_to_add:
                        matches = media_matcher.match_content(video_files, item_data)
                        if matches:
                            # Use just the filename without the path
                            matched_file = next(os.path.basename(f['full_path']) for f in video_files if f['path'] == matches[0][0])
                            item_data['filled_by_file'] = matched_file
                            item_data['filled_by_title'] = torrent_info.get('filename', '')
                            logging.info(f"Found matching file for {item_data.get('title')} S{item_data.get('season_number'):02d}E{item_data.get('episode_number'):02d}: {matched_file}")
                            
                            try:
                                # Remove fields that aren't in the database schema
                                db_item = {k: v for k, v in item_data.items() if k not in [
                                    'series_title', 'season', 'episode', 'series_year', 'media_type', '_matcher_data'
                                ]}
                                
                                # Prepare notification data
                                notification_data = {
                                    'id': None,
                                    'title': db_item.get('title', 'Unknown Title'),
                                    'type': db_item.get('type', 'unknown'),
                                    'year': db_item.get('year', ''),
                                    'version': db_item.get('version', ''),
                                    'season_number': db_item.get('season_number'),
                                    'episode_number': db_item.get('episode_number'),
                                    'new_state': 'Checking',
                                    'is_upgrade': False,
                                    'upgrading_from': None
                                }

                                # Add to database
                                item_id = add_media_item(db_item)
                                if item_id:
                                    notification_data['id'] = item_id
                                    added_items.append(notification_data)
                                else:
                                    logging.error(f"Failed to add item to database: {db_item}")
                            except Exception as e:
                                logging.error(f"Error adding item to database: {str(e)}")
                        else:
                            logging.warning(f"No matching file found for {item_data.get('title')} S{item_data.get('season_number'):02d}E{item_data.get('episode_number'):02d}")

                    # Send notifications for all added items
                    if added_items:
                        try:
                            from notifications import send_notifications
                            from routes.settings_routes import get_enabled_notifications_for_category
                            from extensions import app

                            with app.app_context():
                                response = get_enabled_notifications_for_category('checking')
                                if response.json['success']:
                                    enabled_notifications = response.json['enabled_notifications']
                                    if enabled_notifications:
                                        send_notifications(added_items, enabled_notifications, notification_category='state_change')
                        except Exception as e:
                            logging.error(f"Failed to send notifications: {str(e)}")

                    if added_items:
                        return jsonify({
                            'success': True,
                            'added_items': len(added_items),
                            'message': f'Successfully added {len(added_items)} matched items to database'
                        })
                    else:
                        return jsonify({
                            'success': False,
                            'added_items': 0,
                            'error': 'No matching files found in torrent'
                        }), 500

            except Exception as e:
                logging.error(f"Error assigning magnet: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': str(e)
                }), 500

    return render_template('magnet_assign.html', step='search')

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
