"""Media-type check and movie/episode/season/series item creation."""

from flask import request, jsonify
from ..models import admin_required
from datetime import datetime, timezone
import logging
from cli_battery.app.direct_api import DirectAPI
from content_checkers.content_source_detail import append_content_source_detail
from ._common import magnet_bp

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
            # Parse the UTC datetime string — Trakt sends "2023-10-26T18:00:00.000Z"
            # (fractional seconds + Z), Scrob sends "2022-04-09T14:00:00" (no
            # fractional seconds, no Z) — try both instead of assuming one source's
            # format.
            for _fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
                try:
                    first_aired_utc = datetime.strptime(first_aired_str, _fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"time data {first_aired_str!r} does not match any known first_aired format")
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
