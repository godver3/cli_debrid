from flask import Blueprint, request, render_template, flash, redirect, url_for, jsonify
from debrid import get_debrid_provider
from database.database_writing import add_media_item
from metadata.metadata import get_metadata, _get_local_timezone
from .models import admin_required
from config_manager import load_config
from queues.checking_queue import CheckingQueue
from datetime import datetime, timezone
from queues.media_matcher import MediaMatcher
import logging
from cli_battery.app.direct_api import DirectAPI

magnet_bp = Blueprint('magnet', __name__)

@magnet_bp.route('/get_versions')
def get_versions():
    settings = load_config()
    version_terms = settings.get('Scraping', {}).get('versions', {})
    # Return list of version keys
    return jsonify(list(version_terms.keys()))

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
            
            # Add season/episode info to results if provided
            if season:
                for result in search_results:
                    result['selected_season'] = season
                    if episode:
                        result['selected_episode'] = episode

            return render_template('magnet_assign.html', 
                                search_results=search_results,
                                search_term=search_term,
                                step='results')
        
        elif action == 'assign':
            # Get form data
            tmdb_id = request.form.get('tmdb_id')
            media_type = request.form.get('media_type')
            magnet_link = request.form.get('magnet_link')
            title = request.form.get('title')
            year = request.form.get('year')
            version = request.form.get('version')
            season = request.form.get('season')
            episode = request.form.get('episode')

            # Convert season and episode to integers if present
            try:
                season_number = int(season) if season and season.lower() != 'null' else None
            except (ValueError, TypeError):
                season_number = None

            try:
                episode_number = int(episode) if episode and episode.lower() != 'null' else None
            except (ValueError, TypeError):
                episode_number = None

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

                # Get torrent info for file matching
                torrent_info = debrid_provider.get_torrent_info(torrent_id)
                if not torrent_info:
                    flash('Failed to get torrent info', 'error')
                    return redirect(url_for('magnet.assign_magnet'))

                # Get metadata to determine genres
                metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type) if tmdb_id else {}
                genres = metadata.get('genres', [])
                genres_str = ','.join(genres) if genres else ''

                # Create base item data
                item_data = {
                    'imdb_id': metadata.get('imdb_id'),
                    'title': title,
                    'year': year,
                    'type': 'episode' if media_type in ['tv', 'show'] else 'movie',
                    'version': version,
                    'tmdb_id': tmdb_id,
                    'state': 'Checking',
                    'filled_by_magnet': magnet_link,
                    'filled_by_torrent_id': torrent_id,
                    'genres': genres_str
                }

                # Get release date from metadata
                first_aired = metadata.get('first_aired')
                release_date = metadata.get('release_date')
                release_dates = metadata.get('release_dates', {})
                
                if first_aired:
                    try:
                        # Try parsing with microseconds first
                        try:
                            first_aired_utc = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S.%fZ")
                        except ValueError:
                            # If that fails, try without microseconds
                            first_aired_utc = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S")
                        first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)
                        # Convert UTC to local timezone
                        local_tz = _get_local_timezone()
                        local_dt = first_aired_utc.astimezone(local_tz)
                        item_data['release_date'] = local_dt.strftime("%Y-%m-%d")
                    except ValueError as e:
                        logging.warning(f"Could not parse first_aired date '{first_aired}': {e}")
                        item_data['release_date'] = '1970-01-01'
                elif release_date:
                    # For movies, use the release_date field
                    item_data['release_date'] = release_date
                elif release_dates:
                    # If we have release dates, try to find the earliest theatrical or digital release
                    earliest_date = None
                    for country_releases in release_dates.values():
                        for release in country_releases:
                            if release['type'] in ['theatrical', 'digital']:
                                date = release['date']
                                if not earliest_date or date < earliest_date:
                                    earliest_date = date
                    if earliest_date:
                        item_data['release_date'] = earliest_date
                    else:
                        item_data['release_date'] = '1970-01-01'
                else:
                    item_data['release_date'] = '1970-01-01'

                # Handle TV show episodes
                if media_type in ['tv', 'show']:
                    # Create MediaMatcher instance for file matching
                    media_matcher = MediaMatcher()
                    files = torrent_info.get('files', [])

                    # If this is a season pack (has season but no episode number)
                    if season_number is not None and episode_number is None:
                        logging.info(f"Processing season pack for {title} Season {season_number}")
                        
                        # Get seasons data which contains episodes
                        seasons_data, _ = DirectAPI.get_show_seasons(metadata.get('imdb_id'))
                        logging.info(f"Got seasons data: {seasons_data.keys() if seasons_data else None}")
                        
                        if seasons_data and season_number in seasons_data:
                            season_info = seasons_data[season_number]
                            logging.info(f"Season info: {season_info}")
                            episodes = season_info.get('episodes', {})
                            logging.info(f"Episodes data type: {type(episodes)}")
                            logging.info(f"Found {len(episodes)} episodes in season {season_number}")
                            
                            # Create MediaMatcher instance for file matching
                            media_matcher = MediaMatcher()
                            files = torrent_info.get('files', [])
                            logging.info(f"Found {len(files)} files in torrent")
                            
                            # Log raw torrent info
                            logging.info(f"Raw torrent info: {torrent_info}")
                            
                            success_count = 0
                            # For each episode in the season
                            for episode_num, episode_data in episodes.items():
                                try:
                                    episode_num = int(episode_num)
                                    logging.info(f"Processing episode {episode_num}: {episode_data}")
                                    
                                    # Create episode-specific item for MediaMatcher
                                    matcher_item = {
                                        'type': 'episode',
                                        'series_title': title,  # Required by MediaMatcher
                                        'title': title,  # Keep original title for reference
                                        'season': season_number,  # MediaMatcher uses 'season'
                                        'episode': episode_num,  # MediaMatcher uses 'episode'
                                        'genres': metadata.get('genres', [])  # Pass genres for anime detection
                                    }
                                    
                                    # Find matching file for this episode
                                    matches = media_matcher.match_content(files, matcher_item)
                                    if matches:
                                        # Get release date from first_aired
                                        first_aired = episode_data.get('first_aired')
                                        if first_aired:
                                            try:
                                                # Try parsing with microseconds first
                                                try:
                                                    first_aired_utc = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S.%fZ")
                                                except ValueError:
                                                    # If that fails, try without microseconds
                                                    first_aired_utc = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S")
                                                first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)
                                                # Convert UTC to local timezone
                                                local_tz = _get_local_timezone()
                                                local_dt = first_aired_utc.astimezone(local_tz)
                                                release_date = local_dt.strftime("%Y-%m-%d")
                                            except ValueError as e:
                                                logging.warning(f"Could not parse first_aired date '{first_aired}': {e}")
                                                release_date = '1970-01-01'
                                        else:
                                            release_date = '1970-01-01'

                                        # Create database item with only fields that exist in schema
                                        episode_item = {
                                            'type': 'episode',
                                            'title': title,
                                            'season_number': season_number,
                                            'episode_number': episode_num,
                                            'episode_title': episode_data.get('title', f'Episode {episode_num}'),
                                            'imdb_id': metadata.get('imdb_id'),
                                            'tmdb_id': metadata.get('tmdb_id'),
                                            'year': metadata.get('year'),
                                            'release_date': release_date,
                                            'genres': ','.join(metadata.get('genres', [])),
                                            'runtime': metadata.get('runtime'),
                                            'state': 'Checking',
                                            'filled_by_magnet': magnet_link,
                                            'filled_by_torrent_id': torrent_id,
                                            'filled_by_file': matches[0][0],  # Use first match's path
                                            'filled_by_title': torrent_info.get('filename', ''),  # Use torrent filename
                                            'version': version
                                        }
                                        
                                        logging.info(f"Found matching file for episode {episode_num}: {matches[0][0]}")
                                        
                                        # Add episode to database
                                        episode_id = add_media_item(episode_item)
                                        if episode_id is not None:
                                            # Update episode_item with the database ID
                                            episode_item['id'] = episode_id
                                            
                                            # Add to checking queue
                                            checking_queue = CheckingQueue()
                                            checking_queue.add_item(episode_item)
                                            success_count += 1
                                            logging.info(f"Added episode {episode_num} to checking queue")
                                        else:
                                            logging.error(f"Failed to add episode {episode_num} to database")
                                    else:
                                        logging.warning(f"No matching file found for episode {episode_num}")
                                except Exception as e:
                                    logging.error(f"Error processing episode {episode_num}: {str(e)}")
                                    continue
                            
                            if success_count > 0:
                                flash(f'Successfully processed {success_count} episodes from season pack', 'success')
                            else:
                                flash('No episodes were successfully processed from season pack', 'error')
                        else:
                            logging.error(f"No episodes data found in season {season_number}")
                            flash(f'No episodes found for season {season_number}', 'error')
                        
                        return redirect(url_for('magnet.assign_magnet'))

                    # Single episode
                    else:
                        item_data.update({
                            'season_number': season_number,
                            'episode_number': episode_number
                        })
                        
                        if metadata and metadata.get('seasons'):
                            season_data = metadata['seasons'].get(str(season_number), {})
                            episode_data = season_data.get('episodes', {}).get(str(episode_number), {})
                            item_data['episode_title'] = episode_data.get('title', f'Episode {episode_number}')

                        # Find matching file
                        matches = media_matcher.match_content(files, item_data)
                        if matches:
                            item_data['filled_by_file'] = matches[0][0]  # Use first match's path

                # Add the item to the database
                item_id = add_media_item(item_data)
                if item_id is not None:
                    flash('Successfully added media item to database', 'success')
                else:
                    flash('Failed to add media item to database', 'error')
                    return redirect(url_for('magnet.assign_magnet'))

                return redirect(url_for('magnet.assign_magnet'))

            except Exception as e:
                logging.error(f"Error assigning magnet: {str(e)}")
                flash(f'Error: {str(e)}', 'error')
                return redirect(url_for('magnet.assign_magnet'))

    return render_template('magnet_assign.html', step='search')
