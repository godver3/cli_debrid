"""Show detail page/data, broken-file check, missing to Wanted, show metadata refresh."""

from flask import render_template, jsonify, request
from flask_login import current_user
from ..models import user_required, onboarding_required
from database.core import get_db_connection
from utilities.settings import get_setting
import logging
import requests
from datetime import datetime
from ..discover_routes import get_certification
from utilities.tmdb_cache import get_tmdb_json
from concurrent.futures import ThreadPoolExecutor
from ..poster_cache import load_cache, save_cache
from ._common import library_bp

@library_bp.route('/show/<media_id>')
@user_required
@onboarding_required
def show_detail(media_id):
    """
    Show detail page - displays all seasons and episodes for a TV show
    media_id can be either tmdb_id or imdb_id
    """
    from utilities.web_scraper import get_available_versions

    # Check if either Plex or TMDB is configured
    plex_url = get_setting('Plex', 'url', default='')
    plex_token = get_setting('Plex', 'token', default='')
    tmdb_api_key = get_setting('TMDB', 'api_key', default='')

    plex_configured = bool(plex_url and plex_token)
    tmdb_configured = bool(tmdb_api_key)
    versions = get_available_versions()

    # Check user permissions - if auth is disabled, grant all permissions
    from ..utils import is_user_system_enabled
    if not is_user_system_enabled():
        has_admin_permissions = True
        has_user_permissions = True
    else:
        has_admin_permissions = current_user.is_authenticated and current_user.role == 'admin'
        has_user_permissions = current_user.is_authenticated and current_user.role in ['admin', 'user']

    return render_template('library_show.html',
                         media_id=media_id,
                         plex_configured=plex_configured,
                         tmdb_configured=tmdb_configured,
                         versions=versions,
                         has_admin_permissions=has_admin_permissions,
                         has_user_permissions=has_user_permissions)

@library_bp.route('/show/<media_id>/data')
@user_required
def show_detail_data(media_id):
    """
    API endpoint to fetch show details and all episodes
    Returns show metadata and episodes grouped by season
    media_id can be either tmdb_id or imdb_id
    """
    try:
        conn = get_db_connection()

        # Try to find show by tmdb_id, imdb_id, or id
        # First try tmdb_id and imdb_id (string fields)
        # Note: TV show episodes have type='episode', not 'show'
        show_query = """
            SELECT
                id,
                title,
                year,
                tmdb_id,
                imdb_id,
                version,
                location_on_disk,
                collected_at
            FROM media_items
            WHERE (tmdb_id = ? OR imdb_id = ?) AND type = 'episode'
            LIMIT 1
        """
        show_data = conn.execute(show_query, (str(media_id), str(media_id))).fetchone()

        # If not found and media_id is numeric, try by database id
        if not show_data and str(media_id).isdigit():
            show_query = """
                SELECT
                    id,
                    title,
                    year,
                    tmdb_id,
                    imdb_id,
                    version,
                    location_on_disk,
                    collected_at
                FROM media_items
                WHERE id = ? AND type = 'episode'
                LIMIT 1
            """
            show_data = conn.execute(show_query, (int(media_id),)).fetchone()

        if not show_data:
            # Debug: Check if show exists with different criteria
            debug_query = "SELECT COUNT(*) as count, type FROM media_items WHERE tmdb_id = ? OR imdb_id = ? GROUP BY type"
            debug_result = conn.execute(debug_query, (str(media_id), str(media_id))).fetchall()

            logging.warning(f"Show {media_id} not found. Debug info: {debug_result}")

            return jsonify({
                'success': False,
                'error': f'Show not found in library. ID: {media_id}'
            }), 404

        # Get all episodes for this show - use whichever ID is available
        # Prefer tmdb_id for grouping episodes, fall back to imdb_id, then database id
        # This ensures all episodes of the same show are grouped together
        if show_data['tmdb_id']:
            id_field = 'tmdb_id'
            id_value = show_data['tmdb_id']
        elif show_data['imdb_id']:
            id_field = 'imdb_id'
            id_value = show_data['imdb_id']
        elif show_data['id']:
            id_field = 'id'
            id_value = show_data['id']
        else:
            logging.error(f"Show {media_id} found but has no valid ID fields for grouping episodes")
            return jsonify({
                'success': False,
                'error': 'Show found but has no valid ID for grouping episodes'
            }), 500

        episodes_query = f"""
            SELECT
                id,
                season_number,
                episode_number,
                episode_title,
                state,
                version,
                filled_by_file,
                filled_by_magnet,
                collected_at,
                release_date,
                airtime,
                content_source,
                content_source_detail,
                imdb_id,
                tmdb_id,
                location_basename,
                location_on_disk,
                ghostlisted,
                size,
                manual_replace,
                ms_item_id,
                ms_audio_track,
                ms_subtitle_track
            FROM media_items
            WHERE {id_field} = ? AND type = 'episode' AND (ghostlisted = 0 OR ghostlisted IS NULL)
            ORDER BY season_number ASC, episode_number ASC
        """
        episodes_raw = conn.execute(episodes_query, (id_value,)).fetchall()
        conn.close()

        # Collect unique content sources from episodes
        content_sources = set()
        for ep in episodes_raw:
            if ep['content_source']:
                content_sources.add(ep['content_source'])

        # Get content source display names
        from queues.config_manager import get_content_source_display_names
        content_source_display_map = get_content_source_display_names()
        content_source_display_list = [
            content_source_display_map.get(src, src) for src in sorted(content_sources)
        ]

        # Get rclone mount path from settings with intelligent content folder handling
        from utilities.path_utils import get_mount_path_for_content
        rclone_shows_path = get_mount_path_for_content(media_type='show')

        # Group episodes by season
        seasons = {}
        for ep in episodes_raw:
            season_num = ep['season_number'] or 0

            if season_num not in seasons:
                seasons[season_num] = {
                    'season_number': season_num,
                    'episodes': []
                }

            # An episode can have multiple entries (different files/versions)
            # Group by episode_number
            episode_data = {
                'id': ep['id'],
                'episode_number': ep['episode_number'],
                'episode_title': ep['episode_title'],
                'state': ep['state'],
                'version': ep['version'],
                'filled_by_file': ep['filled_by_file'],
                'filled_by_magnet': ep['filled_by_magnet'],
                'collected_at': ep['collected_at'],
                'release_date': ep['release_date'],
                'airtime': ep['airtime'],
                'imdb_id': ep['imdb_id'],
                'tmdb_id': ep['tmdb_id'],
                'location_basename': ep['location_basename'],
                'location_on_disk': ep['location_on_disk'],
                'ghostlisted': ep['ghostlisted'],
                'size': ep['size'],
                'manual_replace': bool(ep['manual_replace']),
                'ms_item_id': ep['ms_item_id'],
                'ms_audio_track': ep['ms_audio_track'],
                'ms_subtitle_track': ep['ms_subtitle_track'],
            }

            seasons[season_num]['episodes'].append(episode_data)

        # Convert seasons dict to list and sort
        # Add has_pending_replace flag to each season
        for season in seasons.values():
            season['has_pending_replace'] = any(ep.get('manual_replace') for ep in season['episodes'])
        seasons_list = sorted(seasons.values(), key=lambda x: x['season_number'])

        # Initialize metadata variables
        overview = None
        genres = None
        network = None
        status = None
        rating = None
        vote_count = None
        tmdb_poster_path = None
        tmdb_backdrop_path = None
        tmdb_id = show_data['tmdb_id']
        tvdb_id = None  # TODO: Extract from database if available
        tvdb_slug = None  # TVDB slug for correct URL format
        number_of_seasons = 0  # Total seasons from TMDB for phantom season detection

        # If no TMDB ID but IMDB ID exists, try to look it up
        if not tmdb_id and show_data['imdb_id']:
            from utilities.web_scraper import get_tmdb_id_from_imdb
            logging.info(f"No TMDB ID for show {show_data['title']}, attempting lookup via IMDB ID {show_data['imdb_id']}")
            tmdb_id = get_tmdb_id_from_imdb(show_data['imdb_id'], 'tv')
            if tmdb_id:
                logging.info(f"Found TMDB ID {tmdb_id} for show {show_data['title']} via IMDB lookup")

        # Try Battery first for basic metadata (overview, genres, network, status)
        battery_metadata = None
        if show_data['imdb_id']:
            try:
                from cli_battery.app.direct_api import DirectAPI
                logging.info(f"Fetching metadata from Battery for show {show_data['imdb_id']}")
                battery_metadata, source = DirectAPI.get_show_metadata(show_data['imdb_id'])
                if battery_metadata:
                    logging.info(f"Battery metadata retrieved from {source}")
                    overview = battery_metadata.get('overview', '')
                    genres_list = battery_metadata.get('genres', [])
                    if isinstance(genres_list, list):
                        genres = ', '.join(genres_list) if genres_list else None
                    else:
                        genres = genres_list
                    network = battery_metadata.get('network', '')
                    status = battery_metadata.get('status', '')
                    # Get TVDB ID and slug from Battery metadata
                    battery_ids = battery_metadata.get('ids', {})
                    if isinstance(battery_ids, dict):
                        tvdb_id = battery_ids.get('tvdb')
                        tvdb_slug = battery_ids.get('slug')  # Get TVDB slug for proper URL format
                    logging.info(f"Battery metadata - overview: {len(overview) if overview else 0} chars, genres: {genres}, network: {network}, status: {status}, tvdb_slug: {tvdb_slug}")
            except Exception as e:
                logging.warning(f"Battery metadata fetch failed for {show_data['imdb_id']}: {e}")

        # Always fetch TMDB for ratings and vote counts (not available from Battery)
        # Also used as fallback if Battery didn't provide basic metadata
        if tmdb_id:
            tmdb_api_key = get_setting('TMDB', 'api_key')
            if tmdb_api_key:
                try:
                    details_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    if battery_metadata:
                        logging.info(f"Fetching TMDB ratings for show {tmdb_id} (to supplement Battery data)")
                    else:
                        logging.info(f"Fetching TMDB metadata for show {tmdb_id} (Battery unavailable)")
                    details_data = get_tmdb_json(details_url, timeout=15)

                    # Always get ratings from TMDB
                    rating = details_data.get('vote_average')  # TMDB rating (0-10)
                    vote_count = details_data.get('vote_count')  # Number of votes

                    # Get poster and backdrop paths from TMDB
                    tmdb_poster_path = details_data.get('poster_path')
                    tmdb_backdrop_path = details_data.get('backdrop_path')

                    # Get total number of seasons for phantom season detection
                    number_of_seasons = details_data.get('number_of_seasons', 0)

                    # If Battery didn't provide data, use TMDB for everything
                    if not battery_metadata:
                        overview = details_data.get('overview', '')
                        genres_list = details_data.get('genres', [])
                        genres = ', '.join([g['name'] for g in genres_list]) if genres_list else None

                        # Get network (first network if multiple)
                        networks_list = details_data.get('networks', [])
                        if networks_list:
                            network = networks_list[0].get('name', '')

                        status = details_data.get('status', '')

                    # Get TVDB ID if not already from Battery
                    if not tvdb_id:
                        tvdb_id = details_data.get('external_ids', {}).get('tvdb_id')

                    logging.info(f"TMDB data: rating: {rating}, vote_count: {vote_count}, poster: {tmdb_poster_path}, backdrop: {tmdb_backdrop_path}")

                except Exception as e:
                    logging.error(f"Error fetching TMDB metadata for show {tmdb_id}: {e}")
            else:
                logging.warning(f"TMDB API key not configured, skipping ratings fetch for show {tmdb_id}")
        elif not battery_metadata:
            logging.warning(f"No TMDB ID or IMDb ID available for show {show_data['title']}, skipping metadata fetch")

        # Add phantom seasons for missing seasons if TMDB data is available
        if number_of_seasons > 0 and tmdb_id and tmdb_api_key:
            try:
                existing_season_numbers = set(s['season_number'] for s in seasons_list)
                missing_seasons = []

                # Find missing seasons (1 to number_of_seasons, excluding Season 0/Specials)
                for season_num in range(1, number_of_seasons + 1):
                    if season_num not in existing_season_numbers:
                        missing_seasons.append(season_num)

                if missing_seasons:
                    logging.info(f"Found {len(missing_seasons)} missing season(s): {missing_seasons}")

                    # Fetch episode counts for missing seasons from TMDB, all at once
                    def _fetch_season(season_num):
                        season_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}?api_key={tmdb_api_key}&language=en-US"
                        try:
                            return get_tmdb_json(season_url, timeout=10)
                        except Exception as fetch_err:
                            return fetch_err

                    logging.info(f"Fetching phantom season details from TMDB: {missing_seasons}")
                    with ThreadPoolExecutor(max_workers=min(6, len(missing_seasons))) as executor:
                        season_results = dict(zip(missing_seasons, executor.map(_fetch_season, missing_seasons)))

                    for season_num in missing_seasons:
                        try:
                            season_data = season_results[season_num]
                            if isinstance(season_data, Exception):
                                raise season_data

                            # Get episode count for this season
                            episodes_in_season = season_data.get('episodes', [])
                            episode_count = len(episodes_in_season)

                            if episode_count > 0:
                                # Create phantom season with phantom episodes
                                phantom_episodes = []
                                for ep_num in range(1, episode_count + 1):
                                    phantom_episodes.append({
                                        'id': None,
                                        'episode_number': ep_num,
                                        'episode_title': f'Episode {ep_num}',
                                        'state': 'Missing',
                                        'version': None,
                                        'filled_by_file': None,
                                        'collected_at': None,
                                        'release_date': None,
                                        'airtime': None,
                                        'imdb_id': None,
                                        'tmdb_id': None,
                                        'location_basename': None,
                                        'location_on_disk': None,
                                        'ghostlisted': None,
                                        'size': None,
                                        'is_phantom': True
                                    })

                                phantom_season = {
                                    'season_number': season_num,
                                    'is_phantom_season': True,
                                    'episodes': phantom_episodes
                                }

                                seasons_list.append(phantom_season)
                                logging.info(f"Created phantom season {season_num} with {episode_count} episodes")
                            else:
                                logging.warning(f"Season {season_num} has no episodes according to TMDB, skipping")

                        except requests.exceptions.RequestException as e:
                            logging.error(f"Failed to fetch TMDB data for phantom season {season_num}: {e}")
                            continue
                        except Exception as e:
                            logging.error(f"Error creating phantom season {season_num}: {e}")
                            continue

                    # Re-sort seasons list after adding phantom seasons
                    seasons_list = sorted(seasons_list, key=lambda x: x['season_number'])

            except Exception as e:
                logging.error(f"Error in phantom season creation: {e}")
                # Continue without phantom seasons if there's an error

        # Get poster and backdrop URLs from cache, with TMDB fallback
        from routes.poster_cache import get_cached_poster_url
        cache_id = tmdb_id or show_data['imdb_id'] or media_id
        poster_url = get_cached_poster_url(cache_id, 'tv')
        backdrop_url = get_cached_poster_url(f"{cache_id}_backdrop", 'tv')

        # If not in cache, use TMDB paths directly
        if not poster_url and tmdb_poster_path:
            poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_poster_path}"
            logging.info(f"Using TMDB poster path directly (cache empty): {poster_url}")
        if not backdrop_url and tmdb_backdrop_path:
            backdrop_url = f"https://image.tmdb.org/t/p/original{tmdb_backdrop_path}"
            logging.info(f"Using TMDB backdrop path directly (cache empty): {backdrop_url}")

        # Extract storage path from first episode's filled_by_file or use location_on_disk
        storage_path = show_data['location_on_disk'] if show_data['location_on_disk'] else None
        season_folders = None
        if not storage_path and episodes_raw and episodes_raw[0]['filled_by_file']:
            first_file = episodes_raw[0]['filled_by_file']
            # Extract directory from file path
            import os
            file_dir = os.path.dirname(first_file)

            # Check if using season folders (contains "Season XX" or "S0X")
            import re
            season_folder_pattern = r'[/\\](Season\s*\d+|S\d{2})[/\\]'
            season_folders = bool(re.search(season_folder_pattern, first_file, re.IGNORECASE))

            # Get the show's root directory (go up one level if season folders exist)
            if season_folders:
                storage_path = os.path.dirname(file_dir)
            else:
                storage_path = file_dir

        # Extract path up to content folder (works for any depth, both Plex and Symlink modes)
        from utilities.path_utils import get_content_folder_path
        display_path = get_content_folder_path(storage_path, media_type='show') if storage_path else storage_path

        # Find the added date: use collected_at from first episode of season 1
        added_date = None
        if seasons_list:
            # Find season 1
            season_1 = next((s for s in seasons_list if s['season_number'] == 1), None)
            if season_1 and season_1['episodes']:
                # Sort episodes by episode_number and get the first one with collected_at
                # episode_number can be NULL in the DB, so treat None as last instead of comparing it
                sorted_episodes = sorted(
                    season_1['episodes'],
                    key=lambda ep: ep['episode_number'] if ep.get('episode_number') is not None else 999
                )
                first_ep_with_date = next((ep for ep in sorted_episodes if ep.get('collected_at')), None)
                if first_ep_with_date:
                    added_date = first_ep_with_date['collected_at']

        # Calculate total size from all episodes
        total_size = 0
        for season in seasons_list:
            for episode in season['episodes']:
                episode_size = episode.get('size')
                if episode_size is not None:
                    total_size += episode_size

        # Check if there's an upcoming episode within 2 weeks (override status to "Airing")
        from datetime import datetime, timedelta
        from dateutil import parser as date_parser

        has_upcoming_episode = False
        try:
            now = datetime.now()
            two_weeks_from_now = now + timedelta(days=14)

            for season in seasons_list:
                for episode in season['episodes']:
                    # Only use release_date for upcoming episode checks
                    # Skip airtime-only entries as they default to today's date and cause false positives
                    air_date_str = episode.get('release_date')
                    if air_date_str:
                        try:
                            # Parse the air date using dateutil parser for better compatibility
                            air_date = date_parser.parse(str(air_date_str))
                            # Remove timezone info for comparison
                            if air_date.tzinfo:
                                air_date = air_date.replace(tzinfo=None)

                            logging.info(f"[STATUS CHECK] S{season['season_number']}E{episode.get('episode_number')} - Air date: {air_date}, In range: {now <= air_date <= two_weeks_from_now}")

                            # Check if episode airs within the next 2 weeks
                            if now <= air_date <= two_weeks_from_now:
                                has_upcoming_episode = True
                                logging.info(f"[STATUS CHECK] Found upcoming episode! S{season['season_number']}E{episode.get('episode_number')} airs on {air_date}")
                                break
                        except (ValueError, AttributeError, TypeError) as e:
                            logging.debug(f"Could not parse air date '{air_date_str}': {e}")
                            continue
                if has_upcoming_episode:
                    break

            # Override status if there's an upcoming episode
            if has_upcoming_episode:
                logging.info(f"[STATUS CHECK] Overriding status to 'Airing' (found upcoming episode)")
                status = "Airing"
            else:
                logging.info(f"[STATUS CHECK] No upcoming episodes found, keeping TMDB status: {status}")
        except Exception as e:
            logging.error(f"Error checking upcoming episodes: {e}")
            # Continue without overriding status if there's an error

        # Get auto-ghostlist setting
        auto_ghostlist_enabled = get_setting('Library Manager', 'ghostlist_mode', False)

        # Fetch certification based on user's preferred region
        certification = ''
        if show_data['tmdb_id'] and tmdb_api_key:
            certification_region = get_setting('TMDB', 'certification_region', 'US')
            certification = get_certification(show_data['tmdb_id'], 'tv', tmdb_api_key, certification_region)

        return jsonify({
            'success': True,
            'show': {
                'title': show_data['title'],
                'year': show_data['year'],
                'tmdb_id': show_data['tmdb_id'],
                'imdb_id': show_data['imdb_id'],
                'tvdb_id': tvdb_id,
                'tvdb_slug': tvdb_slug,
                'version': show_data['version'],
                'poster_url': poster_url,
                'backdrop_url': backdrop_url,
                'overview': overview,
                'genres': genres,
                'certification': certification,
                'network': network,
                'status': status,
                'rating': rating,
                'vote_count': vote_count,
                'path': display_path,
                'season_folders': season_folders,
                'added_date': added_date,
                'content_sources': content_source_display_list,
                'rclone_path': rclone_shows_path,
                'total_size': total_size,
                'auto_ghostlist_enabled': auto_ghostlist_enabled
            },
            'seasons': seasons_list
        })

    except Exception as e:
        logging.error(f"Error fetching show details for {media_id}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/check_broken_files', methods=['POST'])
@user_required
def check_broken_files():
    """
    Check for broken files in the __unplayable__ folder
    Returns a set of broken filenames
    """
    try:
        import os

        # Get rclone mount path from settings
        rclone_mount_root = get_setting('Plex', 'mounted_file_location', default='')

        if not rclone_mount_root:
            return jsonify({
                'success': True,
                'broken_files': []
            })

        # Build path to __unplayable__ folder
        # Convert mount path like /media/mount/__all__ to /media/mount
        mount_root = rclone_mount_root.replace('/__all__', '')
        unplayable_path = os.path.join(mount_root, '__unplayable__')
        bad_path = os.path.join(mount_root, '__bad__')

        broken_files = []

        # Check if __unplayable__ folder exists
        if os.path.exists(unplayable_path) and os.path.isdir(unplayable_path):
            # List all files in the __unplayable__ folder
            for filename in os.listdir(unplayable_path):
                file_path = os.path.join(unplayable_path, filename)
                if os.path.isfile(file_path):
                    broken_files.append(filename)

            logging.info(f"Found {len(broken_files)} broken files in {unplayable_path}")

        if os.path.exists(bad_path) and os.path.isdir(bad_path):
            # List all files in the __bad__ folder
            for filename in os.listdir(bad_path):
                file_path = os.path.join(bad_path, filename)
                if os.path.isfile(file_path):
                    broken_files.append(filename)

            logging.info(f"Found {len(broken_files)} broken files in {bad_path}")

        return jsonify({
            'success': True,
            'broken_files': broken_files
        })

    except Exception as e:
        logging.error(f"Error checking broken files: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'broken_files': []
        }), 500

@library_bp.route('/move_missing_to_wanted', methods=['POST'])
@user_required
def move_missing_to_wanted():
    """
    Move all missing episodes (Blacklisted, Error, Ghostlisted states) to Wanted state
    for a specific show
    """
    try:
        from database.database_writing import update_media_item_state

        data = request.json
        imdb_id = data.get('imdb_id')
        tmdb_id = data.get('tmdb_id')

        if not imdb_id and not tmdb_id:
            return jsonify({
                'success': False,
                'error': 'Either imdb_id or tmdb_id is required'
            }), 400

        # Get database connection
        db_content = get_db_connection()
        cursor = db_content.cursor()

        # Build query to find episodes in Blacklisted/Error/Ghostlisted states
        id_field = 'imdb_id' if imdb_id else 'tmdb_id'
        id_value = imdb_id if imdb_id else tmdb_id

        query = f"""
            SELECT id, imdb_id, tmdb_id, season_number, episode_number, state
            FROM media_items
            WHERE {id_field} = ?
            AND type = 'episode'
            AND state IN ('Blacklisted', 'Error', 'Ghostlisted')
            AND state != 'Unreleased'
        """

        cursor.execute(query, (id_value,))
        episodes = cursor.fetchall()

        updated_count = 0
        for episode in episodes:
            ep_id = episode[0]
            ep_imdb = episode[1]
            ep_tmdb = episode[2]
            season = episode[3]
            ep_num = episode[4]
            old_state = episode[5]

            # Update state to Wanted
            update_media_item_state(ep_id, 'Wanted')
            updated_count += 1
            logging.info(f"Moved episode S{season:02d}E{ep_num:02d} from {old_state} to Wanted")

        cursor.close()
        db_content.close()

        return jsonify({
            'success': True,
            'updated_count': updated_count,
            'message': f'Moved {updated_count} episode(s) to Wanted state'
        })

    except Exception as e:
        logging.error(f"Error moving episodes to wanted: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

class _SkipQueueMissing(Exception):
    """Internal: bail out of the queue-missing-episodes step without failing."""

@library_bp.route('/refresh_metadata/show/<media_id>', methods=['POST'])
@user_required
def refresh_show_metadata(media_id, queue_missing=True):
    """
    Refresh show metadata from TMDB/Trakt and update episode titles in database
    """
    try:
        from cli_battery.app.direct_api import DirectAPI

        # Get the show from database
        db_content = get_db_connection()
        cursor = db_content.cursor()

        # Determine if media_id is IMDB or TMDB format
        if media_id.startswith('tt'):
            id_field = 'imdb_id'
            imdb_id = media_id
        else:
            id_field = 'tmdb_id'
            # Need to get imdb_id from database for TMDB-only shows
            cursor.execute("SELECT imdb_id FROM media_items WHERE tmdb_id = ? AND type = 'episode' LIMIT 1", (media_id,))
            result = cursor.fetchone()
            if not result or not result[0]:
                cursor.close()
                db_content.close()
                return jsonify({
                    'success': False,
                    'error': 'Cannot refresh metadata for shows without IMDb ID'
                }), 400
            imdb_id = result[0]

        query = f"""
            SELECT imdb_id, tmdb_id, title, type
            FROM media_items
            WHERE {id_field} = ? AND type = 'episode'
            LIMIT 1
        """

        cursor.execute(query, (media_id,))
        show = cursor.fetchone()

        if not show:
            cursor.close()
            db_content.close()
            return jsonify({
                'success': False,
                'error': 'Show not found'
            }), 404

        imdb_id, tmdb_id, title, media_type = show

        # Force fresh metadata from TVDB/Trakt (not cached - ensures English titles)
        metadata, source = DirectAPI.force_refresh_metadata(imdb_id)

        if not metadata:
            cursor.close()
            db_content.close()
            return jsonify({
                'success': False,
                'error': f'Could not fetch metadata from {source or "API"}'
            }), 500

        # Extract show-level metadata from DirectAPI results (Source of Truth)
        show_title = metadata.get('title', title)
        show_year = str(metadata.get('year', '')) if metadata.get('year') else None
        show_status = metadata.get('status', '')

        # Format genres from list to comma-separated string
        genres_list = metadata.get('genres', [])
        if isinstance(genres_list, list):
            show_genres = ', '.join(genres_list) if genres_list else None
        else:
            show_genres = str(genres_list) if genres_list else None

        # Update episode titles and air dates from fresh metadata
        updated_count = 0
        if 'seasons' in metadata:
            for season_number, season_data in metadata['seasons'].items():
                if 'episodes' in season_data:
                    for episode_number, episode_data in season_data['episodes'].items():
                        episode_title = episode_data.get('title', f"Episode {episode_number}")
                        first_aired = episode_data.get('first_aired')

                        # Extract date and time from first_aired
                        release_date = None
                        airtime = None
                        if first_aired:
                            try:
                                # Handle both space and T separator formats
                                first_aired_str = str(first_aired).replace('T', ' ')
                                if ' ' in first_aired_str:
                                    date_part, time_part = first_aired_str.split(' ', 1)
                                    release_date = date_part[:10]  # YYYY-MM-DD
                                    airtime = time_part[:5]  # HH:MM
                                else:
                                    release_date = first_aired_str[:10]
                            except Exception as e:
                                logging.warning(f"Could not parse first_aired '{first_aired}' for S{season_number}E{episode_number}: {e}")

                        if episode_title and season_number is not None and episode_number is not None:
                            # Update episode metadata in database
                            cursor.execute("""
                                UPDATE media_items
                                SET episode_title = ?,
                                    release_date = COALESCE(?, release_date),
                                    airtime = COALESCE(?, airtime),
                                    metadata_updated = ?,
                                    last_updated = ?
                                WHERE imdb_id = ?
                                  AND type = 'episode'
                                  AND season_number = ?
                                  AND episode_number = ?
                            """, (
                                episode_title,
                                release_date,
                                airtime,
                                datetime.now(),
                                datetime.now(),
                                imdb_id,
                                season_number,
                                episode_number
                            ))
                            if cursor.rowcount > 0:
                                updated_count += cursor.rowcount

        # Fetch fresh show-level metadata from TMDB (Optional fallback/supplement)
        tmdb_updated = False
        if tmdb_id:
            try:
                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    import requests
                    # Fetch show details from TMDB
                    tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}"
                    response = requests.get(tmdb_url, timeout=10)
                    response.raise_for_status()
                    tmdb_data = response.json()

                    # Only update fields from TMDB if they are missing from DirectAPI/Battery
                    if not show_year:
                        show_year = tmdb_data.get('first_air_date', '')[:4] if tmdb_data.get('first_air_date') else None
                    if not show_genres:
                        show_genres = ', '.join([g['name'] for g in tmdb_data.get('genres', [])])
                    if not show_status:
                        show_status = tmdb_data.get('status', '')

                    tmdb_updated = True
                    logging.info(f"Updated supplementary TMDB metadata for {show_title}")
            except Exception as e:
                logging.warning(f"Failed to fetch supplementary TMDB metadata: {e}")

        # Update show-level metadata in media_items (title and genres for episodes)
        cursor.execute("""
            UPDATE media_items
            SET title = ?,
                genres = ?
            WHERE imdb_id = ? AND type = 'episode'
        """, (
            show_title,
            show_genres,
            imdb_id
        ))
        try:
            # Calculate total_seasons from fresh metadata
            total_seasons_refresh = None
            if 'seasons' in metadata:
                total_seasons_refresh = 0
                for k in metadata['seasons'].keys():
                    try:
                        if int(k) != 0:
                            total_seasons_refresh += 1
                    except (ValueError, TypeError):
                        pass
            # Update show-level metadata in tv_shows table (title, year, status, total_seasons)
            cursor.execute("""
                UPDATE tv_shows
                SET title = ?,
                    year = ?,
                    status = ?,
                    total_seasons = COALESCE(?, total_seasons),
                    last_updated = ?
                WHERE imdb_id = ?
            """, (
                show_title,
                show_year,
                show_status,
                total_seasons_refresh,
                datetime.now(),
                imdb_id
            ))

            tmdb_updated = True
            logging.info(f"Updated metadata for {show_title} in both media_items and tv_shows tables")
        except Exception as e:
            logging.warning(f"Failed to update show metadata: {e}")

        # Also update timestamps for all episodes (even if title didn't change)
        cursor.execute(f"""
            UPDATE media_items
            SET metadata_updated = ?,
                last_updated = ?
            WHERE {id_field} = ? AND type = 'episode'
        """, (
            datetime.now(),
            datetime.now(),
            media_id
        ))

        # Add any episodes that are in the refreshed metadata but not yet in the DB.
        # This handles the case where new episodes have been announced/released since the
        # show was first requested (e.g. "3 of 8 episodes" becoming "8 of 8").
        new_episodes_added = 0
        try:
            if not queue_missing:
                raise _SkipQueueMissing()
            from utilities.missing_episodes import queue_missing_episodes

            db_content.commit()  # Commit current updates before process_metadata reads the DB
            # In the versions the show is collected in. This read a `versions`
            # column media_items doesn't have, so it never added anything.
            new_episodes_added = queue_missing_episodes(imdb_id, 'Metadata-Refresh')
            if new_episodes_added:
                logging.info(f"Metadata refresh for {title}: added {new_episodes_added} previously-missing episode(s) to the queue.")
        except _SkipQueueMissing:
            logging.info(f"Metadata refresh for {imdb_id}: not queueing missing episodes (caller opted out)")
        except Exception as e_add:
            logging.warning(f"Could not add missing episodes during metadata refresh for {imdb_id}: {e_add}")

        db_content.commit()
        cursor.close()
        db_content.close()

        # Clear poster/backdrop cache then immediately re-fetch so library page
        # shows the updated poster without requiring a full page reload
        new_poster_url = None
        if tmdb_id:
            try:
                cache = load_cache()
                poster_key = f"{tmdb_id}_tv"
                backdrop_key = f"{tmdb_id}_backdrop_tv"
                cache.pop(poster_key, None)
                cache.pop(backdrop_key, None)
                save_cache(cache)
                logging.info(f"Cleared poster/backdrop cache for show {tmdb_id}")
                # Re-fetch fresh poster and cache it immediately
                from routes.poster_cache import cache_poster_url
                from utilities.settings import get_setting as _gs
                import requests as _req
                _api_key = _gs('TMDB', 'api_key', default='')
                if _api_key:
                    _resp = _req.get(
                        f"https://api.themoviedb.org/3/tv/{tmdb_id}/images"
                        f"?api_key={_api_key}&include_image_language=en",
                        timeout=10
                    )
                    if _resp.status_code == 200:
                        _posters = _resp.json().get('posters', [])
                        # Fall back to en+null if no English-only results
                        if not _posters:
                            _resp2 = _req.get(
                                f"https://api.themoviedb.org/3/tv/{tmdb_id}/images"
                                f"?api_key={_api_key}&include_image_language=en,null",
                                timeout=10
                            )
                            if _resp2.status_code == 200:
                                # Only keep English (iso_639_1='en') posters from fallback
                                _all = _resp2.json().get('posters', [])
                                _posters = [p for p in _all if p.get('iso_639_1') == 'en'] or _all
                        if _posters:
                            _best = sorted(_posters, key=lambda p: p.get('vote_average', 0), reverse=True)[0]
                            new_poster_url = f"https://image.tmdb.org/t/p/w300{_best['file_path']}"
                            cache_poster_url(tmdb_id, 'tv', new_poster_url)
            except Exception as e:
                logging.warning(f"Failed to clear/refresh poster cache: {e}")

        logging.info(f"Refreshed metadata for {title} (IMDb: {imdb_id}): Updated {updated_count} episode titles from {source}, added {new_episodes_added} missing episode(s){', TMDB data updated' if tmdb_updated else ''}")

        msg = f'Metadata refreshed for {title}'
        if new_episodes_added:
            msg += f' — added {new_episodes_added} previously-missing episode(s) to the queue'

        return jsonify({
            'success': True,
            'message': msg,
            'updated_episodes': updated_count,
            'new_episodes_added': new_episodes_added,
            'tmdb_updated': tmdb_updated,
            'source': source,
            'cache_cleared': tmdb_id is not None,
            'new_poster_url': new_poster_url
        })

    except Exception as e:
        logging.error(f"Error refreshing metadata: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
